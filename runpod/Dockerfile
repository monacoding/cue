# RunPod Serverless worker — Qwen-Image + Myeongdong LoRA (DiffSynth-Studio)
# Base: a CUDA + PyTorch image. Pin to the CUDA your endpoint GPU supports.
FROM runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04

WORKDIR /app

# DiffSynth-Studio (match the version you trained with)
RUN pip install --no-cache-dir "git+https://github.com/modelscope/DiffSynth-Studio.git"
RUN pip install --no-cache-dir runpod pillow

COPY handler.py /app/handler.py

# The Qwen-Image base model + your LoRA are large — DON'T bake them in.
# Mount a RunPod Network Volume and point these at it:
ENV MODEL_DIR=/runpod-volume/Qwen-Image \
    LORA_PATH=/runpod-volume/loras/myeongdong.safetensors \
    LORA_ALPHA=1.0

CMD ["python", "-u", "handler.py"]
