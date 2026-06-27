"""Qwen-Image on fal — fal-ai/qwen-image-2512/lora (CLAUDE.md §2.1 provider 추상화, §5).

The preferred Qwen path: pay-per-use, zero idle cost, no infra (vs the RunPod worker).
The project already uses fal (`FAL_KEY`), so this reuses the shared `_fal` helpers.

Optional trained LoRA: set FAL_QWEN_LORA_URL (a .safetensors URL, e.g. the output of fal's
Qwen-Image LoRA trainer) + FAL_QWEN_STYLE (the trigger word). With no LoRA it runs the base
Qwen model. Keyless → deterministic mock, like every other provider.

fal input (fal-ai/qwen-image-2512/lora):
  {"prompt", "negative_prompt", "image_size", "num_inference_steps", "guidance_scale",
   "seed"?, "loras": [{"path": <url>, "scale": <float>}]}
"""
from __future__ import annotations

from typing import List, Optional

from app.config import settings
from app.providers import _mockgen
from app.providers.base import ImageProvider, ImageResult
from app.providers.image_flux import _fal_size  # reuse the aspect_ratio → fal image_size map


class QwenFalProvider(ImageProvider):
    name = "qwen"
    endpoint = "fal-ai/qwen-image-2512/lora"
    max_reference_images = 1
    _STEPS = 28
    _CFG = 4.0

    def __init__(self) -> None:
        self.fal_key = settings.fal_key
        self.lora_url = (settings.fal_qwen_lora_url or "").strip()
        self.lora_scale = settings.fal_qwen_lora_scale
        self.style = (settings.fal_qwen_style or "").strip()  # LoRA trigger / style phrase
        self.last_error: Optional[str] = None   # set by _call_fal for diagnostics

    @property
    def is_real(self) -> bool:
        return bool(self.fal_key) and not settings.force_mock

    def _styled(self, prompt: str) -> str:
        """Append the LoRA trigger / style phrase so the trained look is activated."""
        p = (prompt or "").strip()
        if self.style and self.style.lower() not in p.lower():
            return f"{p}, {self.style}" if p else self.style
        return p

    def _mock(self, prompt, aspect_ratio, reference_images, seed, *, edit_src=None,
              instruction="") -> ImageResult:
        if edit_src is not None:
            data = _mockgen.edit_image(edit_src, instruction)
        else:
            anchor = f"seed{seed}" if seed is not None else ("ref" if reference_images else "")
            data = _mockgen.make_image(prompt, aspect_ratio, anchor=anchor, label="QWEN·MOCK")
        meta = {"mode": "mock"}
        if self.is_real and self.last_error:        # keyed but the real call failed → surface why
            meta["fell_back"] = True
            meta["reason"] = self.last_error
        return ImageResult(image_bytes=data, provider=self.name + "·mock", meta=meta)

    def generate_image(
        self,
        prompt: str,
        aspect_ratio: str = "9:16",
        reference_images: Optional[List[bytes]] = None,
        seed: Optional[int] = None,
    ) -> ImageResult:
        if self.is_real:
            data = self._call_fal(self._styled(prompt), aspect_ratio, seed=seed)
            if data:
                return ImageResult(image_bytes=data, provider=self.name, meta={"mode": "real"})
        return self._mock(prompt, aspect_ratio, reference_images, seed)

    def edit_image(
        self,
        image: bytes,
        instruction: str,
        reference_images: Optional[List[bytes]] = None,
        base_prompt: Optional[str] = None,
        seed: Optional[int] = None,
    ) -> ImageResult:
        # text→image endpoint — re-render the SAME described scene (base_prompt) plus the
        # requested change with a locked seed, instead of producing a brand-new subject.
        if self.is_real and base_prompt:
            merged = f"{base_prompt}. Edit: {instruction}".strip()
            data = self._call_fal(self._styled(merged), "9:16", seed=seed)
            if data:
                return ImageResult(image_bytes=data, provider=self.name, meta={"mode": "real"})
        return self._mock("", "9:16", reference_images, seed, edit_src=image, instruction=instruction)

    # -- fal 호출 ------------------------------------------------------------
    def _payload(self, prompt: str, aspect_ratio: str, seed: Optional[int] = None) -> dict:
        payload = {"prompt": prompt, "image_size": _fal_size(aspect_ratio),
                   "num_inference_steps": self._STEPS, "guidance_scale": self._CFG}
        if seed is not None:
            payload["seed"] = seed  # lock the look across shots (seed strategy / reproduce)
        if self.lora_url:
            payload["loras"] = [{"path": self.lora_url, "scale": self.lora_scale}]
        return payload

    def _call_fal(self, prompt: str, aspect_ratio: str, seed: Optional[int] = None) -> Optional[bytes]:
        from app.providers._fal import fal_image_call

        data, self.last_error = fal_image_call(
            self.endpoint, self._payload(prompt, aspect_ratio, seed), self.fal_key)
        return data

    def diagnose(self) -> dict:
        """Self-test the fal Qwen connection — used by GET /api/providers/fal/test."""
        from app.providers._fal import fal_diagnose

        d = fal_diagnose(self.name, self.endpoint, self.fal_key,
                         self._payload("a small red square", "1:1"))
        d["lora"] = self.lora_url or None
        d["style"] = self.style or None
        return d
