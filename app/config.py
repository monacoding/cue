"""환경설정 로더. .env를 읽고 키 유무에 따라 provider 동작(real/mock)을 결정."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

# .env 로드 (python-dotenv 없으면 수동 파싱)
ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = ROOT / ".env"


def _parse_env_line(line: str):
    """Parse one .env line → (key, value) or None. Mirrors python-dotenv's basics so the
    manual fallback (when python-dotenv isn't installed) doesn't subtly differ:
    handles `export KEY=...` and strips surrounding quotes (a quoted RunPod key would
    otherwise become `Bearer "abc"` → 401)."""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    k, _, v = line.partition("=")
    k = k.strip()
    if k.startswith("export "):
        k = k[len("export "):].strip()
    if not k:
        return None
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1]   # strip matching surrounding quotes
    return k, v


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(_ENV_PATH)
        return
    except Exception:
        pass
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
            parsed = _parse_env_line(line)
            if parsed:
                os.environ.setdefault(parsed[0], parsed[1])


_load_dotenv()


class Settings:
    """런타임 설정 단일 소스."""

    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    fal_key: str = os.getenv("FAL_KEY", "")
    elevenlabs_api_key: str = os.getenv("ELEVENLABS_API_KEY", "")
    replicate_token: str = os.getenv("REPLICATE_API_TOKEN", "")
    modelark_key: str = os.getenv("MODELARK_API_KEY", "")

    # RunPod-hosted Qwen image model (serverless endpoint id, or a full URL for a custom pod)
    runpod_api_key: str = os.getenv("RUNPOD_API_KEY", "")
    runpod_qwen_endpoint: str = os.getenv("RUNPOD_QWEN_ENDPOINT", "")
    # extra/override input fields merged into every request (JSON), e.g.
    # {"negative_prompt":"blurry","num_inference_steps":30,"guidance_scale":4} — adapts to any worker.
    # For a LoRA: put the worker's LoRA fields here, e.g. {"lora":"myeongdong","lora_strength":0.85}.
    runpod_qwen_input: str = os.getenv("RUNPOD_QWEN_INPUT", "")
    # appended to every Qwen prompt — the LoRA trigger word / style phrase (e.g. "myeongdong_street").
    runpod_qwen_style: str = os.getenv("RUNPOD_QWEN_STYLE", "")
    # RunPod-hosted open-source image-to-video model (Wan 2.2 / LTX-Video / SVD …) — the
    # free/open video path. Serverless endpoint id or a full custom-pod URL. Empty → ffmpeg
    # Ken Burns baseline (still works offline, just no generative motion).
    runpod_video_endpoint: str = os.getenv("RUNPOD_VIDEO_ENDPOINT", "")
    # extra/override input fields merged into every video request (JSON), e.g.
    # {"num_frames":81,"fps":16,"guidance_scale":5,"negative_prompt":"blurry"} — adapts to any worker.
    runpod_video_input: str = os.getenv("RUNPOD_VIDEO_INPUT", "")
    # selectable background scenes (your trained LoRA captions), JSON list of {key,label,prompt}.
    # Empty → built-in Myeongdong presets. Per-image background picker uses these.
    image_backgrounds: str = os.getenv("IMAGE_BACKGROUNDS", "")

    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8")
    gemini_image_model: str = os.getenv("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image")

    # LLM via the local Claude CLI (`claude -p`) — uses the user's existing Claude login,
    # no API key needed. Opt-in (CLAUDE_CLI=1); falls back to API key, then mock.
    claude_cli_enabled: bool = os.getenv("CLAUDE_CLI", "0") in ("1", "true", "True")
    claude_cli_model: str = os.getenv("CLAUDE_CLI_MODEL", "")          # "" → CLI default
    claude_cli_timeout: int = int(os.getenv("CLAUDE_CLI_TIMEOUT", "120") or "120")

    # Free, no-key AI image generation via Pollinations.ai (Flux, open) — for testing/demo
    # without paid keys. FREE_IMAGES=1 enables it as the image provider above the mock.
    free_images: bool = os.getenv("FREE_IMAGES", "0") in ("1", "true", "True")
    free_image_model: str = os.getenv("FREE_IMAGE_MODEL", "flux")
    free_image_timeout: int = int(os.getenv("FREE_IMAGE_TIMEOUT", "90") or "90")

    # 한글 폰트 경로 override (컨테이너/Linux 에서 Noto CJK 지정용)
    font_path: str = os.getenv("CUE_FONT", "")

    force_mock: bool = os.getenv("CUE_FORCE_MOCK", "0") in ("1", "true", "True")

    storage_dir: Path = ROOT / "storage"
    projects_dir: Path = ROOT / "storage" / "projects"
    web_dir: Path = ROOT / "app" / "web"

    def __init__(self) -> None:
        self.projects_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
