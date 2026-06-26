"""광고 전문가 패널 평가 + 100-제품 시뮬레이션 하니스 테스트."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.eval import expert_panel  # noqa: E402
from app.eval.expert_panel import AdUnit  # noqa: E402


def test_panel_has_ten_experts_and_scores_in_range():
    ad = AdUnit(product_name="Aura", key_message="40-hour battery, no noise",
                headline="40 hours of silence. Meet Aura.", cta="Shop Aura today",
                shots_text=["40 hours of silence. Meet Aura.", "Crystal clarity", "Shop Aura today"])
    v = expert_panel.evaluate(ad)
    assert len(v["experts"]) == 10
    assert 0 <= v["overall"] <= 100
    for e in v["experts"]:
        assert 0 <= e["score"] <= 100 and e["critique"]
    assert v["budget"]["tier"] in ("scale", "invest", "test", "hold")
    assert len(v["weakest_axes"]) == 3


def test_panel_rewards_strong_over_weak_copy():
    strong = AdUnit(product_name="Aura", key_message="40-hour battery so you never recharge",
                    headline="Never recharge again. Meet Aura.", cta="Shop Aura today",
                    shots_text=["Never recharge again.", "40 hours nonstop", "Shop Aura today"])
    weak = AdUnit(product_name="Aura", key_message="the gadget everyone is talking about",
                  headline="the gadget everyone is talking about that is really very nice and long",
                  cta="Learn more", shots_text=["x", "x", "x"])
    assert expert_panel.evaluate(strong)["overall"] > expert_panel.evaluate(weak)["overall"]


def test_budget_tiers_scale_with_score():
    assert expert_panel._budget_for(90)["tier"] == "scale"
    assert expert_panel._budget_for(74)["tier"] == "invest"
    assert expert_panel._budget_for(60)["tier"] == "test"
    assert expert_panel._budget_for(40)["tier"] == "hold"
    assert expert_panel._budget_for(90)["daily_usd"] > expert_panel._budget_for(60)["daily_usd"]


def test_brand_strategist_rewards_brand_on_surfaces():
    with_brand = AdUnit(product_name="Aura", headline="Meet Aura", cta="Shop Aura", key_message="Aura sounds great")
    without = AdUnit(product_name="Aura", headline="Meet it", cta="Shop now", key_message="sounds great")
    assert expert_panel._brand_strategist(with_brand).score > expert_panel._brand_strategist(without).score


def test_sim_100_harness_small_run_improved_logic():
    """The 100-product harness runs end-to-end (small n) and the improved copy logic clears
    a quality bar — guards against regressions in brief/concept/storyboard copy."""
    from scripts import sim_100

    report = sim_100.run(n=12, save=False)
    assert report["n"] == 12
    assert 0 <= report["overall"]["mean"] <= 100
    assert report["overall"]["mean"] >= 68          # post-improvement floor (was ~66 pre-fix)
    # the name-truncation bug produced lone-letter tokens ("…greens p: owder" → "p") — guard it
    import re
    for row in report["worst_5"] + report["best_5"]:
        lone = [t for t in re.findall(r"[A-Za-z]+", row["headline"]) if len(t) == 1 and t.lower() not in ("a", "i")]
        assert not lone, f"mid-word truncation artifact in hook: {row['headline']!r}"


def test_pick_cta_word_boundary_no_false_match():
    """H1 regression: body captions like 'Forget'/'Budget'/'Target' must NOT be read as a CTA
    (substring matching used to promote them); the real action-led CTA wins."""
    from app.pipeline import pick_cta

    assert pick_cta(["Hook", "Forget the noise", "Budget-friendly", "Shop Aura today"],
                    "Hook", "Aura") == "Shop Aura today"
    # no real CTA among captions → branded fallback, not "Forget the noise"
    assert pick_cta(["Hook", "Forget the noise", "Together at last"], "Hook", "Aura") == "Shop Aura today"
    assert pick_cta([], "", "") == "Shop now"


def test_benefit_phrase_edge_cases():
    """H2/M1 regressions in concept_eval._benefit_phrase."""
    from app.eval import concept_eval as ce

    assert ce._benefit_phrase("X", "widget that helps you save 40 min") == "save 40 min"
    assert ce._benefit_phrase("X", "gadget that works well") == "works well"
    # vague cliché fully stripped → empty (caller falls back to a brand-led hook), never "is talking about"
    assert ce._benefit_phrase("Xva", "the compact mattress everyone is talking about") == ""


def test_first_number_trims_trailing_preposition():
    """M2 regression: number+unit only, no dangling 'in'/'of'."""
    from app.eval import concept_eval as ce

    assert ce._first_number("burns 500 calories in 20 minutes") == "500 calories"
    assert ce._first_number("holds 7 days of food") == "7 days"
    assert ce._first_number("40-hour battery") == "40-hour battery"
    assert ce._first_number("no numbers here") == ""


def test_vague_input_never_yields_broken_hook():
    """M1 end-to-end: a vague product must not produce 'Is talking about.'-style hooks."""
    from types import SimpleNamespace

    from app.eval import concept_eval as ce

    spec = SimpleNamespace(
        product=SimpleNamespace(name="Xva", key_message="the compact mattress everyone is talking about",
                                description=""),
        platform="instagram_reels", tone="")
    for c in ce._heuristic_concepts(spec, 6):
        assert "talking about" not in c["hook"].lower()
        assert c["hook"].strip() not in (".", "")


# ---------------------------------------------------------------------------
# Editor-persona panel (Studio NLE feedback simulation)
# ---------------------------------------------------------------------------
def test_editor_persona_panel():
    from app.eval import editor_personas

    personas = editor_personas.build_personas(100)
    assert len(personas) == 100
    assert len({p.label for p in personas}) > 30        # genuinely diverse
    r = editor_personas.run_panel(100)
    assert r["n"] == 100
    assert 0 <= r["mean_satisfaction"] <= 100
    assert r["top_requests"] and "feature" in r["top_requests"][0]
    # every requested feature is actually missing (present=False) — feedback is grounded
    for req in r["top_requests"]:
        assert editor_personas.FEATURES[req["feature"]].present is False


def test_editor_panel_reflects_implemented_features():
    """Implemented features must NOT show up as gaps (the sim measures real lift)."""
    from app.eval import editor_personas

    r = editor_personas.run_panel(100)
    missing = {req["feature"] for req in r["top_requests"]}
    for done in ("export_dialog", "playback_speed", "jkl_shuttle", "copy_paste",
                 "multi_select", "ripple_delete", "loop_play", "onboarding"):
        assert editor_personas.FEATURES[done].present is True
        assert done not in missing
