"""Security: project_id is used to build filesystem paths (incl. rmtree on delete),
so it must reject path traversal / separators."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import state as state_store  # noqa: E402


@pytest.mark.parametrize(
    "bad",
    ["../evil", "../../etc/passwd", "a/b", "..", ".", "", "x" * 65, "with space", "semi;colon"],
)
def test_project_dir_rejects_traversal(bad):
    with pytest.raises(ValueError):
        state_store.project_dir(bad)


def test_valid_ids_accepted():
    for ok in ["abc123", "ABC_def-789", "a", "0" * 64]:
        p = state_store.project_dir(ok)
        # stays inside the projects dir
        assert state_store.settings.projects_dir in p.resolve().parents or p == state_store.settings.projects_dir / ok


def test_delete_traversal_does_not_rmtree(tmp_path):
    # a real dir that must NOT be deletable via a crafted id
    victim = state_store.settings.projects_dir.parent / "_victim_dir"
    victim.mkdir(exist_ok=True)
    try:
        with pytest.raises(ValueError):
            state_store.delete_project("../_victim_dir")
        assert victim.exists()  # untouched
    finally:
        victim.rmdir()


@pytest.mark.parametrize("bad", ["../evil.png", "a/b.png", "..", ".", "", "x\\y.png"])
def test_save_asset_rejects_traversal_filename(bad):
    from app.pipeline import save_asset

    proj = state_store.create_project("sec", fmt="static_image")
    try:
        with pytest.raises(ValueError):
            save_asset(proj.project_id, bad, b"data")
    finally:
        state_store.delete_project(proj.project_id)


def test_malicious_shot_id_blocked_at_storyboard_and_save_asset():
    """Layered defense: a path-traversal shot id is rejected at PUT /storyboard, and
    save_asset independently refuses a crafted filename (defense in depth)."""
    from app.core.schemas import Shot, SpecRequest, Storyboard
    from app.pipeline import save_asset, step1_brief, step2_spec, step3_storyboard

    proj = state_store.create_project("sec2", fmt="static_image")
    pid = proj.project_id
    victim = state_store.settings.projects_dir.parent / "_evil_shot_v0.png"
    try:
        step1_brief.run(pid, product_text="X.")
        step2_spec.run(pid, SpecRequest(duration_sec=9))
        step3_storyboard.run(pid)
        # layer 1: storyboard update rejects the unsafe id
        with pytest.raises(ValueError):
            step3_storyboard.update(pid, Storyboard(shots=[Shot(id="../../_evil_shot", image_prompt="x")]))
        # layer 2: even if a crafted filename reached save_asset, it is refused
        with pytest.raises(ValueError):
            save_asset(pid, "../../_evil_shot_v0.png", b"x")
        assert not victim.exists()  # nothing written outside assets
    finally:
        if victim.exists():
            victim.unlink()
        state_store.delete_project(pid)


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://127.0.0.1/x",          # loopback
        "http://169.254.169.254/",     # cloud metadata (link-local)
        "http://10.0.0.5/img.png",     # private
        "http://192.168.1.1/",         # private
        "http://[::1]/",               # ipv6 loopback
        "file:///etc/passwd",          # non-http scheme
        "ftp://10.0.0.1/x",            # non-http scheme
        "not-a-url",
        "",
    ],
)
def test_ssrf_blocks_internal_urls(bad_url):
    from app.pipeline import is_safe_remote_url

    assert is_safe_remote_url(bad_url) is False


def test_ssrf_allows_public_ip_literal():
    from app.pipeline import is_safe_remote_url

    # public IP literal → no DNS needed, must be allowed
    assert is_safe_remote_url("http://8.8.8.8/logo.png") is True


def test_fetch_image_bytes_refuses_internal(monkeypatch):
    from app import pipeline

    # if the guard fails, this would attempt a real fetch — assert it short-circuits
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("should not fetch internal URL")

    monkeypatch.setattr(pipeline.httpx, "Client", _boom)
    assert pipeline.fetch_image_bytes("http://169.254.169.254/latest/meta-data/") is None
    assert called["n"] == 0  # never reached the HTTP client


@pytest.mark.parametrize(
    "evil_id",
    ["<img src=x onerror=alert(1)>", "'); alert(1); ('", "a b", "../x", "id/with/slash", "x" * 41, ""],
)
def test_storyboard_rejects_unsafe_shot_ids(evil_id):
    """shot.id is rendered into UI markup → PUT /storyboard must reject unsafe ids."""
    from app.core.schemas import Shot, SpecRequest, Storyboard
    from app.pipeline import step1_brief, step2_spec, step3_storyboard

    proj = state_store.create_project("xss", fmt="static_image")
    pid = proj.project_id
    try:
        step1_brief.run(pid, product_text="X.")
        step2_spec.run(pid, SpecRequest(duration_sec=9))
        step3_storyboard.run(pid)
        with pytest.raises(ValueError):
            step3_storyboard.update(pid, Storyboard(shots=[Shot(id=evil_id, image_prompt="x")]))
    finally:
        state_store.delete_project(pid)


def test_generated_shot_ids_are_safe():
    """Generated storyboard ids are always the safe shot_N form (model id ignored)."""
    import re

    from app.core.schemas import SpecRequest
    from app.pipeline import step1_brief, step2_spec, step3_storyboard

    proj = state_store.create_project("xss2", fmt="static_image")
    pid = proj.project_id
    try:
        step1_brief.run(pid, product_text="X.")
        step2_spec.run(pid, SpecRequest(duration_sec=15))
        sb = step3_storyboard.run(pid)
        assert all(re.fullmatch(r"shot_\d+", s.id) for s in sb.shots)
    finally:
        state_store.delete_project(pid)


@pytest.mark.parametrize("bad_ratio", ["../../../tmp/pwn", "9/16", "..", "9:16:evil", "x" * 50])
def test_export_rejects_unsafe_ratio(bad_ratio):
    """Video export builds a path from `ratio`; only allowlisted ratios are permitted."""
    from app.core.schemas import RenderRequest, SpecRequest
    from app.pipeline import (
        step1_brief, step2_spec, step3_storyboard, step4_hero_image,
        step5_shot_images, step6_image_edit, step8_assemble, step9_encode,
    )

    proj = state_store.create_project("exp", fmt="static_image")
    pid = proj.project_id
    try:
        step1_brief.run(pid, product_text="X.")
        step2_spec.run(pid, SpecRequest(duration_sec=9))
        step3_storyboard.run(pid)
        step4_hero_image.run(pid)
        step5_shot_images.run(pid)
        step6_image_edit.approve_all(pid)
        step8_assemble.run(pid, RenderRequest())
        with pytest.raises(ValueError):
            step9_encode.export_variants(pid, [bad_ratio])
    finally:
        state_store.delete_project(pid)


def test_ssrf_redirect_to_internal_blocked(monkeypatch):
    """A public URL that 302-redirects to an internal host must not be followed."""
    from app import pipeline

    class _Resp:  # streamed response context-manager
        def __init__(self, redirect):
            self.is_redirect = redirect
            self.url = pipeline.httpx.URL("http://93.184.216.34/start")
            self.headers = {"location": "http://169.254.169.254/latest/meta-data/"} if redirect else {}
            self.encoding = "utf-8"

        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield b"SECRET"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Client:
        def __init__(self, *a, **k):
            pass

        def stream(self, method, url, headers=None):
            # first hop (public) returns a redirect to internal
            return _Resp(redirect="93.184" in str(url))

        def close(self):
            pass

    monkeypatch.setattr(pipeline.httpx, "Client", _Client)
    # is_safe_remote_url for the public literal is True; redirect target is link-local → blocked
    assert pipeline.safe_fetch("http://93.184.216.34/start") is None


def test_fetch_image_bytes_success_normalizes_to_png(monkeypatch):
    """A valid remote image is fetched and normalized to PNG bytes (the success path)."""
    import io

    from PIL import Image

    from app import pipeline

    buf = io.BytesIO()
    Image.new("RGB", (24, 24), (10, 20, 30)).save(buf, "JPEG")   # non-PNG source
    fr = pipeline.FetchResult(content=buf.getvalue(), encoding="utf-8", url="http://x/i.jpg")
    monkeypatch.setattr(pipeline, "safe_fetch", lambda *a, **k: fr)

    out = pipeline.fetch_image_bytes("http://x/i.jpg")
    assert out and Image.open(io.BytesIO(out)).format == "PNG"   # normalized to PNG
    assert pipeline.fetch_image_bytes("") is None                # empty url short-circuits


def test_fetchresult_text_decodes_with_encoding():
    """FetchResult.text decodes bytes using the response encoding (lenient)."""
    from app.pipeline import FetchResult

    assert FetchResult("héllo".encode("utf-8"), "utf-8", "x").text == "héllo"
    assert FetchResult(b"\xff\xfe", "utf-8", "x").text == "��"   # bad bytes → replaced


def test_safe_fetch_caps_oversized_body(monkeypatch):
    """A response whose body exceeds the cap must be rejected (memory-exhaustion guard)."""
    from app import pipeline

    class _Resp:
        is_redirect = False
        url = pipeline.httpx.URL("http://93.184.216.34/big")
        headers: dict = {}              # no Content-Length → must be caught by the read cap
        encoding = "utf-8"

        def raise_for_status(self):
            pass

        def iter_bytes(self):
            for _ in range(5):
                yield b"x" * 1024        # 5 KB streamed

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Client:
        def __init__(self, *a, **k):
            pass

        def stream(self, method, url, headers=None):
            return _Resp()

        def close(self):
            pass

    monkeypatch.setattr(pipeline.httpx, "Client", _Client)
    # cap below the streamed size → rejected
    assert pipeline.safe_fetch("http://93.184.216.34/big", max_bytes=2048) is None
    # cap above it → returns the bounded content
    r = pipeline.safe_fetch("http://93.184.216.34/big", max_bytes=1_000_000)
    assert r is not None and len(r.content) == 5 * 1024


def test_safe_fetch_rejects_declared_oversize(monkeypatch):
    """An honestly-declared oversized Content-Length is rejected before reading the body."""
    from app import pipeline

    read = {"n": 0}

    class _Resp:
        is_redirect = False
        url = pipeline.httpx.URL("http://93.184.216.34/big")
        headers = {"content-length": str(50 * 1024 * 1024)}
        encoding = "utf-8"

        def raise_for_status(self):
            pass

        def iter_bytes(self):
            read["n"] += 1
            yield b"x"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Client:
        def __init__(self, *a, **k):
            pass

        def stream(self, method, url, headers=None):
            return _Resp()

        def close(self):
            pass

    monkeypatch.setattr(pipeline.httpx, "Client", _Client)
    assert pipeline.safe_fetch("http://93.184.216.34/big") is None
    assert read["n"] == 0        # body never read — rejected on the header


def test_concept_ids_are_safe():
    from app.core.schemas import SpecRequest
    from app.eval import concept_eval
    from app.pipeline import step1_brief, step2_spec

    proj = state_store.create_project("cid", fmt="static_image")
    pid = proj.project_id
    try:
        step1_brief.run(pid, product_text="X.")
        step2_spec.run(pid, SpecRequest(duration_sec=9))
        cs = concept_eval.evaluate(pid, n=5)
        import re
        assert all(re.fullmatch(r"concept_\d+", c.id) for c in cs)
    finally:
        state_store.delete_project(pid)


def test_concepts_n_is_clamped():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    pid = client.post("/api/projects", json={"title": "n", "product_text": "X."}).json()["project_id"]
    try:
        client.post(f"/api/projects/{pid}/spec", json={"duration_sec": 9})
        r = client.post(f"/api/projects/{pid}/concepts?n=100000000")
        assert r.status_code == 200
        assert len(r.json()) <= 10  # clamped, not 100M
    finally:
        client.delete(f"/api/projects/{pid}")


def test_http_traversal_not_500():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    # single-segment traversal id → handled (400 via ValueError handler), never 500
    r = client.get("/api/projects/..")
    assert r.status_code in (400, 404)
    d = client.delete("/api/projects/..")
    assert d.status_code in (400, 404)
