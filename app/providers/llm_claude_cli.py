"""LLM via the local Claude CLI (`claude -p`) — CLAUDE.md §5 provider 추상화.

API 키 없이 사용자의 기존 Claude 로그인을 그대로 사용한다(브리프/스펙/콘티/컨셉 생성).
opt-in: CLAUDE_CLI=1. 호출은 `claude -p <user> --system-prompt <sys> --output-format json`
으로 단발 완성만 수행 — MCP/도구를 끄고(--strict-mcp-config 빈 설정) 에이전트 루프를 피한다.
실패/미설치/타임아웃 시 mock_fn 으로 폴백(키리스-오프라인 원칙 유지).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Callable, Dict, Optional

from app.config import settings
from app.providers.base import LLMProvider
from app.providers.llm_claude import _extract_json

# env vars the parent process may inject that make a nested `claude` load the agentic
# harness (slow / empty completions). Stripped so the CLI behaves as a clean one-shot.
_STRIP_ENV_PREFIXES = ("CLAUDE_CODE", "CLAUDECODE", "ANTHROPIC_BASE_URL")

# all built-in tools — disallowed so `claude -p` answers in one turn (no agentic loop)
_DISALLOWED_TOOLS = (
    "Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch",
    "Task", "NotebookEdit", "TodoWrite", "BashOutput", "KillShell",
)


def _child_env() -> dict:
    return {k: v for k, v in os.environ.items()
            if not any(k.startswith(p) or k == p for p in _STRIP_ENV_PREFIXES)}


class ClaudeCLIProvider(LLMProvider):
    name = "claude-cli"

    # circuit breaker: after this many consecutive failures (timeout/empty/error), stop
    # calling the CLI for a cooldown so the whole pipeline doesn't wait the full timeout on
    # every step when the CLI is unavailable. Cleared on the first success.
    _FAIL_THRESHOLD = 2
    _COOLDOWN_SEC = 90.0

    def __init__(self) -> None:
        self.bin = shutil.which("claude")
        self.model = settings.claude_cli_model
        self.timeout = settings.claude_cli_timeout
        self.last_error: Optional[str] = None
        self._consec_fail = 0
        self._circuit_until = 0.0

    @property
    def is_real(self) -> bool:
        return bool(self.bin) and settings.claude_cli_enabled and not settings.force_mock

    def _circuit_open(self) -> bool:
        import time
        return time.monotonic() < self._circuit_until

    def _note_failure(self) -> None:
        import time
        self._consec_fail += 1
        if self._consec_fail >= self._FAIL_THRESHOLD:
            self._circuit_until = time.monotonic() + self._COOLDOWN_SEC

    def _note_success(self) -> None:
        self._consec_fail = 0
        self._circuit_until = 0.0

    # -- LLMProvider 인터페이스 ----------------------------------------------
    def complete_json(
        self,
        system: str,
        user: str,
        mock_fn: Optional[Callable[[], Dict[str, Any]]] = None,
        schema_hint: Optional[Dict[str, Any]] = None,
        max_tokens: int = 4000,
        images: Optional[list] = None,
    ) -> Dict[str, Any]:
        # Skip the CLI entirely if it's unavailable OR the circuit is open (recently failed),
        # so a broken/unreachable CLI doesn't make every step wait the full timeout.
        if not self.is_real or self._circuit_open():
            if mock_fn is None:
                raise RuntimeError("claude CLI unavailable and no mock_fn provided")
            return mock_fn()
        sys_prompt = system
        if schema_hint:
            sys_prompt += (
                "\n\nOutput a single JSON object matching the schema below, and nothing else. "
                "No prose, no code fences.\n" + json.dumps(schema_hint, ensure_ascii=False)
            )
        # vision (image) path uses stream-json so we can attach image content blocks
        text = self._invoke_vision(sys_prompt, user, images) if images else self._invoke(sys_prompt, user)
        if text is None:
            self._note_failure()                 # timeout/empty/error → trip the breaker
        else:
            self._note_success()
            try:
                return _extract_json(text)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"unparseable CLI output: {exc}"   # CLI works; bad output, no trip
        # CLI failed/empty/unparseable → deterministic fallback
        if mock_fn is not None:
            return mock_fn()
        raise RuntimeError(self.last_error or "claude CLI returned no usable output")

    def complete_text(self, system: str, user: str, max_tokens: int = 1000) -> str:
        if not self.is_real:
            return ""
        return self._invoke(system, user) or ""

    # -- CLI 호출 -------------------------------------------------------------
    def _invoke(self, system: str, user: str) -> Optional[str]:
        self.last_error = None
        cmd = [
            self.bin, "-p", user,
            "--system-prompt", system,
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            # force a single-turn text completion: with every built-in tool disallowed the
            # model can't run an agentic loop (which otherwise ends with an empty `result`).
            "--disallowed-tools", *_DISALLOWED_TOOLS,
            "--output-format", "json",
        ]
        if self.model:
            cmd += ["--model", self.model]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=self.timeout, env=_child_env())
        except subprocess.TimeoutExpired:
            self.last_error = f"claude CLI timed out after {self.timeout}s"
            return None
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None
        if r.returncode != 0:
            self.last_error = f"claude CLI exit {r.returncode}: {(r.stderr or '')[:300]}"
            return None
        # --output-format json → envelope {"type":"result","result":"<text>", ...}
        try:
            env = json.loads(r.stdout)
            res = env.get("result") if isinstance(env, dict) else None
            if isinstance(res, str) and res.strip():
                return res
            self.last_error = "claude CLI returned an empty result"
            return None
        except Exception:
            # not JSON → treat raw stdout as the text (in case of a plain-text response)
            return r.stdout.strip() or None

    def _invoke_vision(self, system: str, user: str, images: list) -> Optional[str]:
        """Send images to `claude -p` for analysis via stream-json input (vision).
        The CLI requires stream-json for BOTH input and output when images are attached."""
        self.last_error = None
        import base64

        content = [{"type": "text", "text": user}]
        for img in images[:4]:                      # cap attachments
            if not img:
                continue
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png",
                           "data": base64.b64encode(img).decode()},
            })
        message = {"type": "user", "message": {"role": "user", "content": content}}
        cmd = [
            self.bin, "-p",
            "--system-prompt", system,
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--disallowed-tools", *_DISALLOWED_TOOLS,
            "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose",
        ]
        if self.model:
            cmd += ["--model", self.model]
        try:
            r = subprocess.run(cmd, input=json.dumps(message) + "\n", capture_output=True,
                               text=True, timeout=self.timeout, env=_child_env())
        except subprocess.TimeoutExpired:
            self.last_error = f"claude CLI (vision) timed out after {self.timeout}s"
            return None
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None
        if r.returncode != 0:
            self.last_error = f"claude CLI (vision) exit {r.returncode}: {(r.stderr or '')[:300]}"
            return None
        # stream-json output = newline-delimited events; the final {"type":"result"} carries text
        result = None
        for line in (r.stdout or "").splitlines():
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if isinstance(ev, dict) and ev.get("type") == "result" and isinstance(ev.get("result"), str):
                result = ev["result"]
        if result and result.strip():
            return result
        self.last_error = "claude CLI (vision) returned an empty result"
        return None

    def diagnose(self) -> dict:
        """Self-test the Claude CLI backend — used by GET /api/providers/llm/test."""
        if not self.bin:
            return {"available": False, "detail": "`claude` CLI not found on PATH"}
        if not settings.claude_cli_enabled:
            return {"available": True, "enabled": False,
                    "detail": "set CLAUDE_CLI=1 in .env to use the Claude CLI for generation"}
        text = self._invoke(
            "You are a JSON API. Output only the requested JSON object.",
            'Reply with ONLY this JSON: {"ok": true}',
        )
        ok = False
        if text:
            try:
                ok = bool(_extract_json(text).get("ok"))
            except Exception:
                ok = False
        return {
            "available": True, "enabled": True,
            "model": self.model or "(CLI default)",
            "real_call_ok": ok,
            "sample": (text or "")[:120],
            "error": None if ok else self.last_error,
            "hint": None if ok else
            "Run `claude -p \"hi\"` in a normal terminal to confirm you're logged in; "
            "set CLAUDE_CLI_MODEL for a faster model, CLAUDE_CLI_TIMEOUT to allow longer calls.",
        }
