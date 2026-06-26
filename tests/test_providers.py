"""Tests for the real-provider code paths (response parsing, fallback).

These paths only execute when API keys are present, so they have no coverage from
the keyless end-to-end tests. Here we inject fake clients / mock HTTP to exercise the
request-building and response-parsing logic that the user will hit after adding keys.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.providers.llm_claude import ClaudeProvider, _extract_json  # noqa: E402


# ---------------------------------------------------------------------------
# _extract_json — model output → JSON object
# ---------------------------------------------------------------------------
def test_extract_json_bare():
    assert _extract_json('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_extract_json_code_fence():
    text = 'Here you go:\n```json\n{"hook": "buy now"}\n```\nDone.'
    assert _extract_json(text) == {"hook": "buy now"}


def test_extract_json_embedded_prose():
    text = 'The result is {"shots": [{"id": "shot_1"}]} as requested.'
    assert _extract_json(text) == {"shots": [{"id": "shot_1"}]}


def test_extract_json_no_object_raises():
    with pytest.raises(ValueError):
        _extract_json("no json here")


# ---------------------------------------------------------------------------
# ClaudeProvider.complete_json — real-client path via an injected fake client
# ---------------------------------------------------------------------------
class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Msg:
    def __init__(self, text):
        self.content = [_Block(text)]


class _FakeClient:
    def __init__(self, text=None, raise_exc=None):
        self._text = text
        self._raise = raise_exc
        self.last_call = None

    class _Messages:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **kwargs):
            self.outer.last_call = kwargs
            if self.outer._raise:
                raise self.outer._raise
            return _Msg(self.outer._text)

    @property
    def messages(self):
        return self._Messages(self)


def _provider_with(client):
    p = ClaudeProvider.__new__(ClaudeProvider)  # bypass __init__ (no key needed)
    p.api_key = "test"
    p.model = "claude-test"
    p._client = client
    return p


def test_claude_complete_json_parses_real_response():
    p = _provider_with(_FakeClient(text='```json\n{"name": "Aura", "key_message": "40h"}\n```'))
    out = p.complete_json("sys", "user", mock_fn=lambda: {"name": "MOCK"})
    assert out == {"name": "Aura", "key_message": "40h"}
    # request was actually built with the model + messages
    assert p._client.last_call["model"] == "claude-test"
    assert p._client.last_call["messages"][0]["role"] == "user"


def test_claude_schema_hint_injected_into_system():
    fc = _FakeClient(text='{"ok": true}')
    p = _provider_with(fc)
    p.complete_json("BASE SYS", "u", mock_fn=lambda: {}, schema_hint={"ok": "bool"})
    sysp = fc.last_call["system"]
    assert "BASE SYS" in sysp and "ok" in sysp  # schema appended to system prompt
    # the appended schema instruction must be English (all-English requirement) —
    # the only non-ASCII allowed is whatever the caller's own system prompt contains.
    appended = sysp.split("BASE SYS", 1)[-1]
    assert appended.isascii()
    assert "JSON object" in appended


def test_claude_falls_back_to_mock_on_api_error():
    p = _provider_with(_FakeClient(raise_exc=RuntimeError("503")))
    out = p.complete_json("s", "u", mock_fn=lambda: {"fallback": True})
    assert out == {"fallback": True}


def test_claude_falls_back_to_mock_on_unparseable_response():
    p = _provider_with(_FakeClient(text="sorry, I cannot help with that"))
    out = p.complete_json("s", "u", mock_fn=lambda: {"fallback": 1})
    assert out == {"fallback": 1}


def test_claude_no_client_uses_mock():
    p = _provider_with(None)
    assert p.complete_json("s", "u", mock_fn=lambda: {"m": 1}) == {"m": 1}
    with pytest.raises(RuntimeError):
        p.complete_json("s", "u", mock_fn=None)


# ---------------------------------------------------------------------------
# Flux fal path — parse images[0].url then fetch bytes (mock httpx)
# ---------------------------------------------------------------------------
def test_flux_fal_parses_image_url_and_fetches(monkeypatch):
    from app.providers import image_flux

    class _Resp:
        def __init__(self, *, json_data=None, content=None):
            self._json = json_data
            self.content = content

        def raise_for_status(self):
            pass

        def json(self):
            return self._json

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            assert "prompt" in json  # request body built
            return _Resp(json_data={"images": [{"url": "https://x/img.png"}]})

        def get(self, url):
            assert url == "https://x/img.png"
            return _Resp(content=b"PNGBYTES")

    import httpx

    monkeypatch.setattr(httpx, "Client", _Client)  # _call_fal does `import httpx` then httpx.Client(...)

    prov = image_flux.FluxProvider()
    prov.fal_key = "test-key"  # force real path
    data = prov._call_fal("a cat", "9:16")
    assert data == b"PNGBYTES"


def test_flux_forwards_seed_to_fal(monkeypatch):
    """seed strategy must reach the fal payload (real path) — regression for silent drop."""
    from app.providers import image_flux

    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"images": [{"url": "https://x/img.png"}]}

        content = b"PNGBYTES"

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            captured.update(json)
            return _Resp()

        def get(self, url):
            return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "Client", _Client)
    prov = image_flux.FluxProvider()
    prov.fal_key = "k"
    prov._call_fal("a cat", "9:16", seed=4242)
    assert captured.get("seed") == 4242
    # no seed → key absent (don't pin the worker to a default)
    captured.clear()
    prov._call_fal("a cat", "9:16")
    assert "seed" not in captured


def test_flux_mock_honors_seed():
    """Offline (mock) Flux must vary deterministically by seed so the seed strategy is real."""
    from app.providers.image_flux import FluxProvider

    prov = FluxProvider()
    prov.fal_key = ""  # force mock path
    a = prov.generate_image("scene one", seed=111).image_bytes
    b = prov.generate_image("scene two", seed=111).image_bytes   # same seed, diff prompt
    c = prov.generate_image("scene one", seed=999).image_bytes   # diff seed, same prompt
    a2 = prov.generate_image("scene one", seed=111).image_bytes  # fully deterministic
    assert a == a2                  # deterministic
    assert a != c                   # seed changes the output
    assert a != b                   # prompt still varies the image


# ---------------------------------------------------------------------------
# Seedance fal video path — parse video.url then fetch bytes (mock httpx)
# ---------------------------------------------------------------------------
def test_seedance_fal_parses_video_url(monkeypatch):
    import httpx

    from app.providers import video_seedance

    class _Resp:
        def __init__(self, *, json_data=None, content=None):
            self._json, self.content = json_data, content

        def raise_for_status(self):
            pass

        def json(self):
            return self._json

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            assert "image_url" in json and json["image_url"].startswith("data:image/png;base64,")
            return _Resp(json_data={"video": {"url": "https://x/clip.mp4"}})

        def get(self, url):
            assert url == "https://x/clip.mp4"
            return _Resp(content=b"MP4BYTES")

    monkeypatch.setattr(httpx, "Client", _Client)
    prov = video_seedance.SeedanceProvider()
    prov.fal_key = "k"
    assert prov._call_fal(b"PNG", "a runner", 5, True) == b"MP4BYTES"


# ---------------------------------------------------------------------------
# Kling / Veo fal video paths — request building + video.url fetch (mock httpx)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("modname,cls", [
    ("video_kling", "KlingProvider"),
    ("video_veo", "VeoProvider"),
])
def test_alt_video_providers_real_call(monkeypatch, modname, cls):
    import importlib

    import httpx

    mod = importlib.import_module(f"app.providers.{modname}")
    captured = {}

    class _Resp:
        def __init__(self, *, json_data=None, content=None):
            self._json, self.content = json_data, content

        def raise_for_status(self):
            pass

        def json(self):
            return self._json

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            captured.update(url=url, body=json)
            assert json["image_url"].startswith("data:image/png;base64,")
            assert "prompt" in json and "duration" in json
            return _Resp(json_data={"video": {"url": "https://x/clip.mp4"}})

        def get(self, url):
            assert url == "https://x/clip.mp4"
            return _Resp(content=b"MP4BYTES")

    monkeypatch.setattr(httpx, "Client", _Client)
    prov = getattr(mod, cls)()
    prov.fal_key = "k"
    # signatures differ (veo takes with_audio); call image_to_video which routes correctly
    res = prov.image_to_video(b"PNG", "a runner", duration_sec=5)
    assert res.video_bytes == b"MP4BYTES"
    assert res.meta["mode"] == "real"
    assert captured["url"].endswith(prov.endpoint)


@pytest.mark.parametrize("modname,cls,lo,hi", [
    ("video_seedance", "SeedanceProvider", 4, 15),
    ("video_kling", "KlingProvider", 5, 10),
    ("video_veo", "VeoProvider", 4, 8),
])
def test_video_duration_clamped_to_provider_range(modname, cls, lo, hi):
    """Pipeline shots are ~3s; each provider must clamp to its accepted window
    (else fal rejects sub-minimum durations → silent offline fallback)."""
    import importlib

    mod = importlib.import_module(f"app.providers.{modname}")
    prov = getattr(mod, cls)()
    assert prov.clamp_duration(3) == lo        # typical 3s shot → bumped to minimum
    assert prov.clamp_duration(999) == hi      # over-long → capped at maximum
    assert prov.clamp_duration(3.7) == max(lo, 4)   # rounds (3.7→4) before clamping
    assert lo <= prov.clamp_duration(lo) <= hi


@pytest.mark.parametrize("modname,cls", [
    ("video_kling", "KlingProvider"),
    ("video_veo", "VeoProvider"),
])
def test_alt_video_providers_offline_fallback(monkeypatch, modname, cls):
    """No key → ffmpeg Ken Burns baseline (or graceful) without touching the network."""
    import importlib

    mod = importlib.import_module(f"app.providers.{modname}")
    prov = getattr(mod, cls)()
    prov.fal_key = ""            # force offline
    assert prov.is_real is False


# ---------------------------------------------------------------------------
# Upscale (Topaz/clarity) + MiniMax music — fal real paths (mock httpx)
# ---------------------------------------------------------------------------
def _fal_client(monkeypatch, result_key, asset_bytes):
    """Install a fake httpx.Client whose POST returns {result_key:{url}} and GET returns bytes."""
    import httpx
    captured = {}

    class _Resp:
        def __init__(self, *, json_data=None, content=None):
            self._json, self.content = json_data, content

        def raise_for_status(self):
            pass

        def json(self):
            return self._json

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            captured.update(url=url, body=json)
            return _Resp(json_data={result_key: {"url": "https://x/asset"}})

        def get(self, url):
            assert url == "https://x/asset"
            return _Resp(content=asset_bytes)

    monkeypatch.setattr(httpx, "Client", _Client)
    return captured


def test_upscale_fal_real_path(monkeypatch):
    from app.providers import upscale

    cap = _fal_client(monkeypatch, "image", b"UPSCALED")
    prov = upscale.UpscaleProvider()
    prov.fal_key = "k"
    assert prov._call_fal_image(b"PNG") == b"UPSCALED"
    assert cap["body"]["image_url"].startswith("data:image/png;base64,")
    assert cap["url"].endswith(prov.image_endpoint)


def test_upscale_offline_lanczos_baseline():
    """No key → deterministic Lanczos upscale (valid larger PNG), no network."""
    import io

    from PIL import Image

    from app.providers import upscale

    prov = upscale.UpscaleProvider()
    prov.fal_key = ""
    src = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 20, 30)).save(src, "PNG")
    out = prov.upscale_image(src.getvalue(), factor=2)
    im = Image.open(io.BytesIO(out))
    im.verify()
    assert Image.open(io.BytesIO(out)).size == (128, 128)


def test_nanobanana_gemini_real_parse(monkeypatch):
    """Default real image provider: parse candidates→parts→inline_data (google.genai faked)."""
    import sys
    import types as _t

    from app.providers import image_nanobanana

    fake_types = _t.ModuleType("google.genai.types")
    fake_types.GenerateContentConfig = lambda **k: object()
    fake_genai = _t.ModuleType("google.genai")
    fake_genai.types = fake_types
    fake_google = _t.ModuleType("google")
    fake_google.genai = fake_genai
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)

    class _InlineData:
        data = b"GEMINIPNG"

    class _Part:
        inline_data = _InlineData()

    class _Content:
        parts = [_Part()]

    class _Cand:
        content = _Content()

    captured = {}

    class _Models:
        def generate_content(self, **k):
            captured.update(k)

            class _R:
                candidates = [_Cand()]
            return _R()

    class _Client:
        models = _Models()

    prov = image_nanobanana.NanoBananaProvider()
    prov._client = _Client()           # inject (bypass real key/client construction)
    out = prov.generate_image("a cat", "9:16", reference_images=[b"REF"])
    assert out.image_bytes == b"GEMINIPNG"
    assert out.meta["mode"] == "real"
    assert "contents" in captured       # request was built and dispatched


def test_minimax_music_real_path(monkeypatch):
    from app.providers import audio_minimax

    cap = _fal_client(monkeypatch, "audio", b"MP3BYTES")
    prov = audio_minimax.MiniMaxMusicProvider()
    prov.fal_key = "k"
    res = prov.music("upbeat", 15)
    assert res.audio_bytes == b"MP3BYTES" and res.mime == "audio/mpeg"
    assert "music" in cap["body"]["prompt"]
    # MiniMax provides no TTS — VO routes to ElevenLabs
    assert prov.tts("hello").audio_bytes == b""


# ---------------------------------------------------------------------------
# ElevenLabs audio — real TTS/music (mock httpx) + offline fallbacks
# ---------------------------------------------------------------------------
def test_elevenlabs_real_tts_and_music(monkeypatch):
    import httpx

    from app.providers import audio_elevenlabs

    class _Resp:
        content = b"AUDIO"

        def raise_for_status(self):
            pass

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            assert headers.get("xi-api-key") == "k"
            calls.append({"url": url, "json": json})
            return _Resp()

    calls = []
    monkeypatch.setattr(httpx, "Client", _Client)
    p = audio_elevenlabs.ElevenLabsProvider()
    p.api_key = "k"
    from app.config import settings

    monkeypatch.setattr(settings, "force_mock", False)
    assert p.tts("hello", "en").audio_bytes == b"AUDIO"
    assert p.music("upbeat", 10).audio_bytes == b"AUDIO"


def test_elevenlabs_music_length_clamped(monkeypatch):
    """Short ad videos (≈8s) must be bumped to the API's 10s minimum, not rejected."""
    import httpx

    from app.config import settings
    from app.providers import audio_elevenlabs

    sent = {}

    class _Resp:
        content = b"AUDIO"

        def raise_for_status(self):
            pass

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            sent.update(json)
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(settings, "force_mock", False)
    p = audio_elevenlabs.ElevenLabsProvider()
    p.api_key = "k"
    p.music("calm", 8)            # 8s → below the 10s minimum
    assert sent["music_length_ms"] == 10_000
    p.music("calm", 9999)         # absurdly long → capped at 5min
    assert sent["music_length_ms"] == 300_000


