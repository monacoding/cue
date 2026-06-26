"""Qwen image model hosted on RunPod (CLAUDE.md §2 provider 추상화, §5 이미지 대안).

연결: RunPod Serverless / RUNPOD_API_KEY + RUNPOD_QWEN_ENDPOINT
  - RUNPOD_QWEN_ENDPOINT = serverless 엔드포인트 ID  → https://api.runpod.ai/v2/{id}/runsync (+ async /run·/status 폴백)
  - 또는 전체 URL(커스텀 pod/vLLM 등) → 그 URL로 동기 POST.
키/엔드포인트 없으면 결정론적 mock (다른 provider와 동일하게 키 없이 동작).

워커 계약(가정, 배포에 따라 다름 — 조정 가능):
  요청  {"input": {"prompt": str, "width": int, "height": int, "seed": int?, "image": dataURI?}}
  응답  output 이 다음 중 하나: base64 문자열 / URL 문자열 / {"image"|"image_url"|"image_base64"|"url": ...} /
        {"images": [...]} / 중첩 {"output": ...}
"""
from __future__ import annotations

import base64
from typing import List, Optional

from app.config import settings
from app.providers import _mockgen
from app.providers.base import ImageProvider, ImageResult

# 생성 해상도(가로:세로) — 8/64 배수 권장
_GEN_DIMS = {
    "9:16": (768, 1344),
    "16:9": (1344, 768),
    "1:1": (1024, 1024),
    "4:5": (896, 1152),
}


def _dims(aspect_ratio: str):
    return _GEN_DIMS.get(aspect_ratio, (768, 1344))


def _to_bytes(s, client) -> Optional[bytes]:
    """base64 문자열 또는 URL → bytes."""
    if not isinstance(s, str) or not s:
        return None
    if s.startswith("http"):
        try:
            r = client.get(s)
            r.raise_for_status()
            return r.content
        except Exception:
            return None
    if s.startswith("data:"):
        s = s.split(",", 1)[-1]
    try:
        return base64.b64decode(s)
    except Exception:
        return None


# image-bearing string keys seen across RunPod / ComfyUI / A1111 / OpenAI-style workers
_IMG_STR_KEYS = ("image", "image_url", "image_base64", "b64_json", "base64",
                 "image_b64", "url", "data")
# keys whose value is a nested container to recurse into
_IMG_NEST_KEYS = ("images", "output", "data", "result", "artifacts")


