"""Which AI does the app's writing: picking the clips, preview titles and thumbnail ideas (llm/). It's chosen in the
app, or by CLIPPER_LLM_PROVIDER until it's changed there, with a model for each.

That's the main AI. Each task (TASKS) uses it unless the app gives the task an AI of its own, such as Claude Code for
picking clips and Codex for breakdowns, with that AI's model or another of its models for just that task. A task whose
own AI reaches its usage limit asks the main AI instead (WithFallback).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlmodel import Session

from .config import Settings
from .db import get_app_setting, get_engine
from .llm import LLM, ClaudeCode, CliProxy, Codex, LLMError, OpenRouter, UsageLimitError

log = logging.getLogger(__name__)

PROVIDERS = {"claude": "Claude Code", "codex": "Codex", "openrouter": "OpenRouter", "cliproxy": "CLI Proxy"}
PROVIDER_KEY = "llm_provider"  # in the app's settings, with each provider's model under "<provider>_model"

# The work the AI does, each of which can have an AI of its own: in the app's settings, its provider under
# "ai_task_<task>" ("" or none: the main AI) and its model under "ai_task_<task>_model" ("": that AI's model).
TASKS = {
    "clips": "Picking clips",
    "breakdown": "Breakdowns on their own",
    "titles": "Preview titles",
    "thumbnails": "Thumbnail ideas",
}
TASK_HINTS = {
    "clips": "Reads each new stream's transcript and picks its clips, and its breakdown for shows that have one.",
    "breakdown": "clipper breakdown: past streams, ones imported from an archive, or again after the categories change.",
    "titles": "A short title for a video posted whole, from its post's text.",
    "thumbnails": "What a thumbnail shows and the prompt for the image model.",
}


@dataclass(frozen=True)
class Choice:
    provider: str
    model: str  # for Codex, empty is its own default


def default_model(settings: Settings, provider: str) -> str:
    return {"claude": settings.llm_model, "codex": settings.codex_model, "openrouter": settings.openrouter_model,
            "cliproxy": settings.cliproxy_model}[provider]


def provider_model(s: Session, settings: Settings, provider: str) -> str:
    """The model an AI uses, as set in the app or else by default."""
    return get_app_setting(s, f"{provider}_model") or default_model(settings, provider)


def chosen(s: Session, settings: Settings, task: str | None = None) -> Choice:
    """The AI for a task (TASKS): its own if the app gives it one, else the main AI. No task: the main AI."""
    if task and (own := get_app_setting(s, f"ai_task_{task}")) in PROVIDERS:
        return Choice(own, get_app_setting(s, f"ai_task_{task}_model") or provider_model(s, settings, own))
    provider = get_app_setting(s, PROVIDER_KEY) or settings.llm_provider
    if provider not in PROVIDERS:
        provider = "claude"
    return Choice(provider, provider_model(s, settings, provider))


def model(settings: Settings, effort: str | None, timeout_s: int, model_name: str | None = None,
          task: str | None = None, fallback: bool = True) -> LLM:
    """The AI for a task (chosen), set up for one piece of work: how hard it thinks, and how long it may take.
    `model_name` is another of its models to use for this work instead (e.g. a cheaper one for a backfill). With
    `fallback`, a task on an AI other than the main one asks the main AI once its own reaches its usage limit."""
    with Session(get_engine(settings)) as s:
        choice = chosen(s, settings, task)
        main = chosen(s, settings)
    if model_name:
        choice = Choice(choice.provider, model_name)
    llm = backend(settings, choice, effort, timeout_s)
    if not (task and fallback and choice != main):
        return llm
    return WithFallback(llm, lambda: backend(settings, main, effort, timeout_s))


class WithFallback(LLM):
    """A task's own AI, with the main AI to ask once the task's reaches its usage limit. When both have, the work waits
    for whichever limit ends first: its next try asks the task's AI, then the main AI, again."""

    def __init__(self, first: LLM, fallback: Callable[[], LLM]):
        self.first = first
        self._fallback = fallback
        self.name = first.name

    def structured(self, **request):
        try:
            return self.first.structured(**request)
        except UsageLimitError as limit:
            try:
                main = self._fallback()
            except LLMError as e:
                log.warning("%s reached its usage limit, and the main AI can't take over: %s", self.first.name, e)
                raise limit from None
            log.warning("%s reached its usage limit (%s); asking the main AI, %s, instead", self.first.name, limit, main.name)
            self.name = main.name
            try:
                return main.structured(**request)
            except UsageLimitError as also:
                known = [t for t in (limit.resets_at, also.resets_at) if t]
                raise UsageLimitError(f"{limit} Then the main AI: {also}", resets_at=_earliest(known),
                                      note=f"{limit.note} {also.note}") from None

    def plan_usage(self) -> list[dict] | None:
        return self.first.plan_usage()


