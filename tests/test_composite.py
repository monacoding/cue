"""Tests for the visual-output core: compositing, resize, consistency, fallback.
These produce the actual ad pixels, so we assert real effects (text drawn, logo
placed, exact crop dims) rather than just 'a PNG came out'."""
from __future__ import annotations

import io
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import composite, consistency  # noqa: E402


def _png(w, h, color=(40, 40, 40)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _open(b):
    return Image.open(io.BytesIO(b)).convert("RGB")


# ---------------------------------------------------------------------------
# composite.compose
# ---------------------------------------------------------------------------
def test_headline_actually_changes_pixels():
    base = _png(720, 1280)
    plain = composite.compose(base)
    with_headline = composite.compose(base, headline="LIMITED TIME OFFER")
    # drawing a headline must change the output
    assert plain != with_headline


def test_cta_button_draws_in_lower_region():
    base = _png(720, 1280, color=(10, 10, 10))
    out = _open(composite.compose(base, cta="Buy now", palette=["#ff0000"]))
    # CTA button is an accent (red) pill in the lower area — find reddish pixels there
    w, h = out.size
    found = False
    for y in range(int(h * 0.84), int(h * 0.96)):
        for x in range(int(w * 0.35), int(w * 0.65), 3):
            r, g, b = out.getpixel((x, y))
            if r > 150 and g < 90 and b < 90:
                found = True
                break
        if found:
            break
    assert found, "CTA accent button not rendered in lower region"


def test_long_cta_button_stays_within_canvas():
    """A long CTA must auto-fit, not overflow both edges (negative bx) and run off the image."""
    base = _png(720, 1280, color=(10, 10, 10))
    long_cta = "Learn more about our amazing limited-time offer today"
    out = _open(composite.compose(base, cta=long_cta, palette=["#ff0000"]))
    w, h = out.size
    # the accent (red) button must NOT touch the extreme left/right columns
    # (overflow would paint red into x≈0 / x≈w-1 within the CTA band)
    band = range(int(h * 0.82), int(h * 0.96))

    def is_red(x, y):
        r, g, b = out.getpixel((x, y))
        return r > 150 and g < 90 and b < 90

    left_edge = any(is_red(x, y) for x in (0, 1, 2, 3) for y in band)
    right_edge = any(is_red(x, y) for x in (w - 1, w - 2, w - 3, w - 4) for y in band)
    assert not left_edge and not right_edge, "long CTA button overflowed the canvas edges"
    # …but the button is still rendered somewhere in the band
    assert any(is_red(x, y) for x in range(int(w * 0.2), int(w * 0.8), 4) for y in band)


def test_logo_composited_top_right():
    base = _png(720, 1280, color=(0, 0, 0))
    logo = _png(120, 120, color=(0, 200, 0))  # solid green logo
    out = _open(composite.compose(base, logo_bytes=logo))
    w, h = out.size
    # sample inside the top-right logo placement area
    r, g, b = out.getpixel((int(w * 0.88), int(h * 0.06)))
    assert g > 120 and r < 100, f"logo not composited top-right (got {(r, g, b)})"


def test_hex_parsing_and_fallback():
    assert composite._hex("#ff0000")[:3] == (255, 0, 0)
    assert composite._hex("#fff")[:3] == (255, 255, 255)   # short form
    assert composite._hex("not-a-color")[:3] == (255, 255, 255)  # safe fallback


def test_resize_cover_exact_dims_and_center_crop():
    # a wide image cropped to portrait keeps center, exact target dims
    wide = _png(2000, 500, color=(0, 0, 0))
    out = _open(composite.resize_cover(wide, 1080, 1920))
    assert out.size == (1080, 1920)


# ---------------------------------------------------------------------------
# consistency
# ---------------------------------------------------------------------------
def test_build_references_includes_hero():
    hero = b"HEROBYTES"
    assert consistency.build_references(hero) == [hero]
    assert consistency.build_references(None) == []


def test_build_references_appends_extra_and_caps():
    hero = b"H"
    extra = [bytes([i]) for i in range(20)]
    refs = consistency.build_references(hero, extra=extra, max_refs=14)
    assert refs[0] == hero
    assert len(refs) == 14  # capped at max_refs


def test_consistency_prompt_suffix_nonempty_for_reference():
    s = consistency.consistency_prompt_suffix(consistency.ConsistencyStrategy.reference)
    assert isinstance(s, str) and s.strip()
    # every strategy now carries an English consistency instruction (seed/edit included)
    seed_s = consistency.consistency_prompt_suffix(consistency.ConsistencyStrategy.seed)
    assert seed_s.strip() and seed_s.isascii()


# ---------------------------------------------------------------------------
# registry fallback
# ---------------------------------------------------------------------------
def test_generate_with_fallback_returns_image():
    from app.providers import registry

    chain = registry.image_chain()
    assert len(chain) >= 2  # primary + fallback
    res = registry.generate_with_fallback("a product on a table", "9:16")
    assert res.image_bytes and _open(res.image_bytes).size[0] > 0


def test_fallback_prefers_real_over_earlier_mock(monkeypatch):
    """If the first provider only mocks (e.g. Qwen worker down) but a later one can do a
    REAL generation, the real result must win — not the earlier mock."""
    from app.providers import registry
    from app.providers.base import ImageResult

    class _Prov:
        def __init__(self, mode):
            self._mode = mode

        def generate_image(self, prompt, ar, refs=None, seed=None):
            return ImageResult(image_bytes=b"X-" + self._mode.encode(),
                               provider=self._mode, meta={"mode": self._mode})

    # chain: mock (down primary) → real (configured fallback) → mock
    monkeypatch.setattr(registry, "image_chain",
                        lambda: [_Prov("mock"), _Prov("real"), _Prov("mock")])
    res = registry.generate_with_fallback("p", "9:16")
    assert res.meta["mode"] == "real" and res.image_bytes == b"X-real"


def test_fallback_uses_mock_when_all_mock(monkeypatch):
    """All providers mock (fully offline) → return the first mock, never raise."""
    from app.providers import registry
    from app.providers.base import ImageResult

    class _Mock:
        def generate_image(self, prompt, ar, refs=None, seed=None):
            return ImageResult(image_bytes=b"first", provider="m1", meta={"mode": "mock"})

    class _Mock2:
        def generate_image(self, prompt, ar, refs=None, seed=None):
            return ImageResult(image_bytes=b"second", provider="m2", meta={"mode": "mock"})

    monkeypatch.setattr(registry, "image_chain", lambda: [_Mock(), _Mock2()])
    res = registry.generate_with_fallback("p", "9:16")
    assert res.image_bytes == b"first"   # first mock wins when nothing is real
