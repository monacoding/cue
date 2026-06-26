"""파이프라인 단계 (CLAUDE.md §4). 각 단계는 ProjectState 를 읽고 갱신한다."""
from __future__ import annotations

import io
import ipaddress
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from PIL import Image

from app.core import state as state_store

# Cap remote fetches (product pages, images, logos) so a malicious/accidental huge
# response can't exhaust memory before we ever inspect it (decompression bombs, GB bodies).
MAX_FETCH_BYTES = 20 * 1024 * 1024  # 20 MB


@dataclass
class FetchResult:
    """Bounded response: only what callers need (.content / .text), never an unbounded body."""
    content: bytes
    encoding: str
    url: str

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8", "replace")


def is_safe_remote_url(url: str) -> bool:
    """SSRF 방어: 사용자 제공 URL이 공인 http(s) 대상인지 검사.

    호스트를 해석해 사설/루프백/링크로컬(169.254 메타데이터)/예약 IP면 거부.
    (주의: DNS 리바인딩 TOCTOU까지 막진 않음 — 흔한 SSRF 케이스 차단용 방어선.)
    """
    try:
        u = urlparse(url)
    except Exception:
        return False
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    try:
        infos = socket.getaddrinfo(u.hostname, None)
    except Exception:
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (
            addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified
        ):
            return False
    return True


def _safe_asset_path(adir: Path, filename: str) -> Path:
    """Resolve filename inside adir and verify it can't escape.

    Asset filenames are derived from shot ids (e.g. f"{shot.id}_v0.png"), and shot
    ids are attacker-controllable via PUT /storyboard — a malicious id like
    "../../evil" would otherwise write outside the assets dir (arbitrary file write).
    """
    if not filename or "/" in filename or "\\" in filename or filename in (".", ".."):
        raise ValueError(f"invalid asset filename: {filename!r}")
    adir_r = adir.resolve()
    path = (adir_r / filename).resolve()
    if path.parent != adir_r:
        raise ValueError(f"asset path escapes storage: {filename!r}")
    return path


def save_asset(project_id: str, filename: str, data: bytes) -> str:
    """bytes 를 프로젝트 assets 에 저장하고 정적 URL 경로를 반환."""
    adir = state_store.assets_dir(project_id)
    path = _safe_asset_path(adir, filename)
    path.write_bytes(data)
    return f"/storage/projects/{project_id}/assets/{filename}"


def decode_image_data_uri(data: str) -> bytes:
    """Decode a data-URI or bare-base64 image payload to raw bytes, size-bounded by
    MAX_FETCH_BYTES. Raises ValueError (→ HTTP 400 via the app's handler) on
    malformed / empty / oversized input. Shared by upload + recipe-extract endpoints."""
    import base64

    raw = data.split(",", 1)[-1] if data.startswith("data:") else data
    try:
        blob = base64.b64decode(raw, validate=False)
    except Exception as e:
        raise ValueError("invalid base64 image data") from e
    if not blob:
        raise ValueError("empty image")
    if len(blob) > MAX_FETCH_BYTES:
        raise ValueError("image too large (max 20 MB)")
    return blob


def save_image_with_recipe(project_id: str, filename: str, image_bytes: bytes, recipe) -> str:
    """Save a generated image, embedding its GenerationRecipe into the PNG metadata so the
    downloaded file carries its own provenance (ComfyUI-inspired reproducibility, §1)."""
    from app.core import recipe as recipe_mod

    data = recipe_mod.embed(image_bytes, recipe) if recipe is not None else image_bytes
    return save_asset(project_id, filename, data)


def url_to_local(project_id: str, url: str) -> Optional[Path]:
    """저장된 에셋 URL → 로컬 경로."""
    if not url:
        return None
    fname = url.rsplit("/", 1)[-1]
    try:
        p = _safe_asset_path(state_store.assets_dir(project_id), fname)
    except ValueError:
        return None
    return p if p.exists() else None


def read_asset_bytes(project_id: str, url: str) -> Optional[bytes]:
    p = url_to_local(project_id, url)
    return p.read_bytes() if p else None


def safe_fetch(url: str, *, timeout: float = 30, max_redirects: int = 3,
               max_bytes: int = MAX_FETCH_BYTES):
    """SSRF-safe GET: validates EVERY hop (auto-redirects disabled), since a public
    URL can 302 to an internal one. Streams the body with a hard size cap so an
    oversized response can't exhaust memory. Returns a FetchResult or None.
    """
    if not is_safe_remote_url(url):
        return None  # validate before opening any connection
    # a realistic browser UA + Accept headers — many product pages 403 a bot-looking UA
    # (note: heavy anti-bot sites like Coupang still block server fetches regardless)
    browser_headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    client = httpx.Client(timeout=timeout, follow_redirects=False)
    try:
        for _ in range(max_redirects + 1):
            with client.stream("GET", url, headers=browser_headers) as r:
                if r.is_redirect:
                    loc = r.headers.get("location")
                    if not loc:
                        return None
                    url = str(r.url.join(loc))           # resolve (possibly relative) target
                    if not is_safe_remote_url(url):       # re-validate each redirect hop
                        return None
                    continue
                r.raise_for_status()
                # fast-reject an honestly-declared oversized body…
                cl = r.headers.get("content-length")
                if cl and cl.isdigit() and int(cl) > max_bytes:
                    return None
                # …and hard-cap the actual read (covers chunked / lying Content-Length)
                total, chunks = 0, []
                for chunk in r.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        return None
                    chunks.append(chunk)
                return FetchResult(b"".join(chunks), r.encoding or "utf-8", str(r.url))
        return None  # too many redirects
    except Exception:
        return None
    finally:
        client.close()


import re as _re

_CTA_ACTIONS = ("shop", "get", "try", "start", "buy", "join", "discover", "grab",
                "order", "save", "book", "claim")
_CTA_RE = _re.compile(r"\b(?:" + "|".join(_CTA_ACTIONS) + r")\b", _re.IGNORECASE)


def pick_cta(shots_text, headline: str = "", brand: str = "") -> str:
    """Pick the action-led CTA caption from a storyboard's on-screen lines.

    Scans shots from the END (the CTA beat is last) and matches action verbs on WORD
    boundaries — so a body caption like "Forget the noise" / "Budget-friendly" / "Target your
    goals" is NOT mistaken for a CTA (substring matching used to promote those). Falls back to
    a branded, action-led CTA. Shared by render (step8) and the expert-panel evaluator."""
    head = (headline or "").strip()
    for t in reversed(list(shots_text)[1:]):
        t = (t or "").strip()
        if t and t != head and _CTA_RE.search(t):
            return t
    return f"Shop {brand} today" if brand else "Shop now"


def resolve_image_bytes(project_id: str, url: str) -> Optional[bytes]:
    """Image bytes from EITHER a local project asset URL (/storage/.../assets/…, e.g. an
    uploaded/pasted image) OR a remote URL (SSRF-safe fetch). Lets the unified brief input
    mix attached images and hosted URLs transparently."""
    if not url:
        return None
    if "/assets/" in url or url.startswith("/storage/"):
        local = read_asset_bytes(project_id, url)
        if local is not None:
            return local
    return fetch_image_bytes(url)


def fetch_image_bytes(url: str) -> Optional[bytes]:
    """외부 이미지 URL 페치 (제품 이미지 등). 실패/비안전 URL/리다이렉트 우회 시 None."""
    if not url:
        return None
    r = safe_fetch(url, timeout=30)
    if r is None:
        return None
    try:
        img = Image.open(io.BytesIO(r.content)).convert("RGB")  # PNG 정규화
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None
