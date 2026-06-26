"""MiniMax Music provider — low-cost music alternative (CLAUDE.md §5).

연결: fal / FAL_KEY / fal-ai/minimax-music (~$0.035/gen)
음악만 제공(TTS는 ElevenLabs). 키 없으면 ffmpeg 절차적 베드(공유) 폴백.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from app.config import settings
from app.core import assembly
from app.providers.base import AudioProvider, AudioResult


class MiniMaxMusicProvider(AudioProvider):
    name = "minimax-music"
    endpoint = "fal-ai/minimax-music"

    def __init__(self) -> None:
        self.fal_key = settings.fal_key

    @property
    def is_real(self) -> bool:
        return bool(self.fal_key) and not settings.force_mock

    def tts(self, text: str, language: str = "ko", voice: str = "") -> AudioResult:
        # MiniMax 경로는 음악 전용 — VO는 ElevenLabs 사용. 오프라인 무음.
        return AudioResult(audio_bytes=b"", provider=self.name + "·no-tts")

    def music(self, mood: str, duration_sec: float = 15) -> AudioResult:
        if self.is_real:
            data = self._call_fal(mood, duration_sec)
            if data:
                return AudioResult(audio_bytes=data, provider=self.name, mime="audio/mpeg")
        if not assembly.ffmpeg_available():
            return AudioResult(audio_bytes=b"", provider="none")
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "bed.m4a"
            assembly.music_bed(out, duration_sec, mood)
            return AudioResult(audio_bytes=out.read_bytes(), provider=self.name + "·offline-bed",
                               mime="audio/mp4")

    def _call_fal(self, mood: str, duration_sec: float):
        try:
            import httpx

            with httpx.Client(timeout=180) as client:
                r = client.post(
                    f"https://fal.run/{self.endpoint}",
                    headers={"Authorization": f"Key {self.fal_key}"},
                    json={"prompt": f"{mood} background music for an ad"},
                )
                r.raise_for_status()
                url = r.json()["audio"]["url"]
                a = client.get(url)
                a.raise_for_status()
                return a.content
        except Exception:
            return None
