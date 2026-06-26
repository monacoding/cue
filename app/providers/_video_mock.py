"""Shared offline image-to-video baseline (ffmpeg Ken Burns).

All video providers (Seedance/Kling/Veo) fall back to this deterministic clip when
their API key is absent, so the pipeline runs fully offline regardless of routing.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from app.core import assembly
from app.providers.base import VideoResult


def kenburns_clip(
    provider_name: str,
    image: bytes,
    duration_sec: float = 5,
    aspect_ratio: str = "9:16",
    motion: str = "in",
) -> VideoResult:
    with tempfile.TemporaryDirectory() as td:
        still = Path(td) / "still.png"
        clip = Path(td) / "clip.mp4"
        still.write_bytes(image)
        assembly.still_to_kenburns(
            still, clip, duration=max(1.0, duration_sec),
            aspect_ratio=aspect_ratio, motion=motion,
        )
        data = clip.read_bytes()
    return VideoResult(video_bytes=data, provider=provider_name + "·kenburns", meta={"mode": "mock"})
