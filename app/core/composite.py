"""텍스트·CTA·로고 합성 (CLAUDE.md §4.8, §5 — Pillow, AI 생성 아님).

한글 텍스트는 모델 생성에 의존하지 않고 이 결정론적 합성 레이어에서 처리(기본).
8단계 static_image 출력의 핵심.
"""
from __future__ import annotations

import io
from typing import List, Optional

from PIL import Image, ImageDraw, ImageFont

from app.core import fonts
from app.core.schemas import TextOverlay


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in fonts.candidates():
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _hex(color: str) -> tuple:
    color = color.lstrip("#")
    if len(color) == 3:
        color = "".join(c * 2 for c in color)
    try:
        return tuple(int(color[i : i + 2], 16) for i in (0, 2, 4)) + (255,)
    except Exception:
        return (255, 255, 255, 255)


def _y_for(position: str, img_h: int, block_h: int) -> int:
    pos = (position or "bottom").lower()
    if pos == "top":
        return int(img_h * 0.08)
    if pos == "center":
        return (img_h - block_h) // 2
    return int(img_h * 0.86) - block_h


def _draw_text_block(
    base: Image.Image,
    text: str,
    position: str,
    size: int,
    color: str,
) -> None:
    if not text:
        return
    draw = ImageDraw.Draw(base)
    font = _font(size)
    w, h = base.size
    # 줄바꿈
    max_chars = max(8, int(w / (size * 0.62)))
    import textwrap

    lines = []
    for raw in text.split("\n"):
        lines.extend(textwrap.wrap(raw, width=max_chars) or [""])
    line_h = int(size * 1.25)
    block_h = line_h * len(lines)
    y0 = _y_for(position, h, block_h)

    # 반투명 가독성 배경 띠
    pad = int(size * 0.4)
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle([0, y0 - pad, w, y0 + block_h + pad], fill=(0, 0, 0, 110))
    base.alpha_composite(overlay)

    fill = _hex(color)
    y = y0
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        lw = bbox[2] - bbox[0]
        x = (w - lw) // 2
        draw.text((x, y), line, font=font, fill=fill, stroke_width=max(2, size // 24),
                  stroke_fill=(0, 0, 0, 255))
        y += line_h


def _paste_logo(base: Image.Image, logo_bytes: bytes) -> None:
    try:
        logo = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
    except Exception:
        return
    w, h = base.size
    target_w = int(w * 0.22)
    ratio = target_w / logo.width
    logo = logo.resize((target_w, int(logo.height * ratio)))
    margin = int(w * 0.04)
    base.alpha_composite(logo, (w - logo.width - margin, margin))


def upscale_image(image_bytes: bytes, factor: int = 2) -> bytes:
    """Offline upscale baseline — Lanczos resample (CLAUDE.md §9; Topaz/SeedVR with a key)."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img = img.resize((img.width * factor, img.height * factor), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def resize_cover(image_bytes: bytes, target_w: int, target_h: int) -> bytes:
    """object-cover 리사이즈: 비율 채우고 중앙 크롭 (멀티 플랫폼 export)."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    scale = max(target_w / w, target_h / h)
    nw, nh = max(target_w, round(w * scale)), max(target_h, round(h * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - target_w) // 2, (nh - target_h) // 2
    img = img.crop((left, top, left + target_w, top + target_h))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def compose(
    image_bytes: bytes,
    headline: str = "",
    cta: str = "",
    overlays: Optional[List[TextOverlay]] = None,
    logo_bytes: Optional[bytes] = None,
    palette: Optional[List[str]] = None,
) -> bytes:
    """이미지 위에 헤드라인/CTA/추가 오버레이/로고 합성 → PNG bytes."""
    base = Image.open(io.BytesIO(image_bytes)).convert("RGBA")

    accent = (palette[0] if palette else "#FFFFFF")

    if headline:
        _draw_text_block(base, headline, "top", size=max(40, base.width // 16), color="#FFFFFF")

    for ov in overlays or []:
        _draw_text_block(base, ov.text, ov.position, ov.size, ov.color)

    if cta:
        # CTA 버튼 느낌 — accent 색 박스
        draw = ImageDraw.Draw(base)
        w, h = base.size
        pad_x, pad_y = 40, 26
        max_bw = int(w * 0.92)                     # button must fit inside the image
        size = max(34, base.width // 22)
        font = _font(size)
        bbox = draw.textbbox((0, 0), cta, font=font)
        # auto-fit: shrink the CTA font until the button fits — a long CTA used to
        # overflow both edges (bw > w → negative bx) and run off the deliverable.
        while (bbox[2] - bbox[0]) + pad_x * 2 > max_bw and size > 18:
            size -= 2
            font = _font(size)
            bbox = draw.textbbox((0, 0), cta, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        bw, bh = min(tw + pad_x * 2, max_bw), th + pad_y * 2
        bx = (w - bw) // 2
        by = int(h * 0.9) - bh
        draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=bh // 2, fill=_hex(accent))
        # 버튼 글자색은 대비 위해 검정/흰 자동
        r, g, b, _ = _hex(accent)
        text_color = (0, 0, 0, 255) if (r + g + b) / 3 > 140 else (255, 255, 255, 255)
        draw.text((bx + pad_x, by + pad_y - bbox[1]), cta, font=font, fill=text_color)

    if logo_bytes:
        _paste_logo(base, logo_bytes)

    out = io.BytesIO()
    base.convert("RGB").save(out, format="PNG")
    return out.getvalue()
