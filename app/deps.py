"""Shared FastAPI dependencies/helpers used across routers (app/routers/*)."""
from __future__ import annotations

from fastapi import HTTPException

from app.core import state as state_store


def get_state_or_404(project_id: str):
    """Load a project's state or raise 404 — the precondition for every per-project route."""
    st = state_store.load(project_id)
    if st is None:
        raise HTTPException(404, "project not found")
    return st
