"""한글 지원 폰트 해석 — macOS/Linux(컨테이너) 공통.

우선순위: CUE_FONT(env) → macOS 시스템 → Noto CJK(Linux) → 기본.
composite(Pillow) / assembly(ffmpeg drawtext) / _mockgen 가 공유.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from app.config import settings

_CANDIDATES = [
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    # Linux / 컨테이너 (Noto CJK)
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKkr-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
]


def candidates() -> List[str]:
    out: List[str] = []
    if settings.font_path:
        out.append(settings.font_path)
    out.extend(_CANDIDATES)
    return out


def find_font() -> Optional[str]:
    """존재하는 첫 폰트 경로 (없으면 None)."""
    for f in candidates():
        if f and Path(f).exists():
            return f
    return None
