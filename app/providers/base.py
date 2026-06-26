"""Provider 추상 인터페이스 (CLAUDE.md §2.1, §10).

모든 생성 모델은 이 인터페이스 뒤에 둔다. 파이프라인 코드에 모델 SDK를 직접 박지 말 것.
모델 교체 = registry.py 한 줄.

공통 메서드: generate_image, edit_image, image_to_video, tts, music, complete_json
각 provider 는 자신이 지원하는 것만 구현한다.
"""
from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class ImageResult:
    image_bytes: bytes
    mime: str = "image/png"
    provider: str = ""
    meta: Dict[str, Any] = None  # type: ignore


@dataclass
class VideoResult:
    video_bytes: Optional[bytes] = None
    url: Optional[str] = None
    provider: str = ""
    meta: Dict[str, Any] = None  # type: ignore


@dataclass
class AudioResult:
    audio_bytes: bytes = b""
    mime: str = "audio/mpeg"
    provider: str = ""


class LLMProvider(ABC):
    """오케스트레이션 (브리프·스펙·콘티·평가)."""

    name: str = "base-llm"

    def complete_json(
        self,
        system: str,
        user: str,
        schema_hint: Optional[Dict[str, Any]] = None,
        max_tokens: int = 4000,
        images: Optional[List[bytes]] = None,
    ) -> Dict[str, Any]:
        """구조화 JSON 응답을 반환. images(PNG bytes)를 주면 비전(멀티모달)으로 처리.
        실패 시 예외(호출자는 보통 mock_fn 폴백)."""
        raise NotImplementedError

    def complete_text(self, system: str, user: str, max_tokens: int = 1000) -> str:
        raise NotImplementedError


class ImageProvider(ABC):
    """이미지 생성 + 편집 + 일관성."""

    name: str = "base-image"
    max_reference_images: int = 1

    def generate_image(
        self,
        prompt: str,
        aspect_ratio: str = "9:16",
        reference_images: Optional[List[bytes]] = None,
        seed: Optional[int] = None,
    ) -> ImageResult:
        raise NotImplementedError

    def edit_image(
        self,
        image: bytes,
        instruction: str,
        reference_images: Optional[List[bytes]] = None,
        base_prompt: Optional[str] = None,
        seed: Optional[int] = None,
    ) -> ImageResult:
        """Edit an image per a natural-language instruction.

        `base_prompt` (the source image's generation prompt) and `seed` let text→image
        providers that can't do true image-conditioned editing (e.g. Pollinations) regenerate
        the SAME scene + the requested change with a locked composition, instead of a brand-new
        image. Real img2img providers (Nano Banana, Qwen) edit the bytes directly and may ignore
        them."""
        raise NotImplementedError


class VideoProvider(ABC):
    """image-to-video (Phase 2)."""

    name: str = "base-video"
    # Provider's accepted clip length window (seconds), per documented API specs.
    # Pipeline shots are often ~3s, but most image-to-video models reject sub-minimum
    # durations — sending a raw 3 would silently fail and fall back to offline Ken Burns.
    # Override per provider; adjust when verified against the live API.
    min_duration: float = 4.0
    max_duration: float = 15.0

    def clamp_duration(self, duration_sec: float) -> int:
        """Round + clamp to the provider's accepted range (avoids silent API rejection)."""
        d = round(duration_sec)
        return int(max(self.min_duration, min(self.max_duration, d)))

    def image_to_video(
        self,
        image: bytes,
        prompt: str,
        duration_sec: float = 5,
        with_audio: bool = True,
    ) -> VideoResult:
        raise NotImplementedError


class AudioProvider(ABC):
    """TTS / 음악 / SFX (Phase 2)."""

    name: str = "base-audio"

    def tts(self, text: str, language: str = "ko", voice: str = "") -> AudioResult:
        raise NotImplementedError

    def music(self, mood: str, duration_sec: float = 15) -> AudioResult:
        raise NotImplementedError
