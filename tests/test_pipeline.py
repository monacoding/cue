"""Cue 핵심 동작 테스트 (mock 모드 — 키 불필요).

실행: source .venv/bin/activate && python -m pytest -q
파이프라인 로직 + 비용 게이팅 + 영속/버전 + 제품 편집 영속 + HTTP 계층을 커버.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import state as state_store  # noqa: E402
from app.core.schemas import AdSpec, RenderRequest, SpecRequest  # noqa: E402
from app.eval import concept_eval  # noqa: E402
from app.pipeline import (  # noqa: E402
    step1_brief,
    step2_spec,
    step3_storyboard,
    step4_hero_image,
    step5_shot_images,
    step6_image_edit,
    step7_shot_video,
    step8_assemble,
)


@pytest.fixture
def pid():
    st = state_store.create_project("테스트", fmt="static_image")
    yield st.project_id
    state_store.delete_project(st.project_id)


# --------------------------------------------------------------------------
def test_shot_count_derivation():
    assert AdSpec.derive_shot_count(15) == 5
    assert AdSpec.derive_shot_count(3) == 3      # 최소 3
    assert AdSpec.derive_shot_count(60) == 12    # 최대 12
    assert AdSpec.derive_shot_count(9) == 3


def test_spec_derives_shot_count(pid):
    step1_brief.run(pid, product_text="테스트 제품. 핵심 가치.")
    spec = step2_spec.run(pid, SpecRequest(duration_sec=30))
    assert spec.shot_count == AdSpec.derive_shot_count(30)
    assert spec.duration_sec == 30


def test_cost_estimate(pid):
    """Cost estimate reflects format/shot_count/duration (CLAUDE.md §2.5/§5)."""
    from app.core import pricing

    step1_brief.run(pid, product_text="X.")
    step2_spec.run(pid, SpecRequest(duration_sec=9))  # static, 3 shots
    st = state_store.load(pid)
    est = pricing.estimate(st)
    # static: orchestration + images (hero + 3 shots), no video/music
    assert est["images"] == round(4 * pricing.IMAGE_PER, 2)
    assert "video" not in est
    assert est["total"] == round(est["orchestration"] + est["images"], 2)


def test_cost_estimate_video_and_endpoint():
    from fastapi.testclient import TestClient

    from app.core import pricing
    from app.main import app

    client = TestClient(app)
    pid = client.post("/api/projects", json={"title": "c", "format": "ugc_video",
                                             "product_text": "X."}).json()["project_id"]
    try:
        client.post(f"/api/projects/{pid}/spec", json={"duration_sec": 10})
        r = client.get(f"/api/projects/{pid}/cost-estimate")
        assert r.status_code == 200
        body = r.json()
        # video step bills per CLIP (each ≥ the model minimum), not raw total duration:
        # 10s / 3 shots → 3 clips × max(4, 3.33) = 12 billable seconds
        billable = pricing.billable_video_sec(3, 10)
        assert billable == 12.0
        assert body["video_step"]["billable_sec"] == 12.0
        assert body["video_step"]["video"] == round(billable * pricing.VIDEO_PER_SEC, 2)
        assert body["project"]["total"] > body["video_step"]["total"]  # project includes images too
    finally:
        client.delete(f"/api/projects/{pid}")


def test_billable_video_sec_accounts_for_clip_minimum():
    """Cost estimate must not under-report: short shots are billed at the clip minimum."""
    from app.core import pricing

    # 9s / 3 shots → each 3s shot billed at the 4s floor → 12s, not 9s
    assert pricing.billable_video_sec(3, 9) == 12.0
    # long shots above the floor bill at their real length
    assert pricing.billable_video_sec(2, 16) == 16.0   # 8s each > 4s floor
    # degenerate n → falls back to raw duration (no divide-by-zero)
    assert pricing.billable_video_sec(0, 10) == 10.0


def test_cost_safety_caps(pid):
    """shot_count/duration 상한으로 런어웨이 비용 방지 (CLAUDE.md §2.5)."""
    from app.pipeline.step2_spec import MAX_DURATION_SEC, MAX_SHOT_COUNT

    step1_brief.run(pid, product_text="제품.")
    spec = step2_spec.run(pid, SpecRequest(shot_count=1000, duration_sec=99999))
    assert spec.shot_count == MAX_SHOT_COUNT
    assert spec.duration_sec == MAX_DURATION_SEC
    spec2 = step2_spec.run(pid, SpecRequest(shot_count=0))
    assert spec2.shot_count == 1  # 하한


def test_invalid_spec_inputs_rejected(pid):
    step1_brief.run(pid, product_text="제품.")
    with pytest.raises(ValueError):
        step2_spec.run(pid, SpecRequest(aspect_ratio="3:2"))   # 미지원 비율
    with pytest.raises(ValueError):
        step2_spec.run(pid, SpecRequest(platform="facebook_x"))  # 미지원 플랫폼


def test_product_edit_persists_verbatim(pid):
    """사람 수정값이 재추출 없이 그대로 보존되는지 (회귀 방지)."""
    step1_brief.run(pid, product_text="원래 자동 추출된 잡다한 텍스트들. 여러 문장. 더 많이.")
    spec = step1_brief.update_product(
        pid, name="정확한 제품명", description="사람이 쓴 설명", key_message="핵심 한 줄"
    )
    assert spec.product.name == "정확한 제품명"
    assert spec.product.key_message == "핵심 한 줄"
    # 디스크에서 다시 로드해도 보존
    reloaded = state_store.load(pid)
    assert reloaded.adspec.product.name == "정확한 제품명"


def test_hero_prompt_is_english(pid):
    """Hero prompt (sent to the image model) must be English — Korean degrades Qwen/Flux output.
    Covers the fallback path (no storyboard yet)."""
    from app.pipeline import step4_hero_image

    step1_brief.run(pid, product_text="AuraPods. 40-hour battery.")
    step2_spec.run(pid, SpecRequest(duration_sec=9, aspect_ratio="16:9"))
    st = state_store.load(pid)
    assert st.storyboard is None                      # force the fallback branch
    prompt = step4_hero_image._hero_prompt(st)
    assert prompt.isascii()                           # no Korean / non-ASCII
    assert "vertical" not in prompt.lower()           # no hardcoded orientation (was a bug for 16:9)
    assert st.adspec.product.name in prompt


def test_brief_heuristic_splits_name_from_key_message(pid):
    """The witnessed walkthrough fix: name ≠ key_message; a benefit sentence is the key."""
    spec = step1_brief.run(
        pid,
        product_text="Pora — portable espresso maker. Hand-pump, no electricity needed, "
                     "brews a full 9-bar shot in 30 seconds.",
    )
    p = spec.product
    assert p.name == "Pora"                      # brand split off the first line
    assert p.key_message and p.key_message != p.name
    # the key message is the benefit-cue sentence (has a number / 'no ' / 'in seconds')
    assert any(c.isdigit() for c in p.key_message) or "no " in p.key_message.lower()


def test_brief_scrapes_url_html(pid, monkeypatch):
    """URL path: HTML is fetched (SSRF-guarded), title + body extracted, script/style stripped."""
    import app.pipeline as pipeline_pkg

    class _Resp:
        text = ("<html><head><title>AuraPods 40h</title></head>"
                "<body><script>junk()</script><h1>Wireless earbuds</h1>"
                "<p>40-hour battery, ANC.</p></body></html>")

    monkeypatch.setattr(pipeline_pkg, "safe_fetch", lambda *a, **k: _Resp())
    spec = step1_brief.run(pid, product_url="https://example.com/p")
    blob = (spec.product.name + spec.product.description + spec.product.key_message)
    assert "AuraPods" in blob              # title scraped
    assert "junk()" not in blob            # <script> stripped


def test_update_product_edge_cases(pid):
    """update_product: missing project raises; partial updates touch only given fields."""
    with pytest.raises(ValueError):
        step1_brief.update_product("nonexistent-id", name="x")
    step1_brief.run(pid, product_text="Base product. A sentence.")
    before = state_store.load(pid).adspec.product.description
    spec = step1_brief.update_product(pid, image_urls=["https://x/a.png"])
    assert spec.product.image_urls == ["https://x/a.png"]
    assert spec.product.description == before   # untouched field preserved


def test_beat_sync_snaps_durations(pid):
    """Beat-sync snaps shot durations to the music-mood beat grid (§4.8)."""
    from app.core import beatsync

    step1_brief.run(pid, product_text="X.")
    step2_spec.run(pid, SpecRequest(duration_sec=15, music_mood="upbeat"))  # 128 BPM
    step3_storyboard.run(pid)
    synced = step3_storyboard.beat_sync(pid)
    beat = 60.0 / beatsync.bpm_for("upbeat")
    for s in synced.shots:
        beats = s.duration_sec / beat
        assert abs(beats - round(beats)) < 0.02  # each duration is a whole-beat multiple
        assert s.duration_sec >= beat - 0.01     # at least one beat


def test_bpm_for_known_and_default():
    from app.core import beatsync

    assert beatsync.bpm_for("upbeat") == 128
    assert beatsync.bpm_for("") == 110  # neutral default
    assert beatsync.bpm_for("UNKNOWN") == 110


def test_storyboard_shot_count_matches_spec(pid):
    step1_brief.run(pid, product_text="무선 이어버드. 40시간 재생.")
    step2_spec.run(pid, SpecRequest(duration_sec=15))
    sb = step3_storyboard.run(pid)
    st = state_store.load(pid)
    assert len(sb.shots) == st.adspec.shot_count
    # 첫 컷은 훅, 모든 컷에 image_prompt 존재
    assert all(s.image_prompt for s in sb.shots)
    assert all(s.id for s in sb.shots)


def test_concept_eval_ranked(pid):
    step1_brief.run(pid, product_text="스마트 텀블러. 보온 12시간.")
    step2_spec.run(pid, SpecRequest(duration_sec=15))
    concepts = concept_eval.evaluate(pid, n=5)
    assert len(concepts) == 5
    # 점수 내림차순 정렬
    scores = [c.score for c in concepts]
    assert scores == sorted(scores, reverse=True)
    assert all(0 <= c.score <= 1 for c in concepts)


def test_storyboard_parsing_tolerates_malformed_llm(pid, monkeypatch):
    """A real LLM can return off-schema shapes (nested field as string, duration '3s',
    a bare-string shot). Parsing must coerce/skip, not 500."""
    from app.pipeline import step3_storyboard

    step1_brief.run(pid, product_text="Product. A line.")
    step2_spec.run(pid, SpecRequest(duration_sec=9))

    malformed = {"shots": [
        {"description": "ok shot", "duration_sec": "3s",
         "on_screen_text": "Buy now",            # string instead of dict
         "audio_cue": None},                       # null instead of dict
        "a bare string shot",                      # not a dict at all
        {"description": "second", "duration_sec": None},
    ]}

    class _LLM:
        def complete_json(self, *a, **k):
            return malformed

    monkeypatch.setattr(step3_storyboard, "get_llm", lambda: _LLM())
    sb = step3_storyboard.run(pid)                  # must not raise
    assert len(sb.shots) == 2                       # bare-string shot skipped
    assert sb.shots[0].duration_sec == 3.0          # "3s" coerced
    assert sb.shots[1].duration_sec == 3.0          # None → default
    assert sb.shots[0].on_screen_text.text_ko == ""  # string nested field → empty default


def test_concept_score_clarity_handles_empty_key_message():
    """Empty key_message must NOT hand every concept a free clarity boost — `"" in hook`
    is always True, which would inflate (and flatten) scores. Guards a real scoring bug."""
    from types import SimpleNamespace

    from app.eval.concept_eval import _score

    spec_no_msg = SimpleNamespace(
        product=SimpleNamespace(key_message=""), tone="", platform="tiktok")
    s = _score({"hook": "some catchy hook", "angle": "Demo Impact"}, spec_no_msg)
    assert s["clarity"] == 0.5            # base only — no free +0.5 from the empty string
    # a hook that genuinely echoes the key message earns the boost
    spec_msg = SimpleNamespace(
        product=SimpleNamespace(key_message="Brews fast"), tone="", platform="tiktok")
    s2 = _score({"hook": "Brews a full shot fast", "angle": "Demo Impact"}, spec_msg)
    assert s2["clarity"] == 1.0


def test_heuristic_concepts_are_english_without_product_name():
    """With no product name, the heuristic concept hooks must still be English (no '제품')."""
    from types import SimpleNamespace

    from app.eval.concept_eval import _heuristic_concepts

    spec = SimpleNamespace(
        product=SimpleNamespace(name="", key_message=""), platform="tiktok")
    concepts = _heuristic_concepts(spec, 6)
    has_hangul = lambda s: any("가" <= ch <= "힣" for ch in s)  # noqa: E731
    for c in concepts:
        assert not has_hangul(c["hook"] + c["rationale"])   # no Korean (em-dashes etc. are fine)
    # the empty-name fallback is the English "the product", not a Korean placeholder
    assert any("the product" in c["hook"] for c in concepts)


def test_concept_drives_storyboard(pid):
    """선택 컨셉의 hook 이 콘티 첫 컷 카피를 구동 (CLAUDE.md §1 핵심 철학)."""
    step1_brief.run(pid, product_text="블루투스 스피커. 깊은 베이스.")
    step2_spec.run(pid, SpecRequest(duration_sec=9))
    concepts = concept_eval.evaluate(pid, n=5)
    chosen = concepts[2]  # 최고가 아닌 컨셉을 명시 선택
    sb = step3_storyboard.run(pid, concept_id=chosen.id)
    st = state_store.load(pid)
    assert st.selected_concept_id == chosen.id
    # 첫 컷(훅) on_screen_text 가 선택 컨셉 hook 을 반영
    assert sb.shots[0].on_screen_text.text_ko == chosen.hook
    # 컨셉 미지정 시 최고 컨셉(기본)으로 시드
    sb2 = step3_storyboard.run(pid)
    assert state_store.load(pid).selected_concept_id == concepts[0].id
    assert sb2.shots[0].on_screen_text.text_ko == concepts[0].hook
    # 명시한 concept_id 가 없으면 조용히 대체하지 않고 에러 (광고 지능 출력 보호)
    with pytest.raises(ValueError):
        step3_storyboard.run(pid, concept_id="concept_does_not_exist")


def test_images_are_valid_png(pid):
    step1_brief.run(pid, product_text="제품 A.")
    step2_spec.run(pid, SpecRequest(duration_sec=9))  # shot_count 3
    step3_storyboard.run(pid)
    hero = step4_hero_image.run(pid)
    imgs = step5_shot_images.run(pid)
    assert len(imgs) == 3
    # hero 와 컷 이미지가 실제 디코딩 가능한 PNG 인지
    for url in [hero.url] + [a.url for a in imgs]:
        b = (state_store.assets_dir(pid) / url.rsplit("/", 1)[-1]).read_bytes()
        Image.open(io.BytesIO(b)).verify()


def test_shot_images_require_hero(pid):
    step1_brief.run(pid, product_text="제품.")
    step2_spec.run(pid, SpecRequest(duration_sec=9))
    step3_storyboard.run(pid)
    with pytest.raises(ValueError):
        step5_shot_images.run(pid)  # hero 없이 호출 → 차단


def test_edit_hero_via_image_edit(pid):
    """Hero (consistency anchor) can be refined with a natural-language edit."""
    step1_brief.run(pid, product_text="X.")
    step2_spec.run(pid, SpecRequest(duration_sec=9))
    step3_storyboard.run(pid)
    step4_hero_image.run(pid)
    rev0 = state_store.load(pid).hero_image.revision
    edited = step6_image_edit.edit_shot(pid, "hero", "warmer lighting")
    assert edited.shot_id == "hero"
    st = state_store.load(pid)
    assert st.hero_image.revision == rev0 + 1   # hero updated in place
    assert st.hero_image.status == "generated"  # needs re-approval after edit


def test_consistency_coerce_and_seed():
    """Strategy coercion is lenient; project seed is deterministic per-project."""
    from app.core import consistency as c
    assert c.coerce("seed") == c.ConsistencyStrategy.seed
    assert c.coerce("edit") == c.ConsistencyStrategy.edit
    assert c.coerce("bogus") == c.DEFAULT_STRATEGY        # unknown → reference
    assert c.coerce(None) == c.DEFAULT_STRATEGY
    assert c.project_seed("abc") == c.project_seed("abc")  # stable
    assert c.project_seed("abc") != c.project_seed("xyz")  # distinct
    # every strategy has an English (ASCII) consistency instruction; none Korean
    for s in c.ConsistencyStrategy:
        suffix = c.consistency_prompt_suffix(s)
        assert suffix and suffix.isascii()


def test_consistency_references_by_strategy():
    """REFERENCE attaches the hero; SEED/EDIT do not pass it as a reference image."""
    from app.core import consistency as c
    hero = b"heroimg"
    assert c.build_references(hero, strategy=c.ConsistencyStrategy.reference) == [hero]
    assert c.build_references(hero, strategy=c.ConsistencyStrategy.seed) == []
    assert c.build_references(hero, strategy=c.ConsistencyStrategy.edit) == []


@pytest.mark.parametrize("strategy", ["reference", "seed", "edit"])
def test_shot_strategies_produce_images(pid, strategy):
    """All three hero→shot consistency strategies generate valid images (keyless mock)."""
    step1_brief.run(pid, product_text="무선 이어버드. 40시간.")
    step2_spec.run(pid, SpecRequest(duration_sec=9))
    step3_storyboard.run(pid)
    step4_hero_image.run(pid)
    imgs = step5_shot_images.run(pid, strategy=strategy)
    assert imgs and len(imgs) == len(state_store.load(pid).storyboard.shots)
    for a in imgs:
        b = (state_store.assets_dir(pid) / a.url.rsplit("/", 1)[-1]).read_bytes()
        Image.open(io.BytesIO(b)).verify()


def test_edit_increments_revision(pid):
    _full_to_shots(pid)
    st = state_store.load(pid)
    sid = st.shot_images[0].shot_id
    before = st.shot_images[0].revision
    edited = step6_image_edit.edit_shot(pid, sid, "더 밝게")
    assert edited.revision == before + 1
    assert edited.status == "generated"  # 편집 후 재승인 필요


def test_cost_gating(pid):
    """6단계 전체 승인 전 7단계 차단, 승인 후 해제 (CLAUDE.md §2.5)."""
    _full_to_shots(pid)
    with pytest.raises(step7_shot_video.CostGateError):
        step7_shot_video.assert_gate(pid)
    step6_image_edit.approve_all(pid)
    step7_shot_video.assert_gate(pid)  # 통과 (예외 없음)


def test_render_static_and_versions(pid):
    _full_to_shots(pid)
    step6_image_edit.approve_all(pid)
    url = step8_assemble.run(pid, RenderRequest(headline="헤드라인", cta="구매"))
    out = state_store.assets_dir(pid) / url.rsplit("/", 1)[-1]
    assert out.exists() and out.stat().st_size > 0
    Image.open(io.BytesIO(out.read_bytes())).verify()
    st = state_store.load(pid)
    assert st.final_outputs
    # 승인마다 버전 스냅샷 (spec/storyboard/shots 승인 = 3 + render 승인)
    step8_assemble.approve(pid)
    st = state_store.load(pid)
    assert len(st.versions) >= 2


def test_concept_copy_reaches_final_render(pid):
    """컨셉→콘티 카피가 최종 정적 합성의 헤드라인으로 자동 반영 (루프 닫힘)."""
    from app.pipeline import step8_assemble

    step1_brief.run(pid, product_text="공기청정기 PureAir.")
    step2_spec.run(pid, SpecRequest(duration_sec=9))
    concepts = concept_eval.evaluate(pid, n=5)
    step3_storyboard.run(pid, concept_id=concepts[1].id)
    step3_storyboard.approve(pid)
    step4_hero_image.run(pid)
    step4_hero_image.approve(pid)
    step5_shot_images.run(pid)
    step6_image_edit.approve_all(pid)
    st = state_store.load(pid)
    hook = st.storyboard.shots[0].on_screen_text.text_ko
    # req.headline 비움 → 콘티 훅 카피가 기본 헤드라인이 되어야
    assert step8_assemble._default_headline(st) == hook
    assert hook == concepts[1].hook  # 선택 컨셉 hook 까지 추적
    # 렌더 산출물 생성 확인
    url = step8_assemble.run(pid, RenderRequest())  # headline 빈값
    assert url.endswith(".png")


def test_ab_variants(pid):
    """A/B variants render the ad with each top-N concept's hook (§1)."""
    from PIL import Image as PILImage

    step1_brief.run(pid, product_text="Wireless earbuds. Deep bass.")
    step2_spec.run(pid, SpecRequest(duration_sec=9))
    concepts = concept_eval.evaluate(pid, n=5)
    step3_storyboard.run(pid)
    step4_hero_image.run(pid)
    variants = step8_assemble.render_variants(pid, n=3)
    assert len(variants) == 3
    # each variant maps to a distinct top concept and is a valid PNG
    assert [v.concept_id for v in variants] == [c.id for c in concepts[:3]]
    for v in variants:
        assert v.hook  # carries the concept hook
        p = state_store.assets_dir(pid) / v.url.rsplit("/", 1)[-1]
        PILImage.open(p).verify()
    assert len(state_store.load(pid).ab_variants) == 3


