"""Shared test fixtures.

Tests must be hermetic — they must never shell out to the real `claude` CLI even when
the developer has CLAUDE_CLI=1 in their .env. This autouse fixture disables the CLI LLM
backend for every test (the dedicated CLI-provider tests re-enable it locally with a
mocked subprocess), so brief/spec/storyboard/concept generation uses the instant mock.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.providers import registry


@pytest.fixture(autouse=True)
def _hermetic_llm(monkeypatch):
    # tests must be hermetic — never shell out to the real `claude` CLI nor hit the free
    # Pollinations image service. Both fall back to the deterministic mock.
    monkeypatch.setattr(settings, "claude_cli_enabled", False)
    monkeypatch.setattr(settings, "free_images", False)
    # snapshot force_mock so a test/script that flips it globally can't leak into the next test
    monkeypatch.setattr(settings, "force_mock", settings.force_mock)
    registry.get_llm.cache_clear()
    registry._free_image.cache_clear()
    yield
    registry.get_llm.cache_clear()
    registry._free_image.cache_clear()


@pytest.fixture
def drain():
    """Resolve a possibly-async endpoint: if the POST returns {job_id}, poll the job to
    completion and return its result; otherwise return the JSON as-is. Lets tests treat
    the now-async brief/storyboard/concepts endpoints the same as the old sync ones."""
    import time

    def _drain(client, response):
        assert response.status_code == 200, response.text
        body = response.json()
        if not isinstance(body, dict) or "job_id" not in body:
            return body
        jid = body["job_id"]
        for _ in range(400):
            j = client.get(f"/api/jobs/{jid}").json()
            if j["status"] in ("done", "error"):
                assert j["status"] == "done", j.get("error")
                return j["result"]
            time.sleep(0.02)
        raise AssertionError("job did not finish in time")

    return _drain