def test_elevenlabs_offline_music_fallback():
    from app.core import assembly
    from app.providers import audio_elevenlabs

    p = audio_elevenlabs.ElevenLabsProvider()
    p.api_key = ""  # no key → offline path
    res = p.music("upbeat", 3)
    if assembly.ffmpeg_available():
        assert res.audio_bytes and res.provider == "offline-bed"
    # offline TTS can't synthesize speech → empty (silence placeholder)
    assert p.tts("hi", "en").audio_bytes == b""


def test_qwen_runpod_parse_outputs():
    """Robustly parse the common RunPod worker output shapes → image bytes."""
    from app.providers import image_qwen_runpod as q

    class _Client:
        def get(self, url):
            class _R:
                content = b"URLBYTES"

                def raise_for_status(self):
                    pass

            assert url == "https://x/img.png"
            return _R()

    c = _Client()
    import base64

    b64 = base64.b64encode(b"B64BYTES").decode()
    # bare base64, data-uri, dict variants, images list, nested output, url fetch
    assert q._parse_output(b64, c) == b"B64BYTES"
    assert q._parse_output("data:image/png;base64," + b64, c) == b"B64BYTES"
    assert q._parse_output({"image": b64}, c) == b"B64BYTES"
    assert q._parse_output({"images": [b64]}, c) == b"B64BYTES"
    assert q._parse_output({"output": {"image_base64": b64}}, c) == b"B64BYTES"
    assert q._parse_output({"image_url": "https://x/img.png"}, c) == b"URLBYTES"
    assert q._parse_output({"nope": 1}, c) is None
    # real-worker shapes that previously failed to parse:
    assert q._parse_output({"images": [{"data": b64}]}, c) == b"B64BYTES"        # ComfyUI
    assert q._parse_output({"artifacts": [{"base64": b64}]}, c) == b"B64BYTES"   # SD-style
    assert q._parse_output({"b64_json": b64}, c) == b"B64BYTES"                  # OpenAI-style
    assert q._parse_output({"output": [{"image_b64": b64}]}, c) == b"B64BYTES"   # nested list-of-dict
    # robustness: a leading null/metadata item in a list is skipped, not fatal
    assert q._parse_output([None, {"image": b64}], c) == b"B64BYTES"


