"""8단계 — 오디오 + 편집(조립) (CLAUDE.md §4.8).

결정론적 조립: 한글 텍스트·CTA·로고 합성(Pillow). Phase 1 static_image 의 최종 산출.
video 포맷(Phase 2): ffmpeg 컷 결합 + 오디오 믹스 (core/assembly.py).
"""
from __future__ import annotations

from typing import Optional

from app.core import composite
from app.core import state as state_store
from app.core.schemas import (
    OutputFormat,
    PipelineStep,
    RenderRequest,
    StepStatus,
)
from app.pipeline import fetch_image_bytes, pick_cta, read_asset_bytes, save_asset


def _default_headline(st, shot_id=None) -> str:
    """콘티 카피를 헤드라인 기본값으로 — 컨셉이 구동한 광고 카피를 최종에 자동 반영."""
    sb = st.storyboard
    if sb and sb.shots:
        if shot_id:
            s = next((x for x in sb.shots if x.id == shot_id), None)
            if s and s.on_screen_text.text_ko.strip():
                return s.on_screen_text.text_ko
        # 첫 컷(훅)의 on_screen_text = 선택 컨셉 hook
        hook = sb.shots[0].on_screen_text.text_ko.strip()
        if hook:
            return hook
    return st.adspec.product.key_message


def _default_cta(st) -> str:
    """콘티의 CTA 컷 카피를 CTA 기본값으로 — 없으면 브랜드명 + 행동 동사로 합성 (공유 헬퍼)."""
    name = (st.adspec.product.name if st.adspec and st.adspec.product else "") or ""
    sb = st.storyboard
    shots_text = [s.on_screen_text.text_ko.strip() for s in sb.shots] if sb and sb.shots else []
    headline = shots_text[0] if shots_text else ""
    return pick_cta(shots_text, headline, name)


def render_static(project_id: str, req: RenderRequest) -> str:
    """static_image 최종 합성 → 출력 URL."""
    st = state_store.load(project_id)
    if st is None or st.adspec is None:
        raise ValueError("project/spec not found")

    # 베이스 이미지 선택: 지정 shot 또는 hero 또는 첫 컷
    base_url = None
    if req.shot_id:
        a = next((x for x in st.shot_images if x.shot_id == req.shot_id), None)
        base_url = a.url if a else None
    if base_url is None and st.hero_image:
        base_url = st.hero_image.url
    if base_url is None and st.shot_images:
        base_url = st.shot_images[0].url
    if base_url is None:
        raise ValueError("No base image to compose (run step 4/5 first).")

    base_bytes = read_asset_bytes(project_id, base_url)
    if base_bytes is None:
        raise ValueError("base image bytes missing")

    # 헤드라인/CTA 기본값: 콘티 카피(컨셉 구동) → spec 순으로 유도
    headline = req.headline or _default_headline(st, req.shot_id)
    cta = req.cta or _default_cta(st)

    logo_bytes: Optional[bytes] = None
    if req.show_logo and st.adspec.brand.logo_url:
        logo_bytes = fetch_image_bytes(st.adspec.brand.logo_url)

    out_bytes = composite.compose(
        base_bytes,
        headline=headline,
        cta=cta,
        overlays=req.overlays,
        logo_bytes=logo_bytes,
        palette=st.adspec.brand.palette or None,
    )

    idx = len(st.final_outputs) + 1
    url = save_asset(project_id, f"final_{idx}.png", out_bytes)
    st.final_outputs.append(url)
    # remember the recipe so multi-ratio export can RE-COMPOSITE per ratio (re-place the
    # headline/CTA) instead of cropping this 9:16 output and losing the text.
    st.adspec.last_render = {"base_url": base_url, "headline": headline, "cta": cta,
                             "show_logo": bool(req.show_logo)}
    st.set_step_status(PipelineStep.assemble, StepStatus.generated)
    st.current_step = PipelineStep.assemble
    state_store.save(st)
    return url


def render_variants(project_id: str, n: int = 3):
    """A/B variants: render the static ad with each top-N concept's hook as the headline,
    so the user can compare which hook converts (CLAUDE.md §1 ad intelligence)."""
    from app.core.schemas import ABVariant, OutputFormat
    from app.eval import concept_eval

    n = max(1, min(6, n))
    st = state_store.load(project_id)
    if st is None or st.adspec is None:
        raise ValueError("project/spec not found")
    if st.format != OutputFormat.static_image:
        raise ValueError("A/B variants are available for static_image projects.")

    concepts = st.concepts or concept_eval.evaluate(project_id, max(n, 3))
    if not concepts:
        raise ValueError("No concepts to vary — evaluate concepts first.")

    # base image: hero or first shot
    base_url = (st.hero_image.url if st.hero_image else
                (st.shot_images[0].url if st.shot_images else None))
    if base_url is None:
        raise ValueError("No base image (run step 4/5 first).")
    base_bytes = read_asset_bytes(project_id, base_url)
    if base_bytes is None:
        raise ValueError("base image bytes missing")

    logo_bytes = None
    if st.adspec.brand.logo_url:
        logo_bytes = fetch_image_bytes(st.adspec.brand.logo_url)
    cta = _default_cta(st)

    variants = []
    for c in concepts[:n]:
        out_bytes = composite.compose(
            base_bytes, headline=c.hook, cta=cta,
            logo_bytes=logo_bytes, palette=st.adspec.brand.palette or None,
        )
        url = save_asset(project_id, f"variant_{c.id}.png", out_bytes)
        variants.append(ABVariant(concept_id=c.id, hook=c.hook, angle=c.angle,
                                  url=url, score=c.score))
    st.ab_variants = variants
    state_store.save(st)
    return variants