def test_select_ab_winner(pid):
    """Selecting a variant makes it a final output and snapshots the decision."""
    step1_brief.run(pid, product_text="X.")
    step2_spec.run(pid, SpecRequest(duration_sec=9))
    concept_eval.evaluate(pid, n=5)
    step3_storyboard.run(pid)
    step4_hero_image.run(pid)
    variants = step8_assemble.render_variants(pid, n=3)
    chosen = variants[1]
    before_versions = len(state_store.load(pid).versions)
    w = step8_assemble.select_variant(pid, chosen.concept_id)
    assert w.concept_id == chosen.concept_id
    st = state_store.load(pid)
    assert chosen.url in st.final_outputs
    assert len(st.versions) == before_versions + 1  # decision versioned
    # unknown concept rejected
    import pytest as _pt
    with _pt.raises(ValueError):
        step8_assemble.select_variant(pid, "concept_nope")


def test_ab_variants_video_rejected(pid):
    from app.core import state as ss
    from app.core.schemas import OutputFormat

    st = ss.load(pid)
    st.format = OutputFormat.ugc_video
    ss.save(st)
    step1_brief.run(pid, product_text="X.")
    step2_spec.run(pid, SpecRequest(duration_sec=9))
    with pytest.raises(ValueError):
        step8_assemble.render_variants(pid, n=2)  # static only


