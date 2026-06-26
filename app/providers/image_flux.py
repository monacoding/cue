"""Flux 2 / Seedream 5 이미지 fallback provider (CLAUDE.md §5).

연결: fal / Replicate / ModelArk / FAL_KEY / fal-ai/flux-2-flex
키 없으면 mock. (registry 에서 Nano Banana 실패 시 fallback 후보로 사용 가능.)
"""
from __future__ import annotations

from typing import List, Optional

from app.config import settings
from app.providers import _mockgen
from app.providers.base import ImageProvider, ImageResult


class FluxProvider(ImageProvider):
    name = "flux-2"
    max_reference_images = 4

    def __init__(self) -> None:
        self.fal_key = settings.fal_key

    @property
    def is_real(self) -> bool:
        return bool(self.fal_key) and not settings.force_mock

    def generate_image(
        self,
        prompt: str,
        aspect_ratio: str = "9:16",
        reference_images: Optional[List[bytes]] = None,
        seed: Optional[int] = None,
    ) -> ImageResult:
        if self.is_real:
            data = self._call_fal(prompt, aspect_ratio, seed=seed)
            if data:
                return ImageResult(image_bytes=data, provider=self.name, meta={"mode": "real"})
        # mock fallback: seed (seed strategy) or ref (reference strategy) anchors the look
        anchor = f"seed{seed}" if seed is not None else ("ref" if reference_images else "")
        data = _mockgen.make_image(prompt, aspect_ratio, anchor=anchor, label="FLUX·MOCK")
        return ImageResult(image_bytes=data, provider=self.name + "·mock", meta={"mode": "mock"})

    def edit_image(
        self,
        image: bytes,
        instruction: str,
        reference_images: Optional[List[bytes]] = None,
        **_,  # base_prompt/seed — unused by this provider
    ) -> ImageResult:
        data = _mockgen.edit_image(image, instruction)
        return ImageResult(image_bytes=data, provider=self.name + "·mock", meta={"mode": "mock"})

    # -- fal 호출 (httpx) ------------------------------------------------------
    def _call_fal(self, prompt: str, aspect_ratio: str, seed: Optional[int] = None) -> Optional[bytes]:
        try:
            import httpx

            payload = {"prompt": prompt, "image_size": _fal_size(aspect_ratio)}
            if seed is not None:
                payload["seed"] = seed  # lock the look across shots (seed strategy)
            with httpx.Client(timeout=120) as client:
                r = client.post(
                    "https://fal.run/fal-ai/flux-2-flex",
                    headers={"Authorization": f"Key {self.fal_key}"},
                    json=payload,
                )
                r.raise_for_status()
                url = r.json()["images"][0]["url"]
                img = client.get(url)
                img.raise_for_status()
                return img.content
        except Exception:
            return None


def _fal_size(aspect_ratio: str) -> str:
    return {
        "9:16": "portrait_16_9",
        "16:9": "landscape_16_9",
        "1:1": "square_hd",
        "4:5": "portrait_4_3",
    }.get(aspect_ratio, "portrait_16_9")
