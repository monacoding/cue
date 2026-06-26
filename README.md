# 🏭 Cue — AI Ad Production Pipeline (SaaS)

Feed in a product and Cue generates **Brief → Spec → Storyboard → Images → Edit → Output**,
with a human approval gate at every generation step.

> Core philosophy: the value is not *generation* but **ad intelligence** (which hook/concept converts)
> plus a human-in-the-loop UX that converges fast. The generators are swappable parts (`providers/`).

## Quick start

```bash
cd ad-factory
./run.sh
# → http://localhost:8000
```

**It runs without any API keys.** When a key is missing, each provider falls back to a deterministic
mock (placeholder images, heuristic storyboards/concepts, ffmpeg-synthesized clips & music) so you can
experience the whole pipeline end-to-end offline. Real generation switches on automatically once you
add keys to `.env`:

```bash
ANTHROPIC_API_KEY=...   # brief / spec / storyboard / concept eval (Claude)
GEMINI_API_KEY=...      # image gen · edit · consistency (Nano Banana 2)
FAL_KEY=...             # image fallback (Flux) / video (Seedance)
ELEVENLABS_API_KEY=...  # voiceover & music
RUNPOD_API_KEY=...      # Qwen image model on RunPod (preferred image provider when set)
RUNPOD_QWEN_ENDPOINT=...# serverless endpoint id, or a full custom-pod URL
CLAUDE_CLI=1            # OR: use the local Claude CLI (`claude -p`) — your existing login, no API key
FREE_IMAGES=1          # OR: free AI images via Pollinations.ai (Flux) — real images, NO API key
```

**Free images, no key (`FREE_IMAGES=1`):** generate real AI product images via **Pollinations.ai**
(open Flux) with no API key — great for testing/demo. Paid providers (Qwen/Nano Banana/Flux) take
precedence when keyed; otherwise free Pollinations is used, falling back to the deterministic mock on
failure. With the offline **ffmpeg Ken Burns** video baseline you get real image **and** video ads
end-to-end without any paid key.

**LLM via the Claude CLI (no API key):** set `CLAUDE_CLI=1` and Cue drives brief/spec/storyboard/
concept generation through your locally-installed, logged-in `claude` CLI instead of an Anthropic API key.
It runs `claude -p … --output-format json` as an isolated one-shot (MCP/tools off), parses the `result`,
and falls back to the deterministic mock on any failure. Backend priority: **Claude CLI →
`ANTHROPIC_API_KEY` → mock**. Verify with **`GET /api/providers/llm/test`**. Tune via `CLAUDE_CLI_MODEL`
(faster model) and `CLAUDE_CLI_TIMEOUT`. Note: CLI calls are slower than the API (the CLI loads a session
each call) — fine for the per-step human-in-the-loop flow.