def test_upscale_static_output(pid):
    """Upscale 2× the latest output (Lanczos offline baseline, §9)."""
    from PIL import Image as PILImage

    from app.pipeline import step9_encode

    _full_to_shots(pid)
    step6_image_edit.approve_all(pid)
    out_url = step8_assemble.run(pid, RenderRequest())
    base = state_store.assets_dir(pid) / out_url.rsplit("/", 1)[-1]
    bw, bh = PILImage.open(base).size
    v = step9_encode.upscale_output(pid, factor=2)
    assert v.kind == "image" and "upscaled" in v.ratio
    up = state_store.assets_dir(pid) / v.url.rsplit("/", 1)[-1]
    uw, uh = PILImage.open(up).size
    assert (uw, uh) == (bw * 2, bh * 2)   # genuinely 2× resolution
    # invalid factor rejected
    with pytest.raises(ValueError):
        step9_encode.upscale_output(pid, factor=3)


def test_export_variants_static(pid):
    """static 광고 → 멀티 비율 export (one ad → all placements)."""
    from PIL import Image as PILImage

    from app.core import assembly
    from app.pipeline import step9_encode

    _full_to_shots(pid)
    step6_image_edit.approve_all(pid)
    step8_assemble.run(pid, RenderRequest(headline="H", cta="C"))
    variants = step9_encode.export_variants(pid, ["9:16", "1:1", "4:5"])
    assert len(variants) == 3
    for v in variants:
        p = state_store.assets_dir(pid) / v.url.rsplit("/", 1)[-1]
        w, h = PILImage.open(p).size
        assert (w, h) == assembly.wh_for(v.ratio)  # 정확한 비율로 리사이즈
    assert len(state_store.load(pid).exports) == 3


