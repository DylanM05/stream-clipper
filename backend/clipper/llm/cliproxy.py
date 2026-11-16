"""Ask a model through CLIProxyAPI (github.com/router-for-me/CLIProxyAPI): a server signed in to your Claude, ChatGPT or
Gemini plans that answers on an OpenAI-compatible API, with one of the api-keys in its config.yaml. It runs as the
cliproxy service in docker-compose.yml (README), or anywhere the app can reach at CLIPPER_CLIPROXY_URL.

The answer is asked to keep to a JSON schema (response_format), and the schema is in the instructions as well: how
well the proxy passes response_format on depends on the plan behind the model, and pydantic checks the answer anyway.
How hard the model thinks goes on the end of its name, the proxy's way: claude-opus-5-5(high).
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel

from .base import LLM, LLMError, UsageLimitError, llm_schema
from .openrouter import _error, _json, _retry_after

log = logging.getLogger(__name__)

DEFAULT_URL = "http://cliproxy:8317/v1"
DEFAULT_MODEL = "claude-opus-5-5"
MAX_TOKENS = 32000  # the answer and the model's thinking together; a whole stream's clips take a few thousand
RETRY_AFTER_S = 15  # once, when the proxy or the plan behind it is down or busy (502-504)
_EFFORT = {"max": "xhigh"}  # Claude Code's top effort, in the proxy's words
_LIMIT = ("quota", "usage limit", "limit reached", "rate limit", "exhausted", "cooling down", "cooldown")


@dataclass
class CliProxy(LLM):
    url: str = DEFAULT_URL
    api_key: str = ""
    model: str = DEFAULT_MODEL
    effort: str | None = "high"
    timeout_s: int = 3600
    transport: httpx.BaseTransport | None = None  # for tests

    @property
    def name(self) -> str:
        return f"{self.model} (CLI proxy)"

    def _ask(self, system_prompt: str, task: str, stdin_text: str, schema: type[BaseModel]) -> tuple[dict[str, Any], Any]:
        shape = llm_schema(schema)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": f"{system_prompt.strip()}\n\n# Your answer\nAnswer with only a JSON "
                                              f"object that matches this JSON Schema:\n{json.dumps(shape)}"},
                {"role": "user", "content": f"{task}\n\n{stdin_text}".strip()},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "strict": True, "schema": shape},
            },
            "max_tokens": MAX_TOKENS,
            "stream": False,
        }
        if self.effort and not self.model.endswith(")"):  # unless the model's name already says
            body["model"] = f"{self.model}({_EFFORT.get(self.effort, self.effort)})"
        log.info("Asking %s through the CLI proxy...", body["model"])
        res = self._post(body)
        if res.status_code == 400 and any(word in _error(res).lower() for word in ("response_format", "json_schema")):
            # A plan the proxy can't hold to a schema: the instructions still have it.
            del body["response_format"]
            res = self._post(body)
        if res.status_code == 400 and body["model"] != self.model:
            # A model that can't think this hard: the proxy turns down a level it doesn't have. Its own level, then.
            body["model"] = self.model
            res = self._post(body)
        message = _error(res) if res.status_code >= 400 else ""
        if res.status_code == 429 or (res.status_code in (402, 403) and _limit(message)):
            raise UsageLimitError(message, _retry_after(res),
                                  note="The usage limit of the plans behind the CLI proxy was reached.")
        if res.status_code == 401:
            raise LLMError(f"The CLI proxy turned down the API key (CLIPPER_CLIPROXY_API_KEY): {message}")
        if res.status_code >= 400:
            raise LLMError(f"The CLI proxy answered {res.status_code}: {message}")

        data = res.json()
        choice = (data.get("choices") or [{}])[0]
        if data.get("error") or choice.get("finish_reason") == "error":
            raise LLMError(f"The CLI proxy: {(data.get('error') or {}).get('message', 'the model failed')}")
        if choice.get("finish_reason") == "length":
            raise LLMError(f"{self.model}'s answer was cut off at {MAX_TOKENS} tokens")
        usage = data.get("usage") or {}
        log.info("%s answered through the CLI proxy (tokens: %s input, %s output)", self.model,
                 usage.get("prompt_tokens"), usage.get("completion_tokens"))
        return data, _json((choice.get("message") or {}).get("content") or "")

    def models(self) -> list[str] | None:
        """The models the proxy offers with its sign-ins (GET /models); None if it doesn't list them."""
        try:
            with self._client(timeout=httpx.Timeout(10)) as client:
                res = client.get(f"{self.url.rstrip('/')}/models", headers=self._headers())
        except httpx.HTTPError as e:
            raise LLMError(f"Couldn't reach the CLI proxy at {self.url}: {e}") from None
        if res.status_code == 401:
            raise LLMError("The CLI proxy turned down the API key (CLIPPER_CLIPROXY_API_KEY)")
        if res.status_code == 404:
            return None
        if res.status_code >= 400:
            raise LLMError(f"The CLI proxy answered {res.status_code}: {_error(res)}")
        try:
            return [str(m.get("id")) for m in res.json().get("data") or [] if isinstance(m, dict) and m.get("id")]
        except (json.JSONDecodeError, AttributeError):
            raise LLMError(f"The CLI proxy at {self.url} didn't list its models: is that its /v1 address?") from None

    def auth_status(self) -> str:
        try:
            models = self.models()
        except LLMError as e:
            return str(e)
        if models is None:
            return f"The CLI proxy at {self.url} answers, but doesn't list its models"
        return f"The CLI proxy at {self.url} offers {len(models)} models: {', '.join(models[:20]) or 'none'}"

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        url = f"{self.url.rstrip('/')}/chat/completions"
        for attempt in (1, 2):
            try:
                with self._client(timeout=httpx.Timeout(self.timeout_s, connect=30)) as client:
                    res = client.post(url, json=body, headers=self._headers())
            except httpx.TimeoutException:
                raise LLMError(f"{self.model} through the CLI proxy didn't answer within {self.timeout_s} s") from None
            except httpx.HTTPError as e:
                raise LLMError(f"Couldn't reach the CLI proxy at {self.url}: {e}") from None
            if res.status_code not in (502, 503, 504) or attempt == 2:
                return res
            log.warning("The CLI proxy answered %d; asking again in %d s", res.status_code, RETRY_AFTER_S)
            time.sleep(RETRY_AFTER_S)
        return res

    def _client(self, timeout: httpx.Timeout) -> httpx.Client:
        return httpx.Client(timeout=timeout, transport=self.transport)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}


def _limit(message: str) -> bool:
    return any(word in message.lower() for word in _LIMIT)
