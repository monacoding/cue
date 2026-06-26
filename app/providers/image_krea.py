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
        anchor = f"seed{seed}" if seed is not None else ("ref" if reference_images else "")
        data = _mockgen.make_image(prompt, aspect_ratio, anchor=anchor, label="KREA·MOCK")
        return ImageResult(image_bytes=data, provider=self.name + "·mock", meta={"mode": "mock"})

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
        data = _mockgen.edit_image(image, instruction)
        return ImageResult(image_bytes=data, provider=self.name + "·mock", meta={"mode": "mock"})

    # -- fal 호출 (httpx) ------------------------------------------------------
    def _call_fal(self, prompt: str, aspect_ratio: str, seed: Optional[int] = None) -> Optional[bytes]:
        try:
            import httpx

            payload = {
                "prompt": prompt,
                "image_size": _fal_size(aspect_ratio),
                "num_inference_steps": self._STEPS,
            }
            if seed is not None:
                payload["seed"] = seed  # lock the look across shots (seed strategy / reproduce)
            with httpx.Client(timeout=120) as client:
                r = client.post(
                    f"https://fal.run/{self.endpoint}",
                    headers={"Authorization": f"Key {self.fal_key}"},
                    json=payload,
                )
                r.raise_for_status()
                imgs = (r.json() or {}).get("images") or []
                if not imgs:
                    return None
                img = client.get(imgs[0]["url"])
                img.raise_for_status()
                return img.content
        except Exception:
            return None
