"""Async job polling + reconnect (tag: jobs)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.core.jobs import queue
from app.deps import get_state_or_404

router = APIRouter(tags=["jobs"])


@router.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = queue.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job.to_dict()


@router.get("/api/projects/{project_id}/active-job")
def active_job(project_id: str, kind: str):
    """Reconnect endpoint: returns the in-flight job for (project, kind) so a
    reloaded client can resume showing progress instead of losing it."""
    get_state_or_404(project_id)
    j = queue.active_job(project_id, kind)
    return {"job_id": j.id if j else None, "progress": j.progress if j else 0.0,
            "status": j.status if j else None}
