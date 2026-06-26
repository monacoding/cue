"""ElevenLabs 오디오 provider (Phase 2, CLAUDE.md §4.8, §5).

연결: ElevenLabs API / ELEVENLABS_API_KEY (xi-api-key)
- 한국어 VO(TTS), SFX, 음악. 상업 라이선스 깔끔.

Phase 1 에서는 인터페이스만. 키 없으면 무음 자리표시자.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from app.config import settings
from app.core import assembly
from app.providers.base import AudioProvider, AudioResult


# ElevenLabs music API accepts 10s–5min; shorter ad videos (≈8s) would otherwise be
# rejected (→ silent offline-bed fallback). Clamp the requested length into range.
_MUSIC_MIN_MS, _MUSIC_MAX_MS = 10_000, 300_000


class ElevenLabsProvider(AudioProvider):
    name = "elevenlabs"

    def __init__(self) -> None:
        self.api_key = settings.elevenlabs_api_key

    @property
    def is_real(self) -> bool:
        return bool(self.api_key) and not settings.force_mock

    def tts(self, text: str, language: str = "ko", voice: str = "") -> AudioResult:
        if self.is_real:
            data = self._call_tts(text, voice)
            if data:
                return AudioResult(audio_bytes=data, provider=self.name, mime="audio/mpeg")
        # 오프라인: 한국어 음성 합성은 불가 → 무음(VO 자리). 음악 베드로 광고감 유지.
        return AudioResult(audio_bytes=b"", provider=self.name + "·offline-silence")

    def music(self, mood: str, duration_sec: float = 15) -> AudioResult:
        if self.is_real:
            data = self._call_music(mood, duration_sec)
            if data:
                return AudioResult(audio_bytes=data, provider=self.name, mime="audio/mpeg")
        # 오프라인 베이스라인: ffmpeg 절차적 음악 베드
        if not assembly.ffmpeg_available():
            return AudioResult(audio_bytes=b"", provider="none")
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "bed.m4a"
            assembly.music_bed(out, duration_sec, mood)
            return AudioResult(audio_bytes=out.read_bytes(), provider="offline-bed", mime="audio/mp4")

    # -- 실제 ElevenLabs 호출 (키 있을 때) -----------------------------------
    def _call_tts(self, text: str, voice: str):
        try:
            import httpx

            vid = voice or "21m00Tcm4TlvDq8ikWAM"  # 기본 보이스
            with httpx.Client(timeout=120) as client:
                r = client.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{vid}",
                    headers={"xi-api-key": self.api_key, "accept": "audio/mpeg"},
                    json={"text": text, "model_id": "eleven_multilingual_v2"},
                )
                r.raise_for_status()
                return r.content
        except Exception:
            return None

    def _call_music(self, mood: str, duration_sec: float):
        try:
            import httpx

            length_ms = max(_MUSIC_MIN_MS, min(_MUSIC_MAX_MS, int(duration_sec * 1000)))
            with httpx.Client(timeout=180) as client:
                r = client.post(
                    "https://api.elevenlabs.io/v1/music",
                    headers={"xi-api-key": self.api_key, "accept": "audio/mpeg"},
                    json={"prompt": f"{mood} background music for an ad",
                          "music_length_ms": length_ms},
                )
                r.raise_for_status()
                return r.content
        except Exception:
            return None
