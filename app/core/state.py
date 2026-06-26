"""프로젝트 상태 디스크 영속 + 버전 (CLAUDE.md §4 가로지르는 관심사).

각 프로젝트 = storage/projects/<id>/
  state.json              현재 상태
  versions/v<n>.json      사람 개입 스냅샷
  assets/...              생성된 에셋

사람 개입마다 새 버전을 만들고, 임의 단계로 되돌아가 재진입 가능.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from app.config import settings

# project_id is used to build filesystem paths (state/assets/versions, rmtree on
# delete). It must NOT contain path-traversal or separators — otherwise a crafted
# URL like DELETE /api/projects/..%2f..%2fsomedir could touch arbitrary directories.
_VALID_PROJECT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _safe_id(project_id: str) -> str:
    if not isinstance(project_id, str) or not _VALID_PROJECT_ID.match(project_id):
        raise ValueError("invalid project_id")
    return project_id
from app.core.schemas import (
    OutputFormat,
    PipelineStep,
    ProjectState,
    Version,
)

_LOCKS: dict = {}
_GLOBAL_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lock_for(project_id: str) -> threading.Lock:
    with _GLOBAL_LOCK:
        if project_id not in _LOCKS:
            _LOCKS[project_id] = threading.Lock()
        return _LOCKS[project_id]


def project_dir(project_id: str) -> Path:
    # validate before any filesystem use → blocks path traversal
    return settings.projects_dir / _safe_id(project_id)


def assets_dir(project_id: str) -> Path:
    d = project_dir(project_id) / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_path(project_id: str) -> Path:
    return project_dir(project_id) / "state.json"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def create_project(title: str, fmt: OutputFormat) -> ProjectState:
    project_id = uuid.uuid4().hex[:12]
    pdir = project_dir(project_id)
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "versions").mkdir(exist_ok=True)
    now = _now()
    state = ProjectState(
        project_id=project_id,
        title=title,
        format=fmt,
        current_step=PipelineStep.brief,
        created_at=now,
        updated_at=now,
    )
    save(state)
    return state


def load(project_id: str) -> Optional[ProjectState]:
    p = _state_path(project_id)
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    return ProjectState.model_validate(data)


def save(state: ProjectState) -> ProjectState:
    state.updated_at = _now()
    with _lock_for(state.project_id):
        p = _state_path(state.project_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        # 원자적 쓰기: temp 작성 후 교체 (크래시 시 부분 파일 방지)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        import os

        os.replace(tmp, p)
    return state


def list_projects() -> List[dict]:
    out: List[dict] = []
    for d in sorted(settings.projects_dir.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        try:
            st = load(d.name)
        except Exception:
            # 손상/부분 생성된 프로젝트는 목록에서 건너뜀 (전체 목록 실패 방지)
            continue
        if st:
            outs = st.final_outputs or []
            thumb = (outs[-1] if outs else None) or (st.hero_image.url if st.hero_image else None)
            product = st.adspec.product.name if (st.adspec and st.adspec.product) else ""
            out.append(
                {
                    "project_id": st.project_id,
                    "title": st.title,
                    "format": st.format,
                    "current_step": int(st.current_step),
                    "updated_at": st.updated_at,
                    "thumb": thumb,
                    "product": product,
                    "shots": len(st.storyboard.shots) if st.storyboard else 0,
                    "outputs": len(outs),
                }
            )
    out.sort(key=lambda x: x["updated_at"], reverse=True)
    return out


def rename_project(project_id: str, title: str) -> Optional[ProjectState]:
    st = load(project_id)
    if st is None:
        return None
    st.title = (title or "").strip()[:80] or st.title
    save(st)
    return st


def delete_project(project_id: str) -> bool:
    import shutil

    d = project_dir(project_id)
    if d.exists():
        shutil.rmtree(d)
        return True
    return False


def duplicate_project(src_id: str) -> Optional[ProjectState]:
    """Clone a project (state + assets) into a fresh project — iterate without re-entering the brief.

    Asset URLs and ids are rewritten to the new project; version history starts fresh.
    """
    import shutil

    src_dir = project_dir(src_id)  # validates src_id
    if not src_dir.exists():
        return None
    new_id = uuid.uuid4().hex[:12]
    new_dir = project_dir(new_id)
    shutil.copytree(src_dir, new_dir)
    # fresh version history for the copy
    vdir = new_dir / "versions"
    if vdir.exists():
        shutil.rmtree(vdir)
    vdir.mkdir(parents=True, exist_ok=True)
    # rewrite state: src_id → new_id everywhere (project_id fields + asset URLs)
    sp = new_dir / "state.json"
    st = ProjectState.model_validate_json(sp.read_text(encoding="utf-8").replace(src_id, new_id))
    st.project_id = new_id
    st.versions = []
    st.title = (st.title or "Untitled") + " (copy)"
    now = _now()
    st.created_at = now
    st.updated_at = now
    return save(st)


# ---------------------------------------------------------------------------
# 버전 (사람 개입 스냅샷)
# ---------------------------------------------------------------------------
def _next_version_num(vdir: Path) -> int:
    """디스크의 기존 v*.json 최대치+1 — list 길이로 계산하면 revert 후 덮어씀."""
    existing = [
        int(p.stem[1:])
        for p in vdir.glob("v*.json")
        if p.stem[1:].isdigit()
    ]
    return (max(existing) + 1) if existing else 1


def snapshot(state: ProjectState, step: PipelineStep, label: str = "") -> ProjectState:
    """현재 상태를 버전으로 스냅샷하고 versions 리스트에 기록."""
    with _lock_for(state.project_id):
        vdir = project_dir(state.project_id) / "versions"
        vdir.mkdir(parents=True, exist_ok=True)
        vnum = _next_version_num(vdir)  # 디스크 기준 → revert 후에도 덮어쓰지 않음
        ver = Version(version=vnum, step=step, label=label, created_at=_now())
        state.versions.append(ver)
        (vdir / f"v{vnum}.json").write_text(
            state.model_dump_json(indent=2), encoding="utf-8"
        )
    return save(state)


def revert(project_id: str, version: int) -> Optional[ProjectState]:
    """특정 버전으로 되돌리기 (임의 단계 재진입)."""
    vpath = project_dir(project_id) / "versions" / f"v{version}.json"
    if not vpath.exists():
        return None
    data = json.loads(vpath.read_text(encoding="utf-8"))
    state = ProjectState.model_validate(data)
    # Reverting must be non-destructive: the snapshot's own `versions` list only knows
    # versions ≤ this one, so adopting it would hide every LATER version from the history
    # (the v*.json files still exist on disk — you just couldn't see/revert to them).
    # Preserve the full, current history instead.
    current = load(project_id)
    if current is not None:
        state.versions = current.versions
    return save(state)
