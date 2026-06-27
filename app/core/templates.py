"""Ad templates / presets by product category (CLAUDE.md §1 — faster, on-brand defaults).

Each preset pre-fills the step-2 spec (tone / platform / duration) for a product category so
the user doesn't start from scratch. Mirrors core/backgrounds.options() (a flat, JSON-able
list surfaced via an endpoint). Kept deliberately data-only — no concept-rubric coupling
(the rubric is balanced; see Phase 1 note), so presets nudge defaults without regressing eval.
"""
from __future__ import annotations

from typing import List, Optional

_TEMPLATES: List[dict] = [
    {"key": "", "label": "None (manual)", "tone": "", "platform": "instagram_reels",
     "duration_sec": 15, "lens": ""},
    {"key": "food", "label": "F&B · 식음료", "tone": "appetizing, fresh, crave-worthy",
     "platform": "instagram_reels", "duration_sec": 12, "lens": "sensory craving + the moment of taste"},
    {"key": "beauty", "label": "뷰티 · 화장품", "tone": "clean, radiant, confident",
     "platform": "instagram_reels", "duration_sec": 15, "lens": "before→after + confidence"},
    {"key": "tech", "label": "테크 · 가전", "tone": "sleek, modern, premium",
     "platform": "youtube_shorts", "duration_sec": 15, "lens": "spec proof + problem→solution"},
    {"key": "fitness", "label": "피트니스 · 헬스", "tone": "energetic, motivating, bold",
     "platform": "tiktok", "duration_sec": 12, "lens": "transformation + results in numbers"},
    {"key": "fashion", "label": "패션 · 의류", "tone": "stylish, aspirational, editorial",
     "platform": "instagram_reels", "duration_sec": 12, "lens": "lifestyle aspiration"},
    {"key": "home", "label": "리빙 · 홈", "tone": "cozy, calm, practical",
     "platform": "feed", "duration_sec": 15, "lens": "everyday upgrade / ease"},
    {"key": "saas", "label": "앱 · SaaS", "tone": "smart, effortless, trustworthy",
     "platform": "youtube_shorts", "duration_sec": 15, "lens": "pain → one-tap solution"},
]


def options() -> List[dict]:
    """[{key,label,tone,platform,duration_sec,lens}] for the spec-step preset picker."""
    return _TEMPLATES


def get(key: str) -> Optional[dict]:
    if not key:
        return None
    return next((t for t in _TEMPLATES if t["key"] == key), None)
