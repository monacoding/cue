"""7단계 — 콘티별 영상 제작 (비용 절벽 ⚠️, CLAUDE.md §4.7).

전제: 6단계 전체 이미지 승인 후에만 실행 (게이팅 강제, CLAUDE.md §2.5/§10).
처리: 각 still 을 image-to-video 로 애니메이션 → 사람이 컷별 재생성/수정.
모델: Seedance 2.0 (fal). 키 없으면 ffmpeg Ken Burns 베이스라인.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Callable, List, Optional

from app.core import assembly
from app.core import state as state_store
from app.core.schemas import PipelineStep, StepStatus, VideoAsset
from app.pipeline import read_asset_bytes, save_asset
from app.pipeline.step6_image_edit import all_approved
from app.providers.registry import get_video_provider

# 콘티 camera → Ken Burns 모션
_MOTION = {"dolly_in": "in", "tracking": "in", "pan_left": "in",
           "static": "out", "dolly_out": "out"}


class CostGateError(RuntimeError):
    """6단계 전체 승인 전에는 7단계 차단."""


def assert_gate(project_id: str) -> None:
    st = state_store.load(project_id)
    if st is None:
        raise ValueError("project not found")
    if not all_approved(st):
        raise CostGateError(
            "Video stage (7) blocked — all shots must be approved in step 6 first (cost gate)."
        )


def run(
    project_id: str,
    only_shot_id: Optional[str] = None,
    progress_cb: Optional[Callable[[float], None]] = None,
    model: str = "opensource",
    should_cancel: Optional[Callable[[], bool]] = None,
) -> List[VideoAsset]:
    assert_gate(project_id)
    if not assembly.ffmpeg_available():
        raise RuntimeError("ffmpeg/ffprobe not installed — cannot generate video (brew install ffmpeg)")

    st = state_store.load(project_id)
    spec = st.adspec
    shots_by_id = {s.id: s for s in (st.storyboard.shots if st.storyboard else [])}
    provider = get_video_provider(model)  # seedance | kling | veo

    targets = [img for img in st.shot_images if not only_shot_id or img.shot_id == only_shot_id]
    total = max(1, len(targets))
    existing = {v.shot_id: v for v in st.shot_videos}
    for idx, img in enumerate(targets):
        if should_cancel and should_cancel():     # cooperative interrupt — keep clips done so far
            break
        shot = shots_by_id.get(img.shot_id)
        dur = float(shot.duration_sec) if shot else 3.0
        camera = shot.camera if shot else "static"
        prompt = (shot.image_prompt if shot else "") or img.prompt
        still = read_asset_bytes(project_id, img.url)
        result = provider.image_to_video(
            still, prompt, duration_sec=dur,
            with_audio=spec.audio.voiceover if spec else True,
            aspect_ratio=spec.aspect_ratio if spec else "9:16",
            motion=_MOTION.get(camera, "in"),
        ) if still is not None else None

        if result and result.video_bytes:
            # Burn on-screen text into the clip (CTA reinforced on the last shot)
            ost = shot.on_screen_text.text_ko if shot else ""
            position = (shot.on_screen_text.position if shot else "") or "bottom"
            ar = spec.aspect_ratio if spec else "9:16"
            clip_bytes = result.video_bytes
            if ost.strip():
                with tempfile.TemporaryDirectory() as td:
                    raw = Path(td) / "raw.mp4"
                    burned = Path(td) / "burned.mp4"
                    raw.write_bytes(result.video_bytes)
                    assembly.burn_text(raw, burned, ost, position=position, aspect_ratio=ar)
                    clip_bytes = burned.read_bytes()
            url = save_asset(project_id, f"{img.shot_id}_clip.mp4", clip_bytes)
            existing[img.shot_id] = VideoAsset(
                shot_id=img.shot_id, url=url, provider=result.provider,
                status=StepStatus.generated,
            )
        if progress_cb:
            progress_cb((idx + 1) / total)

    order = [s.shot_id for s in st.shot_images]
    st.shot_videos = [existing[i] for i in order if i in existing]
    st.set_step_status(PipelineStep.shot_video, StepStatus.generated)
    st.current_step = PipelineStep.shot_video
    state_store.save(st)
    return st.shot_videos


def approve_all(project_id: str) -> List[VideoAsset]:
    st = state_store.load(project_id)
    if st is None or not st.shot_videos:
        raise ValueError("no shot videos")
    for v in st.shot_videos:
        v.status = StepStatus.approved
    st.set_step_status(PipelineStep.shot_video, StepStatus.approved)
    state_store.snapshot(st, PipelineStep.shot_video, label="all clips approved")
    return st.shot_videos
