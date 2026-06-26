"""Cue — FastAPI 엔트리 (CLAUDE.md §6, §7).

비동기 잡 오케스트레이션 + 단계별 human-in-the-loop API + 정적 UI/에셋 서빙.
모든 사람 개입 단계는 "생성 → 미리보기 반환 → 승인 대기" 패턴 (CLAUDE.md §10).

라우트는 도메인별로 app/routers/* 에 나눠 두고 여기서 조립한다.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.logging_setup import get_logger
from app.routers import jobs, output, pipeline, projects, recipe, system

_log = get_logger("cue.api")

app = FastAPI(
    title="CUE API",
    version="0.1.0",
    description=(
        "AI ad-production pipeline — brief → spec → storyboard → images → edit → "
        "output, with a human approval gate at every generation step. Runs keyless "
        "(deterministic mock/offline) and routes to real models when keys are set. "
        "Interactive docs at /docs, schema at /openapi.json."
    ),
    openapi_tags=[
        {"name": "projects", "description": "Create, list, get, delete, duplicate projects."},
        {"name": "pipeline", "description": "Per-step generation (brief→encode) with approval gates."},
        {"name": "jobs", "description": "Async generation jobs (video/shots) — submit and poll."},
        {"name": "output", "description": "Render, export, A/B variants, upscale, download."},
        {"name": "system", "description": "Health and UI."},
    ],
)

# 정적 에셋 (생성 이미지) 서빙
app.mount("/storage", StaticFiles(directory=str(settings.storage_dir)), name="storage")

# 도메인별 라우터 조립 (경로는 각 라우터가 보유, 태그는 라우터 단위)
for _r in (system, projects, pipeline, recipe, jobs, output):
    app.include_router(_r.router)


@app.exception_handler(ValueError)
async def value_error_handler(request, exc):  # noqa: ANN001
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def unhandled_error_handler(request, exc):  # noqa: ANN001
    # log the full error server-side; return a generic message (don't leak internals)
    _log.exception("unhandled error on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