def test_qwen_to_bytes_and_parse_edge_cases():
    """Defensive branches: bad/empty/non-string input and failed URL fetch → None (no crash)."""
    from app.providers import image_qwen_runpod as q

    class _FailClient:
        def get(self, url):
            raise RuntimeError("network down")

    c = _FailClient()
    assert q._to_bytes("", c) is None            # empty
    assert q._to_bytes(123, c) is None           # non-string
    assert q._to_bytes("!!!not-base64!!!", c) is None  # undecodable base64
    assert q._to_bytes("http://x/i.png", c) is None    # URL fetch raises → None
    assert q._parse_output(None, c) is None      # None output
    assert q._parse_output([], c) is None        # empty list


def test_qwen_parse_extra_input():
    """RUNPOD_QWEN_INPUT: valid JSON dict kept, junk/non-dict ignored (never crashes)."""
    from app.providers.image_qwen_runpod import QwenRunPodProvider as P

    assert P._parse_extra('{"num_inference_steps": 30}') == {"num_inference_steps": 30}
    assert P._parse_extra("") == {}
    assert P._parse_extra("not json") == {}
    assert P._parse_extra("[1,2,3]") == {}       # JSON but not an object


def test_qwen_await_polling_states(monkeypatch):
    """_await: COMPLETED short-circuits, FAILED → None, IN_QUEUE polls /status to COMPLETED."""
    from app.providers import image_qwen_runpod as q

    prov = q.QwenRunPodProvider()
    base = "https://api.runpod.ai/v2/ep"
    # immediate completion (sync) and explicit terminal-fail
    assert prov._await({"status": "COMPLETED", "output": "x"}, base, None, {}) == {"status": "COMPLETED", "output": "x"}
    assert prov._await({"status": "FAILED"}, base, None, {}) is None
    # no id to poll → return as-is
    assert prov._await({"status": "IN_QUEUE"}, base, None, {}) == {"status": "IN_QUEUE"}

    # polling path: IN_PROGRESS → COMPLETED  (_await does a local `import time`)
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_: None)

    class _R:
        def __init__(self, s):
            self._s = s

        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "COMPLETED", "output": "done"} if self._s else {"status": "IN_PROGRESS"}

    class _Client:
        def __init__(self):
            self.n = 0

        def get(self, url, headers=None):
            self.n += 1
            return _R(self.n >= 2)   # second poll completes

    done = prov._await({"status": "IN_PROGRESS", "id": "j1"}, base, _Client(), {})
    assert done["output"] == "done"


