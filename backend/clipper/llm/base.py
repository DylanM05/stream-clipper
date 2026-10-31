"""What the AI backends share: asking for an answer that matches a schema, and the errors they raise."""

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..workspace import write_json

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Schema keywords left out of what the model sees; pydantic still checks the answer afterwards.
_SCHEMA_DROP = {
    "title", "default", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minLength", "maxLength", "pattern", "minItems", "maxItems",
}


class LLMError(RuntimeError):
    """The model call failed or returned an unusable answer."""


class UsageLimitError(LLMError):
    """The plan's usage limit was reached, or OpenRouter's credit ran out. Retry after `resets_at` when it's known.
    `note` says which, in a sentence for the app."""

    def __init__(self, message: str, resets_at: datetime | None = None, note: str = "The AI's usage limit was reached."):
        super().__init__(message)
        self.resets_at = resets_at
        self.note = note


class LLM:
    """An AI that answers with JSON matching a schema. `name` is what messages call it."""

    name = "The AI"

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
        """Ask for JSON matching `schema`, retrying once if the answer doesn't validate."""
        problem = ""
        for attempt in range(1, attempts + 1):
            payload, output = self._ask(system_prompt, task, stdin_text, schema)
            if raw_log:
                write_json(raw_log, payload)
            if output is None:
                problem = "the response had no answer in it"
            else:
                try:
                    return schema.model_validate(output)
                except ValidationError as e:
                    problem = str(e)
            log.warning("%s's answer was unusable (attempt %d/%d): %s", self.name, attempt, attempts, problem[:500])
        raise LLMError(f"{self.name} gave no usable answer after {attempts} attempts: {problem[:1000]}")

    def _ask(self, system_prompt: str, task: str, stdin_text: str, schema: type[BaseModel]) -> tuple[dict[str, Any], Any]:
        """One request: the whole response, which raw_log keeps, and the answer in it (None if there's none)."""
        raise NotImplementedError

    def plan_usage(self) -> list[dict] | None:
        """How much of the plan's usage limits is used (see ClaudeCode.plan_usage); None if this AI can't say."""
        return None


def llm_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The pydantic model's JSON Schema with $refs inlined, range/length/pattern keywords dropped, and
    every field required (fields with defaults are still asked for), which strict structured output needs."""
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
