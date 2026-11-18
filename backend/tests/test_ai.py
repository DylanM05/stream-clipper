"""The AI that picks clips and writes titles and thumbnail ideas: Codex, models on OpenRouter and models through a CLI
proxy as well as Claude Code, and choosing between them in the app."""

import json
import subprocess
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from clipper import ai
from clipper.config import Settings
from clipper.llm import ClaudeCode, CliProxy, Codex, LLMError, OpenRouter, UsageLimitError, cliproxy, codex, openrouter
from clipper.llm.base import llm_schema
from clipper.web import app as web_app


class Answer(BaseModel):
    title: str
    score: float


GOOD = {"title": "Chicken wins", "score": 0.9}


# --- Codex ----------------------------------------------------------------------------


def events(*items: dict) -> str:
    return "\n".join(json.dumps(e) for e in items) + "\n"


def answered(answer: dict | str) -> str:
    text = answer if isinstance(answer, str) else json.dumps(answer)
    return events(
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "i1", "type": "reasoning", "text": "thinking"}},
        {"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": text}},
        {"type": "turn.completed", "usage": {"input_tokens": 1200, "cached_input_tokens": 0, "output_tokens": 30,
                                             "reasoning_output_tokens": 10}},
    )


class FakeCodex:
    """Stands in for subprocess.run, answering `codex exec` with queued JSONL outputs."""

    def __init__(self, *outputs: str, returncode: int = 0):
        self.outputs = list(outputs)
        self.returncode = returncode
        self.calls = []

    def __call__(self, cmd, **kwargs):
        schema_file = cmd[cmd.index("--output-schema") + 1]
        with open(schema_file, encoding="utf-8") as f:  # gone once the call is over
            self.calls.append((cmd, kwargs, json.load(f)))
        return subprocess.CompletedProcess(cmd, self.returncode, stdout=self.outputs.pop(0), stderr="")


def ask_codex(fake, monkeypatch, **kwargs):
    monkeypatch.setattr(codex.subprocess, "run", fake)
    return Codex(model="gpt-6.1-sol", effort="high", **kwargs).structured(
        system_prompt="Pick clips.", task="Pick the best one.", stdin_text="# Transcript\n[00:00:01] Hi", schema=Answer,
    )


