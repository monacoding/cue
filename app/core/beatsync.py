"""Beat-sync — snap shot cut timing to a musical beat grid (CLAUDE.md §4.8 AI 보조).

Deterministic baseline: derive a BPM from the music mood, snap each shot's duration
to the nearest beat multiple so cuts land on the beat. Opt-in (explicit action),
never silently overrides the user's durations. A Claude key could later refine this.
"""
from __future__ import annotations

from typing import List

# music mood → tempo (BPM). Unknown moods get a neutral default.
_MOOD_BPM = {
    "upbeat": 128, "energetic": 140, "calm": 80, "lo-fi": 85, "lofi": 85,
    "epic": 100, "dramatic": 90, "chill": 90, "intense": 150, "ambient": 70,
}


def bpm_for(mood: str) -> int:
    return _MOOD_BPM.get((mood or "").strip().lower(), 110)


def snap_durations(durations: List[float], bpm: int, min_beats: int = 1) -> List[float]:
    """Snap each duration to the nearest whole-beat multiple (>= min_beats)."""
    beat = 60.0 / max(1, bpm)
    out = []
    for d in durations:
        beats = max(min_beats, round(d / beat))
        out.append(round(beats * beat, 2))
    return out
