"""6단계 — 콘티별 이미지 수정 (CLAUDE.md §4.6 — 사람 개입 루프).

각 이미지를 자연어 프롬프트로 편집(업로드 + "이 부분 이렇게").
모델: Nano Banana 2 (이미지 편집 모드).
▶ Phase 1(static_image) 은 여기서 종료 → 8단계 텍스트/로고 합성만 거쳐 출력.

승인 게이트: approve_all() 이 전체 컷 승인 → 7단계(영상) 게이팅 해제 조건.
"""
from __future__ import annotations

from typing import List, Optional

from app.core import consistency
from app.core import recipe as recipe_mod
from app.core import state as state_store
from app.core.schemas import GenerationRecipe, ImageAsset, PipelineStep, StepStatus
from app.pipeline import read_asset_bytes, save_image_with_recipe
from app.providers.registry import get_image_provider_by_name


def _source_prompt(st, shot_id: str, target: ImageAsset) -> str:
    """The generation prompt that produced the image being edited — used to keep a text→image
    edit anchored to the same scene. Prefer the asset's own prompt, then the storyboard shot's
    image prompt, then the hero/main image prompt."""
    if (getattr(target, "prompt", "") or "").strip():
        return target.prompt
    sb = getattr(st, "storyboard", None)
    if sb and sb.shots:
        for s in sb.shots:
            if s.id == shot_id:
                return s.image_prompt or s.description or ""
    if st.hero_image and (st.hero_image.prompt or "").strip():
        return st.hero_image.prompt
    return ""


def edit_shot(project_id: str, shot_id: str, instruction: str,
              model: Optional[str] = None) -> ImageAsset:
    st = state_store.load(project_id)
    if st is None:
        raise ValueError("project not found")
    target = st.image_asset(shot_id)
    if target is None:
        raise ValueError(f"shot image {shot_id} not found")

    cur = read_asset_bytes(project_id, target.url)
    if cur is None:
        raise ValueError("current image bytes missing")

    hero_bytes = read_asset_bytes(project_id, st.hero_image.url) if st.hero_image else None
    refs = consistency.build_references(hero_bytes) if shot_id != "hero" else []

    # The source image's own generation prompt + a stable per-shot seed, so a text→image
    # provider (free Pollinations) re-renders the SAME scene + the requested change instead of
    # a brand-new image/person. Real img2img providers ignore these and edit the bytes directly.
    base_prompt = _source_prompt(st, shot_id, target)
    seed = consistency.project_seed(project_id) ^ (abs(hash(shot_id)) & 0xFFFFFFFF)

    result = get_image_provider_by_name(model).edit_image(
        cur, instruction, reference_images=refs or None, base_prompt=base_prompt, seed=seed)
    rev = target.revision + 1
    recipe = GenerationRecipe(
        kind="edit", shot_id=shot_id, base_prompt=base_prompt, prompt=base_prompt,
        provider=result.provider, model=(model or "auto"),
        aspect_ratio=(st.adspec.aspect_ratio if st.adspec else "9:16"),
        seed=seed, instruction=instruction,
        ref=("hero" if (refs and shot_id != "hero") else ""), created_at=recipe_mod.now_iso(),
    )
    url = save_image_with_recipe(project_id, f"{shot_id}_v{rev}.png", result.image_bytes, recipe)

    target.url = url
    target.revision = rev
    target.provider = result.provider
    target.status = StepStatus.generated  # 편집했으니 재승인 필요
    target.seed = seed
    if model:
        target.model = model
    target.recipe = recipe

    if shot_id == "hero":
        st.hero_image = target
    st.set_step_status(PipelineStep.image_edit, StepStatus.generated)
    st.current_step = PipelineStep.image_edit
    state_store.save(st)
    return target


def approve_shot(project_id: str, shot_id: str) -> ImageAsset:
    st = state_store.load(project_id)
    if st is None:
        raise ValueError("project not found")
    target = st.image_asset(shot_id)
    if target is None:
        raise ValueError("shot not found")
    target.status = StepStatus.approved
    state_store.save(st)
    return target


def approve_all(project_id: str) -> List[ImageAsset]:
    """전체 컷 승인 — 7단계 영상 게이팅 해제 조건 (CLAUDE.md §2.5)."""
    st = state_store.load(project_id)
    if st is None or not st.shot_images:
        raise ValueError("no shot images")
    for a in st.shot_images:
        a.status = StepStatus.approved
    st.set_step_status(PipelineStep.image_edit, StepStatus.approved)
    state_store.snapshot(st, PipelineStep.image_edit, label="all shots approved")
    return st.shot_images


def all_approved(st) -> bool:
    return bool(st.shot_images) and all(
        a.status == StepStatus.approved for a in st.shot_images
    )
