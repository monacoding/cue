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
    endpoint = "fal-ai/flux-2-flex"
    max_reference_images = 4

    def __init__(self) -> None:
        self.fal_key = settings.fal_key
        self.last_error: Optional[str] = None   # set by _call_fal for diagnostics

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
        meta = {"mode": "mock"}
        if self.is_real and self.last_error:        # keyed but the real call failed → surface why
            meta["fell_back"] = True
            meta["reason"] = self.last_error
        return ImageResult(image_bytes=data, provider=self.name + "·mock", meta=meta)

    def edit_image(
        self,
        image: bytes,
        instruction: str,
        reference_images: Optional[List[bytes]] = None,
        **_,  # base_prompt/seed — unused by this provider
    ) -> ImageResult:
        data = _mockgen.edit_image(image, instruction)
        return ImageResult(image_bytes=data, provider=self.name + "·mock", meta={"mode": "mock"})

    # -- fal 호출 ------------------------------------------------------------
    def _call_fal(self, prompt: str, aspect_ratio: str, seed: Optional[int] = None) -> Optional[bytes]:
        from app.providers._fal import fal_image_call

        payload = {"prompt": prompt, "image_size": _fal_size(aspect_ratio)}
        if seed is not None:
            payload["seed"] = seed  # lock the look across shots (seed strategy)
        data, self.last_error = fal_image_call(self.endpoint, payload, self.fal_key)
        return data

    def diagnose(self) -> dict:
        """Self-test the Flux fal connection — used by GET /api/providers/fal/test."""
        from app.providers._fal import fal_diagnose

        return fal_diagnose(self.name, self.endpoint, self.fal_key,
                            {"prompt": "a small red square", "image_size": "square_hd"})


def _fal_size(aspect_ratio: str) -> str:
    return {
        "9:16": "portrait_16_9",
        "16:9": "landscape_16_9",
        "1:1": "square_hd",
        "4:5": "portrait_4_3",
    }.get(aspect_ratio, "portrait_16_9")