**Image on Qwen/RunPod:** set `RUNPOD_API_KEY` + `RUNPOD_QWEN_ENDPOINT` and the pipeline uses your RunPod
Qwen worker for hero/shot/edit generation (falling back to Nano Banana → Flux on error). The worker
contract assumed: request `{"input": {"prompt", "width", "height", "seed"?, "image"? (data-URI for edit)}}`,
response `output` as base64/URL, `{"image"|"image_url"|"image_base64"|"b64_json"|"url"|"data"}`, an
`images`/`artifacts` list (incl. a **list of dicts** like ComfyUI's `{"images":[{"data": b64}]}`), or a
nested `{"output": ...}` — all robustly parsed. Tune to any
worker without code changes via `RUNPOD_QWEN_INPUT` (JSON merged into every request, e.g.
`{"negative_prompt":"blurry","num_inference_steps":30,"guidance_scale":4}`). Verify the connection any
time with **`GET /api/providers/qwen/test`** (or the "🔌 Test RunPod Qwen" button on the Hero step) — it
attempts a real generation and reports success or the exact failure (HTTP status / output shape).

## Output formats

| Format | Pipeline | Status |
|---|---|---|
| `static_image` | steps 1–6 + 8 (text/logo compositing) | ✅ |
| `ugc_video` / `cinematic_video` | steps 1–9 (image-to-video → xfade assemble → encode) | ✅ |

Video uses **Seedance 2.0** image-to-video when `FAL_KEY` is set, otherwise an offline **ffmpeg Ken Burns**
(zoom/pan) baseline. Each clip has its on-screen text burned in, clips are joined with `xfade`, a **music
bed** is mixed (ElevenLabs when keyed, else a procedural bed), and the result is encoded to a platform
preset (9:16, etc.) H.264/MP4. Long video generation runs through an **async job queue** with polling and
a live progress bar (so real, minutes-long generation never blocks the UI).

## Highlights

- **Ad intelligence drives generation** — weighted-rubric concept evaluation ranks hooks; the selected
  concept seeds the storyboard, and that copy flows into the final render. **A/B variants**: render the ad
  with each top concept's hook, compare scores, and pick the winner.
- **Human-in-the-loop everywhere** — generate → preview → approve. Storyboard add/remove/reorder/edit,
  hero & per-shot natural-language editing, per-clip video regeneration. Cost gate (with cost estimate)
  blocks video until all shots are approved.
- **Swappable providers across every modality** — image (**Qwen on RunPod**→Nano Banana→Flux), **video (Seedance/Kling/Veo)**,
  **music (ElevenLabs/MiniMax)** — user-selectable, auto-fallback, keyless-offline.
- **Output options** — multi-format export (selectable 9:16/1:1/4:5/16:9), **beat-sync** cut timing,
  **upscale** (Lanczos / Topaz). Multi-project switch/delete/**duplicate**, version history + revert.
- **Polished, accessible UI** — dark SaaS design, phase-grouped stepper, keyboard nav, ARIA roles,
  focus management, loading skeletons, async progress with reconnect. English throughout.
- **Production-ready** — structured logging, safe error handling, input validation, path-traversal/SSRF/XSS
  hardening (independent security audit), atomic persistence.
- **Tests** — `python -m pytest -q` (215 tests, 93% coverage; pipeline, providers, jobs, security, API,
  video/audio/export/async covered when ffmpeg is present).

## Architecture

```
app/
  main.py            FastAPI — per-step human-in-the-loop API + static UI/asset serving
  config.py          .env loader (key presence → real/mock automatically)
  core/
    schemas.py       AdSpec · Storyboard · ProjectState · ExportVariant (pydantic)
    state.py         disk JSON persistence + atomic save + version snapshots/revert
    jobs.py          async job queue (video generation, polled via /api/jobs/{id})
    consistency.py   hero → shot consistency (user-selectable: reference / seed / edit)
    composite.py     Pillow headline/CTA/logo compositing + object-cover resize
    assembly.py      ffmpeg: Ken Burns, xfade, drawtext burn-in, music bed, encode
    fonts.py         shared font resolver (env override + macOS/Noto CJK)
  providers/         ★ every generator behind one interface — swap in registry.py
    base.py          interface (generate_image/edit_image/image_to_video/tts/music/complete_json)
    llm_claude.py · llm_claude_cli.py (`claude -p`) · image_nanobanana.py · image_flux.py · video_seedance.py · audio_elevenlabs.py
    registry.py      provider selection + automatic image fallback
  pipeline/          step1_brief … step9_encode (per-step logic)
  eval/concept_eval.py   weighted-rubric concept evaluation (the ad intelligence)
  web/index.html     single-page UI (stepper, image/clip grids, concept ranking, export, versions)
storage/projects/<id>/   state · assets · versions
```

## Design principles (CLAUDE.md §2)
1. **Provider abstraction** — the pipeline never touches SDKs; only `registry.py` does.
2. **Human-in-the-loop** — every step is "generate → preview → await approval". No auto-advance.
3. **Deterministic assembly first** — text/logo via Pillow, clip joins/encode via ffmpeg (not AI).
4. **State & version persistence** — a version per human action; re-enter any step.
5. **Cost gating** — video (step 7) only after all shots are approved (`step7.assert_gate`).
6. **Cost safety** — shot count / duration are clamped; bad inputs return clean 400s.

## Concurrency & operational limits
- **One editor per project.** Synchronous mutations (`load → mutate → save`) assume a single
  active editor per project; the model is single-user-per-project, not multi-writer. Concurrent
  **generation** is safe regardless — the async job queue dedupes in-flight work per
  `(project, kind)`, so a double-click can't launch two writers to the same assets.
- **Remote fetches are capped** at 20 MB and stream with a hard byte limit (product pages,
  images, logos), with per-redirect-hop SSRF re-validation — a hostile/huge URL can't exhaust memory.
- **Provider duration windows** are clamped to each model's documented range (Seedance 4–15s,
  Kling 5–10s, Veo 4–8s, ElevenLabs music ≥10s) so a typical ~3s shot isn't silently rejected
  into the offline fallback. Final assembly uses each clip's **probed** length, not the requested one.
- **Image fallback prefers real over mock** — if the primary provider (e.g. a down RunPod Qwen
  worker) can only mock, a configured real fallback (Nano Banana → Flux) is used instead.

## Key API
Interactive OpenAPI docs at **`/docs`** (schema at `/openapi.json`), grouped by tag.
- `GET /api/health` · `GET /` (UI) · `GET/POST/DELETE /api/projects[/{id}]` · `POST /{id}/duplicate`
- `POST /api/projects` — create + run step-1 brief; `PUT /api/projects/{id}/product` — edit brief; `POST /brief`
- `POST /api/projects/{id}/upload-image` — attach a pasted/dropped image (base64 → saved asset URL) for the unified brief input
- `POST /api/projects/{id}/spec` · `/spec/approve`
- `POST /api/projects/{id}/storyboard[?concept_id=]` · `PUT /storyboard` (edit/add/remove/reorder) · `/storyboard/approve`
- `POST /api/projects/{id}/concepts?n=` (rubric-ranked) · `/storyboard/beatsync` (beat-sync durations)
- `POST /api/projects/{id}/hero` · `/hero/approve`
- `POST /api/projects/{id}/shots[?shot_id=&strategy=reference|seed|edit]` → `{job_id}` (async) · `/shots/{shot}/edit` · `/shots/approve`
- `GET  /api/projects/{id}/video/gate` · `/cost-estimate` · `/active-job?kind=` (reconnect)
- `POST /api/projects/{id}/video[?shot_id=&model=seedance|kling|veo]` → `{job_id}`; `GET /api/jobs/{job_id}` — poll
- `POST /api/projects/{id}/render` · `/render/approve` · `/export?ratios=` · `/upscale?factor=`
- `POST /api/projects/{id}/render/variants?n=` (A/B) · `/render/variants/select?concept_id=` (pick winner)
- `GET  /api/projects/{id}/versions` · `POST /revert?version=` · `GET /download-all` (zip of all deliverables)

## Tests
```bash
source .venv/bin/activate
pip install -r requirements-dev.txt   # runtime deps + pytest
python -m pytest -q                    # 202 tests
python scripts/smoke.py                # keyless end-to-end smoke
```

## Deploy
A `Dockerfile` (python 3.12-slim + ffmpeg + Noto CJK fonts) is provided. Keys are injected at runtime;
without them the container still runs fully offline.
