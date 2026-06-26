"""Open-source image-to-video model hosted on RunPod (CLAUDE.md §2 provider 추상화, §4.7 영상 대안).

오픈소스 I2V(예: Wan 2.2 / LTX-Video / Stable Video Diffusion)를 RunPod Serverless 로 래핑 —
이미지의 QwenRunPodProvider 와 동일 패턴. **무료/오픈웨이트(자체 GPU) 영상 경로를 먼저 연결**한다.

연결: RUNPOD_API_KEY + RUNPOD_VIDEO_ENDPOINT
  - serverless 엔드포인트 ID → https://api.runpod.ai/v2/{id}/runsync (+ async /status 폴백)
  - 또는 전체 URL(커스텀 pod) → 그 URL로 동기 POST.
키/엔드포인트 없으면 ffmpeg Ken Burns 베이스라인 (다른 provider처럼 키 없이 오프라인 동작).

워커 계약(가정 — 배포에 맞게 RUNPOD_VIDEO_INPUT JSON 으로 조정):
  요청  {"input": {"image": dataURI, "prompt": str, "num_frames": int, "fps": int,
                    "width": int, "height": int, "seed": int?}}
  응답  output 이 다음 중 하나: base64(mp4) / URL / {"video"|"video_url"|"url"|"data": ...} /
        {"output": ...} 중첩. (이미지 파서를 영상 키까지 확장해 재사용)
"""
from __future__ import annotations

import base64
from typing import Optional

from app.config import settings
from app.providers._video_mock import kenburns_clip
from app.providers.base import VideoProvider, VideoResult
# reuse the battle-tested RunPod helpers (bytes/url decode, preview) from the image provider
from app.providers.image_qwen_runpod import _preview, _to_bytes

# generative I2V resolutions (open-source models run ~480–720p; 8/16 배수 권장)
_VID_DIMS = {
    "9:16": (480, 832),
    "16:9": (832, 480),
    "1:1": (512, 512),
    "4:5": (512, 640),
}
_FPS = 16
# worker output keys that carry a video (string base64/url), then nested containers to recurse
_VID_STR_KEYS = ("video", "video_url", "video_base64", "mp4", "url", "data", "base64", "output")
_VID_NEST_KEYS = ("videos", "output", "data", "result", "artifacts")


def _dims(aspect_ratio: str):
    return _VID_DIMS.get(aspect_ratio, (480, 832))


def _parse_video(out, client) -> Optional[bytes]:
    """Robustly pull video bytes from a worker's output (base64 / data-URI / URL,
    or nested list/dict like {"videos":[{"url":...}]} / {"output":{"video":"b64"}})."""
    if out is None:
        return None
    if isinstance(out, str):
        return _to_bytes(out, client)
    if isinstance(out, (list, tuple)):
        for item in out:
            b = _parse_video(item, client)
            if b:
                return b
        return None
    if isinstance(out, dict):
        for k in _VID_STR_KEYS:
            v = out.get(k)
            if isinstance(v, str) and v:
                b = _to_bytes(v, client)
                if b:
                    return b
        for k in _VID_NEST_KEYS:
            v = out.get(k)
            if v is not None and not isinstance(v, str):
                b = _parse_video(v, client)
                if b:
                    return b
    return None


