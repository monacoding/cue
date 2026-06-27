"""1단계 — 제품 입력 & 정보 수집 (CLAUDE.md §4.1).

입력: 제품 URL 또는 이미지/텍스트.
처리: URL 페치 + (제품 이미지 비전 분석) → 구조화 → AdSpec.product.
모델: Claude (claude-opus/sonnet). 키 없으면 휴리스틱 mock.
"""
from __future__ import annotations

import re
from typing import Dict, List


from app.core import state as state_store
from app.core.schemas import AdSpec, PipelineStep, Product, StepStatus
from app.providers.registry import get_llm


_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp")

# units that signal a concrete, verifiable claim (proof) — used to rank extracted clauses
_PROOF_UNIT = re.compile(
    r"\b(hours?|hrs?|minutes?|mins?|seconds?|secs?|days?|nights?|weeks?|months?|years?|"
    r"x|times|%|percent|calories|cal|bar|watts?|w|lbs?|kg|oz|ounces?|inch|inches|cm|mm|"
    r"k\+?|million|monitors?)\b",
    re.IGNORECASE,
)


def _extract_proofs(text: str, limit: int = 3) -> List[str]:
    """Pull concrete proof claims (short clauses carrying a number) from the source text,
    e.g. "40-hour battery", "100-night trial", "removes 10x more plaque". Numeric clauses are
    the strongest specificity/proof signal; vague clauses are skipped. Returns up to `limit`."""
    out: List[str] = []
    seen = set()
    for raw in re.split(r"[.;,\n]+", text or ""):
        c = raw.strip(" .—–-:•\t")
        if not c or not any(ch.isdigit() for ch in c):
            continue
        words = c.split()
        if not (1 <= len(words) <= 8):
            continue
        # keep clauses that read like a claim: a number plus a unit or a few descriptive words
        if not (_PROOF_UNIT.search(c) or len(words) >= 2):
            continue
        key = c.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(c[:60])
        if len(out) >= limit:
            break
    return out


def _looks_like_image_url(u: str) -> bool:
    """True for a direct image link (ignoring any query string)."""
    return any(u.lower().split("?", 1)[0].rstrip("/").endswith(e) for e in _IMG_EXTS)


def _meta(html: str, key: str, attr: str = "property") -> str:
    """Read a <meta {attr}="key" content="…"> value (content may precede the key attr).
    HTML entities are unescaped — e.g. an og:image URL with `&amp;` must become `&` or the
    fetch breaks."""
    import html as _html

    k = re.escape(key)
    m = re.search(rf'<meta[^>]+{attr}=["\']{k}["\'][^>]+content=["\']([^"\']*)', html, re.I) \
        or re.search(rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+{attr}=["\']{k}["\']', html, re.I)
    return _html.unescape(re.sub(r"\s+", " ", m.group(1)).strip()) if m else ""


def _fetch_url_text(url: str) -> dict:
    """Fetch a product page → {"text", "image"}. Prefers structured OpenGraph/meta tags
    (og:title/description/image, meta description) — the cleanest product signal — and
    falls back to <title> + a crude body strip. og:image lets us auto-attach the product
    photo. Returns empty strings on any failure (caller surfaces a notice)."""
    from app.pipeline import safe_fetch

    empty = {"text": "", "image": ""}
    if not url:
        return empty
    r = safe_fetch(url, timeout=20)  # per-hop SSRF validation (redirect-safe)
    if r is None:
        return empty
    try:
        html = r.text
    except Exception:
        return empty

    og_title = _meta(html, "og:title")
    og_desc = _meta(html, "og:description")
    og_image = _meta(html, "og:image")
    meta_desc = _meta(html, "description", attr="name")
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.I)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    # structured signals first so the extractor latches onto the real product info
    parts = [p for p in (og_title or title, og_desc or meta_desc, body) if p]
    return {"text": "\n".join(parts)[:4000], "image": og_image}


