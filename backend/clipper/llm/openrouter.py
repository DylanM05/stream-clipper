"""Ask a model on OpenRouter (openrouter.ai), paid per use from the account's credit with an API key: the same
CLIPPER_OPENROUTER_API_KEY thumbnails are drawn with.

The answer is held to a JSON schema (structured outputs), and only providers that can do that answer: any model
whose OpenRouter page lists structured outputs works.
"""

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from pydantic import BaseModel

from .base import LLM, LLMError, UsageLimitError, llm_schema

log = logging.getLogger(__name__)

API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "anthropic/claude-opus-5.5"
MAX_TOKENS = 32000  # the answer and the model's thinking together; a whole stream's clips take a few thousand
RETRY_AFTER_S = 15  # once, when the model's provider is down or busy (502-504)
_EFFORT = {"max": "xhigh"}  # Claude Code's top effort, in OpenRouter's words


@dataclass
class OpenRouter(LLM):
    api_key: str = ""
    model: str = DEFAULT_MODEL
    effort: str | None = "high"
    timeout_s: int = 3600
    transport: httpx.BaseTransport | None = None  # for tests

    @property
    def name(self) -> str:
        return self.model

    def _ask(self, system_prompt: str, task: str, stdin_text: str, schema: type[BaseModel]) -> tuple[dict[str, Any], Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"{task}\n\n{stdin_text}".strip()},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "strict": True, "schema": llm_schema(schema)},
            },
            # Only the model's providers that take all of this, the schema above all: others would ignore it.
            "provider": {"require_parameters": True},
            "max_tokens": MAX_TOKENS,
        }
        if self.effort:
            body["reasoning"] = {"effort": _EFFORT.get(self.effort, self.effort)}
        log.info("Asking %s on OpenRouter...", self.model)
        res = self._post(body)
        if res.status_code == 404 and "reasoning" in body and "parameter" in _error(res).lower():
            # A model that keeps to a schema but doesn't think first: ask again without asking it to.
            del body["reasoning"]
            res = self._post(body)
        if res.status_code == 402:
            raise UsageLimitError(_error(res), note="OpenRouter's credit ran out: add some at openrouter.ai.")
        if res.status_code == 429:
            raise UsageLimitError(_error(res), _retry_after(res), note="OpenRouter is turning down requests for now.")
        if res.status_code == 401:
            raise LLMError(f"OpenRouter turned down the API key (CLIPPER_OPENROUTER_API_KEY): {_error(res)}")
        if res.status_code == 404 and "parameter" in _error(res).lower():
            raise LLMError(f"{self.model} can't keep to a JSON schema on OpenRouter; choose a model that lists "
                           f"structured outputs: {_error(res)}")
        if res.status_code >= 400:
            raise LLMError(f"OpenRouter answered {res.status_code}: {_error(res)}")

        data = res.json()
        choice = (data.get("choices") or [{}])[0]
        if data.get("error") or choice.get("error") or choice.get("finish_reason") == "error":
            raise LLMError(f"OpenRouter: {(data.get('error') or choice.get('error') or {}).get('message', 'the model failed')}")
        if choice.get("finish_reason") == "length":
            raise LLMError(f"{self.model}'s answer was cut off at {MAX_TOKENS} tokens")
        usage = data.get("usage") or {}
        log.info("%s answered (tokens: %s input, %s output; cost %s)", self.model,
                 usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("cost"))
        return data, _json((choice.get("message") or {}).get("content") or "")

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        for attempt in (1, 2):
            try:
                with httpx.Client(timeout=httpx.Timeout(self.timeout_s, connect=30), transport=self.transport) as client:
                    res = client.post(API_URL, json=body, headers={"Authorization": f"Bearer {self.api_key}"})
            except httpx.TimeoutException:
                raise LLMError(f"{self.model} on OpenRouter didn't answer within {self.timeout_s} s") from None
            except httpx.HTTPError as e:
                raise LLMError(f"Couldn't reach OpenRouter: {e}") from None
            if res.status_code not in (502, 503, 504) or attempt == 2:
                return res
            log.warning("OpenRouter answered %d; asking again in %d s", res.status_code, RETRY_AFTER_S)
            time.sleep(RETRY_AFTER_S)
        return res


def _error(res: httpx.Response) -> str:
    try:
        error = res.json().get("error") or {}
        return str(error.get("message") or error) if isinstance(error, dict) else str(error)
    except (json.JSONDecodeError, AttributeError):
        return res.text[:500]


def _retry_after(res: httpx.Response) -> datetime | None:
    try:
        return datetime.now(timezone.utc) + timedelta(seconds=float(res.headers.get("retry-after", "")))
    except ValueError:
        return None


def _json(content: str) -> Any:
    """The answer, also if the model wrapped it in a ```json block anyway."""
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