def select_variant(project_id: str, concept_id: str):
    """Pick an A/B variant as the winning ad — it becomes a final output (versioned)."""
    st = state_store.load(project_id)
    if st is None:
        raise ValueError("project not found")
    v = next((x for x in st.ab_variants if x.concept_id == concept_id), None)
    if v is None:
        raise ValueError(f"variant {concept_id!r} not found")
    st.final_outputs.append(v.url)
    st.set_step_status(PipelineStep.assemble, StepStatus.generated)
    state_store.save(st)
    state_store.snapshot(st, PipelineStep.assemble, label=f"A/B winner: {v.hook[:40]}")
    return v


def render_video(project_id: str, req: RenderRequest) -> str:
    """video 포맷 조립 — 컷 클립을 xfade 로 결합 → 최종 mp4 (CLAUDE.md §4.8)."""
    import tempfile
    from pathlib import Path

    from app.core import assembly
    from app.providers.registry import get_audio_provider

    st = state_store.load(project_id)
    if st is None or st.adspec is None:
        raise ValueError("project/spec not found")
    if not st.shot_videos:
        raise ValueError("No shot clips (run step 7 first).")
    if not assembly.ffmpeg_available():
        raise RuntimeError("ffmpeg/ffprobe not installed — cannot assemble video")

    shots_by_id = {s.id: s for s in (st.storyboard.shots if st.storyboard else [])}
    clips, durs, transitions = [], [], []
    for v in st.shot_videos:
        local = read_asset_bytes(project_id, v.url)
        if local is None:
            continue
        p = state_store.assets_dir(project_id) / v.url.rsplit("/", 1)[-1]
        clips.append(p)
        shot = shots_by_id.get(v.shot_id)
        # xfade offsets must use the ACTUAL clip length: real providers (Seedance/Kling/Veo)
        # quantize duration (ask 5s, get 5.2s), so trusting shot.duration_sec drifts the
        # transition offsets across junctions. Probe the file; fall back to the spec value.
        probed = assembly.probe_duration(p)
        durs.append(probed if probed > 0 else (float(shot.duration_sec) if shot else 3.0))
        transitions.append(shot.transition if shot else "crossfade")
    if not clips:
        raise ValueError("No valid clips")

    idx = len(st.final_outputs) + 1
    out_path = state_store.assets_dir(project_id) / f"final_{idx}.mp4"
    with tempfile.TemporaryDirectory() as td:
        joined = Path(td) / "joined.mp4"
        encoded = Path(td) / "encoded.mp4"
        # honor per-shot transitions (cut/crossfade/whip_pan) from the storyboard
        assembly.crossfade_clips(clips, durs, joined, fps=30, transitions=transitions)
        # 9단계 플랫폼 프리셋 인코딩
        assembly.encode_preset(joined, encoded, platform=st.adspec.platform)

        # 오디오: 음악 베드 (키 있으면 ElevenLabs, 없으면 절차적 베드) 믹스
        total = assembly.probe_duration(encoded)
        mood = st.adspec.audio.music_mood or st.adspec.tone
        audio_path = None
        if not getattr(st.adspec.audio, "music_muted", False):
            audio_res = get_audio_provider(st.adspec.audio.music_model).music(mood, total)
            if audio_res.audio_bytes:
                audio_path = Path(td) / "music.m4a"
                audio_path.write_bytes(audio_res.audio_bytes)
        assembly.mux_audio(encoded, audio_path, out_path,
                           volume=getattr(st.adspec.audio, "music_volume", 1.0))

    url = f"/storage/projects/{project_id}/assets/final_{idx}.mp4"
    st.final_outputs.append(url)
    st.set_step_status(PipelineStep.assemble, StepStatus.generated)
    st.current_step = PipelineStep.assemble
    state_store.save(st)
    return url


def run(project_id: str, req: RenderRequest) -> str:
    st = state_store.load(project_id)
    if st is None:
        raise ValueError("project not found")
    if st.format == OutputFormat.static_image:
        return render_static(project_id, req)
    return render_video(project_id, req)


def approve(project_id: str) -> str:
    st = state_store.load(project_id)
    if st is None or not st.final_outputs:
        raise ValueError("no output to approve")
    st.set_step_status(PipelineStep.assemble, StepStatus.approved)
    state_store.snapshot(st, PipelineStep.assemble, label="final approved")
    return st.final_outputs[-1]
