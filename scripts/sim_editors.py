"""100명 가상 편집자 페르소나 → Studio NLE 평가 → 개선 요청 순위 리포트.

실행: source .venv/bin/activate && python scripts/sim_editors.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.eval import editor_personas  # noqa: E402


def run(n: int = 100, save: bool = True) -> dict:
    report = editor_personas.run_panel(n)
    if save:
        out = Path(__file__).resolve().parent.parent / "storage" / "eval"
        out.mkdir(parents=True, exist_ok=True)
        (out / "editor_feedback.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return report


def _print(r: dict) -> None:
    print("\n=== 100 EDITOR-PERSONA PANEL — Studio NLE ===")
    print(f"Mean satisfaction: {r['mean_satisfaction']}  | min {r['min_satisfaction']}  | "
          f"fully satisfied {r['fully_satisfied']}/{r['n']}")
    print(f"Gaps by category: {r['gaps_by_category']}")
    print("\nTop requested improvements (weighted by how many editors want them):")
    for i, req in enumerate(r["top_requests"], 1):
        print(f"  {i:2}. [{req['cat']:10}] {req['label']:42} ← {req['requested_by']}")
    print("\nSample voices:")
    for c in r["sample_comments"]:
        print(f"  • {c}")


if __name__ == "__main__":
    _print(run())
