"""Generation recipes — reproducibility for every generated image (CLAUDE.md §1, §4).

ComfyUI's most-loved property is that every output PNG carries the full workflow, so any
image can be reproduced or remixed by dropping it back in. We borrow exactly that idea
(not the node graph, which would fight our opinionated linear pipeline):

  • the APP owns the seed (generated here, recorded on the asset) → real providers become
    reproducible, not just the deterministic mocks;
  • each image's `GenerationRecipe` is persisted on the asset AND embedded into the PNG's
    tEXt metadata, so a downloaded file travels with its provenance and can be dragged
    back in to restore the recipe (see `extract`).

This module is pure (PIL + schemas only) so it stays free of pipeline import cycles.
"""
from __future__ import annotations

import io
import json
import random
from datetime import datetime, timezone
from typing import Optional

from PIL import Image, PngImagePlugin

from app.core.schemas import GenerationRecipe

# PNG tEXt key holding the JSON recipe — mirrors ComfyUI's "prompt"/"workflow" keys.
PNG_TEXT_KEY = "cue_recipe"

# 32-bit seed space — what image models accept (and what UIs display as a plain int).
SEED_MAX = 2**32 - 1


def new_seed() -> int:
    """A fresh random 32-bit seed. The pipeline calls this so generation is always
    reproducible afterwards (the seed is recorded), the way ComfyUI hands every run a seed."""
    return random.randint(0, SEED_MAX)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def embed(png_bytes: bytes, recipe: GenerationRecipe) -> bytes:
    """Return the PNG with the recipe written into a tEXt chunk.

    Best-effort: if the bytes aren't a re-encodable image, return them unchanged so a
    metadata hiccup can never break generation (the recipe still lives on the asset/state).
    """
    try:
        img = Image.open(io.BytesIO(png_bytes))
        img.load()
        meta = PngImagePlugin.PngInfo()
        meta.add_text(PNG_TEXT_KEY, recipe.model_dump_json())
        buf = io.BytesIO()
        img.save(buf, format="PNG", pnginfo=meta)
        return buf.getvalue()
    except Exception:
        return png_bytes


def extract(png_bytes: bytes) -> Optional[GenerationRecipe]:
    """Read an embedded recipe back out of a PNG (drag-to-restore). None if absent/invalid.

    The input is untrusted (a user-dropped file), so parsing is fully defensive and the
    JSON is validated through the pydantic model before it's trusted anywhere.
    """
    try:
        img = Image.open(io.BytesIO(png_bytes))
        raw = (getattr(img, "text", None) or {}).get(PNG_TEXT_KEY) or img.info.get(PNG_TEXT_KEY)
        if not raw:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        return GenerationRecipe.model_validate(data)
    except Exception:
        return None