def test_qwen_runpod_real_runsync(monkeypatch):
    """Real path: build {input:{prompt,width,height,seed}} and parse a completed runsync."""
    import httpx

    from app.providers.image_qwen_runpod import QwenRunPodProvider

    captured = {}

    class _Resp:
        status_code = 200

        def __init__(self, data):
            self._d = data

        def raise_for_status(self):
            pass

        def json(self):
            return self._d

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["input"] = json["input"]
            import base64
            png = base64.b64encode(b"QWENPNG").decode()
            return _Resp({"status": "COMPLETED", "output": {"image": png}})

    monkeypatch.setattr(httpx, "Client", _Client)
    p = QwenRunPodProvider()
    p.api_key, p.endpoint = "k", "endpoint123"
    from app.config import settings
    monkeypatch.setattr(settings, "force_mock", False)
    res = p.generate_image("a red shoe", "1:1", seed=42)
    assert res.image_bytes == b"QWENPNG" and res.provider == "qwen-runpod"
    assert captured["url"] == "https://api.runpod.ai/v2/endpoint123/runsync"
    assert captured["headers"]["Authorization"] == "Bearer k"
    assert captured["input"] == {"prompt": "a red shoe", "width": 1024, "height": 1024, "seed": 42}


def test_qwen_runpod_extra_input_merged(monkeypatch):
    """RUNPOD_QWEN_INPUT (env JSON) is merged into the request — adapts to any worker."""
    import base64

    import httpx

    from app.providers.image_qwen_runpod import QwenRunPodProvider

    captured = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "COMPLETED", "output": base64.b64encode(b"P").decode()}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            captured["input"] = json["input"]
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    from app.config import settings
    monkeypatch.setattr(settings, "force_mock", False)

    p = QwenRunPodProvider()
    p.api_key, p.endpoint = "k", "ep"
    p.extra_input = {"negative_prompt": "blurry", "num_inference_steps": 30, "width": 512}
    p.generate_image("a cat", "1:1", seed=7)
    inp = captured["input"]
    assert inp["prompt"] == "a cat" and inp["seed"] == 7
    assert inp["negative_prompt"] == "blurry" and inp["num_inference_steps"] == 30
    assert inp["width"] == 512  # env override wins over the default 1024


def test_qwen_runpod_async_poll(monkeypatch):
    """runsync returns IN_QUEUE → poll /status until COMPLETED."""
    import httpx

    from app.providers.image_qwen_runpod import QwenRunPodProvider

    monkeypatch.setattr("time.sleep", lambda *_: None)
    import base64
    png = base64.b64encode(b"ASYNCPNG").decode()
    calls = {"status": 0}

    class _Resp:
        status_code = 200

        def __init__(self, d):
            self._d = d

        def raise_for_status(self):
            pass

        def json(self):
            return self._d

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            return _Resp({"status": "IN_QUEUE", "id": "job1"})

        def get(self, url, headers=None):
            assert url.endswith("/status/job1")
            calls["status"] += 1
            if calls["status"] < 2:
                return _Resp({"status": "IN_PROGRESS"})
            return _Resp({"status": "COMPLETED", "output": png})

    monkeypatch.setattr(httpx, "Client", _Client)
    p = QwenRunPodProvider()
    p.api_key, p.endpoint = "k", "ep"
    from app.config import settings
    monkeypatch.setattr(settings, "force_mock", False)
    assert p.generate_image("x", "9:16").image_bytes == b"ASYNCPNG"