def test_revert_restores_snapshot(pid):
    step1_brief.run(pid, product_text="제품.")
    step2_spec.run(pid, SpecRequest(duration_sec=9, tone="A톤"))
    step2_spec.approve(pid)  # v1 스냅샷
    # 톤 변경
    step2_spec.run(pid, SpecRequest(tone="B톤"))
    assert state_store.load(pid).adspec.tone == "B톤"
    # v1 으로 되돌리기
    reverted = state_store.revert(pid, 1)
    assert reverted.adspec.tone == "A톤"


def test_duplicate_project(pid):
    """Duplicate clones state + assets to a new id; URLs rewritten; versions fresh."""
    step1_brief.run(pid, product_text="Sneaker. Bouncy.")
    step2_spec.run(pid, SpecRequest(duration_sec=9, tone="bold"))
    step2_spec.approve(pid)  # creates a version
    step3_storyboard.run(pid)
    step4_hero_image.run(pid)

    dup = state_store.duplicate_project(pid)
    try:
        assert dup.project_id != pid
        assert dup.title.endswith("(copy)")
        assert dup.adspec.tone == "bold"                 # state copied
        assert dup.adspec.project_id == dup.project_id    # id rewritten in nested field
        assert dup.versions == []                         # fresh history
        # hero asset URL points to the NEW project and the file exists
        assert f"/projects/{dup.project_id}/" in dup.hero_image.url
        assert pid not in dup.hero_image.url
        local = state_store.assets_dir(dup.project_id) / dup.hero_image.url.rsplit("/", 1)[-1]
        assert local.exists() and local.stat().st_size > 0
        # original is untouched
        assert state_store.load(pid) is not None
    finally:
        state_store.delete_project(dup.project_id)


