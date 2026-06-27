"""System & provider-status routes + UI/asset serving (tag: system)."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse, Response

from app.config import settings
from app.core import assembly

router = APIRouter(tags=["system"])


def _claude_cli_active() -> bool:
    """True when the Claude CLI LLM backend is enabled and the binary is present."""
    import shutil
    return settings.claude_cli_enabled and bool(shutil.which("claude"))


@router.get("/api/providers/qwen/test")
def qwen_connectivity_test():
    """Self-test the fal Qwen connection: attempts a real generation and reports
    success or the failure reason (HTTP status / parse / exception). Lets the user
    verify the model + any trained LoRA the moment they add the FAL_KEY."""
    from app.providers.registry import _qwen_image

    return _qwen_image().diagnose()


@router.get("/api/providers/llm/test")
def llm_cli_test():
    """Self-test the Claude CLI LLM backend: runs a tiny `claude -p` completion and
    reports whether it produced valid JSON (or why not). Lets the user verify the CLI
    is connected for brief/spec/storyboard/concept generation."""
    from app.providers.llm_claude_cli import ClaudeCLIProvider

    return ClaudeCLIProvider().diagnose()


@router.get("/api/providers/fal/test")
def fal_connectivity_test():
    """Self-test the fal image providers (Flux, Krea 2). Surfaces the real failure reason
    (e.g. 403 'exhausted balance') instead of letting a keyed account silently fall back to
    a mock. Each entry: {provider, configured, real_call_ok, error}."""
    from app.providers.image_flux import FluxProvider
    from app.providers.image_krea import KreaProvider

    return {"flux": FluxProvider().diagnose(), "krea": KreaProvider().diagnose()}


@router.get("/api/providers/video/test")
def video_connectivity_test():
    """Self-test the RunPod open-source image-to-video worker: attempts a real generation
    and reports success or the failure reason. Lets the user verify their I2V endpoint
    (Wan 2.2 / LTX-Video / SVD) the moment they deploy it."""
    from app.providers.video_runpod import RunPodVideoProvider

    return RunPodVideoProvider().diagnose()


@router.get("/api/health")
def health():
    return {
        "ok": True,
        "providers": {
            "claude": (bool(settings.anthropic_api_key) or _claude_cli_active())
            and not settings.force_mock,
            "claude_cli": _claude_cli_active() and not settings.force_mock,
            "nano_banana": bool(settings.gemini_api_key) and not settings.force_mock,
            "fal": bool(settings.fal_key) and not settings.force_mock,
            "elevenlabs": bool(settings.elevenlabs_api_key) and not settings.force_mock,
            "qwen_runpod": bool(settings.runpod_api_key and settings.runpod_qwen_endpoint)
            and not settings.force_mock,
            "free_images": bool(settings.free_images) and not settings.force_mock,
        },
        "ffmpeg": assembly.ffmpeg_available(),  # required for video formats
        "mock_mode": settings.force_mock
        or not (
            (settings.anthropic_api_key or _claude_cli_active())
            and (settings.gemini_api_key or settings.free_images
                 or (settings.runpod_api_key and settings.runpod_qwen_endpoint))
        ),
    }


@router.get("/api/providers/image-models")
def image_models():
    """Per-image model picker options + availability (Qwen / Nano Banana / Flux / Free)."""
    from app.providers.registry import image_models_status

    return image_models_status()


@router.get("/api/providers/backgrounds")
def background_options():
    """Per-image background scenes (your trained LoRA captions) for the background picker."""
    from app.core import backgrounds

    return backgrounds.options()


@router.get("/api/templates")
def ad_templates():
    """Category presets (tone/platform/duration) that pre-fill the step-2 spec form."""
    from app.core import templates

    return templates.options()


_FAVICON = (  # CUE — a play/cue triangle in a lime rounded badge
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<rect width='32' height='32' rx='8' fill='#c8f750'/>"
    "<path d='M13 9 L24 16 L13 23 Z' fill='#10180a'/></svg>"
).encode()


@router.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(_FAVICON, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/")
def index():
    return FileResponse(str(settings.web_dir / "index.html"))
