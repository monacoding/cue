"""2단계 — 광고 스펙(레이어) 구축 (CLAUDE.md §4.2).

입력: 스타일·길이·플랫폼·비율 등 사람 기입.
처리: duration → shot_count 파생 포함 AdSpec 생성/갱신. 사람 수정.
모델: Claude (tone 제안 등). 핵심 필드는 결정론적.
"""
from __future__ import annotations


from app.core import state as state_store
from app.core.schemas import (
    AdSpec,
    PipelineStep,
    SpecRequest,
    StepStatus,
)

# 비용/리소스 안전 상한 (CLAUDE.md §2.5 비용 의식) — 런어웨이 생성·비용 폭주 방지
MAX_SHOT_COUNT = 20          # 컷당 이미지/영상 생성 비용 → 상한
MIN_SHOT_COUNT = 1
MAX_DURATION_SEC = 120
MIN_DURATION_SEC = 1
VALID_ASPECT_RATIOS = {"9:16", "1:1", "16:9", "4:5"}
VALID_PLATFORMS = {"instagram_reels", "tiktok", "youtube_shorts", "feed"}


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def run(project_id: str, req: SpecRequest) -> AdSpec:
    st = state_store.load(project_id)
    if st is None:
        raise ValueError("project not found")
    if st.adspec is None:
        st.adspec = AdSpec(project_id=project_id, format=st.format)

    spec = st.adspec
    if req.aspect_ratio is not None:
        if req.aspect_ratio not in VALID_ASPECT_RATIOS:
            raise ValueError(f"Unsupported aspect ratio: {req.aspect_ratio} (allowed: {sorted(VALID_ASPECT_RATIOS)})")
        spec.aspect_ratio = req.aspect_ratio
    if req.platform is not None:
        if req.platform not in VALID_PLATFORMS:
            raise ValueError(f"Unsupported platform: {req.platform} (allowed: {sorted(VALID_PLATFORMS)})")
        spec.platform = req.platform
    if req.tone is not None:
        spec.tone = req.tone
    if req.duration_sec is not None:
        spec.duration_sec = _clamp(req.duration_sec, MIN_DURATION_SEC, MAX_DURATION_SEC)
        # shot_count 미지정 시 길이에서 파생
        if req.shot_count is None:
            spec.shot_count = AdSpec.derive_shot_count(spec.duration_sec)
    if req.shot_count is not None:
        # 비용 폭주 방지 상한
        spec.shot_count = _clamp(req.shot_count, MIN_SHOT_COUNT, MAX_SHOT_COUNT)
    if req.palette is not None:
        spec.brand.palette = req.palette
    if req.logo_url is not None:
        spec.brand.logo_url = req.logo_url
    if req.music_mood is not None:
        spec.audio.music_mood = req.music_mood
    if req.music_model is not None:
        from app.providers.registry import MUSIC_MODELS

        if req.music_model not in MUSIC_MODELS:
            raise ValueError(f"Unknown music model: {req.music_model} (allowed: {list(MUSIC_MODELS)})")
        spec.audio.music_model = req.music_model
    if req.language is not None:
        spec.audio.language = req.language

    st.set_step_status(PipelineStep.spec, StepStatus.generated)
    st.current_step = PipelineStep.spec
    state_store.save(st)
    return spec


def approve(project_id: str) -> AdSpec:
    st = state_store.load(project_id)
    if st is None or st.adspec is None:
        raise ValueError("spec not found")
    st.set_step_status(PipelineStep.spec, StepStatus.approved)
    state_store.snapshot(st, PipelineStep.spec, label="spec approved")
    return st.adspec
