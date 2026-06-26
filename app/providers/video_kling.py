"""Kling 3.0 image-to-video provider — low-cost alternative (CLAUDE.md §4.7, §5).

연결: fal / Replicate / FAL_KEY / fal-ai/kling-*  (~$0.029/s)
키 없으면 ffmpeg Ken Burns 오프라인 베이스라인 (공유).
"""
from __future__ import annotations

from app.config import settings
from app.providers._video_mock import kenburns_clip
from app.providers.base import VideoProvider, VideoResult


class KlingProvider(VideoProvider):
    name = "kling-3.0"
    endpoint = "fal-ai/kling-video/v2/standard/image-to-video"
    min_duration, max_duration = 5.0, 10.0   # Kling v2 standard: 5s / 10s

    def __init__(self) -> None:
        self.fal_key = settings.fal_key

    @property
    def is_real(self) -> bool:
        return bool(self.fal_key) and not settings.force_mock

    def image_to_video(
        self,
        image: bytes,
        prompt: str,
        duration_sec: float = 5,
        with_audio: bool = True,
        aspect_ratio: str = "9:16",
        motion: str = "in",
    ) -> VideoResult:
        if self.is_real:
            data = self._call_fal(image, prompt, duration_sec)
            if data:
                return VideoResult(video_bytes=data, provider=self.name, meta={"mode": "real"})
        return kenburns_clip(self.name, image, duration_sec, aspect_ratio, motion)

    def _call_fal(self, image: bytes, prompt: str, duration: float):
        try:
            import base64

            import httpx

            b64 = "data:image/png;base64," + base64.b64encode(image).decode()
            with httpx.Client(timeout=600) as client:
                r = client.post(
                    f"https://fal.run/{self.endpoint}",
                    headers={"Authorization": f"Key {self.fal_key}"},
                    json={"prompt": prompt, "image_url": b64,
                          "duration": str(self.clamp_duration(duration))},
                )
                r.raise_for_status()
                url = r.json()["video"]["url"]
                vid = client.get(url)
                vid.raise_for_status()
                return vid.content
        except Exception:
            return None
