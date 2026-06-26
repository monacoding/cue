"""키 없이 전체 파이프라인을 end-to-end 로 검증하는 스모크 테스트.

각 단계를 직접 호출 → 상태 영속/일관성/게이팅/합성까지 확인.
실행: python scripts/smoke.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import state as state_store  # noqa: E402
from app.core.schemas import RenderRequest, SpecRequest  # noqa: E402
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


def main() -> int:
    print("== Cue smoke (works in mock mode) ==")
    st = state_store.create_project("Smoke ad", fmt="static_image")
    pid = st.project_id
    print(f"[0] project {pid}")

    spec = step1_brief.run(
        pid,
        product_text="Aura wireless earbuds 'AuraPods'. 40-hour battery, active noise cancelling, crystal-clear calls.",
    )
    print(f"[1] brief → {spec.product.name!r} / key: {spec.product.key_message!r}")

    spec = step2_spec.run(pid, SpecRequest(duration_sec=15, platform="instagram_reels",
                                           aspect_ratio="9:16", tone="minimal, high-end"))
    print(f"[2] spec → {spec.duration_sec}s, shot_count={spec.shot_count}, ar={spec.aspect_ratio}")
    step2_spec.approve(pid)

    concepts = concept_eval.evaluate(pid, n=5)
    print(f"[3a] concepts → best: {concepts[0].hook!r} (score {concepts[0].score})")

    sb = step3_storyboard.run(pid)
    print(f"[3b] storyboard → {len(sb.shots)} shots: {[s.id for s in sb.shots]}")
    step3_storyboard.approve(pid)

    hero = step4_hero_image.run(pid)
    print(f"[4] hero → {hero.url} ({hero.provider})")
    step4_hero_image.approve(pid)

    imgs = step5_shot_images.run(pid)
    print(f"[5] shot images → {len(imgs)} generated, provider={imgs[0].provider}")

    edited = step6_image_edit.edit_shot(pid, imgs[0].shot_id, "brighter background, product close-up")
    print(f"[6a] edit {edited.shot_id} → rev {edited.revision}")

    # 게이트: 승인 전엔 차단되어야 함
    try:
        step7_shot_video.assert_gate(pid)
        print("[6b] FAIL: gate must not be open before approval")
        return 1
    except step7_shot_video.CostGateError:
        print("[6b] OK: cost gate works (step 7 blocked before approval)")

    step6_image_edit.approve_all(pid)
    step7_shot_video.assert_gate(pid)  # 이제 통과해야 함
    print("[6c] OK: gate unlocked after approving all")

    out = step8_assemble.run(pid, RenderRequest(headline="40 hours of nonstop sound", cta="Buy now"))
    print(f"[8] render → {out}")
    step8_assemble.approve(pid)

    final = state_store.load(pid)
    assert final.final_outputs, "no output"
    assert len(final.versions) >= 4, "missing version snapshots"
    # 실제 파일 존재 확인
    fpath = state_store.assets_dir(pid) / out.rsplit("/", 1)[-1]
    assert fpath.exists() and fpath.stat().st_size > 0, "missing final file"
    print(f"[9] ✅ versions={len(final.versions)}, final file={fpath.stat().st_size} bytes")
    print("\nSmoke passed — full pipeline OK end-to-end")
    print(f"   see results: storage/projects/{pid}/assets/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