def test_revert_does_not_overwrite_version_history(pid):
    """revert 후 새 snapshot 이 기존 버전 파일을 덮어쓰지 않아야 함 (회귀 방지)."""
    from app.core.schemas import PipelineStep

    step1_brief.run(pid, product_text="제품.")
    step2_spec.run(pid, SpecRequest(duration_sec=9, tone="v1"))
    st = state_store.load(pid)
    state_store.snapshot(st, PipelineStep.spec, "snap1")  # v1
    st = state_store.load(pid); st.adspec.tone = "v2"; state_store.save(st)
    state_store.snapshot(st, PipelineStep.spec, "snap2")  # v2
    st = state_store.load(pid); st.adspec.tone = "v3"; state_store.save(st)
    state_store.snapshot(st, PipelineStep.spec, "snap3")  # v3

    # v2 로 revert
    state_store.revert(pid, 2)
    # 새 변경 + snapshot → v3 를 덮어쓰면 안 됨 (v4 가 되어야)
    st = state_store.load(pid); st.adspec.tone = "v4"; state_store.save(st)
    state_store.snapshot(st, PipelineStep.spec, "snap4")

    vdir = state_store.project_dir(pid) / "versions"
    assert (vdir / "v3.json").exists()
    # 원래 v3 의 tone 이 보존되어야 (덮어쓰기 안 됨)
    import json as _json

    v3 = _json.loads((vdir / "v3.json").read_text())
    assert v3["adspec"]["tone"] == "v3"
    assert (vdir / "v4.json").exists()


def test_revert_keeps_later_versions_visible(pid):
    """Revert must be non-destructive: reverting to an earlier version must NOT hide later
    versions from the history list (their snapshot files still exist; you must be able to
    move forward again). Guards a real bug where the snapshot's stale `versions` truncated it."""
    from app.core.schemas import PipelineStep

    step1_brief.run(pid, product_text="Product. A line.")
    st = state_store.load(pid)
    state_store.snapshot(st, PipelineStep.brief, "v1")            # v1
    st = state_store.load(pid)
    state_store.snapshot(st, PipelineStep.spec, "v2")            # v2
    st = state_store.load(pid)
    state_store.snapshot(st, PipelineStep.storyboard, "v3")      # v3
    assert [v.version for v in state_store.load(pid).versions] == [1, 2, 3]

    state_store.revert(pid, 1)   # revert to the earliest
    # the full history is still listed (not truncated to [1]) …
    assert [v.version for v in state_store.load(pid).versions] == [1, 2, 3]
    # … and we can still revert forward to v3
    assert state_store.revert(pid, 3) is not None


def test_single_shot_storyboard_has_cta(pid):
    """shot_count=1 이어도 CTA 비트가 누락되지 않아야 함."""
    step1_brief.run(pid, product_text="제품. 핵심.")
    step2_spec.run(pid, SpecRequest(duration_sec=9, shot_count=1))
    sb = step3_storyboard.run(pid)
    assert len(sb.shots) == 1
    # CTA 카피가 존재해야 (제품+CTA 비트)
    assert sb.shots[0].on_screen_text.text_ko.strip() != ""


@pytest.mark.skipif(
    not __import__("app.core.assembly", fromlist=["assembly"]).ffmpeg_available(),
    reason="ffmpeg not installed",
)
def test_transitions_affect_assembly(tmp_path):
    """Per-shot transitions are honored: 'cut' overlaps less than 'crossfade'
    → longer total (CLAUDE.md §3.2 transition field)."""
    import subprocess

    from app.core import assembly

    clips, durs = [], [2.0, 2.0, 2.0]
    for i, c in enumerate(["red", "green", "blue"]):
        p = tmp_path / f"c{i}.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={c}:s=320x240:d=2:r=30",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)],
            capture_output=True, check=True,
        )
        clips.append(p)

    cut = tmp_path / "cut.mp4"
    cf = tmp_path / "cf.mp4"
    assembly.crossfade_clips(clips, durs, cut, transitions=["cut", "cut"])
    assembly.crossfade_clips(clips, durs, cf, transitions=["crossfade", "crossfade"])
    d_cut = assembly.probe_duration(cut)
    d_cf = assembly.probe_duration(cf)
    # cut overlaps 0.08s each (total ≈ 5.84), crossfade 0.4s each (≈ 5.2)
    assert d_cut > d_cf + 0.3


@pytest.mark.skipif(
    not __import__("app.core.assembly", fromlist=["assembly"]).ffmpeg_available(),
    reason="ffmpeg not installed",
)
def test_video_pipeline_offline():
    """ugc_video: stills→Ken Burns 클립→xfade 조립→mp4 (오프라인, ffmpeg)."""
    from app.core import assembly

    st = state_store.create_project("영상테스트", fmt="ugc_video")
    pid = st.project_id
    try:
        step1_brief.run(pid, product_text="러닝화 Velocity. 가볍다.")
        step2_spec.run(pid, SpecRequest(duration_sec=9, aspect_ratio="9:16"))  # 3컷
        step2_spec.approve(pid)
        step3_storyboard.run(pid)
        step3_storyboard.approve(pid)
        step4_hero_image.run(pid)
        step4_hero_image.approve(pid)
        step5_shot_images.run(pid)

        # 게이트: 승인 전 영상 차단
        with pytest.raises(step7_shot_video.CostGateError):
            step7_shot_video.run(pid)
        step6_image_edit.approve_all(pid)

        clips = step7_shot_video.run(pid)
        assert len(clips) == 3
        # 각 클립이 실제 재생 가능한 mp4 (길이 > 0)
        for v in clips:
            p = state_store.assets_dir(pid) / v.url.rsplit("/", 1)[-1]
            assert assembly.probe_duration(p) > 0.5

        # 컷별 재생성(only_shot_id): 해당 컷만 대상, 전체 3개 유지 (CLAUDE.md §4.7)
        prog = []
        one = step7_shot_video.run(pid, only_shot_id="shot_2", progress_cb=prog.append)
        assert len(one) == 3 and any(v.shot_id == "shot_2" for v in one)
        assert prog and prog[-1] == 1.0  # 단일 대상이라 진행률 100%로 종료

        step7_shot_video.approve_all(pid)
        url = step8_assemble.run(pid, RenderRequest())
        assert url.endswith(".mp4")
        final = state_store.assets_dir(pid) / url.rsplit("/", 1)[-1]
        total = assembly.probe_duration(final)
        # 3컷×3초 - 크로스페이드 ≈ 8초 내외
        assert 6.0 < total < 10.0
        # 음악 베드가 믹스되어 오디오 스트림이 존재해야 함
        assert assembly.has_audio_stream(final)

        # 영상 멀티 비율 export: 비율 + 오디오 보존
        from app.pipeline import step9_encode

        evs = step9_encode.export_variants(pid, ["1:1", "4:5"])
        assert {v.ratio for v in evs} == {"1:1", "4:5"}
        for v in evs:
            assert v.kind == "video"
            ep = state_store.assets_dir(pid) / v.url.rsplit("/", 1)[-1]
            assert assembly.has_audio_stream(ep)
    finally:
        state_store.delete_project(pid)


