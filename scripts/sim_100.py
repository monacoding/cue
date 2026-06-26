"""100개 가상 제품 → 광고 생성 → 10인 전문가 패널 채점 → 집계 리포트.

목적: 실제 파이프라인 카피 로직(브리프 휴리스틱·컨셉·콘티)을 100개 제품에 돌려 전문가 패널로
채점하고, 집계로 '체계적 약점'(어느 평가축이 일관되게 낮은가)을 드러낸다. 그 신호로 로직을 개선
하고 재실행해 개선폭을 측정한다. 키리스·결정론적(force_mock)이라 재현 가능.

실행: source .venv/bin/activate && python scripts/sim_100.py
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.core import state as state_store  # noqa: E402
from app.core.schemas import SpecRequest  # noqa: E402
from app.eval import concept_eval, expert_panel  # noqa: E402
from app.pipeline import step1_brief, step2_spec, step3_storyboard  # noqa: E402

# ---- 100개 가상 제품 (카테고리 × 혜택 템플릿, 입력 품질 다양) -------------------
_CATEGORIES = [
    ("earbuds", "wireless earbuds", ["40-hour battery", "active noise cancelling", "instant pairing"]),
    ("mattress", "memory-foam mattress", ["sleep cooler all night", "100-night trial", "zero motion transfer"]),
    ("skincare", "vitamin-C serum", ["visibly brighter skin in 14 days", "fragrance-free", "non-greasy"]),
    ("coffee", "portable espresso maker", ["no electricity needed", "9-bar shot in 30 seconds", "fits any bag"]),
    ("fitness", "smart jump rope", ["counts every jump", "burns 500 calories in 20 minutes", "folds flat"]),
    ("pet", "automatic pet feeder", ["feed on schedule from your phone", "holds 7 days of food", "jam-free"]),
    ("kitchen", "self-cleaning blender", ["cleans itself in 30 seconds", "crushes ice effortlessly", "one-touch"]),
    ("desk", "standing desk converter", ["sit-to-stand in one motion", "holds dual monitors", "no assembly"]),
    ("bottle", "self-heating travel mug", ["keeps coffee hot for 6 hours", "leakproof", "fits car cupholders"]),
    ("lamp", "sunrise alarm lamp", ["wake up gently with light", "no more jarring alarms", "30 light settings"]),
    ("backpack", "anti-theft travel backpack", ["hidden zippers", "charges your phone on the go", "fits a 16-inch laptop"]),
    ("toothbrush", "sonic electric toothbrush", ["removes 10x more plaque", "30-day battery", "pressure sensor"]),
    ("vacuum", "cordless stick vacuum", ["60-minute runtime", "picks up pet hair", "weighs under 3 lbs"]),
    ("camera", "pocket gimbal camera", ["buttery-smooth 4K", "fits in your palm", "tracks your face"]),
    ("supplement", "daily greens powder", ["75 vitamins in one scoop", "tastes like green apple", "no bloating"]),
    ("shoes", "barefoot running shoes", ["feel the ground", "zero-drop sole", "weighs 6 ounces"]),
    ("planner", "undated productivity planner", ["plan your week in 5 minutes", "lays flat", "habit tracker built in"]),
    ("speaker", "waterproof bluetooth speaker", ["floats in water", "24-hour playtime", "360-degree sound"]),
    ("knife", "self-sharpening chef knife", ["never needs sharpening", "Japanese steel", "balanced grip"]),
    ("charger", "3-in-1 wireless charger", ["charge phone, watch, and buds at once", "foldable", "fast-charge"]),
]
_QUALIFIERS = ["", "premium ", "compact ", "pro ", "everyday "]


def _make_products(n: int = 100):
    """카테고리를 순환하며 혜택 조합/입력 품질을 바꿔 n개의 다양한 제품 텍스트를 만든다."""
    out = []
    for i in range(n):
        slug, base, benefits = _CATEGORIES[i % len(_CATEGORIES)]
        qual = _QUALIFIERS[(i // len(_CATEGORIES)) % len(_QUALIFIERS)]
        # 브랜드명 합성 (카테고리별로 변주)
        name = f"{slug.capitalize()}{['o','a','ix','ly','va'][i % 5]}"
        # 입력 품질 변주: 일부는 혜택+수치 풍부, 일부는 모호 (로직 스트레스)
        variant = i % 3
        if variant == 0:
            desc = f"{name} — {qual}{base}. {benefits[0].capitalize()}, {benefits[1]}, {benefits[2]}."
        elif variant == 1:
            desc = f"{name} is a {qual}{base} that helps you {benefits[1]}."
        else:
            desc = f"{name}: the {qual}{base} everyone is talking about."   # 모호 — 혜택 없음
        out.append({"slug": slug, "name": name, "desc": desc, "input_quality": variant})
    return out


def _build_ad(product) -> expert_panel.AdUnit:
    st = state_store.create_project(product["name"], fmt="static_image")
    pid = st.project_id
    try:
        step1_brief.run(pid, product_text=product["desc"])
        step2_spec.run(pid, SpecRequest(duration_sec=9, aspect_ratio="9:16"))
        concepts = concept_eval.evaluate(pid, n=5)            # 광고 지능: 컨셉/훅
        cid = concepts[0].id if concepts else None
        step3_storyboard.run(pid, concept_id=cid)             # 훅 → 콘티 카피
        return expert_panel.ad_from_state(state_store.load(pid))
    finally:
        state_store.delete_project(pid)


def run(n: int = 100, save: bool = True) -> dict:
    # 평가 대상은 '로직'이므로 전부 결정론적 mock 으로 — 네트워크/키/이미지 생성 배제.
    # 전역 상태를 오염시키지 않도록 save/restore (테스트 격리).
    _prev = settings.force_mock
    settings.force_mock = True
    try:
        return _run(n, save)
    finally:
        settings.force_mock = _prev


def _run(n: int, save: bool) -> dict:
    products = _make_products(n)
    rows = []
    axis_scores: dict = defaultdict(list)
    tier_counts: Counter = Counter()
    total_budget = 0
    for p in products:
        ad = _build_ad(p)
        verdict = expert_panel.evaluate(ad)
        rows.append({"name": p["name"], "input_quality": p["input_quality"],
                     "headline": ad.headline, "cta": ad.cta,
                     "key_message": ad.key_message, "overall": verdict["overall"],
                     "budget": verdict["budget"], "weakest": verdict["weakest_axes"]})
        for e in verdict["experts"]:
            axis_scores[e["specialty"]].append(e["score"])
        tier_counts[verdict["budget"]["tier"]] += 1
        total_budget += verdict["budget"]["daily_usd"]

    if not rows:
        return {"n": 0, "overall": {}, "axis_means_sorted": {}, "budget_tiers": {},
                "total_daily_budget_usd": 0, "score_by_input_quality": {},
                "worst_5": [], "best_5": []}
    overalls = [r["overall"] for r in rows]
    axis_means = {ax: round(statistics.mean(v), 1) for ax, v in axis_scores.items()}
    by_quality = defaultdict(list)
    for r in rows:
        by_quality[r["input_quality"]].append(r["overall"])
    report = {
        "n": n,
        "overall": {
            "mean": round(statistics.mean(overalls), 1),
            "median": round(statistics.median(overalls), 1),
            "min": min(overalls), "max": max(overalls),
            "stdev": round(statistics.pstdev(overalls), 1),
        },
        "axis_means_sorted": dict(sorted(axis_means.items(), key=lambda kv: kv[1])),
        "budget_tiers": dict(tier_counts),
        "total_daily_budget_usd": total_budget,
        "score_by_input_quality": {q: round(statistics.mean(v), 1) for q, v in sorted(by_quality.items())},
        "worst_5": sorted(rows, key=lambda r: r["overall"])[:5],
        "best_5": sorted(rows, key=lambda r: r["overall"], reverse=True)[:5],
    }
    if save:
        out = Path(__file__).resolve().parent.parent / "storage" / "eval"
        out.mkdir(parents=True, exist_ok=True)
        (out / "sim_100_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return report


def _print(report: dict) -> None:
    o = report["overall"]
    print("\n=== 100-PRODUCT EXPERT PANEL SIM ===")
    print(f"Overall: mean {o['mean']} | median {o['median']} | min {o['min']} | max {o['max']} | stdev {o['stdev']}")
    print(f"Budget tiers: {report['budget_tiers']}  → total recommended ${report['total_daily_budget_usd']}/day")
    print(f"Score by input quality (0=rich,1=mid,2=vague): {report['score_by_input_quality']}")
    print("\nWeakest axes (panel mean, ascending) — systematic logic gaps:")
    for ax, sc in list(report["axis_means_sorted"].items())[:6]:
        print(f"  {sc:5.1f}  {ax}")
    print("\nWorst 5 ads:")
    for r in report["worst_5"]:
        print(f"  {r['overall']:5.1f}  [{r['name']}] hook={r['headline']!r} cta={r['cta']!r}")


if __name__ == "__main__":
    _print(run())
