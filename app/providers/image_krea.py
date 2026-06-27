"""Krea 2 (Turbo) 이미지 provider — krea-ai/krea-2 (CLAUDE.md §2.1 provider 추상화, §5).

Krea 2는 오픈웨이트(HF: Krea 2 RAW / Turbo)이자 fal API(`fal-ai/krea-2/turbo`)로 제공된다.
프로젝트가 이미 `FAL_KEY`로 fal을 쓰므로 가장 단순한 fal 경로로 연결한다(키 없으면 mock 폴백).
turbo = 8-step distilled(cfg 0) → 빠르고 저렴. text→image 전용이라 edit는 base_prompt+지시로 재생성해 근사.
"""
from __future__ import annotations

from typing import List, Optional

from app.config import settings
from app.providers import _mockgen
from app.providers.base import ImageProvider, ImageResult
from app.providers.image_flux import _fal_size  # reuse the aspect_ratio → fal image_size map


class KreaProvider(ImageProvider):
    name = "krea-2"
    endpoint = "fal-ai/krea-2/turbo"
    max_reference_images = 1
    _STEPS = 8  # Krea 2 Turbo distilled

    def __init__(self) -> None:
        self.fal_key = settings.fal_key
        self.last_error: Optional[str] = None   # set by _call_fal for diagnostics

    @property
    def is_real(self) -> bool:
        return bool(self.fal_key) and not settings.force_mock

    def _mock(self, prompt, aspect_ratio, reference_images, seed, *, edit_src=None,
              instruction="") -> ImageResult:
        if edit_src is not None:
            data = _mockgen.edit_image(edit_src, instruction)
        else:
            anchor = f"seed{seed}" if seed is not None else ("ref" if reference_images else "")
            data = _mockgen.make_image(prompt, aspect_ratio, anchor=anchor, label="KREA·MOCK")
        meta = {"mode": "mock"}
        if self.is_real and self.last_error:        # keyed but the real call failed → surface why
            meta["fell_back"] = True
            meta["reason"] = self.last_error
        return ImageResult(image_bytes=data, provider=self.name + "·mock", meta=meta)

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
        return self._mock(prompt, aspect_ratio, reference_images, seed)

    def edit_image(
        self,
        image: bytes,
        instruction: str,
        reference_images: Optional[List[bytes]] = None,
        base_prompt: Optional[str] = None,
        seed: Optional[int] = None,
    ) -> ImageResult:
        # Krea 2 Turbo is text→image — re-render the SAME described scene (base_prompt) plus the
        # requested change, with a locked seed, instead of producing a brand-new subject.
        if self.is_real and base_prompt:
            merged = f"{base_prompt}. Edit: {instruction}".strip()
            data = self._call_fal(merged, "9:16", seed=seed)
            if data:
                return ImageResult(image_bytes=data, provider=self.name, meta={"mode": "real"})
        return self._mock("", "9:16", reference_images, seed, edit_src=image, instruction=instruction)

    # -- fal 호출 ------------------------------------------------------------
    def _call_fal(self, prompt: str, aspect_ratio: str, seed: Optional[int] = None) -> Optional[bytes]:
        from app.providers._fal import fal_image_call

        payload = {"prompt": prompt, "image_size": _fal_size(aspect_ratio),
                   "num_inference_steps": self._STEPS}
        if seed is not None:
            payload["seed"] = seed  # lock the look across shots (seed strategy / reproduce)
        data, self.last_error = fal_image_call(self.endpoint, payload, self.fal_key)
        return data

    def diagnose(self) -> dict:
        """Self-test the Krea 2 fal connection — used by GET /api/providers/fal/test."""
        from app.providers._fal import fal_diagnose

        return fal_diagnose(self.name, self.endpoint,
                            self.fal_key, {"prompt": "a small red square",
                                           "image_size": "square_hd",
                                           "num_inference_steps": self._STEPS})