@pytest.mark.skipif(
    not __import__("app.core.assembly", fromlist=["assembly"]).ffmpeg_available(),
    reason="ffmpeg not installed",
)
def test_render_video_uses_probed_clip_duration(monkeypatch):
    """xfade offsets must come from the ACTUAL probed clip length, not shot.duration_sec —
    real video providers quantize duration, so trusting the spec value drifts transitions."""
    import shutil

    import app.core.assembly as A

    st = state_store.create_project("dur-probe", fmt="ugc_video")
    pid = st.project_id
    try:
        step1_brief.run(pid, product_text="Shoe. Light.")
        step2_spec.run(pid, SpecRequest(duration_sec=9, aspect_ratio="9:16"))  # 3 shots × 3.0s
        step2_spec.approve(pid)
        step3_storyboard.run(pid)
        step3_storyboard.approve(pid)
        step4_hero_image.run(pid)
        step4_hero_image.approve(pid)
        step5_shot_images.run(pid)
        step6_image_edit.approve_all(pid)
        step7_shot_video.run(pid)
        step7_shot_video.approve_all(pid)

        captured = {}

        def fake_cf(clips, durations, out, **k):
            captured["durs"] = list(durations)
            shutil.copy(clips[0], out)        # produce a valid mp4 for downstream encode
            return out

        # pretend the real clips came back at 4.25s (≠ the 3.0s requested in the spec)
        monkeypatch.setattr(A, "probe_duration", lambda p: 4.25)
        monkeypatch.setattr(A, "crossfade_clips", fake_cf)
        step8_assemble.render_video(pid, RenderRequest())

        # the fix: durations passed to xfade are the probed 4.25s, NOT shot.duration_sec (3.0)
        assert captured["durs"] == [4.25, 4.25, 4.25]
    finally:
        state_store.delete_project(pid)


# --------------------------------------------------------------------------
def _full_to_shots(pid):
    step1_brief.run(pid, product_text="무선 이어버드. 40시간.")
    step2_spec.run(pid, SpecRequest(duration_sec=9))
    step2_spec.approve(pid)
    step3_storyboard.run(pid)
    step3_storyboard.approve(pid)
    step4_hero_image.run(pid)
    step4_hero_image.approve(pid)
    step5_shot_images.run(pid)


# --------------------------------------------------------------------------
# HTTP 계층 (FastAPI TestClient)
# --------------------------------------------------------------------------
def _await_job(client, job_id, timeout=60):
    """Poll a job to completion; return the final job dict."""
    import time

    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get(f"/api/jobs/{job_id}").json()
        if last["status"] in ("done", "error"):
            break
        time.sleep(0.3)
    assert last and last["status"] == "done", f"job did not finish: {last}"
    return last


@pytest.mark.skipif(
    not __import__("app.core.assembly", fromlist=["assembly"]).ffmpeg_available(),
    reason="ffmpeg/ffprobe not installed",
)
def test_async_video_job(drain):
    """Video generation runs via the async job queue with progress + polling (§6)."""

    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    pid = client.post("/api/projects", json={"title": "J", "format": "ugc_video",
                                             "product_text": "Runner shoes."}).json()["project_id"]
    try:
        client.post(f"/api/projects/{pid}/spec", json={"duration_sec": 9})
        client.post(f"/api/projects/{pid}/spec/approve")
        drain(client, client.post(f"/api/projects/{pid}/storyboard"))
        client.post(f"/api/projects/{pid}/storyboard/approve")
        client.post(f"/api/projects/{pid}/hero", json={})
        client.post(f"/api/projects/{pid}/hero/approve")
        _await_job(client, client.post(f"/api/projects/{pid}/shots").json()["job_id"])

        # gate: before approval the async submit returns 409 synchronously
        assert client.post(f"/api/projects/{pid}/video").status_code == 409
        client.post(f"/api/projects/{pid}/shots/approve")

        # submit async video job and poll to completion
        r = client.post(f"/api/projects/{pid}/video")
        assert r.status_code == 200
        last = _await_job(client, r.json()["job_id"])
        assert last["progress"] == 1.0
        assert len(last["result"]) == 3  # 3 clips produced
        # clips landed in state
        assert len(client.get(f"/api/projects/{pid}").json()["shot_videos"]) == 3
    finally:
        client.delete(f"/api/projects/{pid}")


def test_health_reports_ffmpeg():
    from fastapi.testclient import TestClient

    from app.core import assembly
    from app.main import app

    h = TestClient(app).get("/api/health").json()
    assert h["ok"] is True
    assert "ffmpeg" in h and h["ffmpeg"] == assembly.ffmpeg_available()


def test_http_flow_and_gating(drain):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    assert client.get("/api/health").json()["ok"] is True

    r = client.post("/api/projects", json={"title": "HTTP", "product_text": "Product X. Key point."})
    pid = r.json()["project_id"]
    try:
        client.post(f"/api/projects/{pid}/spec", json={"duration_sec": 9})
        client.post(f"/api/projects/{pid}/spec/approve")
        drain(client, client.post(f"/api/projects/{pid}/storyboard"))
        client.post(f"/api/projects/{pid}/storyboard/approve")
        client.post(f"/api/projects/{pid}/hero", json={})
        client.post(f"/api/projects/{pid}/hero/approve")
        # shots are async now → poll the job to completion
        shots_job = client.post(f"/api/projects/{pid}/shots").json()["job_id"]
        _await_job(client, shots_job)
        assert len(client.get(f"/api/projects/{pid}").json()["shot_images"]) == 3

        # 게이트: 승인 전 영상 호출 → 409
        assert client.get(f"/api/projects/{pid}/video/gate").json()["unlocked"] is False
        assert client.post(f"/api/projects/{pid}/video").status_code == 409

        client.post(f"/api/projects/{pid}/shots/approve")
        assert client.get(f"/api/projects/{pid}/video/gate").json()["unlocked"] is True

        rr = client.post(f"/api/projects/{pid}/render", json={"headline": "H", "cta": "C"})
        assert rr.status_code == 200
        out_url = rr.json()["output_url"]
        assert client.get(out_url).status_code == 200
    finally:
        client.delete(f"/api/projects/{pid}")


