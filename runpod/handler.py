"""RunPod Serverless handler — Qwen-Image + your Myeongdong LoRA (DiffSynth-Studio).

Contract (matches Cue's QwenRunPodProvider):
  request  {"input": {"prompt": str, "width": int, "height": int,
                       "seed": int?, "image": "data:image/png;base64,..."?,   # img2img/edit
                       "lora": str?, "lora_strength": float?,                  # via RUNPOD_QWEN_INPUT
                       "num_inference_steps": int?, "guidance_scale": float?, "negative_prompt": str?}}
  response {"image_base64": "<base64 PNG>"}     # the app also accepts image/image_url/images/...

The model + LoRA are loaded ONCE at cold start and reused across requests.
Set MODEL_DIR / LORA_PATH env vars (point at a RunPod Network Volume, e.g. /runpod-volume/...).
"""
import base64
import io
import os
import time

import runpod
import torch
from PIL import Image

MODEL_DIR = os.getenv("MODEL_DIR", "/workspace/DiffSynth-Studio/models/Qwen/Qwen-Image")
LORA_PATH = os.getenv("LORA_PATH", "/runpod-volume/loras/myeongdong.safetensors")
LORA_ALPHA = float(os.getenv("LORA_ALPHA", "1.0"))

_pipe = None


def _load_pipe():
    """Cold-start: build the Qwen-Image pipeline and attach the LoRA.

    NOTE: confirm these two lines against the Qwen-Image example you trained with
    (DiffSynth-Studio/examples/qwen_image/…). The import path / from_pretrained args can
    differ slightly by DiffSynth-Studio version — copy them from your working inference script.
    """
    global _pipe
    if _pipe is not None:
        return _pipe
    t = time.time()
    from diffsynth.pipelines.qwen_image import ModelConfig, QwenImagePipeline

    pipe = QwenImagePipeline.from_pretrained(
        torch_dtype=torch.bfloat16,
        device="cuda",
        model_configs=[
            ModelConfig(path=os.path.join(MODEL_DIR, "transformer")),
            ModelConfig(path=os.path.join(MODEL_DIR, "text_encoder")),
            ModelConfig(path=os.path.join(MODEL_DIR, "vae")),
        ],
        tokenizer_config=ModelConfig(path=os.path.join(MODEL_DIR, "tokenizer")),
    )
    if LORA_PATH and os.path.exists(LORA_PATH):
        pipe.load_lora(pipe.dit, LORA_PATH, alpha=LORA_ALPHA)
        print(f"[handler] LoRA loaded: {LORA_PATH} (alpha={LORA_ALPHA})")
    else:
        print(f"[handler] WARNING: LoRA not found at {LORA_PATH} — running base model only")
    print(f"[handler] pipeline ready in {time.time() - t:.1f}s")
    _pipe = pipe
    return pipe


def _decode_data_uri(s: str):
    if not s:
        return None
    raw = s.split(",", 1)[-1] if s.startswith("data:") else s
    return Image.open(io.BytesIO(base64.b64decode(raw))).convert("RGB")


def handler(event):
    inp = event.get("input", {}) or {}
    prompt = (inp.get("prompt") or "").strip()
    if not prompt:
        return {"error": "no prompt"}
    width = int(inp.get("width", 768))
    height = int(inp.get("height", 1344))
    seed = inp.get("seed")
    steps = int(inp.get("num_inference_steps", 30))
    cfg = float(inp.get("guidance_scale", 4.0))
    neg = inp.get("negative_prompt", "")
    init = _decode_data_uri(inp.get("image"))     # present for edit/img2img

    pipe = _load_pipe()
    kwargs = dict(prompt=prompt, negative_prompt=neg, height=height, width=width,
                  num_inference_steps=steps, cfg_scale=cfg)
    if seed is not None:
        kwargs["seed"] = int(seed)
    if init is not None:
        kwargs["input_image"] = init             # img2img; adjust kw to your pipeline if needed
        kwargs["denoising_strength"] = float(inp.get("denoising_strength", 0.7))

    image = pipe(**kwargs)
    if isinstance(image, (list, tuple)):
        image = image[0]
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return {"image_base64": base64.b64encode(buf.getvalue()).decode()}


runpod.serverless.start({"handler": handler})