def test_qwen_diagnose_not_configured():
    from app.providers.image_qwen_runpod import QwenRunPodProvider

    p = QwenRunPodProvider()
    p.api_key, p.endpoint = "", ""
    d = p.diagnose()
    assert d["configured"] is False


def test_qwen_diagnose_reports_http_error(monkeypatch):
    """A misconfigured worker surfaces a clear failure reason (not a silent mock)."""
    import httpx

    from app.providers.image_qwen_runpod import QwenRunPodProvider

    class _Resp:
        status_code = 404
        text = "endpoint not found"

        def raise_for_status(self):
            pass

        def json(self):
            return {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    p = QwenRunPodProvider()
    p.api_key, p.endpoint = "k", "badid"
    d = p.diagnose()
    assert d["configured"] is True and d["real_call_ok"] is False
    assert "HTTP 404" in d["error"]


def test_qwen_diagnose_reports_parse_failure(monkeypatch):
    import httpx

    from app.providers.image_qwen_runpod import QwenRunPodProvider

    class _Resp:
        status_code = 200
        text = "ok"

        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "COMPLETED", "output": {"unexpected_key": "no image here"}}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    p = QwenRunPodProvider()
    p.api_key, p.endpoint = "k", "ep"
    d = p.diagnose()
    assert d["real_call_ok"] is False and "no image found" in d["error"]
    assert "unexpected_key" in d["error"]  # shows the actual output shape for debugging
    assert "preview" in d["error"] and "no image here" in d["error"]  # raw preview to map the shape


def test_qwen_preview_clips_long_values():
    """Diagnostic preview keeps structure/keys but clips long base64 blobs."""
    from app.providers.image_qwen_runpod import _preview

    big = "A" * 5000
    out = {"weird_key": big, "meta": {"seed": 7}}
    p = _preview(out)
    assert "weird_key" in p and "meta" in p and "seed" in p   # structure preserved
    assert big not in p and "5000 chars" in p                 # blob clipped, size noted
    assert len(p) <= 400


def test_qwen_test_endpoint():
    from fastapi.testclient import TestClient

    from app.main import app

    r = TestClient(app).get("/api/providers/qwen/test")
    assert r.status_code == 200 and "configured" in r.json()


def test_qwen_runpod_mock_when_not_configured():
    import io

    from PIL import Image

    from app.providers.image_qwen_runpod import QwenRunPodProvider

    p = QwenRunPodProvider()
    p.api_key, p.endpoint = "", ""   # not configured → mock
    out = p.generate_image("a lamp", "9:16")
    assert out.image_bytes and "mock" in out.provider
    Image.open(io.BytesIO(out.image_bytes)).verify()


def test_registry_prefers_qwen_when_configured(monkeypatch):
    from app.config import settings
    from app.providers import registry
    from app.providers.image_qwen_runpod import QwenRunPodProvider

    monkeypatch.setattr(settings, "runpod_api_key", "k")
    monkeypatch.setattr(settings, "runpod_qwen_endpoint", "ep")
    monkeypatch.setattr(settings, "force_mock", False)
    registry._qwen_image.cache_clear()
    try:
        chain = registry.image_chain()
        assert isinstance(chain[0], QwenRunPodProvider)        # Qwen first when configured
        assert isinstance(registry.get_image_provider(), QwenRunPodProvider)
    finally:
        registry._qwen_image.cache_clear()


def test_env_line_parser_strips_quotes_and_export():
    """Manual .env fallback must match python-dotenv basics — a quoted RunPod key would
    otherwise become `Bearer "abc"` → 401."""
    from app.config import _parse_env_line

    assert _parse_env_line('RUNPOD_API_KEY="abc123"') == ("RUNPOD_API_KEY", "abc123")
    assert _parse_env_line("RUNPOD_API_KEY='abc123'") == ("RUNPOD_API_KEY", "abc123")
    assert _parse_env_line("RUNPOD_API_KEY=abc123") == ("RUNPOD_API_KEY", "abc123")
    assert _parse_env_line("export FAL_KEY=xyz") == ("FAL_KEY", "xyz")
    assert _parse_env_line("  export GEMINI_API_KEY = spaced ") == ("GEMINI_API_KEY", "spaced")
    # JSON value with '=' inside survives (partition on first '=')
    assert _parse_env_line('RUNPOD_QWEN_INPUT={"a":"b=c"}') == ("RUNPOD_QWEN_INPUT", '{"a":"b=c"}')
    # comments / blanks / malformed → ignored
    assert _parse_env_line("# a comment") is None
    assert _parse_env_line("") is None
    assert _parse_env_line("no_equals_here") is None


def test_video_provider_routing():
    """get_video_provider routes opensource/seedance/kling/veo, defaulting to the
    open-source RunPod I2V (free/open path first, §4.7/§9)."""
    from app.providers.registry import VIDEO_MODELS, get_video_provider
    from app.providers.video_kling import KlingProvider
    from app.providers.video_runpod import RunPodVideoProvider
    from app.providers.video_seedance import SeedanceProvider
    from app.providers.video_veo import VeoProvider

    assert set(VIDEO_MODELS) == {"opensource", "seedance", "kling", "veo"}
    assert isinstance(get_video_provider("opensource"), RunPodVideoProvider)
    assert isinstance(get_video_provider("seedance"), SeedanceProvider)
    assert isinstance(get_video_provider("kling"), KlingProvider)
    assert isinstance(get_video_provider("veo"), VeoProvider)
    assert isinstance(get_video_provider("unknown"), RunPodVideoProvider)  # open-source default
    assert isinstance(get_video_provider(), RunPodVideoProvider)


