"""Claude 오케스트레이션 provider (CLAUDE.md §5).

연결: Anthropic API / ANTHROPIC_API_KEY / claude-opus | claude-sonnet
키가 없거나 SDK 미설치 시 → 호출자가 제공한 mock_fn 으로 결정론적 폴백.

※ 모델 문자열은 호출 직전 공식 문서로 확인 (config.ANTHROPIC_MODEL).
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, Optional

from app.config import settings
from app.providers.base import LLMProvider


def _extract_json(text: str) -> Dict[str, Any]:
    """모델 출력에서 첫 JSON 객체 추출."""
    text = text.strip()
    # ```json ... ``` 펜스 제거
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object in response")
    return json.loads(text[start : end + 1])


class ClaudeProvider(LLMProvider):
    name = "claude"

    def __init__(self) -> None:
        self.api_key = settings.anthropic_api_key
        self.model = settings.anthropic_model
        self._client = None
        if self.is_real:
            try:
                import anthropic  # type: ignore

                self._client = anthropic.Anthropic(api_key=self.api_key)
            except Exception:
                self._client = None

    @property
    def is_real(self) -> bool:
        return bool(self.api_key) and not settings.force_mock

    def complete_json(
        self,
        system: str,
        user: str,
        mock_fn: Optional[Callable[[], Dict[str, Any]]] = None,
        schema_hint: Optional[Dict[str, Any]] = None,
        max_tokens: int = 4000,
        images: Optional[list] = None,
    ) -> Dict[str, Any]:
        if self._client is None:
            if mock_fn is None:
                raise RuntimeError("Claude unavailable and no mock_fn provided")
            return mock_fn()

        sys_prompt = system
        if schema_hint:
            sys_prompt += (
                "\n\nOutput a single JSON object matching the schema below, and nothing else. "
                "No prose, no code fences.\n" + json.dumps(schema_hint, ensure_ascii=False)
            )
        # multimodal: attach image blocks alongside the text when provided (vision)
        if images:
            import base64
            content = [{"type": "text", "text": user}]
            for img in images[:4]:
                if img:
                    content.append({"type": "image", "source": {
                        "type": "base64", "media_type": "image/png",
                        "data": base64.b64encode(img).decode()}})
            user_content = content
        else:
            user_content = user
        try:
            msg = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=sys_prompt,
                messages=[{"role": "user", "content": user_content}],
            )
            text = "".join(
                block.text for block in msg.content if getattr(block, "type", "") == "text"
            )
            return _extract_json(text)
        except Exception:
            if mock_fn is not None:
                return mock_fn()
            raise

    def complete_text(self, system: str, user: str, max_tokens: int = 1000) -> str:
        if self._client is None:
            return ""
        msg = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(
            block.text for block in msg.content if getattr(block, "type", "") == "text"
        )