def test_codex_answers_from_whats_piped_in_locked_down_and_held_to_the_schema(monkeypatch):
    fake = FakeCodex(answered(GOOD))
    assert ask_codex(fake, monkeypatch) == Answer(**GOOD)
    [(cmd, kwargs, schema)] = fake.calls
    assert cmd[:2] == ["codex", "exec"] and cmd[-1] == "-"  # the prompt comes on stdin
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert {"--json", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check"} <= set(cmd)
    assert cmd[cmd.index("--model") + 1] == "gpt-6.1-sol"
    assert cmd[cmd.index("--config") + 1] == 'model_reasoning_effort="high"'
    assert schema == llm_schema(Answer)
    assert "Pick clips." in kwargs["input"] and "Pick the best one." in kwargs["input"] and "[00:00:01] Hi" in kwargs["input"]


def test_codex_on_its_own_default_model_and_effort_and_asked_again_for_an_unusable_answer(monkeypatch):
    fake = FakeCodex(answered("not json"), answered(GOOD))
    monkeypatch.setattr(codex.subprocess, "run", fake)
    answer = Codex(model="", effort=None).structured(system_prompt="s", task="t", stdin_text="", schema=Answer)
    assert answer.score == 0.9 and len(fake.calls) == 2
    cmd = fake.calls[0][0]
    assert "--model" not in cmd and "--config" not in cmd


def test_codex_usage_limit_says_when_to_try_again(monkeypatch):
    failed = events({"type": "turn.started"}, {"type": "turn.failed", "error": {
        "message": "You've hit your usage limit. Upgrade to Pro or try again in 2 hours 5 minutes."}})
    with pytest.raises(UsageLimitError) as err:
        ask_codex(FakeCodex(failed, returncode=1), monkeypatch)
    assert err.value.note == "Codex's usage limit was reached."
    soon = datetime.now(timezone.utc) + timedelta(hours=2, minutes=5)
    assert abs((err.value.resets_at - soon).total_seconds()) < 60


def test_codex_usage_limit_says_when_to_try_again_as_it_words_it_now():
    now = datetime(2026, 11, 11, 9, 0).astimezone()
    today = codex._reset_time("You’ve hit your usage limit. Try again at 3:45 PM.", now)
    assert today == datetime(2026, 11, 11, 15, 45).astimezone()
    another_day = codex._reset_time(
        "You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
        "https://chatgpt.com/settings/usage to purchase more credits or try again at Nov 13th, 2026 12:05 AM.", now)
    assert another_day == datetime(2026, 11, 13, 0, 5).astimezone()
    assert codex._reset_time("You’ve hit your usage limit. Try again at 8:15 AM.", now) is None  # gone: try again soon
    assert codex._reset_time("You’ve hit your usage limit. Try again later.", now) is None


def test_codex_not_signed_in_or_failing_says_so(monkeypatch):
    signed_out = events({"type": "error", "message": "Not logged in. Run codex login."})
    with pytest.raises(LLMError, match="codex login --device-auth"):
        ask_codex(FakeCodex(signed_out, returncode=1), monkeypatch)
    with pytest.raises(LLMError, match="Codex reported an error: model overloaded"):
        ask_codex(FakeCodex(events({"type": "turn.failed", "error": {"message": "model overloaded"}}), returncode=1), monkeypatch)


# --- OpenRouter -----------------------------------------------------------------------------


def openrouter_answers(*answers):
    """A fake OpenRouter: each request gets the next (status, json body), and is kept."""
    sent = []
    queue = list(answers)

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        status, body = queue.pop(0)
        return httpx.Response(status, json=body, headers=body.pop("_headers", {}) if isinstance(body, dict) else {})

    return sent, httpx.MockTransport(handle)


def message(content: str, finish: str = "stop") -> dict:
    return {"choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 40, "cost": 0.006}}


def ask_openrouter(transport, effort="high"):
    return OpenRouter(api_key="or-key", model="anthropic/claude-opus-5.5", effort=effort, transport=transport).structured(
        system_prompt="Pick clips.", task="Pick the best one.", stdin_text="# Transcript", schema=Answer,
    )


def test_openrouter_is_held_to_the_schema_by_providers_that_can(monkeypatch):
    sent, transport = openrouter_answers((200, message(json.dumps(GOOD))))
    assert ask_openrouter(transport) == Answer(**GOOD)
    [body] = sent
    assert body["model"] == "anthropic/claude-opus-5.5"
    assert body["messages"][0] == {"role": "system", "content": "Pick clips."}
    assert body["messages"][1]["content"] == "Pick the best one.\n\n# Transcript"
    assert body["response_format"]["json_schema"] == {"name": "Answer", "strict": True, "schema": llm_schema(Answer)}
    assert body["provider"] == {"require_parameters": True}
    assert body["reasoning"] == {"effort": "high"}


def test_openrouter_takes_an_answer_in_a_code_block_and_a_model_that_doesnt_think(monkeypatch):
    sent, transport = openrouter_answers(
        (404, {"error": {"message": "No endpoints found that can handle the requested parameters."}}),
        (200, message("```json\n" + json.dumps(GOOD) + "\n```")),
    )
    assert ask_openrouter(transport).title == "Chicken wins"
    assert "reasoning" in sent[0] and "reasoning" not in sent[1]


@pytest.mark.parametrize("status, body, error, match", [
    (402, {"error": {"message": "Insufficient credits"}}, UsageLimitError, "Insufficient credits"),
    (429, {"error": {"message": "Rate limit exceeded"}, "_headers": {"retry-after": "120"}}, UsageLimitError, "Rate limit"),
    (401, {"error": {"message": "No auth credentials found"}}, LLMError, "turned down the API key"),
    (404, {"error": {"message": "No endpoints found that can handle the requested parameters."}}, LLMError, "JSON schema"),
    (200, message('{"title": "cut', finish="length"), LLMError, "cut off"),
])
def test_openrouter_problems_say_what_happened(monkeypatch, status, body, error, match):
    _, transport = openrouter_answers((status, body), (status, dict(body)))
    with pytest.raises(error, match=match) as err:
        ask_openrouter(transport)
    if status == 402:
        assert "credit ran out" in err.value.note and err.value.resets_at is None
    if status == 429:
        assert err.value.resets_at > datetime.now(timezone.utc) + timedelta(seconds=100)


def test_openrouter_asks_again_once_when_the_models_provider_is_down(monkeypatch):
    monkeypatch.setattr(openrouter, "RETRY_AFTER_S", 0)
    sent, transport = openrouter_answers((503, {"error": {"message": "Provider down"}}), (200, message(json.dumps(GOOD))))
    assert ask_openrouter(transport, effort="max").score == 0.9
    assert len(sent) == 2 and sent[0]["reasoning"] == {"effort": "xhigh"}  # Claude Code's top effort


# --- The CLI proxy -----------------------------------------------------------------------------


def proxy_answers(*answers):
    """A fake CLIProxyAPI: each request gets the next (status, json body), and is kept with where it went."""
    sent = []
    queue = list(answers)

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append((request.method, str(request.url), request.headers.get("authorization"),
                     json.loads(request.content) if request.content else None))
        status, body = queue.pop(0)
        return httpx.Response(status, json=body, headers=body.pop("_headers", {}) if isinstance(body, dict) else {})

    return sent, httpx.MockTransport(handle)


def ask_proxy(transport, effort="high"):
    return CliProxy(url="http://cliproxy:8317/v1/", api_key="proxy-key", model="claude-opus-5-5", effort=effort,
                    transport=transport).structured(
        system_prompt="Pick clips.", task="Pick the best one.", stdin_text="# Transcript", schema=Answer,
    )


def test_cli_proxy_is_asked_for_the_schema_on_its_openai_api():
    sent, transport = proxy_answers((200, message(json.dumps(GOOD))))
    assert ask_proxy(transport) == Answer(**GOOD)
    [(method, url, auth, body)] = sent
    assert (method, url, auth) == ("POST", "http://cliproxy:8317/v1/chat/completions", "Bearer proxy-key")
    assert body["model"] == "claude-opus-5-5(high)"  # how hard it thinks, the proxy's way
    # The schema is in the instructions too, for plans the proxy can't pass response_format on to.
    assert body["messages"][0]["content"].startswith("Pick clips.")
    assert json.dumps(llm_schema(Answer)) in body["messages"][0]["content"]
    assert body["messages"][1]["content"] == "Pick the best one.\n\n# Transcript"
    assert body["response_format"]["json_schema"] == {"name": "Answer", "strict": True, "schema": llm_schema(Answer)}
    assert "provider" not in body and "reasoning" not in body


def test_cli_proxy_takes_an_answer_in_a_code_block_without_the_schema_or_the_effort_it_cant_take():
    sent, transport = proxy_answers(
        (400, {"error": {"message": "response_format json_schema is not supported"}}),
        (400, {"error": {"message": "unsupported thinking level xhigh"}}),
        (200, message("```json\n" + json.dumps(GOOD) + "\n```")),
    )
    assert ask_proxy(transport, effort="max").title == "Chicken wins"
    assert sent[0][3]["model"] == "claude-opus-5-5(xhigh)" and "response_format" in sent[0][3]
    assert "response_format" not in sent[1][3] and sent[2][3]["model"] == "claude-opus-5-5"


def test_cli_proxy_keeps_an_effort_in_the_models_name_and_none_when_theres_none():
    sent, transport = proxy_answers((200, message(json.dumps(GOOD))), (200, message(json.dumps(GOOD))))
    for model, effort in (("gpt-6.1-sol(low)", "high"), ("gpt-6.1-sol", None)):
        CliProxy(api_key="k", model=model, effort=effort, transport=transport).structured(
            system_prompt="S.", task="T.", stdin_text="", schema=Answer)
    assert [body["model"] for *_, body in sent] == ["gpt-6.1-sol(low)", "gpt-6.1-sol"]


@pytest.mark.parametrize("status, body, error, match", [
    (429, {"error": {"message": "All credentials are cooling down"}, "_headers": {"retry-after": "120"}},
     UsageLimitError, "cooling down"),
    (403, {"error": {"message": "Quota exceeded for this account"}}, UsageLimitError, "Quota"),
    (401, {"error": {"message": "Invalid API key"}}, LLMError, "CLIPPER_CLIPROXY_API_KEY"),
    (500, {"error": {"message": "auth_unavailable: no auth available"}}, LLMError, "answered 500"),
    (200, message('{"title": "cut', finish="length"), LLMError, "cut off"),
])
def test_cli_proxy_problems_say_what_happened(status, body, error, match):
    _, transport = proxy_answers((status, body), (status, dict(body)))
    with pytest.raises(error, match=match) as err:
        ask_proxy(transport)
    if status == 429:
        assert "CLI proxy" in err.value.note
        assert err.value.resets_at > datetime.now(timezone.utc) + timedelta(seconds=100)


def test_cli_proxy_asks_again_once_when_its_down(monkeypatch):
    monkeypatch.setattr(cliproxy, "RETRY_AFTER_S", 0)
    sent, transport = proxy_answers((502, {"error": {"message": "Bad gateway"}}), (200, message(json.dumps(GOOD))))
    assert ask_proxy(transport).score == 0.9
    assert len(sent) == 2


# --- Choosing one in the app --------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path, monkeypatch):
    # Signed in, rather than asking whichever `claude` or `codex` this computer has.
    monkeypatch.setattr(ClaudeCode, "signed_in", lambda self: True)
    monkeypatch.setattr(Codex, "signed_in", lambda self: True)
    return Settings(data_dir=tmp_path / "data")


def test_claude_code_until_another_is_chosen_with_each_ones_model(settings):
    llm = ai.model(settings, effort="low", timeout_s=300)
    assert isinstance(llm, ClaudeCode) and (llm.model, llm.effort, llm.timeout_s) == ("opus", "low", 300)
    # A piece of work can use another of the chosen AI's models, e.g. a cheaper one for a backfill.
    llm = ai.model(settings, effort="low", timeout_s=300, model_name="sonnet")
    assert isinstance(llm, ClaudeCode) and llm.model == "sonnet"

    client = TestClient(web_app.create_app(settings))
    setup = client.patch("/api/ai", json={"provider": "codex", "model": "gpt-6.1-sol"}).json()
    assert setup["provider"] == "codex"
    assert {p["id"]: p["model"] for p in setup["providers"]} == {
        "claude": "opus", "codex": "gpt-6.1-sol", "openrouter": "anthropic/claude-opus-5.5", "cliproxy": "claude-opus-5-5",
    }
    llm = ai.model(settings, effort="high", timeout_s=3600)
    assert isinstance(llm, Codex) and llm.model == "gpt-6.1-sol"

    # Back to Claude Code with a model of its own; Codex keeps the one it had.
    client.patch("/api/ai", json={"provider": "claude"})
    client.patch("/api/ai", json={"model": "sonnet"})
    setup = client.get("/api/ai").json()
    assert setup["provider"] == "claude"
    assert {p["id"]: p["model"] for p in setup["providers"]}["claude"] == "sonnet"
    assert {p["id"]: p["model"] for p in setup["providers"]}["codex"] == "gpt-6.1-sol"
    # An empty model is the default again; one with a space in it isn't a model.
    assert {p["id"]: p["model"] for p in client.patch("/api/ai", json={"model": ""}).json()["providers"]}["claude"] == "opus"
    assert client.patch("/api/ai", json={"model": "gpt 6"}).status_code == 422
    assert client.patch("/api/ai", json={"provider": "gemini"}).status_code == 422


def test_openrouter_needs_its_key(settings):
    client = TestClient(web_app.create_app(settings))
    setup = client.patch("/api/ai", json={"provider": "openrouter"}).json()
    assert "CLIPPER_OPENROUTER_API_KEY" in setup["problem"]
    with pytest.raises(LLMError, match="CLIPPER_OPENROUTER_API_KEY"):
        ai.model(settings, effort="high", timeout_s=60)

    settings.openrouter_api_key = "or-key"
    assert client.get("/api/ai").json()["problem"] == ""
    llm = ai.model(settings, effort="high", timeout_s=60)
    assert isinstance(llm, OpenRouter) and (llm.model, llm.api_key) == ("anthropic/claude-opus-5.5", "or-key")


def test_a_cli_that_isnt_signed_in_is_a_problem_to_fix_on_the_server(settings, monkeypatch):
    client = TestClient(web_app.create_app(settings))
    monkeypatch.setattr(Codex, "signed_in", lambda self: False)
    assert "--entrypoint codex worker login --device-auth" in client.patch("/api/ai", json={"provider": "codex"}).json()["problem"]
    monkeypatch.setattr(Codex, "signed_in", lambda self: True)
    assert client.get("/api/ai").json()["problem"] == ""


def test_a_task_uses_the_main_ai_until_it_gets_one_of_its_own(settings):
    client = TestClient(web_app.create_app(settings))
    tasks = {t["id"]: t for t in client.get("/api/ai").json()["tasks"]}
    assert set(tasks) == {"clips", "breakdown", "titles", "thumbnails"}
    assert tasks["breakdown"]["provider"] == "" and tasks["breakdown"]["uses"] == "Claude Code (opus)"

    # Breakdowns on Codex with a model of their own; everything else stays on the main AI.
    setup = client.patch("/api/ai", json={"task": "breakdown", "provider": "codex", "model": "gpt-6-luna"}).json()
    tasks = {t["id"]: t for t in setup["tasks"]}
    assert (tasks["breakdown"]["provider"], tasks["breakdown"]["model"]) == ("codex", "gpt-6-luna")
    assert tasks["breakdown"]["uses"] == "Codex (gpt-6-luna)" and tasks["clips"]["uses"] == "Claude Code (opus)"
    breakdowns, clips = ai.model(settings, "low", 60, task="breakdown"), ai.model(settings, "low", 60, task="clips")
    assert isinstance(breakdowns, ai.WithFallback)  # to the main AI, at Codex's usage limit
    breakdowns = breakdowns.first
    assert isinstance(breakdowns, Codex) and breakdowns.model == "gpt-6-luna"
    assert isinstance(clips, ClaudeCode) and clips.model == "opus"

    # A task on another AI with no model of its own uses that AI's model; the main AI changing doesn't move it.
    client.patch("/api/ai", json={"task": "titles", "provider": "codex"})
    client.patch("/api/ai", json={"provider": "codex", "model": "gpt-6-astra"})
    client.patch("/api/ai", json={"provider": "claude"})
    assert ai.model(settings, "low", 60, task="titles").first.model == "gpt-6-astra"

    # Back on the main AI, its own model goes too.
    tasks = {t["id"]: t for t in client.patch("/api/ai", json={"task": "breakdown", "provider": ""}).json()["tasks"]}
    assert (tasks["breakdown"]["provider"], tasks["breakdown"]["model"]) == ("", "")
    assert isinstance(ai.model(settings, "low", 60, task="breakdown"), ClaudeCode)
    assert client.patch("/api/ai", json={"task": "dancing", "provider": "codex"}).status_code == 422
    assert client.patch("/api/ai", json={"provider": ""}).status_code == 422


class Limited(ai.LLM):
    """An AI that answers, or is at its usage limit until `resets_at`."""

    def __init__(self, name: str, at_limit: bool = False, resets_at: datetime | None = None):
        self.name, self.at_limit, self.resets_at, self.asked = name, at_limit, resets_at, 0

    def structured(self, **request):
        self.asked += 1
        if self.at_limit:
            raise UsageLimitError(f"{self.name} says no", self.resets_at, note=f"{self.name}'s usage limit was reached.")
        return Answer(title=f"From {self.name}", score=0.5)


REQUEST = {"system_prompt": "s", "task": "t", "stdin_text": "x", "schema": Answer}


def test_a_task_at_its_own_ais_usage_limit_asks_the_main_ai():
    own, main = Limited("Codex", at_limit=True), Limited("Claude")
    llm = ai.WithFallback(own, lambda: main)
    assert llm.structured(**REQUEST).title == "From Claude"
    assert (own.asked, main.asked, llm.name) == (1, 1, "Claude")

    # A task whose own AI answers never gets the main AI set up.
    def no_main():
        raise AssertionError("the main AI wasn't needed")

    assert ai.WithFallback(Limited("Codex"), no_main).structured(**REQUEST).title == "From Codex"


def test_picking_clips_on_claude_code_at_its_session_limit_goes_to_codex_the_main_ai(settings, monkeypatch):
    client = TestClient(web_app.create_app(settings))
    client.patch("/api/ai", json={"provider": "codex"})
    client.patch("/api/ai", json={"task": "clips", "provider": "claude"})
    limit = json.dumps({"type": "result", "subtype": "success", "is_error": True,
                        "result": "You've hit your session limit · resets 3pm (America/New_York)"})
    codex_run = FakeCodex(answered(GOOD))

    def run(cmd, **kwargs):  # one subprocess module for both
        if cmd[0] == "claude":
            return subprocess.CompletedProcess(cmd, 1, stdout=limit, stderr="")
        return codex_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    llm = ai.model(settings, "low", 60, task="clips")
    assert llm.structured(**REQUEST) == Answer(**GOOD)
    assert llm.name == "Codex" and len(codex_run.calls) == 1


def test_with_both_at_their_limits_the_work_waits_for_the_first_to_reset():
    soon, later = datetime(2026, 11, 18, 3, tzinfo=timezone.utc), datetime(2026, 11, 20, tzinfo=timezone.utc)
    llm = ai.WithFallback(Limited("Codex", True, later), lambda: Limited("Claude", True, soon))
    with pytest.raises(UsageLimitError) as raised:
        llm.structured(**REQUEST)
    assert raised.value.resets_at == soon
    assert raised.value.note == "Codex's usage limit was reached. Claude's usage limit was reached."

    # One that doesn't say when it resets: the other's time.
    llm = ai.WithFallback(Limited("Codex", True, None), lambda: Limited("Claude", True, later))
    with pytest.raises(UsageLimitError) as raised:
        llm.structured(**REQUEST)
    assert raised.value.resets_at == later


def test_a_main_ai_that_cant_be_set_up_leaves_the_tasks_own_limit_to_wait_out(settings):
    settings.openrouter_api_key = ""
    client = TestClient(web_app.create_app(settings))
    client.patch("/api/ai", json={"provider": "openrouter"})  # the main AI, with no key
    client.patch("/api/ai", json={"task": "clips", "provider": "claude"})
    limit = datetime(2026, 11, 18, tzinfo=timezone.utc)
    llm = ai.model(settings, "low", 60, task="clips")
    llm.first = Limited("Claude", True, limit)
    with pytest.raises(UsageLimitError) as raised:
        llm.structured(**REQUEST)
    assert (raised.value.resets_at, raised.value.note) == (limit, "Claude's usage limit was reached.")


def test_only_a_task_on_another_ai_falls_back_and_never_the_breakdown_backfill(settings):
    client = TestClient(web_app.create_app(settings))
    assert isinstance(ai.model(settings, "low", 60, task="clips"), ClaudeCode)  # on the main AI already
    assert isinstance(ai.model(settings, "low", 60), ClaudeCode)
    # The same AI with another model is another AI to fall back from (OpenRouter's and the proxy's limits are a model's).
    client.patch("/api/ai", json={"task": "clips", "provider": "claude", "model": "sonnet"})
    assert isinstance(ai.model(settings, "low", 60, task="clips"), ai.WithFallback)
    client.patch("/api/ai", json={"task": "breakdown", "provider": "codex"})
    assert isinstance(ai.model(settings, "low", 60, task="breakdown", fallback=False), Codex)
    # Nor a backfill's cheaper model of the main AI itself.
    assert isinstance(ai.model(settings, "low", 60, task="titles", model_name="opus"), ClaudeCode)


def test_the_cli_proxy_needs_its_key_its_sign_ins_and_the_model(settings, monkeypatch):
    client = TestClient(web_app.create_app(settings))
    setup = client.patch("/api/ai", json={"provider": "cliproxy"}).json()
    assert setup["provider"] == "cliproxy" and "CLIPPER_CLIPROXY_API_KEY" in setup["problem"]
    with pytest.raises(LLMError, match="CLIPPER_CLIPROXY_API_KEY"):
        ai.model(settings, effort="high", timeout_s=60)

    settings.cliproxy_api_key = "proxy-key"
    offered: list = []
    monkeypatch.setattr(CliProxy, "models", lambda self: offered)
    assert "isn't signed in to any plan" in client.get("/api/ai").json()["problem"]
    offered[:] = ["gpt-6.1-sol", "claude-sonnet-5-5"]
    assert "no model 'claude-opus-5-5'" in client.get("/api/ai").json()["problem"]
    offered.append("claude-opus-5-5")
    assert client.get("/api/ai").json()["problem"] == ""
    offered = None  # a proxy that doesn't list its models: llm-check tries it
    assert client.get("/api/ai").json()["problem"] == ""
    llm = ai.model(settings, effort="high", timeout_s=60)
    assert isinstance(llm, CliProxy) and (llm.url, llm.api_key, llm.model) == ("http://cliproxy:8317/v1", "proxy-key",
                                                                                "claude-opus-5-5")

    # A task of its own on another of the proxy's models.
    tasks = {t["id"]: t for t in client.patch(
        "/api/ai", json={"task": "breakdown", "provider": "cliproxy", "model": "gpt-6.1-sol"}).json()["tasks"]}
    assert tasks["breakdown"]["uses"] == "CLI Proxy (gpt-6.1-sol)" and tasks["breakdown"]["problem"] == ""


def test_the_cli_proxy_lists_its_models_or_says_why_it_cant():
    sent, transport = proxy_answers((200, {"object": "list", "data": [{"id": "claude-opus-5-5"}, {"id": "gpt-6.1-sol"}]}))
    proxy = CliProxy(url="http://cliproxy:8317/v1", api_key="proxy-key", transport=transport)
    assert proxy.models() == ["claude-opus-5-5", "gpt-6.1-sol"]
    assert sent[0][:3] == ("GET", "http://cliproxy:8317/v1/models", "Bearer proxy-key")

    _, transport = proxy_answers((401, {"error": {"message": "Invalid API key"}}))
    with pytest.raises(LLMError, match="turned down the API key"):
        CliProxy(api_key="wrong", transport=transport).models()
    _, transport = proxy_answers((404, {"error": {"message": "not found"}}))
    assert CliProxy(transport=transport).models() is None  # one that doesn't list them

    def unreachable(request):
        raise httpx.ConnectError("Connection refused")

    assert "Couldn't reach the CLI proxy" in CliProxy(transport=httpx.MockTransport(unreachable)).auth_status()
