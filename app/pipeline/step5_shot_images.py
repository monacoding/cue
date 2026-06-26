"""5단계 — 콘티별 이미지 생성 (CLAUDE.md §4.5 — 엔지니어링 난이도 90%).

각 컷 이미지를 hero_image 를 reference 로 넣어 일관성 유지하며 생성.
일관성 로직은 core/consistency.py 로 분리(전략 교체 가능).
모델: Nano Banana 2 (최대 14 ref).
"""
from __future__ import annotations

from typing import Callable, List, Optional

from app.core import backgrounds
from app.core import consistency
from app.core import recipe as recipe_mod
from app.core import state as state_store
from app.core.schemas import GenerationRecipe, ImageAsset, PipelineStep, StepStatus
from app.pipeline import read_asset_bytes, save_image_with_recipe
from app.providers.registry import get_image_provider_by_name


def run(
    project_id: str,
    only_shot_id: Optional[str] = None,
    progress_cb: Optional[Callable[[float], None]] = None,
    strategy: str = "reference",
    model: Optional[str] = None,
    background: Optional[str] = None,
    seed: Optional[int] = None,
) -> List[ImageAsset]:
    st = state_store.load(project_id)
    if st is None or st.storyboard is None:
        raise ValueError("storyboard required")
    if st.hero_image is None:
        raise ValueError("hero image required (run step 4 first)")

    spec = st.adspec
    strat = consistency.coerce(strategy)
    hero_bytes = read_asset_bytes(project_id, st.hero_image.url)
    # consistency method is swappable (CLAUDE.md §4.5): reference / seed / edit
    refs = consistency.build_references(hero_bytes, strategy=strat)
    suffix = consistency.consistency_prompt_suffix(strat)
    provider = get_image_provider_by_name(model)
    ref_anchor = "hero" if (strat == consistency.ConsistencyStrategy.reference and hero_bytes) else ""

    targets = [s for s in st.storyboard.shots if not only_shot_id or s.id == only_shot_id]
    total = max(1, len(targets))
    existing = {a.shot_id: a for a in st.shot_images}
    for idx, shot in enumerate(targets):
        base_prompt = (shot.image_prompt or shot.description) + suffix
        prompt = backgrounds.apply(base_prompt, background)
        # App-owned seed (ComfyUI-style): the SEED strategy locks the project seed across shots;
        # otherwise each shot gets its own recorded seed. An explicit `seed` (reproduce/remix a
        # single shot) overrides both so the exact frame can be recreated.
        if seed is not None:
            shot_seed = seed
        elif strat == consistency.ConsistencyStrategy.seed:
            shot_seed = consistency.project_seed(project_id)
        else:
            shot_seed = recipe_mod.new_seed()
        if strat == consistency.ConsistencyStrategy.edit and hero_bytes:
            # derive each shot by editing the hero (consistency via shared base)
            result = provider.edit_image(hero_bytes, prompt)
        else:
            # reference: hero as ref image; seed: locked seed, no ref
            result = provider.generate_image(prompt, spec.aspect_ratio,
                                             reference_images=refs or None, seed=shot_seed)
        prev = existing.get(shot.id)
        rev = (prev.revision + 1) if prev else 0
        recipe = GenerationRecipe(
            kind="image", shot_id=shot.id, base_prompt=base_prompt, prompt=prompt,
            provider=result.provider, model=(model or "auto"), background=(background or ""),
            aspect_ratio=spec.aspect_ratio, seed=shot_seed, strategy=strat.value,
            ref=ref_anchor, created_at=recipe_mod.now_iso(),
        )
        url = save_image_with_recipe(project_id, f"{shot.id}_v{rev}.png", result.image_bytes, recipe)
        existing[shot.id] = ImageAsset(
            shot_id=shot.id,
            url=url,
            prompt=prompt,
            provider=result.provider,
            status=StepStatus.generated,
            revision=rev,
            seed=shot_seed,
            model=(model or "auto"),
            background=(background or ""),
            recipe=recipe,
        )
        if progress_cb:
            progress_cb((idx + 1) / total)

    # storyboard 순서 유지
    order = [s.id for s in st.storyboard.shots]
    st.shot_images = [existing[i] for i in order if i in existing]
    st.set_step_status(PipelineStep.shot_images, StepStatus.generated)
    st.current_step = PipelineStep.shot_images
    state_store.save(st)
    return st.shot_images
