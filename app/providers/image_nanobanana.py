"""Nano Banana 2 이미지 provider (CLAUDE.md §4.4-4.6, §5).

연결: Google Gemini API / Vertex AI / GEMINI_API_KEY / gemini-3.1-flash-image
- 생성 + 편집 + 일관성 (최대 14개 레퍼런스 입력).
- 키 없으면 결정론적 mock (_mockgen).

※ SynthID 비가시 워터마크 포함됨(인지).
※ 모델 ID/엔드포인트는 호출 직전 공식 문서로 확인.
"""
from __future__ import annotations

from typing import List, Optional

from app.config import settings
from app.providers import _mockgen
from app.providers.base import ImageProvider, ImageResult


class NanoBananaProvider(ImageProvider):
    name = "nano-banana-2"
    max_reference_images = 14

    def __init__(self) -> None:
        self.api_key = settings.gemini_api_key
        self.model = settings.gemini_image_model
        self._client = None
        if self.is_real:
            try:
                from google import genai  # type: ignore

                self._client = genai.Client(api_key=self.api_key)
            except Exception:
                self._client = None

    @property
    def is_real(self) -> bool:
        return bool(self.api_key) and not settings.force_mock

    # -- 내부: 실제 Gemini 호출 ------------------------------------------------
    def _call_gemini(self, parts: list) -> Optional[bytes]:
        if self._client is None:
            return None
        try:
            from google.genai import types  # type: ignore

            resp = self._client.models.generate_content(
                model=self.model,
                contents=parts,
                config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
            )
            for cand in resp.candidates:
                for part in cand.content.parts:
                    if getattr(part, "inline_data", None) and part.inline_data.data:
                        return part.inline_data.data
        except Exception:
            return None
        return None

    # -- 인터페이스 ------------------------------------------------------------
    def generate_image(
        self,
        prompt: str,
        aspect_ratio: str = "9:16",
        reference_images: Optional[List[bytes]] = None,
        seed: Optional[int] = None,
    ) -> ImageResult:
        if self._client is not None:
            parts: list = [prompt + f"\n\n(aspect ratio: {aspect_ratio})"]
            for ref in (reference_images or [])[: self.max_reference_images]:
                try:
                    from google.genai import types  # type: ignore

                    parts.append(types.Part.from_bytes(data=ref, mime_type="image/png"))
                except Exception:
                    pass
            data = self._call_gemini(parts)
            if data:
                return ImageResult(image_bytes=data, provider=self.name, meta={"mode": "real"})

        # mock 폴백: seed(시드 전략) 또는 ref(레퍼런스 전략)로 일관성 anchor 구성
        anchor = f"seed{seed}" if seed is not None else ("ref" if reference_images else "")
        data = _mockgen.make_image(prompt, aspect_ratio, anchor=anchor, label="NANO·MOCK")
        return ImageResult(image_bytes=data, provider=self.name + "·mock", meta={"mode": "mock"})

    def edit_image(
        self,
        image: bytes,
        instruction: str,
        reference_images: Optional[List[bytes]] = None,
        **_,  # base_prompt/seed — real img2img edits the bytes directly; ignored
    ) -> ImageResult:
        if self._client is not None:
            try:
                from google.genai import types  # type: ignore

                parts: list = [
                    instruction,
                    types.Part.from_bytes(data=image, mime_type="image/png"),
                ]
                for ref in (reference_images or [])[: self.max_reference_images - 1]:
                    parts.append(types.Part.from_bytes(data=ref, mime_type="image/png"))
                data = self._call_gemini(parts)
                if data:
                    return ImageResult(image_bytes=data, provider=self.name, meta={"mode": "real"})
            except Exception:
                pass
        data = _mockgen.edit_image(image, instruction)
        return ImageResult(image_bytes=data, provider=self.name + "·mock", meta={"mode": "mock"})
