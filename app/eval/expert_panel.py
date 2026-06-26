"""광고 전문가 패널 평가 (CLAUDE.md §1 — '광고 지능'의 확장).

생성된 광고(제품·헤드라인·CTA·컨셉·콘티 카피)를 10명의 광고 전문가 페르소나가 각자의 전문
관점에서 0~100으로 채점하고, 합산 점수를 권장 광고 예산(광고비)으로 환산한다. 결정론적
휴리스틱(텍스트 특징 기반)이라 키 없이 재현 가능하며, 100개 제품 시뮬레이션의 집계로 파이프라인
카피 로직의 체계적 약점을 드러내는 데 쓴다.

각 전문가는 (점수, 한줄 비평)을 돌려준다. 점수는 실제 카피 신호(숫자/행동동사/감정어/길이/
브랜드 일관성/클리셰 회피/콘티 다양성)에 근거한다 — 난수가 아니다.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Callable, Dict, List

# ---- 텍스트 특징 도우미 ----------------------------------------------------
_ACTION_VERBS = {
    "shop", "buy", "get", "try", "start", "discover", "join", "grab", "claim",
    "save", "unlock", "see", "order", "book", "download", "learn", "explore",
    "switch", "upgrade", "find", "meet",
}
_EMOTION_WORDS = {
    "love", "fear", "dream", "finally", "never", "imagine", "effortless", "free",
    "stop", "transform", "tired", "struggle", "wish", "deserve", "proud", "calm",
    "instant", "again", "without", "more", "less", "worry",
}
_CLICHES = (
    "chosen by thousands", "game-changer", "game changer", "revolutionary",
    "the best", "amazing", "world-class", "next level", "must-have", "you won't believe",
    "say goodbye", "look no further", "one and only",
)
_CURIOSITY = ("?", "what if", "the secret", "why ", "how ", "imagine", "ever wonder")


def _words(s: str) -> List[str]:
    return re.findall(r"[A-Za-z0-9']+", (s or "").lower())


def _has_number(s: str) -> bool:
    return bool(re.search(r"\d", s or ""))


def _has_any(s: str, bag) -> bool:
    low = (s or "").lower()
    return any(w in low for w in bag)


def _count_any(words: List[str], bag) -> int:
    return sum(1 for w in words if w in bag)


# ---- 평가 대상 광고 단위 ---------------------------------------------------
@dataclass
class AdUnit:
    product_name: str = ""
    description: str = ""
    key_message: str = ""
    headline: str = ""           # 첫 컷 훅 = 화면 헤드라인
    cta: str = ""
    concept_angle: str = ""
    shots_text: List[str] = field(default_factory=list)
    platform: str = "instagram_reels"
    tone: str = ""


# ---- 전문가 페르소나 -------------------------------------------------------
@dataclass
class Expert:
    name: str
    specialty: str
    weight: float
    scorer: Callable[[AdUnit], "ExpertVerdict"]


@dataclass
class ExpertVerdict:
    score: float
    critique: str


def _clamp(v: float) -> float:
    return round(max(0.0, min(100.0, v)), 1)


# 1) 훅 스페셜리스트 — 첫 1초 스크롤 정지력
def _hook_specialist(ad: AdUnit) -> ExpertVerdict:
    h = ad.headline.strip()
    words = _words(h)
    s = 45.0
    if h:
        if len(words) <= 8:
            s += 15                      # 짧을수록 강함
        if _has_any(h, _CURIOSITY):
            s += 18
        if _has_number(h):
            s += 12
        if len(words) > 14:
            s -= 18                       # 너무 길면 시선 못 잡음
    else:
        s = 10
    note = "tight, curiosity-driven hook" if s >= 70 else (
        "hook is long or flat — front-load intrigue" if h else "no headline")
    return ExpertVerdict(_clamp(s), note)


# 2) 명료성 에디터 — 메시지 명료/간결성
def _clarity_editor(ad: AdUnit) -> ExpertVerdict:
    h, msg = ad.headline.strip(), ad.key_message.strip()
    wc = len(_words(h)) + len(_words(msg))
    s = 75.0
    if wc == 0:
        s = 15
    elif wc > 30:
        s -= 25
    if msg and msg.lower() == ad.product_name.lower():
        s -= 30                           # 핵심 메시지가 곧 제품명 = 정보 0
    long_words = sum(1 for w in _words(h) + _words(msg) if len(w) >= 12)
    s -= long_words * 4
    note = "clear and scannable" if s >= 70 else "tighten wording / key message lacks substance"
    return ExpertVerdict(_clamp(s), note)


# 3) 브랜드 전략가 — 브랜드명 노출/일관성
def _brand_strategist(ad: AdUnit) -> ExpertVerdict:
    name = ad.product_name.strip().lower()
    s = 40.0
    if name:
        surfaces = [ad.headline, ad.cta, ad.key_message]
        hits = sum(1 for t in surfaces if name in (t or "").lower())
        s += hits * 18                    # 헤드라인/CTA/메시지에 브랜드 반복 노출
        if name in (ad.cta or "").lower():
            s += 8                         # CTA에 브랜드가 있으면 회상↑
    note = "brand present across surfaces" if s >= 70 else "surface the brand name in hook/CTA"
    return ExpertVerdict(_clamp(s), note)


# 4) 전환 최적화 — CTA 행동 유도력
def _conversion_optimizer(ad: AdUnit) -> ExpertVerdict:
    cta = ad.cta.strip()
    words = _words(cta)
    s = 40.0
    if cta:
        if _count_any(words, _ACTION_VERBS):
            s += 28                        # 명령형 행동 동사
        if _has_any(cta, ("now", "today", "free", "limited", "off")):
            s += 16                        # 긴급/인센티브
        if len(words) <= 6:
            s += 8
    else:
        s = 12
    note = "strong, action-led CTA" if s >= 70 else "CTA lacks an action verb or urgency"
    return ExpertVerdict(_clamp(s), note)


# 5) 감정 공명 — 감정 소구
def _emotional_resonance(ad: AdUnit) -> ExpertVerdict:
    blob = " ".join([ad.headline, ad.key_message] + ad.shots_text)
    n = _count_any(_words(blob), _EMOTION_WORDS)
    s = 45.0 + min(35, n * 9)
    if _has_any(ad.headline, ("you", "your")):
        s += 10                            # 2인칭 = 개인적 소구
    note = "emotionally resonant" if s >= 70 else "appeal is functional, add aspiration/pain"
    return ExpertVerdict(_clamp(s), note)


# 6) 구체성 분석가 — 모호한 주장 vs 구체적 수치/혜택
def _specificity_analyst(ad: AdUnit) -> ExpertVerdict:
    blob = " ".join([ad.headline, ad.key_message, ad.description])
    s = 40.0
    if _has_number(blob):
        s += 30                            # 수치 = 신뢰
    if _has_any(blob, ("no ", "without", "in seconds", "hours", "minutes", "x ", "%")):
        s += 18
    if not ad.key_message.strip():
        s -= 20
    note = "concrete, proof-backed claim" if s >= 70 else "vague — add a number or specific benefit"
    return ExpertVerdict(_clamp(s), note)


# 7) 타깃 적합성 — 플랫폼/톤 적합
def _target_fit(ad: AdUnit) -> ExpertVerdict:
    s = 55.0
    if ad.tone.strip():
        s += 15
    short_form = ad.platform in ("instagram_reels", "tiktok", "youtube_shorts")
    if short_form and len(_words(ad.headline)) <= 9:
        s += 18                            # 숏폼엔 짧은 헤드라인
    if short_form and len(_words(ad.headline)) > 14:
        s -= 15
    note = "well-matched to platform/tone" if s >= 70 else "calibrate length/tone to the placement"
    return ExpertVerdict(_clamp(s), note)


# 8) 독창성 비평 — 클리셰/템플릿 회피
def _originality_critic(ad: AdUnit) -> ExpertVerdict:
    h = ad.headline.strip().lower()
    s = 72.0
    if _has_any(h, _CLICHES):
        s -= 35
    # 템플릿 흔적: "before X. after X." / "wait — what is" / "still without"
    if re.search(r"before .+\. after", h) or "wait — what" in h or "wait - what" in h \
            or h.startswith("still without"):
        s -= 22
    if h and len(set(_words(h))) <= 2:
        s -= 15
    note = "fresh angle" if s >= 70 else "reads as a template — vary the structure"
    return ExpertVerdict(_clamp(s), note)


# 9) 비주얼 컨셉 디렉터 — 콘티 다양성/응집
def _visual_director(ad: AdUnit) -> ExpertVerdict:
    shots = [t for t in ad.shots_text if t and t.strip()]
    s = 45.0
    if shots:
        uniq = len(set(s.strip().lower() for s in shots))
        s += min(30, uniq * 8)             # 컷마다 다른 카피 = 서사 진행
        if len(shots) >= 3:
            s += 10
        if uniq == 1 and len(shots) > 1:
            s -= 20                         # 모든 컷이 같은 문구
    note = "varied, progressive storyboard" if s >= 70 else "shots repeat — build a narrative arc"
    return ExpertVerdict(_clamp(s), note)


# 10) 메시지-마켓 핏 — 혜택 주도/헤드라인과 차별
def _message_market_fit(ad: AdUnit) -> ExpertVerdict:
    msg, name, h = ad.key_message.strip(), ad.product_name.strip(), ad.headline.strip()
    s = 60.0
    if msg and msg.lower() != name.lower():
        s += 15
    if msg and h and msg.lower() != h.lower():
        s += 12                            # 메시지와 헤드라인이 다른 정보를 더함
    if _has_any(msg, ("no ", "without", "so you", "for")) or _has_number(msg):
        s += 10                            # 혜택 지향
    if not msg:
        s = 25
    note = "benefit-led, complements the hook" if s >= 70 else "key message echoes name/hook — make it benefit-led"
    return ExpertVerdict(_clamp(s), note)


PANEL: List[Expert] = [
    Expert("Maya Chen", "Hook & scroll-stopping", 0.16, _hook_specialist),
    Expert("David Okoro", "Clarity & concision", 0.10, _clarity_editor),
    Expert("Lena Petrova", "Brand strategy", 0.09, _brand_strategist),
    Expert("Marcus Reed", "Conversion / CTA", 0.13, _conversion_optimizer),
    Expert("Sofia Maretti", "Emotional resonance", 0.10, _emotional_resonance),
    Expert("Raj Malhotra", "Specificity & proof", 0.11, _specificity_analyst),
    Expert("Yuki Tanaka", "Audience / platform fit", 0.09, _target_fit),
    Expert("Grace Adeyemi", "Originality", 0.10, _originality_critic),
    Expert("Tom Becker", "Visual concept", 0.06, _visual_director),
    Expert("Aisha Karim", "Message–market fit", 0.06, _message_market_fit),
]


def _budget_for(score: float) -> Dict:
    """합산 점수 → 권장 일일 광고 예산(광고비) + 입찰 강도. 광고 품질이 미디어 투자 가치를 정한다."""
    if score >= 82:
        return {"tier": "scale", "daily_usd": 500, "max_cpm_usd": 18.0,
                "rationale": "Top-quartile creative — scale spend, bid aggressively."}
    if score >= 70:
        return {"tier": "invest", "daily_usd": 200, "max_cpm_usd": 12.0,
                "rationale": "Solid creative — fund and optimize."}
    if score >= 58:
        return {"tier": "test", "daily_usd": 50, "max_cpm_usd": 7.0,
                "rationale": "Promising — test small, iterate the weak axes."}
    return {"tier": "hold", "daily_usd": 0, "max_cpm_usd": 0.0,
            "rationale": "Below bar — fix copy before spending."}


def evaluate(ad: AdUnit) -> Dict:
    """패널 전체 평가 → 전문가별 점수/비평 + 가중 합산 + 권장 광고비 + 최약 평가축."""
    verdicts = [(e, e.scorer(ad)) for e in PANEL]
    scores = [v.score for _, v in verdicts]
    wsum = sum(e.weight for e, _ in verdicts) or 1.0
    weighted = sum(v.score * e.weight for e, v in verdicts) / wsum
    weakest = sorted(verdicts, key=lambda ev: ev[1].score)[:3]
    return {
        "overall": round(weighted, 1),
        "mean": round(statistics.mean(scores), 1),
        "spread": round(statistics.pstdev(scores), 1),
        "experts": [
            {"name": e.name, "specialty": e.specialty, "weight": e.weight,
             "score": v.score, "critique": v.critique}
            for e, v in verdicts
        ],
        "budget": _budget_for(weighted),
        "weakest_axes": [
            {"specialty": e.specialty, "score": v.score, "critique": v.critique}
            for e, v in weakest
        ],
    }


def ad_from_state(st) -> AdUnit:
    """ProjectState → AdUnit (실제 파이프라인 산출 카피로 평가)."""
    spec = st.adspec
    product = spec.product if spec else None
    headline, cta, shots_text, angle = "", "", [], ""
    sb = getattr(st, "storyboard", None)
    if sb and sb.shots:
        from app.pipeline import pick_cta

        headline = (sb.shots[0].on_screen_text.text_ko or "").strip()
        shots_text = [(s.on_screen_text.text_ko or "").strip() for s in sb.shots]
        # CTA via the shared word-boundary helper (avoids "Forget"/"Budget" false matches)
        pname0 = getattr(product, "name", "") or ""
        cta = pick_cta(shots_text, headline, pname0)
    concepts = getattr(spec, "concepts", None) or []
    if concepts:
        angle = getattr(concepts[0], "angle", "") or ""
        if not headline:
            headline = getattr(concepts[0], "hook", "") or ""
    last = getattr(spec, "last_render", None) or {}
    pname = getattr(product, "name", "") or ""
    cta_fallback = f"Shop {pname} today" if pname else "Shop now"
    return AdUnit(
        product_name=pname,
        description=getattr(product, "description", "") or "",
        key_message=getattr(product, "key_message", "") or "",
        headline=last.get("headline") or headline,
        cta=last.get("cta") or cta or cta_fallback,
        concept_angle=angle,
        shots_text=shots_text,
        platform=getattr(spec, "platform", "instagram_reels") or "instagram_reels",
        tone=getattr(spec, "tone", "") or "",
    )
