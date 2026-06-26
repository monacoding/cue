"""Generation-recipe tests — reproducibility (ComfyUI-inspired, CLAUDE.md §1).

Covers: recipe capture on generate/edit, app-owned seed, PNG embed/extract roundtrip,
reproduce (same seed → same pixels), remix (change one field, lock the rest), and the
HTTP surface (recipe / reproduce / remix / extract). Keyless mock mode — no API needed.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import recipe as recipe_mod  # noqa: E402
from app.core import state as state_store  # noqa: E402
from app.core.schemas import GenerationRecipe, SpecRequest  # noqa: E402
from app.main import app  # noqa: E402
from app.pipeline import (  # noqa: E402
    read_asset_bytes,
    reproduce,
    step1_brief,
    step2_spec,
    step3_storyboard,
    step4_hero_image,
    step5_shot_images,
    step6_image_edit,
)

client = TestClient(app)


@pytest.fixture
def pid():
    st = state_store.create_project("recipe-test", fmt="static_image")
    yield st.project_id
    state_store.delete_project(st.project_id)


def _ready(pid, duration=9):
    """Brief → spec → storyboard → hero, so shots can be generated."""
    step1_brief.run(pid, product_text="Wireless earbuds. 40-hour battery.")
    step2_spec.run(pid, SpecRequest(duration_sec=duration))
    step3_storyboard.run(pid)
    step4_hero_image.run(pid)


def _pixels(pid, url) -> bytes:
    """Decoded pixel bytes of a saved asset (ignores PNG metadata, which carries the recipe)."""
    b = read_asset_bytes(pid, url)
    return Image.open(io.BytesIO(b)).convert("RGB").tobytes()


# --------------------------------------------------------------------------
# Recipe capture
# --------------------------------------------------------------------------
def test_hero_records_recipe_and_seed(pid):
    _ready(pid)
    hero = state_store.load(pid).hero_image
    assert hero.seed is not None                 # app owns the seed
    assert hero.recipe is not None
    assert hero.recipe.kind == "image"
    assert hero.recipe.shot_id == "hero"
    assert hero.recipe.seed == hero.seed
    assert hero.recipe.prompt == hero.prompt


def test_shot_records_recipe(pid):
    _ready(pid)
    imgs = step5_shot_images.run(pid, strategy="reference")
    a = imgs[0]
    assert a.seed is not None
    assert a.recipe is not None
    assert a.recipe.strategy == "reference"
    assert a.recipe.ref == "hero"                # reference strategy anchors on hero
    # distinct shots get distinct app-owned seeds (reference strategy)
    seeds = [x.seed for x in imgs]
    assert len(set(seeds)) == len(seeds)


def test_seed_strategy_locks_one_seed(pid):
    from app.core import consistency

    _ready(pid)
    imgs = step5_shot_images.run(pid, strategy="seed")
    seeds = {a.seed for a in imgs}
    assert seeds == {consistency.project_seed(pid)}   # one locked seed across all shots


def test_edit_records_edit_recipe(pid):
    _ready(pid)
    edited = step6_image_edit.edit_shot(pid, "hero", "warmer lighting")
    assert edited.recipe is not None
    assert edited.recipe.kind == "edit"
    assert edited.recipe.instruction == "warmer lighting"


# --------------------------------------------------------------------------
# PNG embed / extract
# --------------------------------------------------------------------------
def test_png_embed_extract_roundtrip():
    img = Image.new("RGB", (32, 32), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    rec = GenerationRecipe(kind="image", shot_id="hero", prompt="p", seed=12345, model="qwen")
    embedded = recipe_mod.embed(buf.getvalue(), rec)
    out = recipe_mod.extract(embedded)
    assert out is not None
    assert out.seed == 12345
    assert out.model == "qwen"
    assert out.prompt == "p"
    # pixels survive the metadata write
    assert Image.open(io.BytesIO(embedded)).convert("RGB").tobytes() == img.tobytes()


def test_extract_returns_none_without_recipe():
    img = Image.new("RGB", (8, 8), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    assert recipe_mod.extract(buf.getvalue()) is None
    assert recipe_mod.extract(b"not an image") is None


def test_saved_hero_png_carries_recipe(pid):
    _ready(pid)
    hero = state_store.load(pid).hero_image
    blob = read_asset_bytes(pid, hero.url)
    rec = recipe_mod.extract(blob)
    assert rec is not None
    assert rec.seed == hero.seed
    assert rec.shot_id == "hero"


# --------------------------------------------------------------------------
# Reproduce / Remix
# --------------------------------------------------------------------------
def test_reproduce_is_deterministic(pid):
    _ready(pid)
    hero = state_store.load(pid).hero_image
    before_px = _pixels(pid, hero.url)
    before_seed, before_rev = hero.seed, hero.revision

    again = reproduce.reproduce(pid, "hero")
    assert again.seed == before_seed             # same seed
    assert again.revision == before_rev + 1      # new versioned revision
    assert _pixels(pid, again.url) == before_px  # same seed → same image


def test_remix_randomize_seed_changes_image(pid):
    _ready(pid)
    hero = state_store.load(pid).hero_image
    before_px = _pixels(pid, hero.url)
    before_seed = hero.seed

    remixed = reproduce.remix(pid, "hero", {"randomize_seed": True})
    assert remixed.seed != before_seed
    assert _pixels(pid, remixed.url) != before_px


def test_remix_explicit_seed(pid):
    _ready(pid)
    remixed = reproduce.remix(pid, "hero", {"seed": 777})
    assert remixed.seed == 777
    assert remixed.recipe.seed == 777


def test_remix_background_changes_prompt_but_locks_seed(pid):
    _ready(pid)
    hero = state_store.load(pid).hero_image
    base_prompt = hero.recipe.base_prompt
    remixed = reproduce.remix(pid, "hero", {"background": "studio"})
    assert remixed.seed == hero.seed                       # seed locked
    assert remixed.recipe.background == "studio"
    assert remixed.recipe.base_prompt == base_prompt       # base prompt preserved
    # background is re-applied on top of the same base prompt
    assert remixed.prompt != hero.prompt


def test_remix_locks_model_when_not_overridden(pid):
    _ready(pid)
    hero = state_store.load(pid).hero_image
    remixed = reproduce.remix(pid, "hero", {"randomize_seed": True})
    assert remixed.model == hero.model                     # model stays locked


def test_reproduce_requires_recipe(pid):
    # a project with no hero → no asset → ValueError
    with pytest.raises(ValueError):
        reproduce.reproduce(pid, "hero")


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------
def _http_ready(fmt="static_image"):
    p = client.post("/api/projects", json={"title": "t", "format": fmt,
                                           "product_text": "X."}).json()["project_id"]
    client.post(f"/api/projects/{p}/spec", json={"duration_sec": 9})
    jb = client.post(f"/api/projects/{p}/storyboard").json()
    for _ in range(400):
        if client.get(f"/api/jobs/{jb['job_id']}").json()["status"] in ("done", "error"):
            break
    client.post(f"/api/projects/{p}/hero", json={})
    return p


def test_http_recipe_reproduce_remix_extract():
    p = _http_ready()

    # GET recipe
    r = client.get(f"/api/projects/{p}/assets/hero/recipe")
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["seed"] is not None and rec["shot_id"] == "hero"

    # reproduce → same seed, new revision
    rep = client.post(f"/api/projects/{p}/assets/hero/reproduce")
    assert rep.status_code == 200, rep.text
    assert rep.json()["seed"] == rec["seed"]

    # remix → explicit seed
    rm = client.post(f"/api/projects/{p}/assets/hero/remix", json={"seed": 4242})
    assert rm.status_code == 200, rm.text
    assert rm.json()["seed"] == 4242

    # extract: download the hero PNG and POST it back → recovers the recipe
    import base64
    url = rm.json()["url"]
    blob = client.get(url).content
    data_uri = "data:image/png;base64," + base64.b64encode(blob).decode()
    ex = client.post(f"/api/projects/{p}/recipe/extract", json={"data": data_uri})
    assert ex.status_code == 200, ex.text
    assert ex.json()["seed"] == 4242


def test_http_recipe_404_on_missing_image():
    p = client.post("/api/projects", json={"title": "t", "product_text": "X."}).json()["project_id"]
    assert client.get(f"/api/projects/{p}/assets/hero/recipe").status_code == 404
    assert client.post(f"/api/projects/{p}/assets/hero/reproduce").status_code == 400


def test_http_extract_404_without_embedded_recipe():
    import base64

    p = _http_ready()
    img = Image.new("RGB", (8, 8), (1, 2, 3))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    ex = client.post(f"/api/projects/{p}/recipe/extract", json={"data": data_uri})
    assert ex.status_code == 404
