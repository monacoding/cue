"""선택 가능한 배경 프리셋 (사용자가 학습한 LoRA 장면 = 트리거 문구).

이미지 생성 시 '모델(피사체) + 배경'을 따로 고른다. 배경은 사용자가 학습한 LoRA의 장면 캡션
(예: 명동 메인거리/야경/골목)이며, 선택 시 그 문구가 이미지 프롬프트에 더해져 학습된 배경이
활성화된다. IMAGE_BACKGROUNDS(JSON)로 본인 LoRA 캡션에 맞게 정의/교체할 수 있다.
"""
from __future__ import annotations

import json
from typing import List

from app.config import settings

# 기본 프리셋 — 사용자의 myeongdong_street LoRA 트리거 + 학습 캡션 어휘에 맞춤
# (IMAGE_BACKGROUNDS JSON 으로 장면을 추가/교체 가능)
_DEFAULTS: List[dict] = [
    {"key": "", "label": "Default (from prompt)", "prompt": ""},
    {"key": "mdong_main", "label": "명동 · 메인거리 (낮)",
     "prompt": "myeongdong_street, daytime, commercial street scene, layered storefronts and "
               "vertical signboards, busy pedestrian shopping street, bright daylight"},
    {"key": "mdong_alley", "label": "명동 · 좁은 골목",
     "prompt": "myeongdong_street, narrow shopping alley, dense vertical signboards and pedestrians, "
               "entrance view, mixed Korean and Chinese signage"},
    {"key": "mdong_storefront", "label": "명동 · 매장 앞",
     "prompt": "myeongdong_street, retail storefront, glass display windows, outdoor menu boards and "
               "retail signs, modern retail facade, daytime"},
    {"key": "mdong_crowd", "label": "명동 · 인파",
     "prompt": "myeongdong_street, busy pedestrian shopping street, people walking past, tall "
               "storefronts and layered signboards, bright daylight"},
    {"key": "studio", "label": "스튜디오 (LoRA 미적용)",
     "prompt": "in a clean seamless studio backdrop, soft even lighting"},
]


def options() -> List[dict]:
    """[{key,label,prompt}] — IMAGE_BACKGROUNDS(JSON list) 가 있으면 그것으로, 없으면 기본 프리셋."""
    raw = (settings.image_backgrounds or "").strip()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                opts = [{"key": str(d.get("key", "")), "label": d.get("label") or d.get("key", ""),
                         "prompt": d.get("prompt", "")}
                        for d in data if isinstance(d, dict) and (d.get("key") or d.get("prompt"))]
                if opts:
                    return [{"key": "", "label": "Default (from prompt)", "prompt": ""}] + opts
        except Exception:
            pass
    return _DEFAULTS


def prompt_for(key: str) -> str:
    """배경 key → 추가 프롬프트 문구 (없거나 'default' 면 빈 문자열)."""
    if not key:
        return ""
    for o in options():
        if o["key"] == key:
            return o["prompt"] or ""
    return ""


def apply(prompt: str, key: str) -> str:
    """주제 프롬프트에 선택한 배경 문구를 덧붙인다 (중복 회피)."""
    bg = prompt_for(key).strip()
    p = (prompt or "").strip()
    if bg and bg.lower() not in p.lower():
        return f"{p}, {bg}" if p else bg
    return p
