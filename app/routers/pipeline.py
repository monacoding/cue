"""Generation pipeline steps 1–6 (brief → spec → storyboard → hero → shots → edit) (tag: pipeline)."""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException

from app.core import consistency
from app.core import state as state_store
from app.core.jobs import queue
from app.core.schemas import (
    CreateProjectRequest,
    HeroRequest,
    ProductUpdate,
    ShotEditRequest,
    SpecRequest,
    Storyboard,
)
from app.deps import get_state_or_404
from app.eval import concept_eval
from app.pipeline import (
    step1_brief,
    step2_spec,
    step3_storyboard,
    step4_hero_image,
    step5_shot_images,
    step6_image_edit,
)

router = APIRouter(tags=["pipeline"])


# ---------------------------------------------------------------------------
# 1단계 — 브리프 재실행
# ---------------------------------------------------------------------------
@router.post("/api/projects/{project_id}/brief")
def rerun_brief(project_id: str, req: CreateProjectRequest):
    """Run step-1 brief extraction asynchronously — the LLM (Claude CLI/API) can take
    many seconds, so return a job id immediately and let the UI poll instead of holding
    the request open (which felt frozen). Falls back to mock inside the job on failure."""
    get_state_or_404(project_id)
    job = queue.submit(
        "brief",
        project_id,
        lambda j: step1_brief.run(
            project_id,
            product_url=req.product_url,
            product_text=req.product_text,
            image_urls=req.image_urls,
        ).model_dump(),
        dedupe=True,
    )
    return {"job_id": job.id, "status": job.status}


@router.post("/api/projects/{project_id}/upload-image")
def upload_image(project_id: str, req: dict):
    """Accept a pasted/dropped image (data-URI or bare base64), validate + normalize to
    PNG, save it as a project asset, and return its local URL — used by the unified brief
    input so the user can attach product images without hosting them somewhere."""
    import io
    import uuid

    from PIL import Image

    from app.pipeline import decode_image_data_uri, save_asset

    get_state_or_404(project_id)
    blob = decode_image_data_uri((req or {}).get("data") or "")
    try:
        img = Image.open(io.BytesIO(blob)).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
    except Exception:
        raise HTTPException(400, "not a decodable image")
    url = save_asset(project_id, f"upload_{uuid.uuid4().hex[:8]}.png", buf.getvalue())
    return {"url": url, "width": img.width, "height": img.height}


@router.put("/api/projects/{project_id}/product")
def edit_product(project_id: str, req: ProductUpdate):
    """사람이 수정한 제품 필드를 그대로 저장 (재추출 없음)."""
    get_state_or_404(project_id)
    spec = step1_brief.update_product(
        project_id,
        name=req.name,
        description=req.description,
        key_message=req.key_message,
        image_urls=req.image_urls,
    )
    return spec.model_dump()


# ---------------------------------------------------------------------------
# 2단계 — 스펙
# ---------------------------------------------------------------------------
@router.post("/api/projects/{project_id}/spec")
def build_spec(project_id: str, req: SpecRequest):
    get_state_or_404(project_id)
    return step2_spec.run(project_id, req).model_dump()


@router.post("/api/projects/{project_id}/spec/approve")
def approve_spec(project_id: str):
    get_state_or_404(project_id)
    return step2_spec.approve(project_id).model_dump()


# ---------------------------------------------------------------------------
# 3단계 — 스토리보드 (+ 컨셉 평가)
# ---------------------------------------------------------------------------
@router.post("/api/projects/{project_id}/storyboard")
def gen_storyboard(project_id: str, concept_id: str = ""):
    """Async (LLM-backed) — return a job id so the UI doesn't freeze during generation."""
    get_state_or_404(project_id)
    cid = concept_id or None
    job = queue.submit(
        "storyboard", project_id,
        lambda j: step3_storyboard.run(project_id, concept_id=cid).model_dump(),
        dedupe=True,
    )
    return {"job_id": job.id, "status": job.status}


@router.put("/api/projects/{project_id}/storyboard")
def update_storyboard(project_id: str, sb: Storyboard):
    get_state_or_404(project_id)
    return step3_storyboard.update(project_id, sb).model_dump()


@router.post("/api/projects/{project_id}/storyboard/approve")
def approve_storyboard(project_id: str):
    get_state_or_404(project_id)
    return step3_storyboard.approve(project_id).model_dump()


@router.post("/api/projects/{project_id}/storyboard/beatsync")
def beatsync_storyboard(project_id: str):
    """Snap shot durations to the music-mood beat grid (opt-in, §4.8)."""
    get_state_or_404(project_id)
    return step3_storyboard.beat_sync(project_id).model_dump()


@router.post("/api/projects/{project_id}/concepts")
def eval_concepts(project_id: str, n: int = 5):
    """Async (LLM-backed) — return a job id; the UI polls for the ranked concepts."""
    get_state_or_404(project_id)
    n = max(1, min(10, n))  # clamp — unbounded n would be a memory/CPU DoS
    job = queue.submit(
        "concepts", project_id,
        lambda j: [c.model_dump() for c in concept_eval.evaluate(project_id, n)],
        dedupe=True,
    )
    return {"job_id": job.id, "status": job.status}


