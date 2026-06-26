"""Provider 레지스트리 — 모델 교체는 여기 한 곳에서 (CLAUDE.md §2.1, §10).

파이프라인은 get_llm()/get_image_provider()/... 만 호출한다.
이미지는 1순위(Nano Banana) 실패 시 fallback(Flux) 으로 자동 전환.
"""
from __future__ import annotations

from functools import lru_cache
from typing import List, Optional

from app.providers.audio_elevenlabs import ElevenLabsProvider
from app.providers.audio_minimax import MiniMaxMusicProvider
from app.providers.base import (
    AudioProvider,
    ImageProvider,
    ImageResult,
    LLMProvider,
    VideoProvider,
)
from app.providers.image_flux import FluxProvider
from app.providers.image_nanobanana import NanoBananaProvider
from app.providers.image_pollinations import PollinationsProvider
from app.providers.image_qwen_runpod import QwenRunPodProvider
from app.providers.llm_claude import ClaudeProvider
from app.providers.llm_claude_cli import ClaudeCLIProvider
from app.providers.video_kling import KlingProvider
from app.providers.video_runpod import RunPodVideoProvider
from app.providers.video_seedance import SeedanceProvider
from app.providers.video_veo import VeoProvider


@lru_cache(maxsize=1)
def get_llm() -> LLMProvider:
    """LLM backend: Claude CLI (CLAUDE_CLI=1, uses local login) → Anthropic API key →
    deterministic mock. The CLI provider falls back to mock_fn on any failure, so the
    keyless-offline guarantee holds regardless."""
    cli = ClaudeCLIProvider()
    if cli.is_real:
        return cli
    return ClaudeProvider()


@lru_cache(maxsize=1)
def _qwen_image() -> ImageProvider:
    return QwenRunPodProvider()


@lru_cache(maxsize=1)
def _primary_image() -> ImageProvider:
    return NanoBananaProvider()


@lru_cache(maxsize=1)
def _fallback_image() -> ImageProvider:
    return FluxProvider()


@lru_cache(maxsize=1)
def _free_image() -> ImageProvider:
    return PollinationsProvider()


def image_chain() -> List[ImageProvider]:
    """이미지 provider 우선순위. RunPod Qwen(키) → Nano Banana/Flux(키) → Pollinations(무료, 키 없음)
    → mock. 유료 키가 우선, 없으면 무료 Pollinations로 실제 이미지 생성 (CLAUDE.md §2.1)."""
    chain: List[ImageProvider] = []
    qwen = _qwen_image()
    if qwen.is_real:
        chain.append(qwen)
    chain += [_primary_image(), _fallback_image()]   # real if keyed, else mock
    free = _free_image()
    if free.is_real:                                  # free AI images (no key) before the mock fallback
        chain.append(free)
    return chain


def get_image_provider() -> ImageProvider:
    """The best available image provider — prefer a real one (keyed or free) over a mock."""
    chain = image_chain()
    for p in chain:
        if getattr(p, "is_real", False):
            return p
    return chain[0]


# per-image model routing — let each image pick its generator (CLAUDE.md §2.1 swappable).
_IMAGE_PROVIDERS_BY_KEY = {
    "qwen": _qwen_image,
    "nano_banana": _primary_image,
    "flux": _fallback_image,
    "free": _free_image,
}
IMAGE_MODEL_LABELS = {
    "auto": "Auto (best available)",
    "qwen": "Qwen (RunPod)",
    "nano_banana": "Nano Banana",
    "flux": "Flux",
    "free": "Free · Pollinations",
}


def get_image_provider_by_name(name: Optional[str]) -> ImageProvider:
    """Provider for a chosen image model. Falls back to the best-available provider when the
    choice is 'auto' or the chosen model isn't configured (e.g. Qwen picked with no RunPod key)."""
    if name and name.lower() not in ("auto", ""):
        factory = _IMAGE_PROVIDERS_BY_KEY.get(name.lower())
        if factory is not None:
            prov = factory()
            if getattr(prov, "is_real", False):
                return prov
    return get_image_provider()


def image_models_status() -> List[dict]:
    """[{key,name,available}] for the per-image model picker UI."""
    out = [{"key": "auto", "name": IMAGE_MODEL_LABELS["auto"], "available": True}]
    for key, factory in _IMAGE_PROVIDERS_BY_KEY.items():
        out.append({"key": key, "name": IMAGE_MODEL_LABELS[key],
                    "available": bool(getattr(factory(), "is_real", False))})
    return out


# video model routing (CLAUDE.md §4.7/§9) — open-source first: a RunPod-hosted I2V model
# (Wan 2.2 / LTX-Video / SVD) is the default, free/open path (ffmpeg Ken Burns until an
# endpoint is deployed). seedance/kling/veo are the proprietary fal alternatives.
_VIDEO_PROVIDERS = {
    "opensource": RunPodVideoProvider,
    "seedance": SeedanceProvider,
    "kling": KlingProvider,
    "veo": VeoProvider,
}
VIDEO_MODELS = tuple(_VIDEO_PROVIDERS.keys())
DEFAULT_VIDEO_MODEL = "opensource"


@lru_cache(maxsize=8)
def get_video_provider(model: str = DEFAULT_VIDEO_MODEL) -> VideoProvider:
    cls = _VIDEO_PROVIDERS.get((model or DEFAULT_VIDEO_MODEL).lower(), RunPodVideoProvider)
    return cls()


# audio/music routing — elevenlabs (default, also does TTS), minimax (low-cost music).
_AUDIO_PROVIDERS = {
    "elevenlabs": ElevenLabsProvider,
    "minimax": MiniMaxMusicProvider,
}
MUSIC_MODELS = tuple(_AUDIO_PROVIDERS.keys())


@lru_cache(maxsize=4)
def get_audio_provider(model: str = "elevenlabs") -> AudioProvider:
    cls = _AUDIO_PROVIDERS.get((model or "elevenlabs").lower(), ElevenLabsProvider)
    return cls()


def generate_with_fallback(
    prompt: str,
    aspect_ratio: str,
    reference_images: Optional[List[bytes]] = None,
    seed: Optional[int] = None,
    model: Optional[str] = None,
) -> ImageResult:
    """Automatic image-provider fallback (CLAUDE.md Phase 3).

    Providers don't raise on a failed real call — they return their own deterministic
    mock. So a plain "return the first result" would mask a down Qwen worker behind a
    Qwen mock even when a real fallback (Nano Banana / Flux) is configured. Instead:
    return the first REAL result; remember a mock and keep trying for a real one; only
    fall back to the mock if every provider mocked.

    `model` (per-image choice) puts that provider first when it's available — so a picked
    Qwen/Nano Banana/Flux/Free is used, with the rest as automatic fallback.
    """
    chain = image_chain()
    if model and model.lower() not in ("auto", ""):
        chosen = get_image_provider_by_name(model)
        if getattr(chosen, "is_real", False):
            chain = [chosen] + [p for p in chain if p is not chosen]
    first_mock: Optional[ImageResult] = None
    last_exc: Optional[Exception] = None
    for prov in chain:
        try:
            res = prov.generate_image(prompt, aspect_ratio, reference_images, seed=seed)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue
        if res is None:
            continue
        if (res.meta or {}).get("mode") == "real":
            return res                      # prefer a real generation
        if first_mock is None:
            first_mock = res                # remember a mock, keep looking for real
    if first_mock is not None:
        return first_mock                   # everything mocked → use the first mock
    raise RuntimeError(f"All image providers failed: {last_exc}")
