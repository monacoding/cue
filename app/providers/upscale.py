"""Upscale provider (CLAUDE.md §9, 선택) — Topaz / SeedVR via fal.

키 없으면 결정론적 오프라인 베이스라인(Lanczos: Pillow 이미지 / ffmpeg 영상).
"""
from __future__ import annotations

import base64
from pathlib import Path

from app.config import settings
from app.core import assembly, composite


class UpscaleProvider:
    name = "topaz"
    image_endpoint = "fal-ai/clarity-upscaler"

    def __init__(self) -> None:
        self.fal_key = settings.fal_key

    @property
    def is_real(self) -> bool:
        return bool(self.fal_key) and not settings.force_mock

    def upscale_image(self, image: bytes, factor: int = 2) -> bytes:
        if self.is_real:
            data = self._call_fal_image(image)
            if data:
                return data
        return composite.upscale_image(image, factor)  # Lanczos offline baseline

    def upscale_video(self, in_path: Path, out_path: Path, factor: int = 2) -> Path:
        # 영상 업스케일은 fal 비동기 큐가 필요 — 여기서는 오프라인 Lanczos 베이스라인.
        return assembly.upscale_video(in_path, out_path, factor)

    def _call_fal_image(self, image: bytes):
        try:
            import httpx

            b64 = "data:image/png;base64," + base64.b64encode(image).decode()
            with httpx.Client(timeout=300) as client:
                r = client.post(
                    f"https://fal.run/{self.image_endpoint}",
                    headers={"Authorization": f"Key {self.fal_key}"},
                    json={"image_url": b64},
                )
                r.raise_for_status()
                url = r.json()["image"]["url"]
                img = client.get(url)
                img.raise_for_status()
                return img.content
        except Exception:
            return None
