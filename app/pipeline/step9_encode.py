"""9단계 — 최종 인코딩 & 다운로드 (Phase 2, CLAUDE.md §4.9).

ffmpeg 인코딩: H.264/MP4, 플랫폼별 프리셋(9:16 등). (선택) 업스케일.
static_image 는 8단계 출력이 곧 최종 → 다운로드만 제공.
"""
from __future__ import annotations

from typing import List

from app.core import assembly, composite
from app.core import state as state_store
from app.core.schemas import ExportVariant
from app.pipeline import read_asset_bytes

# 플랫폼별 권장 비율 (멀티 export 기본값)
DEFAULT_RATIOS = ["9:16", "1:1", "4:5"]


def final_outputs(project_id: str):
    st = state_store.load(project_id)
    if st is None:
        raise ValueError("project not found")
    return st.final_outputs


def run(project_id: str):
    st = state_store.load(project_id)
    if st is None:
        raise ValueError("project not found")
    # static_image: 이미 PNG 최종본. video: 8단계 render_video 가 이미
    # encode_preset(플랫폼 프리셋) 까지 수행하므로 최종본을 그대로 반환.
    return st.final_outputs


def upscale_output(project_id: str, factor: int = 2) -> ExportVariant:
    """Upscale the latest final output (CLAUDE.md §9, opt-in). Lanczos offline, Topaz with a key."""
    from app.providers.upscale import UpscaleProvider

    st = state_store.load(project_id)
    if st is None:
        raise ValueError("project not found")
    if not st.final_outputs:
        raise ValueError("No final output yet (run step 8 first).")
    if factor not in (2, 4):
        raise ValueError("factor must be 2 or 4")

    src_url = st.final_outputs[-1]
    is_video = src_url.endswith(".mp4")
    prov = UpscaleProvider()
    base = len([e for e in st.exports if e.ratio.startswith("upscaled")])
    if is_video:
        if not assembly.ffmpeg_available():
            raise RuntimeError("ffmpeg not installed — cannot upscale video")
        src = state_store.assets_dir(project_id) / src_url.rsplit("/", 1)[-1]
        out = state_store.assets_dir(project_id) / f"upscaled_{base}.mp4"
        prov.upscale_video(src, out, factor)
        url = f"/storage/projects/{project_id}/assets/{out.name}"
        variant = ExportVariant(ratio=f"upscaled {factor}x", url=url, kind="video")
    else:
        data = read_asset_bytes(project_id, src_url)
        up = prov.upscale_image(data, factor)
        from app.pipeline import save_asset

        url = save_asset(project_id, f"upscaled_{base}.png", up)
        variant = ExportVariant(ratio=f"upscaled {factor}x", url=url, kind="image")
    st.exports.append(variant)
    state_store.save(st)
    return variant


def export_variants(project_id: str, ratios: List[str] = None) -> List[ExportVariant]:
    """최종 산출물을 여러 비율로 리사이즈 export — "one ad → all placements".

    static: Pillow object-cover. video: ffmpeg scale+crop. 오프라인 동작.
    """
    ratios = ratios or DEFAULT_RATIOS
    # ratio reaches a filesystem path (export_{i}_{ratio}.mp4) — the video branch
    # bypasses save_asset, so an unvalidated ratio like '../../x' would write
    # outside storage. Restrict to the known aspect set.
    bad = [r for r in ratios if r not in assembly.ASPECT_WH]
    if bad:
        raise ValueError(f"Unsupported export ratio(s): {bad} (allowed: {sorted(assembly.ASPECT_WH)})")

    st = state_store.load(project_id)
    if st is None:
        raise ValueError("project not found")
    if not st.final_outputs:
        raise ValueError("No final output yet (run step 8 first).")

    src_url = st.final_outputs[-1]
    is_video = src_url.endswith(".mp4")
    if is_video and not assembly.ffmpeg_available():
        raise RuntimeError("ffmpeg not installed — cannot export video")

    src_bytes = read_asset_bytes(project_id, src_url)
    src_path = state_store.assets_dir(project_id) / src_url.rsplit("/", 1)[-1]
    base = len(st.exports)
    variants: List[ExportVariant] = []
    from app.pipeline import _safe_asset_path, fetch_image_bytes, save_asset

    # Static images RE-COMPOSITE per ratio (headline/CTA re-placed for that aspect) so
    # vertical-format text isn't cropped off. Needs the original render recipe + base image.
    recipe = (st.adspec.last_render if st.adspec else None) if not is_video else None
    recipe_base = read_asset_bytes(project_id, recipe["base_url"]) if recipe else None
    logo_bytes = None
    if recipe and recipe.get("show_logo") and st.adspec.brand.logo_url:
        logo_bytes = fetch_image_bytes(st.adspec.brand.logo_url)

    adir = state_store.assets_dir(project_id)
    for i, ratio in enumerate(ratios):
        w, h = assembly.wh_for(ratio)
        safe = ratio.replace(":", "x")  # ratio is now allowlisted → safe token
        if is_video:
            out = _safe_asset_path(adir, f"export_{base+i}_{safe}.mp4")  # defense-in-depth
            assembly.resize_cover_video(src_path, out, w, h)
            url = f"/storage/projects/{project_id}/assets/{out.name}"
            variants.append(ExportVariant(ratio=ratio, url=url, kind="video"))
        else:
            if recipe_base is not None:
                base_r = composite.resize_cover(recipe_base, w, h)   # base photo at target ratio
                data = composite.compose(base_r, headline=recipe.get("headline", ""),
                                         cta=recipe.get("cta", ""), logo_bytes=logo_bytes,
                                         palette=st.adspec.brand.palette or None)
            else:
                data = composite.resize_cover(src_bytes, w, h)        # fallback: crop the final
            url = save_asset(project_id, f"export_{base+i}_{safe}.png", data)
            variants.append(ExportVariant(ratio=ratio, url=url, kind="image"))

    st.exports.extend(variants)
    state_store.save(st)
    return variants
