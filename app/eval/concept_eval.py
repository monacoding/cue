"""가중 루브릭 컨셉 평가 (CLAUDE.md §4.3 선택 — 광고 지능의 핵심).

컨셉/훅을 N개 생성 → 전환 가중 루브릭(RUBRIC)으로 결정론적 채점 → best 선별.
이것이 '생성'이 아닌 '광고 지능' 차별점. 모델: Claude(컨셉 생성). 키 없으면 휴리스틱.
(주: 채점은 결정론적 — 키리스 재현성 유지. 'Monte-Carlo'가 아님.)
"""
from __future__ import annotations

from typing import Dict, List

from app.core import state as state_store
from app.core.schemas import Concept
from app.providers.registry import get_llm

# 전환에 영향 주는 평가 축 (가중치)
RUBRIC = {
    "scroll_stopping": 0.30,   # 첫 1초 시선 정지력
    "clarity": 0.20,           # 메시지 명료성
    "relevance": 0.20,         # 타깃 적합성
    "emotional_pull": 0.15,    # 감정 소구
    "cta_strength": 0.15,      # 행동 유도력
}


import re as _re

# filler/cliché clauses that add no information to a hook — stripped from key messages.
# the cliché tail is matched WHOLE ("the … everyone is talking about") so no fragment is left.
_FILLER = _re.compile(
    r"\b(the\s+.*?\beveryone(?:'s| is)?\s+talking about|everyone(?:'s| is)?\s+talking about|"
    r"that everyone(?:'s| is)?|you(?:'ll| will) love|best[- ]in[- ]class|"
    r"world[- ]class|game[- ]chang\w*)\b",
    _re.IGNORECASE,
)
_LEAD_ARTICLES = _re.compile(r"^(the|a|an|is a|is the|a new)\s+", _re.IGNORECASE)

# "<noun> that helps you <benefit>" → keep the benefit. Prefer the explicit benefit connective
# ("helps you"/"lets you"/"so you can") over a bare "that", and match non-greedily so the FIRST
# such connective wins (a greedy ".*" would skip to the last one and drop the real benefit).
_HELPS = _re.compile(r"^.*?\b(?:helps? you|lets? you|so you can)\s+", _re.IGNORECASE)
_THAT = _re.compile(r"^.*?\bthat\s+", _re.IGNORECASE)

# stop-words: a benefit made of only these is meaningless (e.g. leftover "is talking about")
_STOP = {"is", "the", "a", "an", "of", "to", "in", "on", "for", "and", "about", "talking",
         "that", "this", "it", "you", "your", "are", "with", "by", "be"}


def _benefit_phrase(name: str, msg: str) -> str:
    """Distil a clean, viewer-facing benefit clause from the key message — strip the brand
    name, leading articles, and cliché filler, and keep the strongest (numeric) clause.
    Returns "" when nothing meaningful remains (caller falls back to a brand-led hook)."""
    b = _FILLER.sub("", msg or "").strip(" .,—–-:")
    # drop the brand name if the message just repeats it
    if name and b.lower().startswith(name.lower()):
        b = b[len(name):].strip(" .,—–-:")
    # "<product noun> that helps you <benefit>" → keep just the benefit clause
    if _HELPS.match(b):
        b = _HELPS.sub("", b, count=1).strip()
    elif _THAT.match(b):
        b = _THAT.sub("", b, count=1).strip()
    b = _LEAD_ARTICLES.sub("", b).strip()
    # if several benefits are comma-listed, lead with the one carrying a number (proof)
    parts = [p.strip() for p in _re.split(r"[;,]", b) if p.strip()]
    if parts:
        numeric = next((p for p in parts if any(c.isdigit() for c in p)), None)
        b = numeric or parts[0]
    b = b.strip(" .,—–-:")
    # reject a benefit with no meaningful (non-stop-word, non-numeric) content
    meaningful = [w for w in _re.findall(r"[A-Za-z]+", b.lower()) if w not in _STOP]
    if not meaningful and not any(c.isdigit() for c in b):
        return ""
    return b


def _first_number(text: str) -> str:
    # number + unit word (e.g. "40-hour battery", "500 calories"), trimming a trailing
    # function word so we don't emit "500 calories in" / "7 days of".
    m = _re.search(r"\d[\d,]*\s*[A-Za-z%\-]*(?:\s+[A-Za-z]+)?", text or "")
    if not m:
        return ""
    return _re.sub(r"\s+(?:in|of|on|for|to|and|the|a|with|per|so)$", "",
                   m.group(0).strip(), flags=_re.IGNORECASE)


