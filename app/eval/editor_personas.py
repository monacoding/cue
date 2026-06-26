"""가상 편집자 페르소나 패널 — Studio NLE(편집기) UX 평가 (CLAUDE.md §1 광고지능 확장).

100명의 다양한 영상 편집자 페르소나(숙련도·배경 소프트웨어·작업 초점·성향)를 구성하고, 각자가
'최고급 편집 프로그램'에게 기대하는 기능 집합과 현재 Studio의 기능 인벤토리를 대조해 만족도·결핍
기능(피드백)을 산출한다. 결정론적 휴리스틱이라 키 없이 재현 가능하며, 집계로 '가장 많이 요청된
개선'을 순위화해 실제 구현 우선순위를 정한다.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List


# ---- 현재 Studio 기능 인벤토리 (present=구현됨) ----------------------------
@dataclass
class Feature:
    cat: str
    label: str
    present: bool


FEATURES: Dict[str, Feature] = {
    # timeline
    "ruler_playhead": Feature("timeline", "Ruler + scrubbable playhead", True),
    "trim_drag": Feature("timeline", "Drag clip edges to trim", True),
    "reorder_drag": Feature("timeline", "Drag clips to reorder", True),
    "snapping": Feature("timeline", "Snapping to cuts", True),
    "zoom": Feature("timeline", "Zoom in/out", True),
    "zoom_fit": Feature("timeline", "Zoom to fit", True),
    "markers": Feature("timeline", "Markers", True),
    "ripple_delete": Feature("timeline", "Ripple delete (close the gap)", True),
    "ripple_trim": Feature("timeline", "Ripple trim", True),
    "multi_select": Feature("timeline", "Multi-select clips", True),
    "copy_paste": Feature("timeline", "Copy / paste clips", True),
    "nudge_clip": Feature("timeline", "Nudge selected clip by frame", False),
    "in_out_points": Feature("timeline", "In/out points + range", False),
    "minimap": Feature("timeline", "Timeline minimap / overview", False),
    # clips
    "split": Feature("clip", "Split at playhead (razor)", True),
    "duplicate": Feature("clip", "Duplicate clip", True),
    "delete": Feature("clip", "Delete clip", True),
    "color_labels": Feature("clip", "Color labels", True),
    "clip_speed": Feature("clip", "Clip speed / time-remap", False),
    "filmstrip": Feature("clip", "Filmstrip thumbnails on clips", False),
    # transitions
    "transition_types": Feature("transition", "Transition types (4)", True),
    "change_transition": Feature("transition", "Change a transition", True),
    "transition_duration": Feature("transition", "Transition duration control", False),
    # preview / monitor
    "program_monitor": Feature("preview", "Program monitor", True),
    "live_preview": Feature("preview", "Live playhead preview", True),
    "crossfade_preview": Feature("preview", "Crossfade preview", True),
    "fullscreen": Feature("preview", "Fullscreen / theater", True),
    "playback_speed": Feature("preview", "Playback speed (0.5x/2x)", True),
    "loop_play": Feature("preview", "Loop playback", True),
    "safe_guides": Feature("preview", "Safe-area / title guides", False),
    # audio
    "audio_track": Feature("audio", "Audio track", True),
    "volume": Feature("audio", "Music volume", True),
    "mute": Feature("audio", "Mute", True),
    "waveform": Feature("audio", "Waveform display", True),
    "real_waveform": Feature("audio", "Real waveform from the audio", False),
    "audio_fades": Feature("audio", "Audio fade in/out handles", False),
    "audio_keyframes": Feature("audio", "Volume keyframes (ducking)", False),
    "audio_scrub": Feature("audio", "Audio scrubbing", False),
    "multi_audio": Feature("audio", "Multiple audio tracks (VO + music)", False),
    # text / graphics
    "onscreen_text": Feature("graphics", "Per-shot on-screen text", True),
    "text_styling": Feature("graphics", "Text styling (font/size/color/pos)", False),
    "title_templates": Feature("graphics", "Title / lower-third templates", False),
    "auto_captions": Feature("graphics", "Auto captions / subtitles", False),
    "overlay_track": Feature("graphics", "Overlay track (PiP / text layer)", False),
    # color
    "color_correct": Feature("color", "Color correction (lift/gamma/gain)", False),
    "filters_looks": Feature("color", "Filters / LUT looks", False),
    "adjust_per_clip": Feature("color", "Per-clip brightness/contrast", False),
    # workflow
    "undo_redo": Feature("workflow", "Undo / redo", True),
    "shortcuts": Feature("workflow", "Keyboard shortcuts", True),
    "help": Feature("workflow", "Shortcut help / cheatsheet", True),
    "context_menu": Feature("workflow", "Right-click clip menu", True),
    "per_image_model": Feature("workflow", "Per-image model choice", True),
    "frame_step": Feature("workflow", "Frame-step (←/→)", True),
    "jkl_shuttle": Feature("workflow", "J-K-L shuttle", True),
    "export_dialog": Feature("workflow", "Export settings (res/format/ratio)", True),
    "autosave": Feature("workflow", "Autosave indicator", False),
    "onboarding": Feature("workflow", "Onboarding tips / coachmarks", True),
    # accessibility
    "kbd_nav": Feature("a11y", "Keyboard navigation", True),
    "aria_focus": Feature("a11y", "ARIA roles + focus rings (timeline)", False),
    "reduced_motion": Feature("a11y", "Reduced-motion option", False),
}


# ---- 페르소나 ---------------------------------------------------------------
_SKILLS = ["novice", "hobbyist", "prosumer", "professional", "finisher"]
_BG = ["CapCut", "Premiere Pro", "DaVinci Resolve", "Final Cut", "mobile/Reels", "first-timer"]
_FOCUS = ["short-form social", "audio post", "color grading", "motion graphics",
          "precision editing", "accessibility", "storytelling", "high-volume speed"]
_PATIENCE = ["impatient", "balanced", "meticulous"]
_NAMES = ["Mina", "Theo", "Yuna", "Marco", "Aria", "Devin", "Sora", "Liam", "Nadia", "Owen"]

# skill → baseline expected features
_SKILL_NEEDS = {
    "novice": ["program_monitor", "undo_redo", "onboarding", "fullscreen", "help"],
    "hobbyist": ["program_monitor", "undo_redo", "text_styling", "transition_duration",
                 "filters_looks", "volume", "markers"],
    "prosumer": ["ripple_delete", "clip_speed", "audio_fades", "in_out_points", "copy_paste",
                 "export_dialog", "multi_select"],
    "professional": ["ripple_delete", "ripple_trim", "in_out_points", "jkl_shuttle", "multi_select",
                     "copy_paste", "nudge_clip", "export_dialog", "real_waveform", "overlay_track"],
    "finisher": ["color_correct", "filters_looks", "adjust_per_clip", "real_waveform",
                 "audio_keyframes", "safe_guides", "export_dialog"],
}
_FOCUS_NEEDS = {
    "short-form social": ["auto_captions", "text_styling", "clip_speed", "title_templates", "playback_speed"],
    "audio post": ["real_waveform", "audio_fades", "audio_keyframes", "audio_scrub", "multi_audio"],
    "color grading": ["color_correct", "filters_looks", "adjust_per_clip", "safe_guides"],
    "motion graphics": ["overlay_track", "text_styling", "title_templates", "transition_duration"],
    "precision editing": ["ripple_delete", "in_out_points", "nudge_clip", "frame_step", "jkl_shuttle", "minimap"],
    "accessibility": ["aria_focus", "kbd_nav", "reduced_motion", "shortcuts"],
    "storytelling": ["markers", "program_monitor", "reorder_drag", "transition_duration", "overlay_track"],
    "high-volume speed": ["copy_paste", "multi_select", "ripple_delete", "autosave", "export_dialog"],
}
_BG_NEEDS = {
    "CapCut": ["auto_captions", "clip_speed", "title_templates", "filters_looks", "playback_speed"],
    "Premiere Pro": ["ripple_delete", "in_out_points", "jkl_shuttle", "export_dialog", "multi_select"],
    "DaVinci Resolve": ["color_correct", "real_waveform", "audio_keyframes", "in_out_points"],
    "Final Cut": ["ripple_trim", "ripple_delete", "clip_speed", "overlay_track"],
    "mobile/Reels": ["auto_captions", "text_styling", "playback_speed", "onboarding"],
    "first-timer": ["onboarding", "help", "undo_redo", "fullscreen"],
}


@dataclass
class Persona:
    name: str
    skill: str
    background: str
    focus: str
    patience: str
    needs: List[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.name} · {self.skill} · ex-{self.background} · {self.focus}"


def build_personas(n: int = 100) -> List[Persona]:
    out = []
    for i in range(n):
        skill = _SKILLS[i % len(_SKILLS)]
        bg = _BG[(i // 5) % len(_BG)]
        focus = _FOCUS[(i // 3) % len(_FOCUS)]
        pat = _PATIENCE[i % len(_PATIENCE)]
        name = f"{_NAMES[i % len(_NAMES)]}{i // len(_NAMES) + 1}"
        needs = set(_SKILL_NEEDS[skill]) | set(_FOCUS_NEEDS[focus]) | set(_BG_NEEDS[bg])
        needs &= set(FEATURES)               # keep only known features
        out.append(Persona(name, skill, bg, focus, pat, sorted(needs)))
    return out


def evaluate_persona(p: Persona) -> Dict:
    missing = [f for f in p.needs if not FEATURES[f].present]
    have = [f for f in p.needs if FEATURES[f].present]
    sat = round(100 * len(have) / max(1, len(p.needs)), 1)
    # the impatient weight their single biggest gap harder
    top = missing[0] if missing else None
    return {"persona": p.label, "satisfaction": sat, "missing": missing,
            "top_request": top, "comment": _comment(p, missing)}


def _comment(p: Persona, missing: List[str]) -> str:
    if not missing:
        return "This already covers my whole workflow — ship it."
    f = FEATURES[missing[0]].label
    by = {"impatient": "I'd bounce without", "balanced": "I really want",
          "meticulous": "I can't finish a cut without"}[p.patience]
    return f"As an ex-{p.background} {p.focus} editor, {by} {f.lower()}."


def run_panel(n: int = 100) -> Dict:
    """100 페르소나 평가 → 만족도 + 기능별 요청 순위 + 카테고리 집계."""
    personas = build_personas(n)
    evals = [evaluate_persona(p) for p in personas]
    sat = [e["satisfaction"] for e in evals]
    # weighted request tally (impatient personas count 1.4, meticulous 1.2)
    w = {"impatient": 1.4, "balanced": 1.0, "meticulous": 1.2}
    req: Counter = Counter()
    cat_gap: Dict[str, int] = defaultdict(int)
    for p, e in zip(personas, evals):
        for f in e["missing"]:
            req[f] += w[p.patience]
            cat_gap[FEATURES[f].cat] += 1
    ranked = [{"feature": f, "label": FEATURES[f].label, "cat": FEATURES[f].cat,
               "requested_by": round(c, 1)} for f, c in req.most_common()]
    return {
        "n": n,
        "mean_satisfaction": round(sum(sat) / len(sat), 1),
        "min_satisfaction": min(sat),
        "fully_satisfied": sum(1 for s in sat if s >= 100),
        "top_requests": ranked[:15],
        "gaps_by_category": dict(sorted(cat_gap.items(), key=lambda kv: -kv[1])),
        "sample_comments": [e["comment"] for e in evals[:8]],
    }