def _preview(out, limit: int = 400) -> str:
    """A short JSON preview of a worker response for diagnostics — long string values
    (e.g. a base64 blob under an unexpected key) are clipped so the structure/keys stay
    visible without dumping megabytes."""
    def clip(v):
        if isinstance(v, str) and len(v) > 60:
            return v[:40] + f"…<{len(v)} chars>"
        if isinstance(v, dict):
            return {k: clip(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [clip(x) for x in v[:4]]
        return v
    try:
        import json
        return json.dumps(clip(out), ensure_ascii=False)[:limit]
    except Exception:
        return str(out)[:limit]


def _parse_output(out, client) -> Optional[bytes]:
    """워커 output 의 흔한 형태들을 robust 하게 파싱해 이미지 bytes 반환.

    Handles: bare base64 / data-URI / URL string; list (tries each item, e.g.
    {"images": ["b64", ...]}); dict with a string image key; and a *list of dicts*
    like ComfyUI's {"images": [{"data": "b64"}]} or {"artifacts": [{"base64": ...}]}.
    """
    if out is None:
        return None
    if isinstance(out, str):
        return _to_bytes(out, client)
    if isinstance(out, (list, tuple)):
        for item in out:                       # try each — first item may be null/metadata
            b = _parse_output(item, client)
            if b:
                return b
        return None
    if isinstance(out, dict):
        for k in _IMG_STR_KEYS:                 # direct string value → decode/fetch
            v = out.get(k)
            if isinstance(v, str) and v:
                b = _to_bytes(v, client)
                if b:                           # fall through if this key didn't yield bytes
                    return b
        for k in _IMG_NEST_KEYS:                # nested container → recurse
            v = out.get(k)
            if v is not None and not isinstance(v, str):
                b = _parse_output(v, client)
                if b:
                    return b
    return None


class QwenRunPodProvider(ImageProvider):
    name = "qwen-runpod"
    max_reference_images = 1

    def __init__(self) -> None:
        self.api_key = settings.runpod_api_key
        self.endpoint = settings.runpod_qwen_endpoint
        self.extra_input = self._parse_extra(settings.runpod_qwen_input)
        self.style = (settings.runpod_qwen_style or "").strip()  # LoRA trigger / style phrase
        self.last_error: Optional[str] = None  # set by _run for diagnostics

    def _styled(self, prompt: str) -> str:
        """Append the LoRA trigger / style phrase so the trained look is activated."""
        p = (prompt or "").strip()
        if self.style and self.style.lower() not in p.lower():
            return f"{p}, {self.style}" if p else self.style
        return p

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

    # -- ImageProvider 인터페이스 ---------------------------------------------
    def generate_image(
        self,
        prompt: str,
        aspect_ratio: str = "9:16",
        reference_images: Optional[List[bytes]] = None,
        seed: Optional[int] = None,
    ) -> ImageResult:
        if self.is_real:
            w, h = _dims(aspect_ratio)
            inp = {"prompt": self._styled(prompt), "width": w, "height": h}
            if seed is not None:
                inp["seed"] = seed
            data = self._run(inp)
            if data:
                return ImageResult(image_bytes=data, provider=self.name, meta={"mode": "real"})
        anchor = f"seed{seed}" if seed is not None else ("ref" if reference_images else "")
        data = _mockgen.make_image(prompt, aspect_ratio, anchor=anchor, label="QWEN·MOCK")
        return ImageResult(image_bytes=data, provider=self.name + "·mock", meta={"mode": "mock"})

    def edit_image(
        self,
        image: bytes,
        instruction: str,
        reference_images: Optional[List[bytes]] = None,
        **_,  # base_prompt/seed — real img2img edits the bytes directly; ignored
    ) -> ImageResult:
        if self.is_real:
            inp = {
                "prompt": self._styled(instruction),
                "image": "data:image/png;base64," + base64.b64encode(image).decode(),
            }
            data = self._run(inp)
            if data:
                return ImageResult(image_bytes=data, provider=self.name, meta={"mode": "real"})
        data = _mockgen.edit_image(image, instruction)
        return ImageResult(image_bytes=data, provider=self.name + "·mock", meta={"mode": "mock"})

    # -- RunPod 호출 ----------------------------------------------------------
    def _run(self, inp: dict) -> Optional[bytes]:
        self.last_error = None
        try:
            import httpx

            if self.extra_input:
                inp = {**inp, **self.extra_input}  # env JSON overrides/extends (steps, cfg, negative_prompt, …)
            headers = {"Authorization": f"Bearer {self.api_key}"}
            with httpx.Client(timeout=300) as client:
                e = self.endpoint.rstrip("/")
                if e.startswith("http"):
                    r = client.post(e, headers=headers, json={"input": inp})  # custom full URL
                else:
                    e = f"https://api.runpod.ai/v2/{e}/runsync"  # serverless endpoint id
                    r = client.post(e, headers=headers, json={"input": inp})
                if r.status_code >= 400:
                    self.last_error = f"HTTP {r.status_code}: {r.text[:300]}"
                    return None
                body = r.json()
                if not e.startswith("https://api.runpod.ai"):
                    base = None
                else:
                    base = e.rsplit("/", 1)[0]
                if base:
                    body = self._await(body, base, client, headers)
                    if body is None:
                        self.last_error = self.last_error or "job did not complete (failed/timeout)"
                        return None
                out = body.get("output", body) if isinstance(body, dict) else body
                data = _parse_output(out, client)
                if data is None:
                    shape = list(out.keys()) if isinstance(out, dict) else type(out).__name__
                    # include a truncated raw preview so the user can see their worker's
                    # actual response shape and map it (set RUNPOD_QWEN_INPUT / report keys)
                    self.last_error = (
                        f"no image found in worker output (output shape: {shape}; "
                        f"preview: {_preview(out)})"
                    )
                return data
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None

    def diagnose(self) -> dict:
        """Self-test the RunPod connection — used by GET /api/providers/qwen/test.
        Attempts a real generation (bypassing force_mock) and reports success/why-not."""
        if not (self.api_key and self.endpoint):
            return {"configured": False,
                    "detail": "set RUNPOD_API_KEY and RUNPOD_QWEN_ENDPOINT in .env"}
        w, h = _dims("1:1")
        data = self._run({"prompt": "connectivity self-test, a small red square", "width": w, "height": h})
        return {
            "configured": True,
            "endpoint_kind": "custom_url" if self.endpoint.startswith("http") else "serverless_id",
            "real_call_ok": data is not None,
            "image_bytes": len(data) if data else 0,
            "error": None if data is not None else self.last_error,
            "hint": None if data is not None else
            "Tune RUNPOD_QWEN_INPUT (JSON) to match your worker's input fields, "
            "or share the output shape above if the image key is unusual.",
        }

    def _await(self, body, base, client, headers, max_polls: int = 180):
        """runsync 가 즉시 완료되지 않은 경우 /status 폴링."""
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
