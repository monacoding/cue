# RunPod Serverless worker — Qwen-Image + Myeongdong LoRA

Serves your trained LoRA to Cue. The app sends `{"input": {prompt, width, height, seed, image?}}`
and expects an image back; this worker returns `{"image_base64": "..."}`.

## What you do while the LoRA trains

1. **Keep the base model + LoRA on a RunPod Network Volume** (so the serverless endpoint can mount them):
   - `Qwen-Image/` (the base you already downloaded into `DiffSynth-Studio/models/Qwen/Qwen-Image`)
   - `loras/myeongdong.safetensors` (your training output, when done)
2. **Confirm the 2 model-load lines** in `handler.py::_load_pipe()` against the Qwen-Image inference
   example in your `DiffSynth-Studio/examples/` (copy the exact `from_pretrained` / `load_lora` calls
   from a script you've run successfully — the API differs slightly by version).
3. **Build & push the image**, then create the serverless endpoint.

## Build & deploy

```
cd ad-factory/runpod
docker build -t YOURUSER/cue-qwen-lora:latest .
docker push YOURUSER/cue-qwen-lora:latest
```

In RunPod → **Serverless → New Endpoint**:
- Container image: `YOURUSER/cue-qwen-lora:latest`
- Attach the **Network Volume** (mounts at `/runpod-volume`)
- GPU: 24GB+ (Qwen-Image is large)
- Env (override if your paths differ): `MODEL_DIR`, `LORA_PATH`, `LORA_ALPHA`

Copy the **Endpoint ID**.

## Connect Cue

In `ad-factory/.env`:
```
RUNPOD_API_KEY=your_key
RUNPOD_QWEN_ENDPOINT=your_endpoint_id
RUNPOD_QWEN_STYLE=myeongdong_street
```
Then verify: `curl http://localhost:8011/api/providers/qwen/test` → `real_call_ok: true`.

In Studio, pick **Image model = Qwen** and **Background = 명동 …** on any image.

## Notes
- First request is slow (cold start loads the model). Keep 1 worker warm for snappy generation.
- LoRA strength: tune `LORA_ALPHA` (0.6–1.0) for how strongly the Myeongdong look applies.
- Trigger word `myeongdong_street` is auto-added to prompts via `RUNPOD_QWEN_STYLE` + the background presets.
