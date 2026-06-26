"""키 없이도 파이프라인이 눈에 보이게 동작하도록 결정론적 placeholder 이미지 생성.

Pillow 로 비율 맞춤 캔버스 + 프롬프트 해시 기반 그라데이션 + 프롬프트 텍스트를 렌더.
같은 프롬프트 → 같은 이미지 (결정론적). 일관성 데모를 위해 anchor seed 도 반영.
"""
from __future__ import annotations

import hashlib
import io
import textwrap
from typing import Tuple

from PIL import Image, ImageDraw, ImageFont

ASPECT_DIMS = {
    "9:16": (720, 1280),
    "16:9": (1280, 720),
    "1:1": (1024, 1024),
    "4:5": (864, 1080),
}


def _dims(aspect_ratio: str) -> Tuple[int, int]:
    return ASPECT_DIMS.get(aspect_ratio, (720, 1280))


def _seed_int(text: str) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16)


def _color_from(seed: int, salt: int) -> Tuple[int, int, int]:
    h = (seed >> (salt * 8)) & 0xFFFFFF
    return ((h >> 16) & 0xFF, (h >> 8) & 0xFF, h & 0xFF)


def _font(size: int) -> ImageFont.FreeTypeFont:
    from app.core import fonts

    for path in fonts.candidates():
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def make_image(
    prompt: str,
    aspect_ratio: str = "9:16",
    anchor: str = "",
    label: str = "MOCK",
) -> bytes:
    """프롬프트 기반 결정론적 placeholder PNG bytes."""
    w, h = _dims(aspect_ratio)
    seed = _seed_int(anchor + "::" + prompt)
    c1 = _color_from(seed, 0)
    c2 = _color_from(seed, 2)

    img = Image.new("RGB", (w, h), c1)
    draw = ImageDraw.Draw(img)
    # 수직 그라데이션
    for y in range(h):
        t = y / max(1, h - 1)
        col = tuple(int(c1[i] * (1 - t) + c2[i] * t) for i in range(3))
        draw.line([(0, y), (w, y)], fill=col)

    # anchor 색 띠 (일관성 데모 — 같은 anchor면 같은 띠)
    if anchor:
        band = _color_from(_seed_int(anchor), 1)
        draw.rectangle([0, 0, w, 12], fill=band)
        draw.rectangle([0, h - 12, w, h], fill=band)

    # 라벨 배지
    badge = _font(26)
    draw.rectangle([16, 16, 16 + 150, 16 + 42], fill=(0, 0, 0))
    draw.text((28, 24), label, font=badge, fill=(255, 255, 255))

    # 중앙 이미지 플레이스홀더 프레임 + 짧은 타이틀 (전체 프롬프트 덤프 대신 — 깔끔한 미리보기)
    gw = int(w * 0.46)
    gx, gy = (w - gw) // 2, h // 2 - gw // 2
    draw.rounded_rectangle([gx, gy, gx + gw, gy + gw], radius=22, outline=(255, 255, 255), width=4)
    # 간단한 "사진" 글리프 (해 + 산)
    r = gw // 9
    draw.ellipse([gx + gw - 3 * r, gy + r, gx + gw - r, gy + 3 * r], outline=(255, 255, 255), width=4)
    draw.line([(gx + r, gy + gw - r), (gx + gw * 0.45, gy + gw * 0.5),
               (gx + gw * 0.72, gy + gw - r)], fill=(255, 255, 255), width=4, joint="curve")
    # 짧은 타이틀 (첫 구절만)
    title = (prompt.strip().split(".")[0] or prompt.strip())[:60]
    body = _font(30)
    wrapped = textwrap.fill(title, width=max(14, w // 24))
    draw.multiline_text((40, gy + gw + 28), wrapped, font=body, fill=(255, 255, 255),
                        spacing=8, stroke_width=2, stroke_fill=(0, 0, 0))

    small = _font(20)
    draw.text((40, h - 48), f"preview · {aspect_ratio}", font=small, fill=(255, 255, 255))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def edit_image(image: bytes, instruction: str) -> bytes:
    """편집 mock — 원본에 편집 지시 띠를 합성하고 색조를 약간 변형."""
    img = Image.open(io.BytesIO(image)).convert("RGB")
    draw = ImageDraw.Draw(img)
    w, h = img.size
    seed = _seed_int(instruction)
    tint = _color_from(seed, 0)
    overlay = Image.new("RGB", (w, h), tint)
    img = Image.blend(img, overlay, 0.12)
    draw = ImageDraw.Draw(img)
    band_h = 70
    draw.rectangle([0, h - band_h, w, h], fill=(0, 0, 0))
    f = _font(26)
    draw.text((20, h - band_h + 20), "EDIT: " + instruction[:60], font=f, fill=(255, 255, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
