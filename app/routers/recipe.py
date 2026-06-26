"""Generation-recipe routes — reproduce / remix / restore (ComfyUI-inspired, tag: pipeline)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.core.schemas import RemixRequest
from app.deps import get_state_or_404

router = APIRouter(tags=["pipeline"])


@router.get("/api/projects/{project_id}/assets/{shot_id}/recipe")
def get_recipe(project_id: str, shot_id: str):
    """The full generation recipe for an image (how it was made) — seed, prompt, model,
    background, provider. Powers the 'ⓘ recipe' panel and the Reproduce/Remix actions."""
    st = get_state_or_404(project_id)
    asset = st.image_asset(shot_id)
    if asset is None:
        raise HTTPException(404, "image not found")
    if asset.recipe is None:
        raise HTTPException(404, "no recipe recorded for this image (regenerate it to capture one)")
    return asset.recipe.model_dump()


@router.post("/api/projects/{project_id}/assets/{shot_id}/reproduce")
def reproduce_asset(project_id: str, shot_id: str):
    """Regenerate an image from its exact recipe (same seed → same image)."""
    from app.pipeline import reproduce

    get_state_or_404(project_id)
    try:
        return reproduce.reproduce(project_id, shot_id).model_dump()
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/projects/{project_id}/assets/{shot_id}/remix")
def remix_asset(project_id: str, shot_id: str, req: RemixRequest):
    """Regenerate with selected recipe fields changed and the rest locked — the
    'keep everything, change one thing' convergence move."""
    from app.pipeline import reproduce

    get_state_or_404(project_id)
    overrides = {k: v for k, v in req.model_dump().items() if v is not None}
    try:
        return reproduce.remix(project_id, shot_id, overrides).model_dump()
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/projects/{project_id}/recipe/extract")
def extract_recipe(project_id: str, req: dict):
    """Read a recipe embedded in an uploaded PNG (drag a generated ad back in to recover how
    it was made). Body: {data: <data-URI or bare base64 PNG>}. 404 if no recipe is embedded."""
    from app.core import recipe as recipe_mod
    from app.pipeline import decode_image_data_uri

    get_state_or_404(project_id)
    blob = decode_image_data_uri((req or {}).get("data") or "")
    rec = recipe_mod.extract(blob)
    if rec is None:
        raise HTTPException(404, "no recipe embedded in this image")
    return rec.model_dump()
