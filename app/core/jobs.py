"""비동기 잡 큐 (CLAUDE.md §6). Phase 2 영상 생성은 분 단위 비동기 → 폴링/웹훅.

Phase 1 이미지 생성은 동기로 충분하지만, 7단계 영상 생성을 위해 인터페이스를 미리 둔다.
표준 라이브러리만 사용한 경량 인메모리 큐 (재시작 시 디스크 복구는 phase 3).
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from app.logging_setup import get_logger

_log = get_logger("cue.jobs")


@dataclass
class Job:
    id: str
    kind: str
    project_id: str
    status: str = "queued"           # queued | running | done | error | cancelled
    progress: float = 0.0
    result: Any = None
    error: Optional[str] = None
    cancel_requested: bool = False   # cooperative interrupt (ComfyUI-style) — checked by long loops
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class JobQueue:
    # keep recently-finished jobs at least this long so a poller can still read them
    _EVICT_GRACE_SEC = 60.0

    def __init__(self, max_workers: int = 4, max_jobs: int = 200) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._jobs: Dict[str, Job] = {}
        self._active: Dict[Tuple[str, str], str] = {}  # (project_id, kind) → active job id
        self._finished_at: Dict[str, float] = {}       # job id → monotonic finish time
        self._lock = threading.Lock()
        self._max_jobs = max_jobs

    def submit(
        self,
        kind: str,
        project_id: str,
        fn: Callable[[Job], Any],
        dedupe: bool = False,
    ) -> Job:
        """Schedule a job. With dedupe=True, an in-flight job for the same
        (project_id, kind) is returned instead of starting a second one —
        prevents concurrent workers writing the same asset files (double-click).
        """
        key = (project_id, kind)
        with self._lock:
            if dedupe:
                jid = self._active.get(key)
                if jid:
                    existing = self._jobs.get(jid)
                    if existing and existing.status in ("queued", "running"):
                        return existing
            job = Job(id=uuid.uuid4().hex[:12], kind=kind, project_id=project_id)
            self._jobs[job.id] = job
            if dedupe:
                self._active[key] = job.id
            self._evict_locked()
        self._pool.submit(self._run, job, fn, key if dedupe else None)
        return job

    def _run(self, job: Job, fn: Callable[[Job], Any], key: Optional[Tuple[str, str]]) -> None:
        if job.cancel_requested:                      # cancelled before a worker picked it up
            job.status = "cancelled"
            _log.info("job %s [%s] cancelled before start", job.id, job.kind)
            self._finalize(job, key)
            return
        job.status = "running"
        job.updated_at = datetime.now(timezone.utc).isoformat()
        _log.info("job %s [%s] running (project=%s)", job.id, job.kind, job.project_id)
        try:
            result = fn(job)
            job.result = result
            job.progress = 1.0          # set progress + log before status so observers
            # the work loop breaks early on cancel — report it as cancelled, not done
            job.status = "cancelled" if job.cancel_requested else "done"
            _log.info("job %s [%s] %s", job.id, job.kind, job.status)
        except Exception as exc:  # noqa: BLE001
            job.error = f"{exc}\n{traceback.format_exc()}"
            _log.error("job %s [%s] error: %s", job.id, job.kind, exc)
            job.status = "error"     # set terminal status last
        finally:
            self._finalize(job, key)

    def _finalize(self, job: Job, key: Optional[Tuple[str, str]]) -> None:
        """Stamp finish time and clear the (project,kind) active slot for a terminal job."""
        job.updated_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._finished_at[job.id] = time.monotonic()
            if key is not None and self._active.get(key) == job.id:
                del self._active[key]

    def cancel(self, job_id: str) -> bool:
        """Request cooperative interrupt of a job (ComfyUI-style). A queued job is cancelled
        immediately; a running one stops at the next loop checkpoint (see is_cancelled).
        Returns False if the job is unknown or already finished."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status in ("done", "error", "cancelled"):
                return False
            job.cancel_requested = True
            if job.status == "queued":     # not started yet → cancel now (worker will skip it)
                job.status = "cancelled"
                job.updated_at = datetime.now(timezone.utc).isoformat()
                self._finished_at[job.id] = time.monotonic()
                for k, v in list(self._active.items()):
                    if v == job_id:
                        del self._active[k]
            _log.info("job %s [%s] cancel requested", job.id, job.kind)
            return True

    def is_cancelled(self, job: Job) -> bool:
        """Checked by long-running work loops (shots/video) to stop early on interrupt."""
        return job.cancel_requested

    def _evict_locked(self) -> None:
        """Bound memory: drop oldest finished jobs beyond max_jobs (caller holds lock).

        Recently-finished jobs are kept for a grace window so a client polling
        GET /api/jobs/{id} can still read the result — unless we're far over the
        cap (>2×), in which case the grace is ignored to keep memory bounded.
        """
        if len(self._jobs) <= self._max_jobs:
            return
        now = time.monotonic()
        hard = len(self._jobs) > 2 * self._max_jobs
        finished = sorted(
            (j for j in self._jobs.values() if j.status in ("done", "error")),
            key=lambda j: self._finished_at.get(j.id, 0.0),
        )
        overflow = len(self._jobs) - self._max_jobs
        removed = 0
        for j in finished:
            if removed >= overflow:
                break
            if not hard and (now - self._finished_at.get(j.id, 0.0)) < self._EVICT_GRACE_SEC:
                continue  # keep recently-finished for pollers
            self._jobs.pop(j.id, None)
            self._finished_at.pop(j.id, None)
            removed += 1

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def active_job(self, project_id: str, kind: str) -> Optional[Job]:
        """The in-flight job for (project_id, kind), if any — lets a reloaded
        client reconnect to a running generation's progress."""
        with self._lock:
            jid = self._active.get((project_id, kind))
            return self._jobs.get(jid) if jid else None

    def update_progress(self, job: Job, progress: float) -> None:
        job.progress = max(0.0, min(1.0, progress))
        job.updated_at = datetime.now(timezone.utc).isoformat()


queue = JobQueue()
