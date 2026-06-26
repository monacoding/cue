"""Reproduce & Remix — regenerate any image from its recipe (CLAUDE.md §1, ComfyUI-inspired).

Two operations on a generated image's `GenerationRecipe`:

  • **reproduce** — re-run with the SAME recipe (same seed) → the same image. Deterministic
    for the keyless mocks; faithful for real providers because the app owns the seed.
  • **remix** — re-run with a few fields changed (prompt / background / seed / model /
    edit instruction) and the rest LOCKED. This is the human-in-the-loop convergence move:
    "keep everything, change one thing."

Both produce a new revision of the same asset (versioned, like every other edit), so the
history is preserved. We regenerate at the provider level using the recipe directly rather
than re-entering step4/5 — that keeps reproduce *exact* (no re-derivation surprises).
"""
from __future__ import annotations

from typing import Any, Dict

from app.core import backgrounds
from app.core import consistency
from app.core import recipe as recipe_mod
from app.core import state as state_store
from app.core.schemas import ImageAsset, PipelineStep, StepStatus
from app.pipeline import read_asset_bytes, resolve_image_bytes, save_image_with_recipe
from app.providers.registry import generate_with_fallback, get_image_provider_by_name


def _hero_product_refs(project_id: str, st) -> list:
    """Product images used as hero references (mirrors step4) so a hero reproduce is faithful."""
    refs = []
    if st.adspec:
        for u in st.adspec.product.image_urls[:3]:
            b = resolve_image_bytes(project_id, u)
            if b:
                refs.append(b)
    return refs


def reproduce(project_id: str, shot_id: str) -> ImageAsset:
    """Regenerate the image with its exact stored recipe (same seed → same image)."""
    return _regenerate(project_id, shot_id, overrides={})


def remix(project_id: str, shot_id: str, overrides: Dict[str, Any]) -> ImageAsset:
    """Regenerate with selected fields overridden and the rest locked.

    Recognised override keys: base_prompt, background, seed, model, instruction (edit),
    randomize_seed (bool — pick a fresh seed when no explicit seed is given).
    """
    return _regenerate(project_id, shot_id, overrides=overrides or {})


def _regenerate(project_id: str, shot_id: str, *, overrides: Dict[str, Any]) -> ImageAsset:
    st = state_store.load(project_id)
    if st is None:
        raise ValueError("project not found")
    target = st.image_asset(shot_id)
    if target is None:
        raise ValueError(f"image {shot_id} not found")
    r = target.recipe
    if r is None:
        raise ValueError(f"image {shot_id} has no recipe (regenerate it once to capture one)")

    model = overrides.get("model") or r.model
    background = overrides.get("background", r.background)
    base_prompt = overrides.get("base_prompt") or r.base_prompt
    aspect = r.aspect_ratio or (st.adspec.aspect_ratio if st.adspec else "9:16")

    # Seed resolution: explicit override > randomize > the recipe's recorded seed.
    if overrides.get("seed") is not None:
        seed = int(overrides["seed"]) & recipe_mod.SEED_MAX
    elif overrides.get("randomize_seed"):
        seed = recipe_mod.new_seed()
    else:
        seed = r.seed if r.seed is not None else recipe_mod.new_seed()

    if r.kind == "edit":
        instruction = overrides.get("instruction") or r.instruction
        cur = read_asset_bytes(project_id, target.url)
        if cur is None:
            raise ValueError("current image bytes missing")
        hero_bytes = read_asset_bytes(project_id, st.hero_image.url) if st.hero_image else None
        refs = consistency.build_references(hero_bytes) if (r.ref == "hero" and shot_id != "hero") else []
        result = get_image_provider_by_name(model).edit_image(
            cur, instruction, reference_images=refs or None, base_prompt=base_prompt, seed=seed)
        final_prompt = base_prompt
        new_recipe = r.model_copy(update={
            "provider": result.provider, "model": model, "seed": seed,
            "base_prompt": base_prompt, "prompt": base_prompt, "instruction": instruction,
            "created_at": recipe_mod.now_iso(),
        })
    else:
        # base_prompt already carries the consistency suffix; only (re-)apply the background.
        final_prompt = backgrounds.apply(base_prompt, background)
        if shot_id == "hero":
            refs = _hero_product_refs(project_id, st)
        elif r.ref == "hero" and st.hero_image:
            hb = read_asset_bytes(project_id, st.hero_image.url)
            refs = [hb] if hb else []
        else:
            refs = []
        result = generate_with_fallback(final_prompt, aspect, refs or None, seed=seed, model=model)
        new_recipe = r.model_copy(update={
            "provider": result.provider, "model": model, "background": background,
            "base_prompt": base_prompt, "prompt": final_prompt, "seed": seed,
            "aspect_ratio": aspect, "created_at": recipe_mod.now_iso(),
        })

    rev = target.revision + 1
    url = save_image_with_recipe(project_id, f"{shot_id}_v{rev}.png", result.image_bytes, new_recipe)
    target.url = url
    target.revision = rev
    target.provider = result.provider
    target.prompt = final_prompt
    target.seed = seed
    target.model = model
    target.background = background
    target.recipe = new_recipe
    target.status = StepStatus.generated  # changed → needs re-approval

    if shot_id == "hero":
        st.hero_image = target
        st.set_step_status(PipelineStep.hero_image, StepStatus.generated)
        st.current_step = PipelineStep.hero_image
    else:
        st.set_step_status(PipelineStep.shot_images, StepStatus.generated)
        st.current_step = PipelineStep.shot_images
    state_store.save(st)
    return target
