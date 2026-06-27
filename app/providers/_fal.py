"""Shared fal.run helpers — image call with error capture + connectivity diagnose.

fal image providers (Flux, Krea 2) used to swallow failures silently and fall back to a
mock, so a keyed-but-failing account (e.g. 403 "exhausted balance") looked like the model
"just didn't run". These helpers return the failure REASON so providers can surface it
(meta.reason) and a /api/providers/fal/test endpoint can report it.
"""
from __future__ import annotations

from typing import Optional, Tuple


def fal_image_call(endpoint: str, payload: dict, fal_key: str,
                   timeout: float = 120) -> Tuple[Optional[bytes], Optional[str]]:
    """POST to https://fal.run/{endpoint} and fetch the first image.

    Returns (image_bytes, None) on success, or (None, reason) on failure — the reason
    includes the HTTP status + body (e.g. fal's "exhausted balance" message)."""
    try:
        import httpx

        with httpx.Client(timeout=timeout) as client:
            r = client.post(f"https://fal.run/{endpoint}",
                            headers={"Authorization": f"Key {fal_key}"}, json=payload)
            status = getattr(r, "status_code", 200)
            if status >= 400:
                return None, f"HTTP {status}: {r.text[:200]}"
            imgs = (r.json() or {}).get("images") or []
            if not imgs:
                return None, "no images in fal response"
            img = client.get(imgs[0]["url"])
            img.raise_for_status()
            return img.content, None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def fal_diagnose(name: str, endpoint: str, fal_key: str, payload: dict) -> dict:
    """Self-test a fal image endpoint — used by GET /api/providers/fal/test."""
    if not fal_key:
        return {"provider": name, "configured": False, "detail": "set FAL_KEY in .env"}
    data, err = fal_image_call(endpoint, payload, fal_key)
    return {"provider": name, "configured": True, "real_call_ok": data is not None,
            "image_bytes": len(data) if data else 0, "error": err}
