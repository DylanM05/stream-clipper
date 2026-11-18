"""Run Claude through the Claude Code CLI in headless mode (`claude -p`), signed in with your own subscription.

Each call runs in an empty temporary folder with every tool switched off, so Claude only sees what we
pipe in and answers with JSON matching a schema. Don't add `--bare`: it skips the subscription login.
"""

import json
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .base import LLM, LLMError, UsageLimitError, llm_schema

log = logging.getLogger(__name__)

__all__ = ["ClaudeCode", "llm_schema"]

# Older versions said "Claude AI usage limit reached|<epoch>"; now it's "You've hit your session limit · resets 3pm (…)",
# or "You've reached your …", the starts Claude Code itself tells its limits by.
_LIMIT = re.compile(r"usage limit|limit reached|rate.?limit|out of (extra )?usage|\b(hit|reached) your\b", re.IGNORECASE)
_RESET_EPOCH = re.compile(r"\|(\d{10})\b")


@dataclass
class ClaudeCode(LLM):
    bin: str = "claude"
    model: str = "opus"
    effort: str | None = "high"
    timeout_s: int = 3600
    name = "Claude"

    def command(self, task: str, system_prompt_file: Path, schema: type[BaseModel]) -> list[str]:
        cmd = [
            self.bin, "-p", task,
            "--output-format", "json",
            "--json-schema", json.dumps(llm_schema(schema)),
            "--model", self.model,
            "--system-prompt-file", str(system_prompt_file),
            # Claude only needs what we pipe in: no tools, no MCP servers, no saved session.
            "--tools", "",
            "--disallowedTools", "mcp__*",
            "--strict-mcp-config",
            "--permission-mode", "dontAsk",
            "--no-session-persistence",
        ]
        if self.effort:
            cmd += ["--effort", self.effort]
        return cmd

    def auth_status(self) -> str:
        try:
            proc = subprocess.run(
                [self.bin, "auth", "status"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60
            )
        except FileNotFoundError:
            return f"Claude Code CLI not found ({self.bin!r})"
        return (proc.stdout or proc.stderr).strip()

    def signed_in(self) -> bool:
        try:
            return bool(json.loads(self.auth_status()).get("loggedIn"))
        except (json.JSONDecodeError, AttributeError):
            return False

    def plan_usage(self) -> list[dict] | None:
        """The plan's usage meters, from Claude Code's /usage report, which asks the model nothing: each with how much
        is used (`percent`, 0-100), when it resets (`resets_at`) and what it measures (`kind`, `group`). None when
        Claude Code can't give them as data: signed in another way than with a claude.ai account, or too old."""
        with tempfile.TemporaryDirectory(prefix="clipper-usage-") as cwd:
            try:
                proc = subprocess.run(
                    [self.bin, "-p", "/usage", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
                     "--strict-mcp-config"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd, timeout=120,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                return None
        for line in proc.stdout.splitlines():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            report = message.get("usage_report") if isinstance(message, dict) else None
            if isinstance(report, dict):
                limits = (report.get("rate_limits") or {}).get("limits")
                return limits if isinstance(limits, list) else None
        return None

    def _ask(self, system_prompt: str, task: str, stdin_text: str, schema: type[BaseModel]) -> tuple[dict[str, Any], Any]:
        payload = self._run(system_prompt, task, stdin_text, schema)
        return payload, payload.get("structured_output")

    def _run(self, system_prompt: str, task: str, stdin_text: str, schema: type[BaseModel]) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="clipper-llm-") as cwd:
            prompt_file = Path(cwd) / "system-prompt.md"
            prompt_file.write_text(system_prompt, encoding="utf-8")
            log.info("Asking Claude (%s) through Claude Code...", self.model)
            try:
                proc = subprocess.run(
                    self.command(task, prompt_file, schema),
                    input=stdin_text,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=cwd,
                    timeout=self.timeout_s,
                )
            except FileNotFoundError:
                raise LLMError(f"Claude Code CLI not found ({self.bin!r}). Install it and sign in; see README.") from None
            except subprocess.TimeoutExpired:
                raise LLMError(f"Claude Code didn't finish within {self.timeout_s} s") from None

        payload = _parse_output(proc.stdout)
        if payload is None:
            detail = (proc.stderr or proc.stdout).strip()[-1500:]
            raise LLMError(f"Claude Code exited with {proc.returncode} and no JSON result: {detail}")
        if payload.get("is_error") or proc.returncode != 0:
            message = str(payload.get("result") or payload.get("subtype") or "unknown error")
            if _LIMIT.search(message):
                raise UsageLimitError(message, _reset_time(message), note="Claude's usage limit was reached.")
            raise LLMError(f"Claude Code reported an error: {message}")

        usage = payload.get("usage") or {}
        log.info(
            "Claude answered in %.0f s (tokens: %s input, %s cache write, %s cache read, %s output)",
            (payload.get("duration_ms") or 0) / 1000,
            usage.get("input_tokens"),
            usage.get("cache_creation_input_tokens"),
            usage.get("cache_read_input_tokens"),
            usage.get("output_tokens"),
        )
        return payload


def _parse_output(stdout: str) -> dict[str, Any] | None:
    """`--output-format json` prints one result object; tolerate a list of messages too."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(data, list):
        data = next((m for m in reversed(data) if isinstance(m, dict) and m.get("type") == "result"), None)
    return data if isinstance(data, dict) else None


def _reset_time(message: str) -> datetime | None:
    m = _RESET_EPOCH.search(message)
    return datetime.fromtimestamp(int(m[1]), tz=timezone.utc) if m else None