def _earliest(times: list[datetime]) -> datetime | None:
    return min(times) if times else None


def in_use(s: Session, settings: Settings) -> list[Choice]:
    """Every AI the tasks use, each once: the ones to check are set up."""
    return list(dict.fromkeys([chosen(s, settings), *(chosen(s, settings, task) for task in TASKS)]))


def backend(settings: Settings, choice: Choice, effort: str | None = None, timeout_s: int = 300) -> LLM:
    if choice.provider == "codex":
        return Codex(bin=settings.codex_bin, model=choice.model, effort=effort, timeout_s=timeout_s)
    if choice.provider == "openrouter":
        if not settings.openrouter_api_key:
            raise LLMError("A model on OpenRouter needs CLIPPER_OPENROUTER_API_KEY in .env (see the README)")
        return OpenRouter(api_key=settings.openrouter_api_key, model=choice.model, effort=effort, timeout_s=timeout_s)
    if choice.provider == "cliproxy":
        if not settings.cliproxy_api_key:
            raise LLMError("The CLI proxy needs CLIPPER_CLIPROXY_API_KEY in .env (see the README)")
        return CliProxy(url=settings.cliproxy_url, api_key=settings.cliproxy_api_key, model=choice.model, effort=effort,
                        timeout_s=timeout_s)
    return ClaudeCode(bin=settings.claude_bin, model=choice.model, effort=effort, timeout_s=timeout_s)


def problem(settings: Settings, choice: Choice) -> str:
    """What stops the chosen AI from working, in a sentence for the app; empty if nothing does."""
    if choice.provider == "openrouter":
        return "" if settings.openrouter_api_key else "It needs CLIPPER_OPENROUTER_API_KEY in the server's .env."
    if choice.provider == "cliproxy":
        return cliproxy_problem(settings, choice)
    llm = backend(settings, choice)
    if isinstance(llm, Codex) and not llm.signed_in():
        return ("Codex isn't signed in on the server: run "
                "docker compose run --rm --no-deps --entrypoint codex worker login --device-auth there.")
    if isinstance(llm, ClaudeCode) and not llm.signed_in():
        return "Claude Code isn't signed in on the server: run docker compose run --rm --no-deps --entrypoint claude worker there."
    return ""


def cliproxy_problem(settings: Settings, choice: Choice) -> str:
    """Whether the CLI proxy answers with the key, and offers the model: it lists the ones its sign-ins can use."""
    if not settings.cliproxy_api_key:
        return "It needs CLIPPER_CLIPROXY_API_KEY in the server's .env: one of the api-keys in the proxy's config.yaml."
    try:
        models = CliProxy(url=settings.cliproxy_url, api_key=settings.cliproxy_api_key).models()
    except LLMError as e:
        return f"{e}."
    if models is None:  # it doesn't list them: llm-check tries the model
        return ""
    if not models:
        return ("The CLI proxy isn't signed in to any plan yet: run docker compose exec cliproxy /CLIProxyAPI/CLIProxyAPI "
                "-no-browser --claude-login (or --codex-login) on the server (README).")
    if choice.model.split("(")[0] not in models:
        return f"The CLI proxy has no model {choice.model!r}. It has: {', '.join(models[:12])}."
    return ""