def test_opensource_video_offline_fallback(monkeypatch):
    """With no RunPod video endpoint the open-source provider falls back to the ffmpeg
    Ken Burns baseline — video still works for free/offline."""
    import io

    from PIL import Image

    from app.config import settings
    from app.core import assembly
    from app.providers.video_runpod import RunPodVideoProvider

    monkeypatch.setattr(settings, "runpod_video_endpoint", "")
    prov = RunPodVideoProvider()
    assert prov.is_real is False
    if not assembly.ffmpeg_available():
        pytest.skip("ffmpeg not installed")
    buf = io.BytesIO()
    Image.new("RGB", (64, 112), (180, 60, 40)).save(buf, format="PNG")
    res = prov.image_to_video(buf.getvalue(), "ramen steam rising", duration_sec=3, aspect_ratio="9:16")
    assert res.video_bytes and len(res.video_bytes) > 0
    assert "kenburns" in res.provider


def test_audio_provider_routing():
    """get_audio_provider routes elevenlabs/minimax, defaults to elevenlabs (§5)."""
    from app.providers.audio_elevenlabs import ElevenLabsProvider
    from app.providers.audio_minimax import MiniMaxMusicProvider
    from app.providers.registry import MUSIC_MODELS, get_audio_provider

    assert set(MUSIC_MODELS) == {"elevenlabs", "minimax"}
    assert isinstance(get_audio_provider("elevenlabs"), ElevenLabsProvider)
    assert isinstance(get_audio_provider("minimax"), MiniMaxMusicProvider)
    assert isinstance(get_audio_provider("unknown"), ElevenLabsProvider)  # safe default


def test_minimax_music_offline_bed():
    from app.core import assembly
    from app.providers.audio_minimax import MiniMaxMusicProvider

    p = MiniMaxMusicProvider()
    p.fal_key = ""  # offline
    res = p.music("upbeat", 3)
    if assembly.ffmpeg_available():
        assert res.audio_bytes and "offline-bed" in res.provider


def test_video_providers_mock_offline(tmp_path):
    """All video providers fall back to the shared Ken Burns clip when keyless."""
    from app.core import assembly
    from app.providers.image_nanobanana import NanoBananaProvider
    from app.providers.video_kling import KlingProvider
    from app.providers.video_veo import VeoProvider

    if not assembly.ffmpeg_available():
        import pytest as _pt
        _pt.skip("ffmpeg not installed")
    still = NanoBananaProvider().generate_image("x", "9:16").image_bytes  # deterministic mock still
    for prov in (KlingProvider(), VeoProvider()):
        prov.fal_key = ""  # force offline
        res = prov.image_to_video(still, "p", duration_sec=2)
        assert res.video_bytes and "kenburns" in res.provider
        out = tmp_path / "c.mp4"
        out.write_bytes(res.video_bytes)
        assert assembly.probe_duration(out) > 1.0  # real, playable clip


def test_nanobanana_real_call_parses_inline_data(monkeypatch):
    """De-risk the primary image path: parse inline_data from a (mocked) Gemini response."""
    import sys
    import types as pytypes

    from app.providers import image_nanobanana

    # fake `google.genai` module so `from google.genai import types` resolves
    class _GCConfig:
        def __init__(self, **k):
            pass

    genai_mod = pytypes.ModuleType("google.genai")
    genai_mod.types = pytypes.SimpleNamespace(GenerateContentConfig=_GCConfig)
    google_mod = sys.modules.get("google") or pytypes.ModuleType("google")
    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.genai", genai_mod)

    # fake client returning candidates[].content.parts[].inline_data.data
    inline = pytypes.SimpleNamespace(data=b"GEMINIPNG")
    part = pytypes.SimpleNamespace(inline_data=inline)
    cand = pytypes.SimpleNamespace(content=pytypes.SimpleNamespace(parts=[part]))

    class _Models:
        def generate_content(self, model, contents, config):
            return pytypes.SimpleNamespace(candidates=[cand])

    p = image_nanobanana.NanoBananaProvider()
    p._client = pytypes.SimpleNamespace(models=_Models())
    assert p._call_gemini(["a prompt"]) == b"GEMINIPNG"


def test_nanobanana_mock_when_no_key():
    """Without GEMINI key, generate/edit return deterministic mock bytes."""
    import io

    from PIL import Image

    from app.providers import image_nanobanana

    p = image_nanobanana.NanoBananaProvider()
    p.api_key = ""
    p._client = None
    out = p.generate_image("a lamp", "9:16")
    assert out.image_bytes and "mock" in out.provider
    Image.open(io.BytesIO(out.image_bytes)).verify()
    edited = p.edit_image(out.image_bytes, "brighter")
    assert edited.image_bytes and "mock" in edited.provider


# ---------------------------------------------------------------------------
# Claude CLI LLM backend (`claude -p`) — mocked subprocess
# ---------------------------------------------------------------------------
def _fake_run(stdout="", returncode=0, stderr="", capture=None):
    def run(cmd, capture_output=True, text=True, timeout=None, env=None):
        if capture is not None:
            capture["cmd"] = cmd
            capture["env"] = env
        class _R:
            pass
        r = _R()
        r.stdout, r.returncode, r.stderr = stdout, returncode, stderr
        return r
    return run


def _cli_provider(monkeypatch, *, enabled=True, has_bin=True):
    from app.config import settings
    from app.providers import llm_claude_cli as m

    monkeypatch.setattr(settings, "claude_cli_enabled", enabled)
    monkeypatch.setattr(settings, "force_mock", False)
    p = m.ClaudeCLIProvider()
    p.bin = "/usr/bin/claude" if has_bin else None
    return p, m