def test_upload_image_endpoint_and_resolve(tmp_path):
    """Unified brief input: a pasted/uploaded base64 image is saved as a local asset and
    is usable as a reference (resolve_image_bytes handles local + remote URLs)."""
    import base64
    import io

    from fastapi.testclient import TestClient
    from PIL import Image

    from app.main import app
    from app.pipeline import resolve_image_bytes

    client = TestClient(app)
    pid = client.post("/api/projects", json={"title": "u", "format": "static_image",
                                             "product_text": "X."}).json()["project_id"]
    try:
        buf = io.BytesIO()
        Image.new("RGB", (32, 32), (5, 9, 7)).save(buf, "PNG")
        data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        r = client.post(f"/api/projects/{pid}/upload-image", json={"data": data_uri})
        assert r.status_code == 200
        url = r.json()["url"]
        assert url.endswith(".png") and "/assets/" in url
        # the saved local asset resolves back to bytes (a valid image)
        b = resolve_image_bytes(pid, url)
        assert b and Image.open(io.BytesIO(b)).size == (32, 32)
        # garbage rejected with 400, not a 500
        assert client.post(f"/api/projects/{pid}/upload-image",
                           json={"data": "not-base64!!"}).status_code == 400
        assert client.post(f"/api/projects/{pid}/upload-image",
                           json={"data": ""}).status_code == 400
    finally:
        state_store.delete_project(pid)


def test_brief_notice_on_unfetchable_url(pid, monkeypatch):
    """A URL that can't be fetched (e.g. a site that blocks bots) must surface a clear
    notice telling the user to paste text/image — not silently produce a generic brief."""
    import app.pipeline as pipeline_pkg

    monkeypatch.setattr(pipeline_pkg, "safe_fetch", lambda *a, **k: None)  # simulate 403/block
    spec = step1_brief.run(pid, product_url="https://link.coupang.com/a/blocked")
    assert spec.brief_notice and "Couldn't read that URL" in spec.brief_notice
    # a successful fetch (or no URL) leaves the notice empty
    spec2 = step1_brief.run(pid, product_text="A real pasted description. Fast and light.")
    assert spec2.brief_notice == ""


def test_brief_extracts_opengraph(pid, monkeypatch):
    """A product page's OpenGraph tags drive a clean brief + auto-attach the og:image."""
    import app.pipeline as pipeline_pkg

    class _Resp:
        text = (
            "<html><head>"
            "<meta property='og:title' content='Pora Espresso Maker'>"
            "<meta property='og:description' content='Hand-pump, no electricity, 9-bar shot in 30s.'>"
            "<meta property='og:image' content='https://cdn.example.com/pora.jpg'>"
            "<title>Buy now | Shop</title></head><body><p>filler</p></body></html>"
        )

    monkeypatch.setattr(pipeline_pkg, "safe_fetch", lambda *a, **k: _Resp())
    spec = step1_brief.run(pid, product_url="https://shop.example.com/pora")
    blob = spec.product.name + spec.product.description + spec.product.key_message
    assert "Pora" in blob                                   # og:title preferred over <title>
    assert "https://cdn.example.com/pora.jpg" in spec.product.image_urls  # og:image auto-attached
    assert spec.brief_notice == ""                          # fetch succeeded → no notice


def test_brief_image_only_does_not_fabricate_name(pid):
    """Image-only brief (no text/URL) must leave fields blank with a guiding notice —
    not fabricate 'New product' for every field (regression for a real user report)."""
    spec = step1_brief.run(pid, image_urls=["/storage/projects/x/assets/upload_1.png"])
    assert spec.product.name == "" and spec.product.description == ""
    assert spec.product.key_message == ""
    assert spec.product.image_urls == ["/storage/projects/x/assets/upload_1.png"]  # image kept
    assert "image" in spec.brief_notice and "Add the product name" in spec.brief_notice
    # …and a later text edit/extract fills it in normally
    spec2 = step1_brief.run(pid, product_text="Aura earbuds. 40-hour battery.")
    assert spec2.product.name and spec2.product.name != "New product"
    assert spec2.brief_notice == ""


def test_brief_routes_direct_image_url_to_images(pid):
    """A pasted direct image link is treated as a product photo, not a page to scrape."""
    from app.pipeline.step1_brief import _looks_like_image_url

    assert _looks_like_image_url("https://cdn.shop.com/pora.jpg?v=2")
    assert _looks_like_image_url("https://x.com/a.PNG")
    assert not _looks_like_image_url("https://shop.com/products/pora")  # a page, not an image
    spec = step1_brief.run(pid, product_url="https://cdn.shop.com/pora.jpg?v=2")
    assert "https://cdn.shop.com/pora.jpg?v=2" in spec.product.image_urls   # routed to images
    # …and NOT mistaken for a failed webpage scrape
    assert "blocks automated access" not in spec.brief_notice


def test_export_recomposites_per_ratio_not_crop(pid):
    """Multi-ratio static export must re-composite (recipe-based) so the headline/CTA are
    re-placed for each ratio, not cropped off the 9:16 output."""
    import io

    from PIL import Image

    from app.pipeline import step9_encode
    _full_to_shots(pid)
    step6_image_edit.approve_all(pid)
    step8_assemble.run(pid, RenderRequest())
    st = state_store.load(pid)
    # the render recipe was stored for re-compositing
    assert st.adspec.last_render and st.adspec.last_render["base_url"]
    assert "headline" in st.adspec.last_render and "cta" in st.adspec.last_render

    evs = step9_encode.export_variants(pid, ["1:1", "16:9"])
    assert {v.ratio for v in evs} == {"1:1", "16:9"}
    for v in evs:
        p = state_store.assets_dir(pid) / v.url.rsplit("/", 1)[-1]
        img = Image.open(io.BytesIO(p.read_bytes()))
        w, h = img.size
        # square / landscape dims prove a fresh compose at that ratio, not a 9:16 crop
        if v.ratio == "1:1":
            assert w == h
        else:
            assert w > h


def test_brief_name_not_truncated_midword(pid):
    """Regression: 'Brando is a premium widget that helps you X' must yield brand 'Brando',
    not a 40-char mid-word slug (which leaked ': ' artifacts into hooks)."""
    spec = step1_brief.run(
        pid, product_text="Brando is a premium daily greens powder that helps you feel great.")
    assert spec.product.name == "Brando"               # brand token only, not a clause
    assert len(spec.product.name.split()) <= 5


def test_concept_hooks_surface_brand_and_have_no_artifacts(pid):
    """Improved hooks: brand appears across the concept set, benefit is distilled, and no
    mid-word truncation artifacts (lone-letter tokens)."""
    import re

    from app.eval import concept_eval

    step1_brief.run(pid, product_text="Aura is a premium widget that helps you save 40 minutes a day.")
    step2_spec.run(pid, SpecRequest(duration_sec=9))
    concepts = concept_eval.evaluate(pid, n=6)
    hooks = [c.hook for c in concepts]
    assert any("Aura" in h for h in hooks)             # brand surfaced in at least one hook
    for h in hooks:
        lone = [t for t in re.findall(r"[A-Za-z]+", h) if len(t) == 1 and t.lower() not in ("a", "i")]
        assert not lone, f"artifact in hook: {h!r}"


