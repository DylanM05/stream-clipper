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
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..workspace import write_json
from .base import LLMError, UsageLimitError

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_LIMIT = re.compile(r"usage limit|limit reached|rate.?limit|out of (extra )?usage", re.IGNORECASE)
_RESET_EPOCH = re.compile(r"\|(\d{10})\b")

# Schema keywords left out of what Claude sees; pydantic still checks the answer afterwards.
_SCHEMA_DROP = {
    "title", "default", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minLength", "maxLength", "pattern", "minItems", "maxItems",
}


@dataclass
class ClaudeCode:
    bin: str = "claude"
    model: str = "opus"
    effort: str | None = "high"
    timeout_s: int = 3600

    def structured(
        self,
        *,
        system_prompt: str,
        task: str,
        stdin_text: str,
        schema: type[T],
        raw_log: Path | None = None,
        attempts: int = 2,
    ) -> T:
        """Ask Claude for JSON matching `schema`, retrying once if the answer doesn't validate."""
        problem = ""
        for attempt in range(1, attempts + 1):
            payload = self._run(system_prompt, task, stdin_text, schema)
            if raw_log:
                write_json(raw_log, payload)
            output = payload.get("structured_output")
            if output is None:
                problem = "the response had no structured_output"
            else:
                try:
                    return schema.model_validate(output)
                except ValidationError as e:
                    problem = str(e)
            log.warning("Claude's answer was unusable (attempt %d/%d): %s", attempt, attempts, problem[:500])
        raise LLMError(f"Claude gave no usable answer after {attempts} attempts: {problem[:1000]}")

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
                raise UsageLimitError(message, _reset_time(message))
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


def llm_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The pydantic model's JSON Schema with $refs inlined, range/length/pattern keywords dropped, and
    every field required (fields with defaults are still asked for)."""
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(v) for v in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return walk(defs[node["$ref"].rsplit("/", 1)[-1]])
        out = {}
        for key, value in node.items():
            if key in _SCHEMA_DROP:
                continue
            # Under "properties" the keys are field names (which may be "title"), not schema keywords.
            out[key] = {name: walk(sub) for name, sub in value.items()} if key == "properties" else walk(value)
        if out.get("type") == "object":
            out.setdefault("additionalProperties", False)
            if "properties" in out:
                out["required"] = list(out["properties"])
        return out

    return walk(raw)


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
