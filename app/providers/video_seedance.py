"""Seedance 2.0 image-to-video provider (CLAUDE.md §4.7, §5).

연결: fal (ByteDance 공식 파트너) / FAL_KEY
엔드포인트: fal-ai/bytedance/seedance-2.0/image-to-video
- 4~15초/콜, 멀티샷, 네이티브 오디오 포함.
- 비용 절벽 ⚠️ — 6단계 전체 승인 후에만 (게이팅은 pipeline/step7 에서 강제).

키 없으면 ffmpeg Ken Burns 로 결정론적 오프라인 클립 생성 (베이스라인).
"""
from __future__ import annotations

from app.config import settings
from app.providers._video_mock import kenburns_clip
from app.providers.base import VideoProvider, VideoResult


class SeedanceProvider(VideoProvider):
    name = "seedance-2.0"
    endpoint = "fal-ai/bytedance/seedance-2.0/image-to-video"
    min_duration, max_duration = 4.0, 15.0   # Seedance: 4–15s/call

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
            data = self._call_fal(image, prompt, duration_sec, with_audio)
            if data:
                return VideoResult(video_bytes=data, provider=self.name, meta={"mode": "real"})
        return kenburns_clip(self.name, image, duration_sec, aspect_ratio, motion)

    def _call_fal(self, image: bytes, prompt: str, duration: float, with_audio: bool):
        try:
            import base64

            import httpx

            b64 = "data:image/png;base64," + base64.b64encode(image).decode()
            with httpx.Client(timeout=600) as client:
                r = client.post(
                    f"https://fal.run/{self.endpoint}",
                    headers={"Authorization": f"Key {self.fal_key}"},
                    json={"prompt": prompt, "image_url": b64,
                          "duration": self.clamp_duration(duration), "audio": with_audio},
                )
                r.raise_for_status()
                url = r.json()["video"]["url"]
                vid = client.get(url)
                vid.raise_for_status()
                return vid.content
        except Exception:
            return None