def _heuristic_concepts(spec, n: int) -> List[Dict]:
    name = spec.product.name or "the product"
    raw_msg = spec.product.key_message or name
    # empty benefit (vague/meaningless message) → angles fall back to brand-led hooks,
    # rather than echoing cliché filler like "…everyone is talking about" on screen
    benefit = _benefit_phrase(name, raw_msg)
    blow = benefit.lower()
    num = _first_number(raw_msg + " " + (getattr(spec.product, "description", "") or ""))
    # (angle, VIEWER-FACING hook copy, why-it-works). The hook is on-screen ad copy — it
    # gets burned onto shot 1, so it must read as a headline, NOT as a director's note.
    # Each angle surfaces the BRAND and leads with a benefit/number/emotion — avoid templates.
    angles = [
        ("Benefit-Lead", f"{benefit[:1].upper()}{benefit[1:]}." if benefit else f"Meet {name}.",
         "Leads with the core benefit so the value lands in the first second."),
        ("Brand-Benefit", f"{name}: {benefit}." if benefit else f"This is {name}.",
         "Pairs the brand with its payoff so the name and value register together."),
        (("Proof-First" if num else "Second-Person"),
         (f"{num.capitalize()} — meet {name}." if num else f"Your upgrade is here: {name}."),
         "A concrete number (or a direct 'you') earns trust in the first second."),
        ("Loss-Aversion", f"Still settling for less? Try {name}.",
         "Framing the gap creates urgency to act now — and surfaces the brand."),
        ("Emotion", f"Finally, {blow or 'something that just works'}.",
         "An emotional release ('finally') makes the benefit feel personal."),
        ("Curiosity", f"What if {blow or 'it just worked'}?",
         "An open question stops the scroll and invites the answer."),
    ]
    out = []
    for i in range(n):
        angle, hook, why = angles[i % len(angles)]
        hook = _re.sub(r"\s+", " ", hook).strip()
        out.append({"id": f"concept_{i+1}", "hook": hook, "angle": angle,
                    "rationale": f"{why} (optimized for {spec.platform})"})
    return out


_EMO_SIGNALS = ("finally", "never", "without", "you", "your", "stop", "more", "less",
                "instant", "effortless", "imagine", "again")


def _score(concept: Dict, spec) -> Dict[str, float]:
    # Deterministic heuristic on text signals (real service would use an LLM/model judge).
    # Aligned with direct-response copy principles: short + curious + specific + emotional + branded.
    hook = concept.get("hook", "")
    low = hook.lower()
    nwords = len(hook.split())
    breakdown = {}
    has_num = any(c.isdigit() for c in hook)
    curious = ("?" in hook) or ("what if" in low) or low.startswith("still ")
    breakdown["scroll_stopping"] = min(
        1.0, 0.35 + (nwords <= 8) * 0.25 + curious * 0.2 + has_num * 0.2
    )
    # clarity: does the hook echo the key message? Guard the empty case — "" is "in"
    # every string, which would hand every concept a free +0.5 when key_message is blank.
    kw = (spec.product.key_message or "")[:4].strip().lower()
    breakdown["clarity"] = min(1.0, 0.5 + (1 if kw and kw in low else 0) * 0.5)
    breakdown["relevance"] = 0.6 + (spec.tone != "") * 0.2
    emo_hits = sum(1 for w in _EMO_SIGNALS if w in low)
    breakdown["emotional_pull"] = min(1.0, 0.4 + min(0.5, emo_hits * 0.18))
    # CTA/action strength: brand surfaced or an urgency/loss frame
    brand = (getattr(spec.product, "name", "") or "").lower()
    breakdown["cta_strength"] = min(
        1.0, 0.5 + (bool(brand) and brand in low) * 0.2
        + (concept.get("angle") in ("Loss-Aversion", "Proof-First")) * 0.2
    )
    return {k: round(min(1.0, v), 3) for k, v in breakdown.items()}


def evaluate(project_id: str, n: int = 5) -> List[Concept]:
    st = state_store.load(project_id)
    if st is None or st.adspec is None:
        raise ValueError("AdSpec required")
    spec = st.adspec

    lang = (spec.audio.language or "en").lower()
    lang_name = "Korean" if lang == "ko" else "English"
    system = (
        "You are a direct-response ad strategist. Generate distinct ad concepts/hooks for the product, "
        f"each from a different angle. Each concept has id, hook (one line in {lang_name}), angle, rationale."
    )
    user = f"AdSpec:\n{spec.model_dump_json(indent=2)}\nGenerate {n} distinct concepts."
    schema = {"concepts": [{"id": "str", "hook": "str", "angle": "str", "rationale": "str"}]}

    data = get_llm().complete_json(
        system, user,
        mock_fn=lambda: {"concepts": _heuristic_concepts(spec, n)},
        schema_hint=schema,
    )

    concepts: List[Concept] = []
    for c in data.get("concepts", []):
        breakdown = _score(c, spec)
        total = round(sum(breakdown[k] * w for k, w in RUBRIC.items()), 4)
        concepts.append(
            Concept(
                # force deterministic, safe ids — id reaches a UI onclick JS-string,
                # never trust model-provided ids (mirrors shot_id handling)
                id=f"concept_{len(concepts)+1}",
                hook=c.get("hook", ""),
                angle=c.get("angle", ""),
                rationale=c.get("rationale", ""),
                score=total,
                score_breakdown=breakdown,
            )
        )
    concepts.sort(key=lambda x: x.score, reverse=True)
    st.concepts = concepts
    state_store.save(st)
    return concepts
