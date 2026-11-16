"""Run a model through the Codex CLI (`codex exec`), signed in with your ChatGPT plan.

Each call runs in an empty temporary folder, read-only and without saving the session or reading your own Codex
settings, with the instructions and everything to read piped in, and the answer held to a JSON schema
(--output-schema). Codex is an agent that can run commands, but there it has nothing to look at and can change
nothing. Sign in once with `codex login --device-auth` (README); the sign-in is kept in CODEX_HOME.
"""

import json
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .base import LLM, LLMError, UsageLimitError, llm_schema

log = logging.getLogger(__name__)

_LIMIT = re.compile(r"usage limit|limit reached|rate.?limit|too many requests", re.IGNORECASE)
_SIGNED_OUT = re.compile(r"not logged in|codex login|unauthori[sz]ed|\b401\b", re.IGNORECASE)
# "…or try again at 3:45 PM." later today, or "…at Nov 13th, 2026 3:45 PM.", in the time zone Codex runs in (ours).
_TRY_AGAIN_AT = re.compile(
    r"try again at (?:([a-z]{3}) (\d{1,2})(?:st|nd|rd|th)?, (\d{4}) )?(\d{1,2}):(\d{2}) ([ap]m)", re.IGNORECASE
)
# "…or try again in 2 days 3 hours 15 minutes.", as older versions put it.
_TRY_AGAIN = re.compile(r"try again in ((?:\d+\s*[a-z]+[\s,]*(?:and\s+)?)+)", re.IGNORECASE)
_SPAN = re.compile(r"(\d+)\s*(d|h|m|s)[a-z]*", re.IGNORECASE)
_UNIT_S = {"d": 86400, "h": 3600, "m": 60, "s": 1}


@dataclass
class Codex(LLM):
    bin: str = "codex"
    model: str = ""  # empty: Codex's own default
    effort: str | None = "high"
    timeout_s: int = 3600
    name = "Codex"

    def command(self, workdir: Path, schema_file: Path) -> list[str]:
        cmd = [
            self.bin, "exec", "--json", "--color", "never",
            "--cd", str(workdir), "--skip-git-repo-check", "--sandbox", "read-only",
            "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--output-schema", str(schema_file),
        ]
        if self.model:
            cmd += ["--model", self.model]
        if self.effort:
            cmd += ["--config", f'model_reasoning_effort="{self.effort}"']
        return cmd + ["-"]  # the prompt comes on stdin: a whole stream's transcript is too long for an argument

    def auth_status(self) -> str:
        try:
            proc = subprocess.run(
                [self.bin, "login", "status"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60
            )
        except FileNotFoundError:
            return f"Codex CLI not found ({self.bin!r})"
        return (proc.stdout or proc.stderr).strip() or f"exit {proc.returncode}"

    def signed_in(self) -> bool:
        try:
            return subprocess.run([self.bin, "login", "status"], capture_output=True, timeout=60).returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _ask(self, system_prompt: str, task: str, stdin_text: str, schema: type[BaseModel]) -> tuple[dict[str, Any], Any]:
        prompt = (
            "Work only from what's below: there are no files to read and nothing to run.\n\n"
            f"{system_prompt.strip()}\n\n# Your task\n{task}\n\n{stdin_text}"
        )
        with tempfile.TemporaryDirectory(prefix="clipper-llm-") as tmp:
            workdir = Path(tmp) / "work"
            workdir.mkdir()
            schema_file = Path(tmp) / "schema.json"
            schema_file.write_text(json.dumps(llm_schema(schema)), encoding="utf-8")
            log.info("Asking Codex (%s)...", self.model or "its default model")
            try:
                proc = subprocess.run(
                    self.command(workdir, schema_file),
                    input=prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_s,
                )
            except FileNotFoundError:
                raise LLMError(f"Codex CLI not found ({self.bin!r}). It comes with the app's image; see README.") from None
            except subprocess.TimeoutExpired:
                raise LLMError(f"Codex didn't finish within {self.timeout_s} s") from None

        events = _events(proc.stdout)
        answer = next(
            (e["item"].get("text") for e in reversed(events)
             if e.get("type") == "item.completed" and (e.get("item") or {}).get("type") == "agent_message"),
            None,
        )
        if answer is None:
            message = _failure(events) or (proc.stderr or proc.stdout).strip()[-1500:] or f"exit {proc.returncode}"
            if _LIMIT.search(message):
                raise UsageLimitError(message, _reset_time(message), note="Codex's usage limit was reached.")
            if _SIGNED_OUT.search(message):
                raise LLMError(f"Codex isn't signed in: sign in with `codex login --device-auth` (README). {message}")
            raise LLMError(f"Codex reported an error: {message}")

        usage = next((e.get("usage") for e in reversed(events) if e.get("type") == "turn.completed"), None) or {}
        log.info(
            "Codex answered (tokens: %s input, %s cached, %s output, %s reasoning)",
            usage.get("input_tokens"), usage.get("cached_input_tokens"), usage.get("output_tokens"),
            usage.get("reasoning_output_tokens"),
        )
        try:
            output = json.loads(answer)
        except json.JSONDecodeError:
            output = None
        return {"events": events}, output


def _events(stdout: str) -> list[dict[str, Any]]:
    """`--json` prints one event per line."""
    events = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _failure(events: list[dict[str, Any]]) -> str:
    """What a run that gave no answer said went wrong."""
    for e in reversed(events):
        if e.get("type") == "turn.failed":
            return str((e.get("error") or {}).get("message") or "")
        if e.get("type") == "error":
            return str(e.get("message") or "")
    return ""


def _reset_time(message: str, now: datetime | None = None) -> datetime | None:
    """When the usage limit resets, from what Codex said; None if it didn't say, or said a time that's gone."""
    now = now or datetime.now().astimezone()
    if m := _TRY_AGAIN_AT.search(message):
        month, day, year, hour, minute, half = m.groups()
        try:
            date = datetime.strptime(f"{month} {day} {year}", "%b %d %Y").date() if month else now.date()
        except ValueError:
            return None
        hour_24 = int(hour) % 12 + (12 if half.lower() == "pm" else 0)
        at = datetime.combine(date, time(hour_24, int(minute))).astimezone()  # this computer's time zone
        return at.astimezone(timezone.utc) if at > now else None
    m = _TRY_AGAIN.search(message)
    if not m:
        return None
    seconds = sum(int(n) * _UNIT_S[unit.lower()] for n, unit in _SPAN.findall(m[1]))
    return now.astimezone(timezone.utc) + timedelta(seconds=seconds) if seconds else None