def test_claude_cli_parses_json_envelope(monkeypatch):
    import subprocess

    p, m = _cli_provider(monkeypatch)
    cap = {}
    envelope = '{"type":"result","result":"{\\"name\\": \\"Pora\\"}"}'
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout=envelope, capture=cap))
    out = p.complete_json("sys", "user", mock_fn=lambda: {"name": "MOCK"},
                          schema_hint={"name": "str"})
    assert out == {"name": "Pora"}                 # result text → extracted JSON
    assert "-p" in cap["cmd"] and "--output-format" in cap["cmd"]
    # tools disallowed (single-turn completion) and --output-format still parsed after them
    assert "--disallowed-tools" in cap["cmd"] and "Bash" in cap["cmd"]
    di = cap["cmd"].index("--disallowed-tools")
    of = cap["cmd"].index("--output-format")
    assert di < of and cap["cmd"][of + 1] == "json"   # variadic stops before --output-format
    # interference env vars are stripped for the child process
    assert not any(k.startswith("CLAUDE_CODE") for k in cap["env"])


def test_claude_cli_falls_back_to_mock_when_disabled(monkeypatch):
    p, _ = _cli_provider(monkeypatch, enabled=False)
    assert p.is_real is False
    assert p.complete_json("s", "u", mock_fn=lambda: {"m": 1}) == {"m": 1}


def test_claude_cli_falls_back_on_empty_or_error(monkeypatch):
    import subprocess

    p, _ = _cli_provider(monkeypatch)
    # empty result envelope → mock
    monkeypatch.setattr(subprocess, "run",
                        _fake_run(stdout='{"type":"result","result":""}'))
    assert p.complete_json("s", "u", mock_fn=lambda: {"fb": 1}) == {"fb": 1}
    # non-zero exit → mock
    monkeypatch.setattr(subprocess, "run", _fake_run(returncode=1, stderr="boom"))
    assert p.complete_json("s", "u", mock_fn=lambda: {"fb": 2}) == {"fb": 2}


def test_claude_cli_timeout_falls_back(monkeypatch):
    import subprocess

    p, _ = _cli_provider(monkeypatch)

    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
    monkeypatch.setattr(subprocess, "run", _boom)
    assert p.complete_json("s", "u", mock_fn=lambda: {"fb": 3}) == {"fb": 3}
    assert "timed out" in (p.last_error or "")


def test_claude_cli_diagnose_states(monkeypatch):
    # not on PATH
    p, _ = _cli_provider(monkeypatch, has_bin=False)
    assert p.diagnose()["available"] is False
    # present but disabled
    p2, _ = _cli_provider(monkeypatch, enabled=False)
    d = p2.diagnose()
    assert d["available"] is True and d["enabled"] is False


def test_registry_prefers_cli_when_enabled(monkeypatch):
    from app.config import settings
    from app.providers import registry
    from app.providers.llm_claude_cli import ClaudeCLIProvider

    monkeypatch.setattr(settings, "claude_cli_enabled", True)
    monkeypatch.setattr(settings, "force_mock", False)
    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/claude")
    registry.get_llm.cache_clear()
    try:
        assert isinstance(registry.get_llm(), ClaudeCLIProvider)
    finally:
        registry.get_llm.cache_clear()


def test_claude_cli_vision_sends_image_stream_json(monkeypatch):
    """With images, the CLI provider must use stream-json in/out and embed a base64 image
    block — and parse the final stream-json result event."""
    import json as _json
    import subprocess

    from app.config import settings
    from app.providers import llm_claude_cli as m

    monkeypatch.setattr(settings, "claude_cli_enabled", True)
    monkeypatch.setattr(settings, "force_mock", False)
    cap = {}

    def fake_run(cmd, capture_output=True, text=True, timeout=None, env=None, input=None):
        cap["cmd"] = cmd
        cap["input"] = input
        # stream-json output: an init line + the terminal result event
        out = "\n".join([
            _json.dumps({"type": "system", "subtype": "init"}),
            _json.dumps({"type": "result", "subtype": "success",
                         "result": '{"name": "SoyMilk", "key_message": "Plant protein"}'}),
        ])
        class _R:
            stdout, returncode, stderr = out, 0, ""
        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    p = m.ClaudeCLIProvider()
    p.bin = "/usr/bin/claude"
    out = p.complete_json("sys", "user", mock_fn=lambda: {"name": "MOCK"},
                          schema_hint={"name": "str"}, images=[b"\x89PNGfakebytes"])
    assert out == {"name": "SoyMilk", "key_message": "Plant protein"}   # parsed result event
    assert "stream-json" in cap["cmd"] and "--input-format" in cap["cmd"]
    msg = _json.loads(cap["input"].strip())
    blocks = msg["message"]["content"]
    assert any(b.get("type") == "image" for b in blocks)               # image block embedded
    assert any(b.get("type") == "text" for b in blocks)


def test_claude_cli_circuit_breaker(monkeypatch):
    """After consecutive CLI failures the breaker opens — subsequent calls skip the (slow)
    CLI and go straight to mock, instead of timing out on every step."""
    import subprocess

    from app.config import settings
    from app.providers import llm_claude_cli as m

    monkeypatch.setattr(settings, "claude_cli_enabled", True)
    monkeypatch.setattr(settings, "force_mock", False)
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
    monkeypatch.setattr(subprocess, "run", boom)

    p = m.ClaudeCLIProvider()
    p.bin = "/usr/bin/claude"
    mock = lambda: {"fallback": True}
    # first two calls actually invoke the CLI (and fail) → breaker trips at threshold 2
    assert p.complete_json("s", "u", mock_fn=mock) == {"fallback": True}
    assert p.complete_json("s", "u", mock_fn=mock) == {"fallback": True}
    assert calls["n"] == 2 and p._circuit_open()
    # further calls short-circuit to mock WITHOUT invoking the CLI again
    p.complete_json("s", "u", mock_fn=mock)
    p.complete_json("s", "u", mock_fn=mock)
    assert calls["n"] == 2                          # CLI not called while circuit open
    # a success resets the breaker
    def ok(*a, **k):
        class _R:
            stdout, returncode, stderr = '{"type":"result","result":"{\\"x\\":1}"}', 0, ""
        return _R()
    monkeypatch.setattr(subprocess, "run", ok)
    p._circuit_until = 0.0                          # simulate cooldown elapsed
    assert p.complete_json("s", "u", mock_fn=mock) == {"x": 1}
    assert p._consec_fail == 0 and not p._circuit_open()