# ---------------------------------------------------------------------------
# 4단계 — Hero (+ audio track settings)
# ---------------------------------------------------------------------------
@router.post("/api/projects/{project_id}/audio")
def update_audio(project_id: str, req: dict):
    """Update the music track settings (volume / mute / mood) — Studio audio-track controls."""
    st = get_state_or_404(project_id)
    if st.adspec is None:
        raise HTTPException(400, "no spec yet")
    a = st.adspec.audio
    if "music_volume" in req:
        a.music_volume = max(0.0, min(2.0, float(req["music_volume"])))
    if "music_muted" in req:
        a.music_muted = bool(req["music_muted"])
    if "music_mood" in req:
        a.music_mood = str(req["music_mood"])[:60]
    state_store.save(st)
    return st.adspec.model_dump()


@router.post("/api/projects/{project_id}/hero")
def gen_hero(project_id: str, req: HeroRequest):
    get_state_or_404(project_id)
    return step4_hero_image.run(project_id, prompt_override=req.prompt,
                                model=req.model, background=req.background,
                                seed=req.seed).model_dump()


@router.post("/api/projects/{project_id}/hero/approve")
def approve_hero(project_id: str):
    get_state_or_404(project_id)
    return step4_hero_image.approve(project_id).model_dump()


# ---------------------------------------------------------------------------
# 5단계 — 컷 이미지
# ---------------------------------------------------------------------------
@router.post("/api/projects/{project_id}/shots")
def gen_shots(project_id: str, shot_id: str = "", strategy: str = "reference", model: str = "", background: str = ""):
    """Submit async shot-image generation (N sequential model calls → job + polling).

    State preconditions (storyboard + hero) are checked synchronously for an immediate 400;
    progress is polled via GET /api/jobs/{id}. `strategy` selects the hero→shot consistency
    method (reference | seed | edit); unknown values fall back to reference.
    """
    st = get_state_or_404(project_id)
    if st.storyboard is None:
        raise HTTPException(400, "storyboard required")
    if st.hero_image is None:
        raise HTTPException(400, "hero image required (run step 4 first)")
    sid = shot_id or None
    strat = consistency.coerce(strategy).value
    job = queue.submit(
        "shots",
        project_id,
        lambda j: [
            a.model_dump()
            for a in step5_shot_images.run(
                project_id, only_shot_id=sid, strategy=strat, model=(model or None),
                background=(background or None),
                progress_cb=lambda p: queue.update_progress(j, p),
                should_cancel=lambda: queue.is_cancelled(j),
            )
        ],
        dedupe=True,
    )
    return {"job_id": job.id, "status": job.status, "strategy": strat}


# ---------------------------------------------------------------------------
# 6단계 — 이미지 편집 + 승인 게이트
# ---------------------------------------------------------------------------
@router.post("/api/projects/{project_id}/shots/{shot_id}/edit")
def edit_shot(project_id: str, shot_id: str, req: ShotEditRequest):
    get_state_or_404(project_id)
    return step6_image_edit.edit_shot(project_id, shot_id, req.instruction, model=req.model).model_dump()


@router.post("/api/projects/{project_id}/shots/{shot_id}/approve")
def approve_shot(project_id: str, shot_id: str):
    get_state_or_404(project_id)
    return step6_image_edit.approve_shot(project_id, shot_id).model_dump()


@router.post("/api/projects/{project_id}/shots/{shot_id}/duplicate")
def duplicate_shot(project_id: str, shot_id: str):
    """Duplicate a storyboard shot (inserted right after it) and copy its image so the clone
    shows the same frame immediately — a standard NLE clip action."""
    import copy

    from app.core.schemas import ImageAsset
    from app.pipeline import read_asset_bytes, save_asset

    st = get_state_or_404(project_id)
    if st.storyboard is None:
        raise HTTPException(400, "no storyboard")
    shots = st.storyboard.shots
    idx = next((k for k, s in enumerate(shots) if s.id == shot_id), None)
    if idx is None:
        raise HTTPException(404, "shot not found")
    maxn = max([int(re.search(r"(\d+)", s.id).group(1)) for s in shots if re.search(r"\d+", s.id)] + [0])
    clone = copy.deepcopy(shots[idx])
    clone.id = f"shot_{maxn + 1}"
    shots.insert(idx + 1, clone)
    src = st.image_asset(shot_id)
    if src is not None:
        blob = read_asset_bytes(project_id, src.url)
        if blob:
            url = save_asset(project_id, f"{clone.id}_v0.png", blob)
            st.shot_images.append(ImageAsset(shot_id=clone.id, url=url, prompt=src.prompt,
                                             provider=src.provider, status=src.status, revision=0))
    state_store.save(st)
    return st.model_dump()


@router.post("/api/projects/{project_id}/shots/approve")
def approve_all_shots(project_id: str):
    get_state_or_404(project_id)
    return [a.model_dump() for a in step6_image_edit.approve_all(project_id)]