def test_product_change_invalidates_stale_concepts(pid):
    """Regression (user-reported 1→3 leak): changing the product must clear concepts/selected
    hook from the OLD product, so a regenerated storyboard's hook doesn't show the previous
    product. Was: shot-1 kept 'Still settling for less? Try <old product>'."""
    from app.eval import concept_eval

    step1_brief.run(pid, product_text="LiQ detergent. Removes mites and deep-cleans in one wash.")
    step2_spec.run(pid, SpecRequest(duration_sec=9))
    concepts = concept_eval.evaluate(pid, n=5)
    assert concepts
    st = state_store.load(pid)
    assert st.concepts                                   # concepts populated for product A

    # human edits the product to something completely different
    step1_brief.update_product(pid, name="Street Model",
                               key_message="The living face of Myeongdong street fashion")
    st2 = state_store.load(pid)
    assert st2.concepts == [] and st2.selected_concept_id is None   # stale concepts cleared

    # regenerated storyboard's hook reflects the NEW product, not the old detergent concept
    sb = step3_storyboard.run(pid)
    hook = sb.shots[0].on_screen_text.text_ko.lower()
    assert "liq" not in hook and "detergent" not in hook


def test_edit_passes_base_prompt_and_seed_for_scene_preservation(pid, monkeypatch):
    """STEP-6 edit (user-reported): a text→image provider must regenerate the SAME scene +
    the change, not a brand-new image. edit_shot must pass the source prompt + a stable seed."""
    from app.pipeline import step6_image_edit

    step1_brief.run(pid, product_text="Street model. Myeongdong street fashion face.")
    step2_spec.run(pid, SpecRequest(duration_sec=9))
    step3_storyboard.run(pid)
    step4_hero_image.run(pid)
    step5_shot_images.run(pid)

    captured = {}

    class _Spy:
        name = "spy"
        is_real = True

        def edit_image(self, image, instruction, reference_images=None, base_prompt=None, seed=None):
            captured.update(base_prompt=base_prompt, seed=seed, instruction=instruction)
            from app.providers.base import ImageResult
            return ImageResult(image_bytes=image, provider="spy")

    monkeypatch.setattr(step6_image_edit, "get_image_provider_by_name", lambda name: _Spy())
    shot_id = state_store.load(pid).shot_images[0].shot_id
    step6_image_edit.edit_shot(pid, shot_id, "make the clothes black")
    assert captured["base_prompt"]                       # source scene prompt passed through
    assert isinstance(captured["seed"], int)             # stable seed passed
    assert captured["instruction"] == "make the clothes black"


def test_duplicate_shot_clones_shot_and_image(pid):
    """Studio clip 'Duplicate': clones the storyboard shot (inserted after) and copies its
    image so the clone shows the same frame immediately."""
    from fastapi.testclient import TestClient

    from app.main import app

    step1_brief.run(pid, product_text="Aura. 40h battery.")
    step2_spec.run(pid, SpecRequest(duration_sec=9))
    step3_storyboard.run(pid)
    step4_hero_image.run(pid)
    step5_shot_images.run(pid)
    client = TestClient(app)
    st0 = state_store.load(pid)
    n0, sid = len(st0.storyboard.shots), st0.storyboard.shots[0].id
    img0 = next(a for a in st0.shot_images if a.shot_id == sid)

    r = client.post(f"/api/projects/{pid}/shots/{sid}/duplicate")
    assert r.status_code == 200
    st1 = state_store.load(pid)
    assert len(st1.storyboard.shots) == n0 + 1                 # one shot added
    new_id = st1.storyboard.shots[1].id                        # inserted right after
    assert new_id != sid
    clone_img = next((a for a in st1.shot_images if a.shot_id == new_id), None)
    assert clone_img is not None and clone_img.url != img0.url  # image copied to a new asset
    # 404 for an unknown shot
    assert client.post(f"/api/projects/{pid}/shots/nope/duplicate").status_code == 404


def test_audio_update_endpoint_and_mux_volume(pid):
    """Studio audio track: volume/mute persist via /audio; mux_audio applies the volume filter."""
    from fastapi.testclient import TestClient

    from app.main import app

    step1_brief.run(pid, product_text="X.")
    step2_spec.run(pid, SpecRequest(duration_sec=9))
    client = TestClient(app)
    r = client.post(f"/api/projects/{pid}/audio", json={"music_volume": 0.5, "music_muted": True})
    assert r.status_code == 200
    a = state_store.load(pid).adspec.audio
    assert a.music_volume == 0.5 and a.music_muted is True
    # clamp out-of-range
    client.post(f"/api/projects/{pid}/audio", json={"music_volume": 9})
    assert state_store.load(pid).adspec.audio.music_volume == 2.0


def test_mux_audio_volume_filter(tmp_path, monkeypatch):
    from app.core import assembly

    calls = {}
    monkeypatch.setattr(assembly, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(assembly, "_run", lambda cmd: calls.setdefault("cmd", cmd))
    a = tmp_path / "a.m4a"
    a.write_bytes(b"x")
    assembly.mux_audio(tmp_path / "v.mp4", a, tmp_path / "o.mp4", volume=0.4)
    assert "-filter:a" in calls["cmd"] and "volume=0.40" in calls["cmd"]
    calls.clear()
    assembly.mux_audio(tmp_path / "v.mp4", a, tmp_path / "o.mp4", volume=1.0)
    assert "-filter:a" not in calls["cmd"]   # no filter when at unity


def test_project_list_has_manager_fields_and_rename(pid):
    """Projects manager: list includes thumb/shots/outputs/product; PATCH renames."""
    from fastapi.testclient import TestClient

    from app.main import app

    step1_brief.run(pid, product_text="Aura earbuds. 40-hour battery.")
    client = TestClient(app)
    row = next(p for p in client.get("/api/projects").json() if p["project_id"] == pid)
    for k in ("thumb", "shots", "outputs", "product", "current_step", "updated_at"):
        assert k in row
    # rename via PATCH
    r = client.patch(f"/api/projects/{pid}", json={"title": "Renamed Ad"})
    assert r.status_code == 200 and r.json()["title"] == "Renamed Ad"
    assert state_store.load(pid).title == "Renamed Ad"
    assert client.patch("/api/projects/nope", json={"title": "x"}).status_code == 404
