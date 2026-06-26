"""대략 비용 추정 (CLAUDE.md §2.5 비용 의식 / §5 레지스트리).

값은 2026-06 참고치 — 실제 청구 전 각 공식 가격으로 확인. mock/오프라인 모드에서도
"실제 모델로 돌릴 경우" 비용을 보여줘 비용 절벽(7단계) 전에 사용자가 판단하도록 돕는다.
"""
from __future__ import annotations

from typing import Dict

# 단가 (CLAUDE.md §5)
IMAGE_PER = 0.04          # Nano Banana 2, 이미지당 ~$0.04
VIDEO_PER_SEC = 0.30      # Seedance 2.0 표준 720p, 초당 ~$0.30 (오디오 포함)
MUSIC_PER_MIN = 0.80      # ElevenLabs 음악, 분당 ~$0.80
LLM_PER_STEP = 0.02       # 오케스트레이션(브리프·스펙·콘티·컨셉) 스텝당 대략치
MIN_CLIP_SEC = 4.0        # image-to-video models bill a per-clip minimum (~4s); a 3s
                          # shot is still charged for a full clip — see providers' clamp

VIDEO_FORMATS = ("ugc_video", "cinematic_video")


def billable_video_sec(n: int, dur: float) -> float:
    """Generated video ≈ Σ per-clip lengths, each ≥ the model's clip minimum. A typical
    ~3s shot is billed at the ~4s floor, so total-duration alone under-reports the cost
    cliff (CLAUDE.md §2.5). Mirrors the provider duration clamp."""
    if n <= 0:
        return float(dur)
    return n * max(MIN_CLIP_SEC, dur / n)


def estimate(state) -> Dict[str, float]:
    """프로젝트 예상 비용 분해 (USD, 대략치)."""
    spec = state.adspec
    n = spec.shot_count if spec else 0
    dur = spec.duration_sec if spec else 0
    fmt = str(getattr(state.format, "value", state.format))

    images = (n + 1) * IMAGE_PER  # hero + 컷
    llm = 4 * LLM_PER_STEP        # 브리프/스펙/콘티/컨셉
    breakdown: Dict[str, float] = {"orchestration": round(llm, 2), "images": round(images, 2)}
    total = llm + images

    if fmt in VIDEO_FORMATS:
        billable = billable_video_sec(n, dur)   # per-clip minimum → realistic estimate
        video = billable * VIDEO_PER_SEC
        music = (billable / 60.0) * MUSIC_PER_MIN
        breakdown["video"] = round(video, 2)
        breakdown["music"] = round(music, 2)
        total += video + music

    breakdown["total"] = round(total, 2)
    return breakdown


def video_step_estimate(state) -> Dict[str, float]:
    """7단계(영상)만의 예상 비용 — 비용 절벽 경고용."""
    spec = state.adspec
    dur = spec.duration_sec if spec else 0
    n = spec.shot_count if spec else 0
    billable = billable_video_sec(n, dur)
    video = billable * VIDEO_PER_SEC
    music = (billable / 60.0) * MUSIC_PER_MIN
    return {"video": round(video, 2), "music": round(music, 2), "total": round(video + music, 2),
            "per_sec": VIDEO_PER_SEC, "duration_sec": dur, "billable_sec": round(billable, 2)}
