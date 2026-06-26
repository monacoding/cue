"""JobQueue robustness — dedupe of concurrent jobs + bounded memory."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.jobs import Job, JobQueue  # noqa: E402


def test_dedupe_returns_same_inflight_job():
    """Two submits for the same (project, kind) while one is in flight → same job."""
    q = JobQueue(max_workers=2)
    gate = threading.Event()

    def slow(job: Job):
        gate.wait(2)  # hold the worker so the job stays 'running'
        return "ok"

    j1 = q.submit("video", "p1", slow, dedupe=True)
    # give the worker a moment to flip to 'running'
    time.sleep(0.05)
    j2 = q.submit("video", "p1", slow, dedupe=True)
    assert j1.id == j2.id  # deduped — no second worker started

    # different project is NOT deduped
    j3 = q.submit("video", "p2", slow, dedupe=True)
    assert j3.id != j1.id

    gate.set()
    _await(q, j1.id)
    _await(q, j3.id)

    # after completion, a new submit creates a fresh job (active entry cleared)
    j4 = q.submit("video", "p1", lambda job: "x", dedupe=True)
    assert j4.id != j1.id


def test_no_dedupe_by_default():
    q = JobQueue(max_workers=2)
    gate = threading.Event()
    a = q.submit("k", "p", lambda job: gate.wait(2))
    time.sleep(0.05)
    b = q.submit("k", "p", lambda job: gate.wait(2))
    assert a.id != b.id  # without dedupe, both run
    gate.set()
    _await(q, a.id)
    _await(q, b.id)


def test_eviction_bounds_memory():
    q = JobQueue(max_workers=4, max_jobs=10)
    # a big burst of quick jobs
    for _ in range(40):
        q.submit("k", "p", lambda job: "ok")
    time.sleep(0.5)  # let the burst finish
    # eviction runs on submit; once the burst is finished, a few more submits
    # (past the 2× hard cap) reap the old finished jobs despite the grace window
    last = None
    for _ in range(6):
        last = q.submit("k", "p", lambda job: "ok")
        time.sleep(0.05)
    _await(q, last.id)
    with q._lock:
        # bounded near the hard cap (2× max_jobs), not 46
        assert len(q._jobs) <= 2 * q._max_jobs + q._pool._max_workers
    assert q.get(last.id) is not None  # newest survives eviction


def test_eviction_keeps_recent_within_grace():
    """A just-finished job is kept (not evicted) so a poller can still read it."""
    q = JobQueue(max_workers=4, max_jobs=5)
    # stay just over the cap (not 2×) so the grace window applies
    ids = [q.submit("k", "p", lambda job: "ok").id for _ in range(8)]
    time.sleep(0.3)
    q.submit("k", "p", lambda job: "ok")  # trigger an eviction pass (len 9 > 5, < 10)
    # recently-finished jobs are retained within the grace window
    assert all(q.get(i) is not None for i in ids)


def test_job_lifecycle_is_logged(caplog):
    import logging

    q = JobQueue()
    with caplog.at_level(logging.INFO, logger="cue.jobs"):
        j = q.submit("k", "p", lambda job: "ok")
        _await(q, j.id)
    msgs = " ".join(r.message for r in caplog.records)
    assert "running" in msgs and "done" in msgs


def test_job_error_is_logged(caplog):
    import logging

    def boom(job):
        raise RuntimeError("kaboom")

    q = JobQueue()
    with caplog.at_level(logging.ERROR, logger="cue.jobs"):
        j = q.submit("k", "p", boom)
        _await_status(q, j.id, "error")
    assert any("error" in r.message for r in caplog.records)


def _await_status(q, job_id, status, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        j = q.get(job_id)
        if j and j.status == status:
            return j
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach {status}")


def test_cancel_running_job_is_cooperative():
    """A running job's work loop checks is_cancelled and stops early; status → cancelled."""
    q = JobQueue(max_workers=2)
    started = threading.Event()
    progressed = []

    def work(job):
        started.set()
        for i in range(100):
            if q.is_cancelled(job):
                break
            progressed.append(i)
            time.sleep(0.02)
        return progressed

    j = q.submit("shots", "p", work)
    assert started.wait(2)
    time.sleep(0.05)
    assert q.cancel(j.id) is True
    js = _await_status(q, j.id, "cancelled")
    assert js.status == "cancelled"
    assert len(progressed) < 100          # interrupted before finishing all items
    assert q.cancel(j.id) is False        # already finished → no-op
    assert q.cancel("does-not-exist") is False


def test_cancel_queued_job_before_start():
    """A job cancelled while still queued is marked cancelled and its fn never runs."""
    q = JobQueue(max_workers=1)
    gate = threading.Event()
    ran = []
    blocker = q.submit("k", "p", lambda job: gate.wait(2))   # occupies the only worker
    time.sleep(0.05)
    queued = q.submit("k2", "p2", lambda job: ran.append(1))  # waits behind the blocker
    assert q.cancel(queued.id) is True
    assert q.get(queued.id).status == "cancelled"
    gate.set()
    _await(q, blocker.id)
    time.sleep(0.1)
    assert q.get(queued.id).status == "cancelled"
    assert ran == []                       # the cancelled job's fn was skipped


def test_active_job_lookup_for_reconnect():
    """active_job exposes the in-flight job per (project, kind) for UI reconnect."""
    q = JobQueue(max_workers=2)
    gate = threading.Event()
    j = q.submit("video", "p1", lambda job: gate.wait(2), dedupe=True)
    time.sleep(0.05)
    found = q.active_job("p1", "video")
    assert found is not None and found.id == j.id
    assert q.active_job("p1", "shots") is None      # different kind
    assert q.active_job("other", "video") is None   # different project
    gate.set()
    _await(q, j.id)
    # once finished, it's no longer "active"
    assert q.active_job("p1", "video") is None


def _await(q: JobQueue, job_id: str, timeout: float = 3.0):
    end = time.time() + timeout
    while time.time() < end:
        j = q.get(job_id)
        if j and j.status in ("done", "error"):
            return j
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")