class RunPodVideoProvider(VideoProvider):
    name = "opensource-i2v"
    # open-source I2V models emit short clips; clamp the storyboard's ~3s into a sane window.
    min_duration, max_duration = 2.0, 8.0

    def __init__(self) -> None:
        self.api_key = settings.runpod_api_key
        self.endpoint = settings.runpod_video_endpoint
        self.extra_input = self._parse_extra(settings.runpod_video_input)
        self.last_error: Optional[str] = None

    @staticmethod
    def _parse_extra(raw: str) -> dict:
        if not raw:
            return {}
        try:
            import json
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}

    @property
    def is_real(self) -> bool:
        return bool(self.api_key and self.endpoint) and not settings.force_mock

    def image_to_video(
        self,
        image: bytes,
        prompt: str,
        duration_sec: float = 5,
        with_audio: bool = True,
        aspect_ratio: str = "9:16",
        motion: str = "in",
    ) -> VideoResult:
        if self.is_real:
            secs = self.clamp_duration(duration_sec)
            w, h = _dims(aspect_ratio)
            inp = {
                "image": "data:image/png;base64," + base64.b64encode(image).decode(),
                "prompt": prompt, "num_frames": int(secs * _FPS), "fps": _FPS,
                "width": w, "height": h,
            }
            data = self._run(inp)
            if data:
                return VideoResult(video_bytes=data, provider=self.name, meta={"mode": "real"})
        # free/offline baseline — generative motion when an endpoint is deployed, pan/zoom until then
        return kenburns_clip(self.name, image, duration_sec, aspect_ratio, motion)

    # -- RunPod 호출 (Qwen provider 와 동일 메커니즘) --------------------------
    def _run(self, inp: dict) -> Optional[bytes]:
        self.last_error = None
        try:
            import httpx

            if self.extra_input:
                inp = {**inp, **self.extra_input}
            headers = {"Authorization": f"Bearer {self.api_key}"}
            with httpx.Client(timeout=600) as client:   # video gen is slow
                e = self.endpoint.rstrip("/")
                if e.startswith("http"):
                    r = client.post(e, headers=headers, json={"input": inp})
                    base = None
                else:
                    e = f"https://api.runpod.ai/v2/{e}/runsync"
                    r = client.post(e, headers=headers, json={"input": inp})
                    base = e.rsplit("/", 1)[0]
                if r.status_code >= 400:
                    self.last_error = f"HTTP {r.status_code}: {r.text[:300]}"
                    return None
                body = r.json()
                if base:
                    body = self._await(body, base, client, headers)
                    if body is None:
                        self.last_error = self.last_error or "job did not complete (failed/timeout)"
                        return None
                out = body.get("output", body) if isinstance(body, dict) else body
                data = _parse_video(out, client)
                if data is None:
                    shape = list(out.keys()) if isinstance(out, dict) else type(out).__name__
                    self.last_error = (f"no video found in worker output (shape: {shape}; "
                                       f"preview: {_preview(out)})")
                return data
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None

    def _await(self, body, base, client, headers, max_polls: int = 600):
        """Poll /status when runsync returns an async job (video can take minutes)."""
        import time

        status = body.get("status") if isinstance(body, dict) else None
        if status in (None, "COMPLETED"):
            return body
        if status in ("FAILED", "CANCELLED", "TIMED_OUT"):
            return None
        jid = body.get("id")
        if not jid:
            return body
        for _ in range(max_polls):
            time.sleep(1)
            r = client.get(f"{base}/status/{jid}", headers=headers)
            r.raise_for_status()
            b = r.json()
            s = b.get("status")
            if s == "COMPLETED":
                return b
            if s in ("FAILED", "CANCELLED", "TIMED_OUT"):
                return None
        return None

    def diagnose(self) -> dict:
        """Self-test the RunPod video connection — used by GET /api/providers/video/test."""
        if not (self.api_key and self.endpoint):
            return {"configured": False,
                    "detail": "set RUNPOD_API_KEY and RUNPOD_VIDEO_ENDPOINT in .env "
                              "(deploy an open-source I2V worker: Wan 2.2 / LTX-Video / SVD)"}
        import io

        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (64, 64), (200, 60, 40)).save(buf, format="PNG")
        data = self._run({
            "image": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(),
            "prompt": "connectivity self-test, gentle camera push-in",
            "num_frames": 16, "fps": _FPS, "width": 512, "height": 512,
        })
        return {
            "configured": True,
            "endpoint_kind": "custom_url" if self.endpoint.startswith("http") else "serverless_id",
            "real_call_ok": data is not None,
            "video_bytes": len(data) if data else 0,
            "error": None if data is not None else self.last_error,
            "hint": None if data is not None else
            "Tune RUNPOD_VIDEO_INPUT (JSON) to match your worker's input fields, "
            "or share the output shape above if the video key is unusual.",
        }
