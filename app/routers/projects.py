"""Project CRUD + bundle download (tag: projects)."""
from __future__ import annotations

import io
import re
import zipfile

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.core import state as state_store
from app.core.schemas import CreateProjectRequest
from app.deps import get_state_or_404
from app.pipeline import step1_brief

router = APIRouter(tags=["projects"])


@router.get("/api/projects")
def list_projects():
    return state_store.list_projects()


@router.post("/api/projects")
def create_project(req: CreateProjectRequest):
    st = state_store.create_project(req.title, req.format)
    # 1단계 즉시 실행 (브리프)
    spec = step1_brief.run(
        st.project_id,
        product_url=req.product_url,
        product_text=req.product_text,
        image_urls=req.image_urls,
    )
    return {"project_id": st.project_id, "adspec": spec.model_dump()}


@router.get("/api/projects/{project_id}")
def get_project(project_id: str):
    return get_state_or_404(project_id).model_dump()


@router.get("/api/projects/{project_id}/download-all")
def download_all(project_id: str):
    """Bundle every deliverable (final outputs + platform variants + A/B variants) into one zip."""
    st = get_state_or_404(project_id)
    urls = list(st.final_outputs) + [e.url for e in st.exports] + [v.url for v in st.ab_variants]
    adir = state_store.assets_dir(project_id)
    buf, seen = io.BytesIO(), set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for u in urls:
            fname = u.rsplit("/", 1)[-1]
            if fname in seen:
                continue
            p = adir / fname
            if p.exists():
                z.write(p, arcname=fname)
                seen.add(fname)
    if not seen:
        raise HTTPException(404, "no outputs to download")
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", st.title)[:40] or "ad"
    return Response(
        buf.getvalue(), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe}_assets.zip"'},
    )


@router.delete("/api/projects/{project_id}")
def delete_project(project_id: str):
    if not state_store.delete_project(project_id):
        raise HTTPException(404, "project not found")
    return {"deleted": True}


@router.patch("/api/projects/{project_id}")
def rename_project(project_id: str, req: dict):
    """Rename a project (Projects manager)."""
    st = state_store.rename_project(project_id, (req or {}).get("title", ""))
    if st is None:
        raise HTTPException(404, "project not found")
    return {"project_id": st.project_id, "title": st.title}


@router.post("/api/projects/{project_id}/duplicate")
def duplicate_project(project_id: str):
    """Clone a project (state + assets) — iterate without re-entering the brief/spec."""
    st = state_store.duplicate_project(project_id)
    if st is None:
        raise HTTPException(404, "project not found")
    return {"project_id": st.project_id, "title": st.title}


@router.post("/api/projects/import")
def import_project(data: dict):
    """Import a project from an exported '.cue.json' (portable campaign plan). Restores
    brief/spec/storyboard/concepts into a new project (assets are regenerable, not bundled)."""
    st = state_store.import_project(data or {})
    if st is None:
        raise HTTPException(400, "invalid project export JSON")
    return {"project_id": st.project_id, "title": st.title}


@router.get("/api/projects/{project_id}/export")
def export_project(project_id: str):
    """Download the project as a portable JSON 'workflow' (spec/storyboard/concepts) —
    share or move a campaign across instances; re-import regenerates the assets."""
    st = get_state_or_404(project_id)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", st.title)[:40] or "cue_project"
    return Response(
        st.model_dump_json(indent=2), media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{safe}.cue.json"'},
    )
