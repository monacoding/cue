"""API contract / error-path tests (main.py edge branches): 404s, precondition
400s, revert/approve/export edge cases, the ValueError→400 handler."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402

client = TestClient(app)
MISSING = "nonexistent123"


def _new(fmt="static_image"):
    return client.post("/api/projects", json={"title": "t", "format": fmt,
                                              "product_text": "X."}).json()["project_id"]


# --- 404s on a missing project ---------------------------------------------
@pytest.mark.parametrize("method,path", [
    ("get", f"/api/projects/{MISSING}"),
    ("delete", f"/api/projects/{MISSING}"),
    ("post", f"/api/projects/{MISSING}/spec/approve"),
    ("post", f"/api/projects/{MISSING}/storyboard"),
    ("post", f"/api/projects/{MISSING}/hero/approve"),
    ("get", f"/api/projects/{MISSING}/versions"),
    ("get", f"/api/projects/{MISSING}/cost-estimate"),
    ("get", f"/api/projects/{MISSING}/active-job?kind=video"),
])
def test_missing_project_404(method, path):
    assert getattr(client, method)(path).status_code == 404


def test_root_serves_ui():
    assert client.get("/").status_code == 200


def test_favicon_served():
    r = client.get("/favicon.ico")
    assert r.status_code == 200 and "svg" in r.headers["content-type"]


def test_openapi_docs_and_tags():
    """Auto-generated API docs serve with organized tags (integrator surface)."""
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json").json()
    assert spec["info"]["title"] == "CUE API"
    assert spec["info"].get("description")
    tag_names = {t["name"] for t in spec.get("tags", [])}
    assert {"projects", "pipeline", "jobs", "output"} <= tag_names
    # endpoints are tagged (not all under default)
    used = {t for path in spec["paths"].values() for op in path.values() for t in op.get("tags", [])}
    assert "output" in used and "pipeline" in used


def test_jobs_missing_404():
    assert client.get("/api/jobs/nope").status_code == 404


# --- precondition 400s ------------------------------------------------------
def test_shots_requires_storyboard_and_hero(drain):
    pid = _new()
    try:
        client.post(f"/api/projects/{pid}/spec", json={"duration_sec": 9})
        # no storyboard yet
        assert client.post(f"/api/projects/{pid}/shots").status_code == 400
        drain(client, client.post(f"/api/projects/{pid}/storyboard"))
        # storyboard but no hero
        assert client.post(f"/api/projects/{pid}/shots").status_code == 400
    finally:
        client.delete(f"/api/projects/{pid}")


def test_update_storyboard_rejects_bad_shot_id(drain):
    pid = _new()
    try:
        client.post(f"/api/projects/{pid}/spec", json={"duration_sec": 9})
        drain(client, client.post(f"/api/projects/{pid}/storyboard"))
        r = client.put(f"/api/projects/{pid}/storyboard",
                       json={"shots": [{"id": "bad id!", "image_prompt": "x"}]})
        assert r.status_code == 400  # ValueError→400 handler
    finally:
        client.delete(f"/api/projects/{pid}")


def test_render_and_export_without_output_400():
    pid = _new()
    try:
        # approve with no output
        assert client.post(f"/api/projects/{pid}/render/approve").status_code == 400
        # export with no final output
        assert client.post(f"/api/projects/{pid}/export").status_code in (400, 503)
    finally:
        client.delete(f"/api/projects/{pid}")


def test_storyboard_add_and_remove_shots(drain):
    """PUT /storyboard supports adding/removing shots (UI workflow) with valid ids."""
    pid = _new()
    try:
        client.post(f"/api/projects/{pid}/spec", json={"duration_sec": 9})
        sb = drain(client, client.post(f"/api/projects/{pid}/storyboard"))
        n = len(sb["shots"])
        # add a valid new shot
        sb["shots"].append({"id": f"shot_{n+1}", "image_prompt": "added shot"})
        r = client.put(f"/api/projects/{pid}/storyboard", json=sb)
        assert r.status_code == 200 and len(r.json()["shots"]) == n + 1
        # remove one
        sb2 = r.json()
        sb2["shots"].pop(0)
        r2 = client.put(f"/api/projects/{pid}/storyboard", json=sb2)
        assert r2.status_code == 200 and len(r2.json()["shots"]) == n
    finally:
        client.delete(f"/api/projects/{pid}")


def test_download_all_zip(drain):
    """download-all bundles final output + export variants into a valid zip."""
    import io
    import zipfile

    pid = _new()
    try:
        client.post(f"/api/projects/{pid}/spec", json={"duration_sec": 9})
        drain(client, client.post(f"/api/projects/{pid}/storyboard"))
        client.post(f"/api/projects/{pid}/hero", json={})
        client.post(f"/api/projects/{pid}/hero/approve")
        # empty → 404
        assert client.get(f"/api/projects/{pid}/download-all").status_code == 404
        # render + export, then zip
        client.post(f"/api/projects/{pid}/render", json={})
        client.post(f"/api/projects/{pid}/export?ratios=1:1,9:16")
        r = client.get(f"/api/projects/{pid}/download-all")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
        assert any(n.startswith("final_") for n in names)
        assert any(n.startswith("export_") for n in names)
    finally:
        client.delete(f"/api/projects/{pid}")


def test_revert_missing_version_404():
    pid = _new()
    try:
        assert client.post(f"/api/projects/{pid}/revert?version=999").status_code == 404
    finally:
        client.delete(f"/api/projects/{pid}")


def test_active_job_none_when_idle():
    pid = _new()
    try:
        body = client.get(f"/api/projects/{pid}/active-job?kind=shots").json()
        assert body["job_id"] is None and body["status"] is None
    finally:
        client.delete(f"/api/projects/{pid}")


def test_edit_product_and_rerun_brief_endpoints():
    pid = _new()
    try:
        r = client.put(f"/api/projects/{pid}/product", json={"name": "Edited", "key_message": "km"})
        assert r.status_code == 200 and r.json()["product"]["name"] == "Edited"
        # brief is async now → returns a job id; poll it to completion
        r2 = client.post(f"/api/projects/{pid}/brief", json={"product_text": "New brief text."})
        assert r2.status_code == 200
        job_id = r2.json()["job_id"]
        import time
        for _ in range(100):
            j = client.get(f"/api/jobs/{job_id}").json()
            if j["status"] in ("done", "error"):
                break
            time.sleep(0.1)
        assert j["status"] == "done" and j["result"]["product"]["description"]
    finally:
        client.delete(f"/api/projects/{pid}")


def test_delete_returns_404_when_missing():
    assert client.delete(f"/api/projects/{MISSING}").status_code == 404


def test_unhandled_error_returns_clean_500(monkeypatch):
    """An unexpected error is logged server-side and returns a generic 500 (no leak)."""
    import app.main as m
    from app.routers import pipeline as pl

    def boom(*a, **k):
        raise RuntimeError("secret internal detail xyz")

    safe = TestClient(m.app, raise_server_exceptions=False)
    pid = _new()
    try:
        monkeypatch.setattr(pl.step2_spec, "run", boom)
        r = safe.post(f"/api/projects/{pid}/spec", json={"duration_sec": 9})
        assert r.status_code == 500
        assert r.json()["detail"] == "Internal server error"
        assert "secret" not in r.text  # internals not leaked to the client
    finally:
        client.delete(f"/api/projects/{pid}")


def test_shot_edit_and_approve_missing_shot():
    pid = _new()
    try:
        # editing a shot that doesn't exist → ValueError → 400
        r = client.post(f"/api/projects/{pid}/shots/shot_99/edit", json={"instruction": "x"})
        assert r.status_code == 400
    finally:
        client.delete(f"/api/projects/{pid}")