def run(
    project_id: str,
    product_url: str = "",
    product_text: str = "",
    image_urls: List[str] = None,
) -> AdSpec:
    image_urls = image_urls or []
    st = state_store.load(project_id)
    if st is None:
        raise ValueError("project not found")

    # A direct image link (…/product.jpg) is a product photo, not a page to scrape — route it.
    if product_url and _looks_like_image_url(product_url):
        if product_url not in image_urls:
            image_urls = image_urls + [product_url]
        product_url = ""

    fetched = _fetch_url_text(product_url)
    scraped = fetched["text"]
    source = "\n\n".join(filter(None, [product_text, scraped]))
    # auto-attach the page's OpenGraph image as a product image (if not already provided)
    if fetched["image"] and fetched["image"] not in image_urls:
        image_urls = image_urls + [fetched["image"]]

    def mock_fn() -> Dict:
        # Heuristic extraction (no LLM, no vision): when there's no text to read (e.g. an
        # image-only brief in mock mode) return blanks — never fabricate "New product".
        if not source.strip():
            return {"name": "", "description": "", "key_message": ""}
        text = source.strip()
        first_line = text.split("\n")[0].strip() or text[:40]
        # name = the brand token(s): the part before a separator (— - : .) OR a linking verb
        # ("is/are/that/helps…"), capped to a few words so a full sentence with no early
        # separator (e.g. "Brando is a premium widget…") doesn't become a 40-char mid-word slug.
        head = re.split(
            r"\s[—\-–:]\s|[.:]\s|\s+(?:is|are|was|were|that|which|helps|lets|makes|gives)\s+",
            first_line, maxsplit=1,
        )[0].strip(" —-–:")
        name = " ".join(head.split()[:5]).strip()[:40] or first_line[:40]
        desc = text[:280]
        # key_message: a sentence with a number or a benefit cue, different from the name
        sentences = [s.strip() for s in re.split(r"[.\n]+", text) if len(s.strip()) >= 12]
        cues = ("no ", "without", "fits", "brew", "in seconds", "anywhere", "fast",
                "easy", "free", "instant", "save", "more", "less", "best")
        key = ""
        for s in sentences:
            low = s.lower()
            if s != name and (any(c.isdigit() for c in s) or any(w in low for w in cues)):
                key = s
                break
        if not key:
            # fallback: the descriptive remainder after the name on line 1, else next sentence
            rest = first_line[len(name):].lstrip(" —-–:").strip()
            key = rest or (sentences[1] if len(sentences) > 1 else name)
        return {"name": name, "description": desc, "key_message": key[:80],
                "proof_points": _extract_proofs(text)}

    system = (
        "You are an ad strategist. Extract the core ad information from the product source "
        "(web scrape / user input). Produce name (product name), description (2-3 sentences), "
        "key_message (one-line core value proposition), and proof_points (up to 3 concrete, "
        "verifiable claims with numbers/specifics, e.g. '40-hour battery', '100-night trial')."
    )
    user = (f"Product source:\n{source or '(no text provided — read the product from the attached image)'}"
            f"\nURL: {product_url}")
    schema = {"name": "str", "description": "str", "key_message": "str",
              "proof_points": ["str"]}

    have_text = bool(source.strip())
    # Gather image bytes (local uploads or remote URLs) so a vision-capable LLM can read the
    # product straight from the photo. Falls back to mock_fn (blanks for image-only) otherwise.
    from app.pipeline import resolve_image_bytes
    img_bytes = []
    for u in image_urls[:3]:
        b = resolve_image_bytes(project_id, u)
        if b:
            img_bytes.append(b)

    if have_text or img_bytes:
        data = get_llm().complete_json(system, user, mock_fn=mock_fn,
                                       schema_hint=schema, images=img_bytes or None)
    else:
        data = {"name": "", "description": "", "key_message": ""}

    # Use extracted values; fall back to anything the user already entered (don't wipe it).
    prev = st.adspec.product if st.adspec else None

    def _pick(field: str) -> str:
        v = (data.get(field) or "").strip()
        return v or (getattr(prev, field, "") if prev else "")

    # proof_points: model may return a list (or omit it); fall back to extracting from the
    # source text, then to any previously-captured proofs. Normalize to ≤3 clean strings.
    raw_proofs = data.get("proof_points")
    if not isinstance(raw_proofs, list) or not raw_proofs:
        raw_proofs = _extract_proofs(source) or (getattr(prev, "proof_points", []) if prev else [])
    proof_points = [str(p).strip()[:60] for p in raw_proofs if str(p).strip()][:3]

    product = Product(name=_pick("name"), description=_pick("description"),
                      key_message=_pick("key_message"), image_urls=image_urls,
                      proof_points=proof_points)
    if st.adspec is None:
        st.adspec = AdSpec(project_id=project_id, format=st.format, product=product)
    else:
        st.adspec.product = product

    got_content = bool(product.name.strip() or product.description.strip())
    # Tell the user when auto-extraction couldn't produce details, so blank fields aren't a mystery.
    if product_url and not scraped:
        st.adspec.brief_notice = (
            "Couldn't read that URL automatically — the site likely blocks automated access. "
            "Paste the product name/description or drop a product image instead."
        )
    elif image_urls and not have_text and not got_content:
        st.adspec.brief_notice = (
            "Couldn't read product details from the image automatically. Add the product name "
            "and a short description below — your image is kept as the visual reference. "
            "(Tip: enable CLAUDE_CLI in a normal terminal for image understanding.)"
        )
    else:
        st.adspec.brief_notice = ""

    # re-extracted product → drop concepts tied to the previous product (avoid a stale hook
    # leaking into the storyboard). _invalidate_concepts is defined below.
    _invalidate_concepts(st)
    st.set_step_status(PipelineStep.brief, StepStatus.generated)
    st.current_step = PipelineStep.brief
    state_store.save(st)
    return st.adspec


def update_product(
    project_id: str,
    name: str = None,
    description: str = None,
    key_message: str = None,
    image_urls: List[str] = None,
) -> AdSpec:
    """사람이 직접 수정한 제품 필드를 그대로 영속 (재추출 없음).

    브리프 자동 추출과 분리 — 사람 개입 우선 (CLAUDE.md §2.2).
    """
    st = state_store.load(project_id)
    if st is None:
        raise ValueError("project not found")
    if st.adspec is None:
        st.adspec = AdSpec(project_id=project_id, format=st.format)
    p = st.adspec.product
    changed = False
    if name is not None and name != p.name:
        p.name = name
        changed = True
    if description is not None and description != p.description:
        p.description = description
        changed = True
    if key_message is not None and key_message != p.key_message:
        p.key_message = key_message
        changed = True
    if image_urls is not None:
        p.image_urls = image_urls
    # product changed → concepts/selected hook from the OLD product are stale and must not
    # leak into a regenerated storyboard (shot-1 hook). Clear them so the next storyboard /
    # concept eval rebuilds from the new product.
    if changed:
        _invalidate_concepts(st)
    st.set_step_status(PipelineStep.brief, StepStatus.approved)
    state_store.save(st)
    return st.adspec


def _invalidate_concepts(st) -> None:
    """Drop concepts/selected-concept tied to a previous product (called on a product change)."""
    if getattr(st, "adspec", None) is not None and getattr(st.adspec, "concepts", None):
        st.adspec.concepts = []
    if hasattr(st, "concepts"):
        st.concepts = []
    if hasattr(st, "selected_concept_id"):
        st.selected_concept_id = None
