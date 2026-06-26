"""Output stage — video (7), assemble (8), export/upscale (9) + versioning (tag: output)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.core import assembly
from app.core import state as state_store
from app.core.jobs import queue
from app.core.schemas import RenderRequest
from app.deps import get_state_or_404
from app.pipeline import step7_shot_video, step8_assemble, step9_encode

router = APIRouter(tags=["output"])


# ---------------------------------------------------------------------------
# 7단계 — 영상 (게이팅 확인, Phase 2)
# ---------------------------------------------------------------------------
@router.get("/api/projects/{project_id}/cost-estimate")
def cost_estimate(project_id: str):
    """Approximate cost (real-model) for the project + the video step (cost cliff)."""
    from app.core import pricing

    st = get_state_or_404(project_id)
    return {"project": pricing.estimate(st), "video_step": pricing.video_step_estimate(st)}


@router.get("/api/projects/{project_id}/video/gate")
def video_gate(project_id: str):
    st = get_state_or_404(project_id)
    from app.pipeline.step6_image_edit import all_approved

    return {"unlocked": all_approved(st)}


@router.post("/api/projects/{project_id}/video")
def gen_video(project_id: str, shot_id: str = "", model: str = "opensource"):
    """Submit async clip generation (CLAUDE.md §6 — video is minutes-long → job queue).

    Gate + ffmpeg checks run synchronously (fast) so the caller gets 409/503 immediately;
    the actual generation runs in a worker and is polled via GET /api/jobs/{id}.
    model selects the video provider: seedance | kling | veo (§4.7/§9).
    """
    from app.providers.registry import VIDEO_MODELS

    get_state_or_404(project_id)
    if model not in VIDEO_MODELS:
        raise HTTPException(400, f"Unknown video model: {model} (allowed: {list(VIDEO_MODELS)})")
    try:
        step7_shot_video.assert_gate(project_id)
    except step7_shot_video.CostGateError as e:
        raise HTTPException(409, str(e))
    if not assembly.ffmpeg_available():
        raise HTTPException(503, "ffmpeg/ffprobe not installed — cannot generate video")

    sid = shot_id or None
    job = queue.submit(
        "video",
        project_id,
        lambda j: [
            v.model_dump()
            for v in step7_shot_video.run(
                project_id, only_shot_id=sid, model=model,
                progress_cb=lambda p: queue.update_progress(j, p),
            )
        ],
        dedupe=True,
    )
    return {"job_id": job.id, "status": job.status}


@router.post("/api/projects/{project_id}/video/approve")
def approve_video(project_id: str):
    get_state_or_404(project_id)
    return [v.model_dump() for v in step7_shot_video.approve_all(project_id)]


# ---------------------------------------------------------------------------
# 8단계 — 합성/조립 (static_image 최종 출력)
# ---------------------------------------------------------------------------
@router.post("/api/projects/{project_id}/render")
def render(project_id: str, req: RenderRequest):
    get_state_or_404(project_id)
    try:
        url = step8_assemble.run(project_id, req)
    except NotImplementedError as e:
        raise HTTPException(501, str(e))
    return {"output_url": url}


@router.post("/api/projects/{project_id}/render/approve")
def approve_render(project_id: str):
    get_state_or_404(project_id)
    return {"output_url": step8_assemble.approve(project_id)}


@router.post("/api/projects/{project_id}/render/variants")
def render_variants(project_id: str, n: int = 3):
    """A/B variants — render the ad with each top-N concept's hook (§1 ad intelligence)."""
    get_state_or_404(project_id)
    return [v.model_dump() for v in step8_assemble.render_variants(project_id, n)]


@router.post("/api/projects/{project_id}/render/variants/select")
def select_variant(project_id: str, concept_id: str):
    """Pick the winning A/B variant — it becomes a final output (versioned)."""
    get_state_or_404(project_id)
    return step8_assemble.select_variant(project_id, concept_id).model_dump()


# ---------------------------------------------------------------------------
# 9단계 — 멀티 플랫폼 export (one ad → all placements)
# ---------------------------------------------------------------------------
@router.post("/api/projects/{project_id}/upscale")
def upscale_output(project_id: str, factor: int = 2):
    """Upscale the latest final output (§9, opt-in). Lanczos offline, Topaz/SeedVR with a key."""
    get_state_or_404(project_id)
    try:
        return step9_encode.upscale_output(project_id, factor).model_dump()
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@router.post("/api/projects/{project_id}/export")
def export_variants(project_id: str, ratios: str = ""):
    get_state_or_404(project_id)
    rl = [r.strip() for r in ratios.split(",") if r.strip()] or None
    try:
        variants = step9_encode.export_variants(project_id, rl)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return [v.model_dump() for v in variants]


# ---------------------------------------------------------------------------
# 버전 / 되돌리기 (CLAUDE.md §4 가로지르는 관심사)
# ---------------------------------------------------------------------------
@router.get("/api/projects/{project_id}/versions")
def versions(project_id: str):
    st = get_state_or_404(project_id)
    return [v.model_dump() for v in st.versions]


@router.post("/api/projects/{project_id}/revert")
def revert(project_id: str, version: int):
    st = state_store.revert(project_id, version)
    if st is None:
        raise HTTPException(404, "version not found")
    return st.model_dump()
