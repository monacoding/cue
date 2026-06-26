"""4단계 — 대표이미지(Hero) 생성 (CLAUDE.md §4.4).

콘티 핵심 컷의 대표 이미지 1장 생성 → 사람이 프롬프트로 반복해 원하는 룩 고정.
이후 모든 컷의 일관성 기준(hero_image)이 된다.
모델: Nano Banana 2 (fallback Flux). 제품 이미지를 레퍼런스로 활용.
"""
from __future__ import annotations

from typing import Optional

from app.core import backgrounds
from app.core import recipe as recipe_mod
from app.core import state as state_store
from app.core.schemas import GenerationRecipe, ImageAsset, PipelineStep, StepStatus
from app.pipeline import resolve_image_bytes, save_image_with_recipe
from app.providers.registry import generate_with_fallback


def _hero_prompt(st) -> str:
    spec = st.adspec
    if st.storyboard and st.storyboard.shots:
        base = st.storyboard.shots[0].image_prompt
        if base:
            return base
    tone = spec.tone or "sleek and modern"
    return (
        f"{tone} hero image for a commercial ad. {spec.product.name}. "
        f"{spec.product.key_message}. {spec.aspect_ratio} composition, "
        f"premium advertising look, high quality, photorealistic."
    )


def run(project_id: str, prompt_override: Optional[str] = None,
        model: Optional[str] = None, background: Optional[str] = None,
        seed: Optional[int] = None) -> ImageAsset:
    st = state_store.load(project_id)
    if st is None or st.adspec is None:
        raise ValueError("AdSpec required")
    spec = st.adspec

    base_prompt = prompt_override or _hero_prompt(st)
    prompt = backgrounds.apply(base_prompt, background)

    # 제품 이미지가 있으면 레퍼런스로 (룩 고정 도움)
    refs = []
    for u in spec.product.image_urls[:3]:
        b = resolve_image_bytes(project_id, u)   # local upload OR remote URL
        if b:
            refs.append(b)

    # The app owns the seed (ComfyUI-style) so the result is always reproducible afterwards.
    seed = recipe_mod.new_seed() if seed is None else seed
    result = generate_with_fallback(prompt, spec.aspect_ratio, refs or None, seed=seed, model=model)

    rev = (st.hero_image.revision + 1) if st.hero_image else 0
    recipe = GenerationRecipe(
        kind="image", shot_id="hero", base_prompt=base_prompt, prompt=prompt,
        provider=result.provider, model=(model or "auto"), background=(background or ""),
        aspect_ratio=spec.aspect_ratio, seed=seed, created_at=recipe_mod.now_iso(),
    )
    url = save_image_with_recipe(project_id, f"hero_v{rev}.png", result.image_bytes, recipe)
    st.hero_image = ImageAsset(
        shot_id="hero",
        url=url,
        prompt=prompt,
        provider=result.provider,
        status=StepStatus.generated,
        revision=rev,
        seed=seed,
        model=(model or "auto"),
        background=(background or ""),
        recipe=recipe,
    )
    st.set_step_status(PipelineStep.hero_image, StepStatus.generated)
    st.current_step = PipelineStep.hero_image
    state_store.save(st)
    return st.hero_image


def approve(project_id: str) -> ImageAsset:
    st = state_store.load(project_id)
    if st is None or st.hero_image is None:
        raise ValueError("hero image not found")
    st.hero_image.status = StepStatus.approved
    st.set_step_status(PipelineStep.hero_image, StepStatus.approved)
    state_store.snapshot(st, PipelineStep.hero_image, label="hero approved")
    return st.hero_image