def test_pollinations_free_image_real_and_mock(monkeypatch):
    """Pollinations: real path fetches an image via httpx (no key); failure → deterministic mock."""
    import io

    from PIL import Image

    from app.config import settings
    from app.providers import image_pollinations as m

    captured = {}

    class _Resp:
        status_code = 200
        headers = {"content-type": "image/jpeg"}
        content = (lambda: (lambda b: (Image.new("RGB", (8, 8), (1, 2, 3)).save(b, "JPEG"), b.getvalue())[1])(io.BytesIO()))()

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            captured["url"] = url
            captured["params"] = params
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(settings, "free_images", True)
    monkeypatch.setattr(settings, "force_mock", False)

    p = m.PollinationsProvider()
    res = p.generate_image("wireless earbuds on marble", "9:16", seed=42)
    assert res.meta["mode"] == "real" and res.image_bytes
    assert "image.pollinations.ai/prompt/" in captured["url"]
    assert captured["params"]["width"] == 768 and captured["params"]["seed"] == 42

    # failure (non-200) → mock fallback (valid placeholder image)
    class _Bad(_Client):
        def get(self, url, params=None):
            class _R:
                status_code, headers, content = 500, {}, b""
            return _R()
    monkeypatch.setattr(httpx, "Client", _Bad)
    res2 = p.generate_image("x", "1:1")
    assert "mock" in res2.provider
    Image.open(io.BytesIO(res2.image_bytes)).verify()


def test_pollinations_disabled_when_free_images_off(monkeypatch):
    from app.config import settings
    from app.providers.image_pollinations import PollinationsProvider

    monkeypatch.setattr(settings, "free_images", False)
    assert PollinationsProvider().is_real is False


def test_image_model_routing_and_status(monkeypatch):
    """Per-image model picker: status lists availability; get_image_provider_by_name uses the
    chosen real provider and falls back to best-available when the choice isn't configured."""
    from app.config import settings
    from app.providers import registry

    monkeypatch.setattr(settings, "free_images", True)
    monkeypatch.setattr(settings, "force_mock", False)
    monkeypatch.setattr(settings, "runpod_api_key", "")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "fal_key", "")
    registry._free_image.cache_clear()

    status = {m["key"]: m["available"] for m in registry.image_models_status()}
    assert status["auto"] is True and status["free"] is True
    assert status["qwen"] is False and status["nano_banana"] is False   # no keys

    # chosen free → pollinations; chosen qwen (no key) → falls back to a real provider (free)
    assert registry.get_image_provider_by_name("free").name == "pollinations"
    assert registry.get_image_provider_by_name("qwen").name == "pollinations"
    assert registry.get_image_provider_by_name("auto").is_real
    registry._free_image.cache_clear()


def test_image_models_endpoint():
    from fastapi.testclient import TestClient

    from app.main import app

    r = TestClient(app).get("/api/providers/image-models")
    assert r.status_code == 200
    keys = [m["key"] for m in r.json()]
    assert keys[0] == "auto" and "qwen" in keys and "nano_banana" in keys and "free" in keys


def test_qwen_lora_style_and_input_merge(monkeypatch):
    """LoRA flow: RUNPOD_QWEN_STYLE appends the trigger to the prompt; RUNPOD_QWEN_INPUT merges
    LoRA params into the worker request."""
    from app.config import settings
    from app.providers import image_qwen_runpod as m

    monkeypatch.setattr(settings, "runpod_api_key", "k")
    monkeypatch.setattr(settings, "runpod_qwen_endpoint", "ep")
    monkeypatch.setattr(settings, "runpod_qwen_style", "mdong style")
    monkeypatch.setattr(settings, "runpod_qwen_input", '{"lora":"myeongdong","lora_strength":0.8}')
    monkeypatch.setattr(settings, "force_mock", False)

    cap = {}

    class _R:
        status_code = 200
        text = ""

        def json(self):
            return {"output": {"image_url": "http://x/a.png"}}

    class _G:
        status_code = 200
        content = b"PNG"

    class _C:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            cap["input"] = json["input"]
            return _R()

        def get(self, url, headers=None):
            return _G()

    import httpx
    monkeypatch.setattr(httpx, "Client", _C)
    monkeypatch.setattr(m, "_to_bytes", lambda *a, **k: b"PNG", raising=False)

    p = m.QwenRunPodProvider()
    p.generate_image("a model in Myeongdong", "9:16", seed=7)
    assert "mdong style" in cap["input"]["prompt"]            # LoRA trigger appended
    assert cap["input"]["lora"] == "myeongdong"               # LoRA params merged
    assert cap["input"]["lora_strength"] == 0.8 and cap["input"]["seed"] == 7


def test_backgrounds_options_and_apply(monkeypatch):
    """Per-image background picker: presets + IMAGE_BACKGROUNDS override + prompt injection."""
    from app.config import settings
    from app.core import backgrounds

    monkeypatch.setattr(settings, "image_backgrounds", "")
    opts = backgrounds.options()
    assert opts[0]["key"] == "" and any(o["key"] == "mdong_main" for o in opts)
    # selecting a scene appends its trained-LoRA caption to the subject prompt
    out = backgrounds.apply("a woman model", "mdong_main")
    assert out.startswith("a woman model,") and "myeongdong_street" in out
    assert backgrounds.apply("x", "") == "x"          # default = unchanged
    # IMAGE_BACKGROUNDS overrides the presets
    monkeypatch.setattr(settings, "image_backgrounds",
                        '[{"key":"k1","label":"L1","prompt":"my scene trigger"}]')
    keys = [o["key"] for o in backgrounds.options()]
    assert "k1" in keys and "mdong_main" not in keys
    assert "my scene trigger" in backgrounds.apply("hero", "k1")
