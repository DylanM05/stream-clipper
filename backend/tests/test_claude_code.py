import json
import subprocess

import pytest
from pydantic import BaseModel, Field

from clipper.llm import ClaudeCode, LLMError, UsageLimitError
from clipper.llm import claude_code
from clipper.llm.claude_code import llm_schema


class Item(BaseModel):
    title: str = Field(max_length=100, description="A title")
    score: float = Field(ge=0, le=1)


class Answer(BaseModel):
    items: list[Item]


def test_llm_schema_inlines_refs_and_drops_constraints_but_keeps_fields_named_title():
    schema = llm_schema(Answer)
    assert "$defs" not in schema
    assert "title" not in schema  # the schema keyword...
    item = schema["properties"]["items"]["items"]
    assert set(item["properties"]) == {"title", "score"}  # ...but not the field
    assert item["additionalProperties"] is False
    assert item["properties"]["title"] == {"description": "A title", "type": "string"}
    assert "minimum" not in item["properties"]["score"]


class FakeClaude:
    """Stands in for subprocess.run, returning queued `claude -p` outputs."""

    def __init__(self, *outputs: dict | str, returncode: int = 0):
        self.outputs = list(outputs)
        self.returncode = returncode
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        out = self.outputs.pop(0)
        stdout = out if isinstance(out, str) else json.dumps(out)
        return subprocess.CompletedProcess(cmd, self.returncode, stdout=stdout, stderr="")


def result(structured=None, **extra) -> dict:
    payload = {"type": "result", "subtype": "success", "is_error": False, "result": "", "usage": {}, **extra}
    if structured is not None:
        payload["structured_output"] = structured
    return payload


GOOD = {"items": [{"title": "Chicken wins", "score": 0.9}]}


def ask(fake, monkeypatch, **kwargs):
    monkeypatch.setattr(claude_code.subprocess, "run", fake)
    return ClaudeCode(bin="claude").structured(system_prompt="sys", task="task", stdin_text="transcript", schema=Answer, **kwargs)


def test_returns_validated_structured_output_and_runs_locked_down(monkeypatch):
    fake = FakeClaude(result(GOOD))
    answer = ask(fake, monkeypatch)
    assert answer.items[0].title == "Chicken wins"

    cmd, kwargs = fake.calls[0]
    assert cmd[:3] == ["claude", "-p", "task"]
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--bare" not in cmd  # --bare would skip the subscription login
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == llm_schema(Answer)
    assert kwargs["input"] == "transcript"


def test_retries_once_when_the_answer_is_unusable(monkeypatch):
    fake = FakeClaude(result(None), result({"items": [{"title": "ok", "score": "high"}]}), result(GOOD))
    with pytest.raises(LLMError):
        ask(fake, monkeypatch)
    assert len(fake.calls) == 2

    fake = FakeClaude(result(None), result(GOOD))
    assert ask(fake, monkeypatch).items[0].score == 0.9


def test_usage_limit_is_reported_with_reset_time(monkeypatch):
    fake = FakeClaude(result(is_error=True, result="Claude AI usage limit reached|1767225600"), returncode=1)
    with pytest.raises(UsageLimitError) as err:
        ask(fake, monkeypatch)
    assert err.value.resets_at is not None
    assert err.value.resets_at.year == 2026


def test_other_errors_and_non_json_output_raise(monkeypatch):
    with pytest.raises(LLMError, match="Invalid API key"):
        ask(FakeClaude(result(is_error=True, result="Invalid API key"), returncode=1), monkeypatch)
    with pytest.raises(LLMError, match="no JSON"):
        ask(FakeClaude("Segmentation fault", returncode=139), monkeypatch)


def test_saves_the_raw_response(monkeypatch, tmp_path):
    log = tmp_path / "llm" / "response.json"
    ask(FakeClaude(result(GOOD)), monkeypatch, raw_log=log)
    assert json.loads(log.read_text(encoding="utf-8"))["structured_output"] == GOOD
