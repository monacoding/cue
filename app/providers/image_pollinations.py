"""무료 이미지 생성 provider — Pollinations.ai (CLAUDE.md §2.1 provider 추상화).

키 없이 실제 AI 이미지를 생성한다(오픈 Flux 기반). 테스트/데모용 — FREE_IMAGES=1로 활성.
text→image 전용이라 edit는 instruction으로 재생성해 근사. 실패 시 결정론적 mock 폴백.
"""
from __future__ import annotations

import urllib.parse
from typing import List, Optional

from app.config import settings
from app.providers import _mockgen
from app.providers.base import ImageProvider, ImageResult

# 비율 → 생성 해상도
_GEN_DIMS = {"9:16": (768, 1344), "16:9": (1344, 768), "1:1": (1024, 1024), "4:5": (896, 1152)}


class PollinationsProvider(ImageProvider):
    name = "pollinations"
    endpoint = "https://image.pollinations.ai/prompt/"

    def __init__(self) -> None:
        self.model = settings.free_image_model
        self.timeout = settings.free_image_timeout

    @property
    def is_real(self) -> bool:
        return bool(settings.free_images) and not settings.force_mock

    # -- ImageProvider 인터페이스 ---------------------------------------------
    def generate_image(
        self,
        prompt: str,
        aspect_ratio: str = "9:16",
        reference_images: Optional[List[bytes]] = None,
        seed: Optional[int] = None,
    ) -> ImageResult:
        if self.is_real:
            data = self._fetch(prompt, aspect_ratio, seed)
            if data:
                return ImageResult(image_bytes=data, provider=self.name, meta={"mode": "real"})
        anchor = f"seed{seed}" if seed is not None else ("ref" if reference_images else "")
        data = _mockgen.make_image(prompt, aspect_ratio, anchor=anchor, label="FREE·MOCK")
        return ImageResult(image_bytes=data, provider=self.name + "·mock", meta={"mode": "mock"})

    def edit_image(
        self,
        image: bytes,
        instruction: str,
        reference_images: Optional[List[bytes]] = None,
        base_prompt: Optional[str] = None,
        seed: Optional[int] = None,
    ) -> ImageResult:
        # Pollinations is text→image only (can't edit specific pixels of an existing photo).
        # To AVOID regenerating a brand-new image/person, re-render the SAME described scene
        # (base_prompt) plus the requested change, with a locked seed so the composition and
        # subject are preserved as much as a text model allows.
        if self.is_real:
            if base_prompt:
                base = base_prompt.strip().rstrip(". ")
                prompt = f"{base}. Keep the same scene, subject and composition. Change: {instruction}"
            else:
                prompt = instruction
            data = self._fetch(prompt, "9:16", seed)
            if data:
                return ImageResult(image_bytes=data, provider=self.name, meta={"mode": "real"})
        data = _mockgen.edit_image(image, instruction)
        return ImageResult(image_bytes=data, provider=self.name + "·mock", meta={"mode": "mock"})

    # -- Pollinations 호출 ----------------------------------------------------
    def _fetch(self, prompt: str, aspect_ratio: str, seed: Optional[int]) -> Optional[bytes]:
        try:
            import httpx

            w, h = _GEN_DIMS.get(aspect_ratio, (768, 1344))
            url = self.endpoint + urllib.parse.quote((prompt or "product photo")[:500])
            params = {"width": w, "height": h, "nologo": "true", "model": self.model}
            if seed is not None:
                params["seed"] = int(seed)
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as c:
                r = c.get(url, params=params)
                if r.status_code == 200 and "image" in r.headers.get("content-type", ""):
                    return r.content
        except Exception:
            return None
        return None
