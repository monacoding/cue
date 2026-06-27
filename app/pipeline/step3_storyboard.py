"""3단계 — 콘티(스토리보드) 생성 (CLAUDE.md §4.3).

처리: AdSpec 기반 컷별 Storyboard JSON 자동 생성 → 사람 수정.
산문 금지, 구조 데이터. 모델: Claude. 키 없으면 결정론적 mock.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from app.core import state as state_store

# shot.id flows into UI markup and asset filenames — must be a safe token to
# prevent stored XSS / JS injection (PUT /storyboard accepts arbitrary ids).
_SHOT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
from app.core.schemas import (
    AudioCue,
    Concept,
    OnScreenText,
    PipelineStep,
    Shot,
    StepStatus,
    Storyboard,
)
from app.providers.registry import get_llm

# Ad narrative beats (distributed across shot count) — part of the ad intelligence
NARRATIVE_BEATS = [
    ("hook", "Strong visual hook — a scroll-stopping opening frame", "dolly_in", "center"),
    ("problem", "Show the pain/friction the target audience feels", "static", "top"),
    ("product", "Product reveal — close-up on the key feature", "tracking", "center"),
    ("benefit", "The changed result/emotion after using the product", "pan_left", "center"),
    ("proof", "Trust signal — product in use / detail shot", "static", "bottom"),
    ("cta", "Call to action — product + CTA", "static", "bottom"),
]


def _mock_storyboard(spec, concept: Optional[Concept] = None) -> Dict:
    n = spec.shot_count
    product = spec.product.name or "the product"
    msg = spec.product.key_message or product
    # Ad intelligence: selected concept hook seeds shot-1 copy; angle shapes the narrative
    hook_text = concept.hook if concept else msg
    # branded, action-led CTA copy (surfaces the brand + a verb) instead of a generic one
    cta_text = f"Shop {spec.product.name} today" if spec.product.name else "Shop now"
    # supporting captions: lead with concrete proof claims, then key-message benefit clauses —
    # so each body shot carries a DISTINCT, specific on-screen line (proof surfaced + varied
    # storyboard) instead of repeating one generic caption.
    proofs = list(getattr(spec.product, "proof_points", None) or [])   # concrete numeric claims
    benefits = [b.strip(" .").capitalize() for b in re.split(r"[;,]", msg) if len(b.strip()) > 2]
    captions: List[str] = []
    for c in proofs + benefits:                                        # proof first, dedup
        if c and c not in captions:
            captions.append(c)
    body_idx = 0
    beats = _distribute_beats(n)
    per = max(1.0, round(spec.duration_sec / n, 1))
    shots: List[Dict] = []
    for i, (beat, desc, camera, pos) in enumerate(beats, start=1):
        is_last = i == n
        if beat == "hook":
            desc = f"{desc}. [concept: {concept.angle}]" if concept else desc
        if beat == "hook":
            on_text = hook_text
        elif beat == "cta":
            on_text = cta_text
        elif captions:
            on_text = captions[body_idx % len(captions)]   # distinct proof/benefit caption
            body_idx += 1
        else:
            on_text = ""
        # vary transitions so the per-shot transition feature is exercised:
        # energetic whip into the hook, crossfades through the body, hard cut to end.
        if is_last:
            transition = "cut"
        elif beat == "hook":
            transition = "whip_pan"
        else:
            transition = "crossfade"
        shots.append(
            {
                "id": f"shot_{i}",
                "description": f"{desc}. Subject: {product}.",
                "camera": camera,
                "duration_sec": per,
                "transition": transition,
                "on_screen_text": {"text_ko": on_text, "position": pos, "style_ref": "bold"},
                "audio_cue": {
                    "vo_ko": msg if beat in ("hook", "cta") else "",
                    "sfx": "whoosh" if beat == "hook" else "",
                    "music_beat": "drop" if beat == "cta" else "build",
                },
                "image_prompt": _img_prompt(spec, beat, desc),
                "consistency_anchor": "hero_image",
            }
        )
    return {"shots": shots}


def _distribute_beats(n: int) -> List:
    if n <= 1:
        # Single shot: product + CTA beat (avoid dropping the CTA)
        return [NARRATIVE_BEATS[-1]]
    if n <= len(NARRATIVE_BEATS):
        # hook and cta are always included
        core = [NARRATIVE_BEATS[0]]
        middle = NARRATIVE_BEATS[1:-1]
        core += middle[: max(0, n - 2)]
        core += [NARRATIVE_BEATS[-1]]
        return core[:n]
    out = list(NARRATIVE_BEATS)
    while len(out) < n:
        out.insert(-1, NARRATIVE_BEATS[3])  # repeat benefit
    return out[:n]


def _img_prompt(spec, beat: str, desc: str) -> str:
    tone = spec.tone or "sleek and modern"
    return (
        f"{tone} advertising photo. {spec.product.name or 'the product'}. {desc} "
        f"{spec.aspect_ratio} composition for {spec.platform}. High quality, commercial ad look."
    )


def _num(v, default: float) -> float:
    """Coerce a model-provided value to float, tolerating '3s' / None / junk."""
    try:
        return float(v)
    except (TypeError, ValueError):
        try:
            import re
            m = re.search(r"-?\d+(\.\d+)?", str(v))
            return float(m.group()) if m else default
        except Exception:
            return default


def _resolve_concept(st, concept_id: Optional[str]) -> Optional[Concept]:
    """선택 컨셉 결정 — 명시 id 우선, 없으면 평가된 최고 컨셉 사용.

    명시한 concept_id 가 없으면(오타/재평가로 id 변경) 조용히 다른 컨셉으로
    대체하지 않고 에러를 던진다 — 광고 지능 출력이 의도와 달라지는 것 방지.
    """
    if not st.concepts:
        return None
    if concept_id:
        for c in st.concepts:
            if c.id == concept_id:
                return c
        raise ValueError(f"concept_id {concept_id!r} not found (re-evaluate concepts?)")
    return st.concepts[0]  # 이미 점수 내림차순 정렬


def run(project_id: str, concept_id: Optional[str] = None) -> Storyboard:
    st = state_store.load(project_id)
    if st is None or st.adspec is None:
        raise ValueError("AdSpec required before storyboard")
    spec = st.adspec
    concept = _resolve_concept(st, concept_id)

    lang = (spec.audio.language or "en").lower()
    lang_name = "Korean" if lang == "ko" else "English"
    system = (
        "You are a performance-ad storyboard writer. Build a per-shot storyboard from the given AdSpec. "
        "No prose. Each element of the shots array has id, description, camera, duration_sec, transition, "
        "on_screen_text{text_ko,position,style_ref}, audio_cue{vo_ko,sfx,music_beat}, "
        "image_prompt, consistency_anchor. "
        f"Follow a hook→…→cta ad narrative; shot 1 must be a strong hook. "
        f"At least one shot's on_screen_text must state a concrete proof claim from the "
        f"product's proof_points (a number/specific), and the benefit beat should carry an "
        f"emotional payoff. Write all on-screen text and voiceover in {lang_name}."
    )
    concept_block = ""
    if concept:
        concept_block = (
            f"\n[Selected ad concept — make this hook/angle the center of the storyboard]\n"
            f"hook: {concept.hook}\nangle: {concept.angle}\nrationale: {concept.rationale}\n"
            f"Shot 1's on_screen_text.text_ko must reflect this hook."
        )
    user = (
        f"AdSpec:\n{spec.model_dump_json(indent=2)}{concept_block}\n"
        f"Exactly {spec.shot_count} shots, distributed across ~{spec.duration_sec}s total."
    )
    schema = {"shots": [{"id": "str", "description": "str", "camera": "str",
                         "duration_sec": "num", "transition": "str",
                         "on_screen_text": {"text_ko": "str", "position": "str", "style_ref": "str"},
                         "audio_cue": {"vo_ko": "str", "sfx": "str", "music_beat": "str"},
                         "image_prompt": "str", "consistency_anchor": "str"}]}

    data = get_llm().complete_json(
        system, user, mock_fn=lambda: _mock_storyboard(spec, concept), schema_hint=schema
    )

    shots = []
    for s in data.get("shots", []):
        # A real LLM can deviate from the schema (nested field as a string, duration as
        # "3s", a shot as a bare string). Coerce defensively so one odd shot can't 500.
        if not isinstance(s, dict):
            continue
        ost = s.get("on_screen_text")
        ac = s.get("audio_cue")
        shots.append(
            Shot(
                # force deterministic, safe ids — never trust model/user-provided id
                id=f"shot_{len(shots)+1}",
                description=str(s.get("description", "")),
                camera=s.get("camera", "static") or "static",
                duration_sec=_num(s.get("duration_sec", 3), 3.0),
                transition=s.get("transition", "cut") or "cut",
                on_screen_text=OnScreenText(**(ost if isinstance(ost, dict) else {})),
                audio_cue=AudioCue(**(ac if isinstance(ac, dict) else {})),
                image_prompt=str(s.get("image_prompt", "")),
                consistency_anchor=s.get("consistency_anchor", "hero_image"),
            )
        )
    st.storyboard = Storyboard(shots=shots)
    st.selected_concept_id = concept.id if concept else None
    st.set_step_status(PipelineStep.storyboard, StepStatus.generated)
    st.current_step = PipelineStep.storyboard
    state_store.save(st)
    return st.storyboard


def update(project_id: str, storyboard: Storyboard) -> Storyboard:
    st = state_store.load(project_id)
    if st is None:
        raise ValueError("project not found")
    # reject unsafe shot ids (stored XSS / filename traversal vector)
    seen = set()
    for s in storyboard.shots:
        if not _SHOT_ID_RE.match(s.id):
            raise ValueError(f"invalid shot id: {s.id!r} (allowed: letters, digits, _-)")
        if s.id in seen:
            raise ValueError(f"duplicate shot id: {s.id!r}")
        seen.add(s.id)
    st.storyboard = storyboard
    st.set_step_status(PipelineStep.storyboard, StepStatus.generated)
    state_store.save(st)
    return storyboard


def beat_sync(project_id: str) -> Storyboard:
    """Snap shot durations to the music mood's beat grid (CLAUDE.md §4.8). Opt-in."""
    from app.core import beatsync

    st = state_store.load(project_id)
    if st is None or st.storyboard is None:
        raise ValueError("storyboard not found")
    mood = st.adspec.audio.music_mood if st.adspec else ""
    bpm = beatsync.bpm_for(mood)
    durs = beatsync.snap_durations([s.duration_sec for s in st.storyboard.shots], bpm)
    for shot, d in zip(st.storyboard.shots, durs):
        shot.duration_sec = d
    state_store.save(st)
    return st.storyboard


def approve(project_id: str) -> Storyboard:
    st = state_store.load(project_id)
    if st is None or st.storyboard is None:
        raise ValueError("storyboard not found")
    st.set_step_status(PipelineStep.storyboard, StepStatus.approved)
    state_store.snapshot(st, PipelineStep.storyboard, label="storyboard approved")
    return st.storyboard
