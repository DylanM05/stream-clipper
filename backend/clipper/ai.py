"""Which AI does the app's writing: picking the clips, preview titles and thumbnail ideas (llm/). It's chosen in the
app, or by CLIPPER_LLM_PROVIDER until it's changed there, with a model for each.

That's the main AI. Each task (TASKS) uses it unless the app gives the task an AI of its own, such as Claude Code for
picking clips and Codex for breakdowns, with that AI's model or another of its models for just that task.
"""

from dataclasses import dataclass

from sqlmodel import Session

from .config import Settings
from .db import get_app_setting, get_engine
from .llm import LLM, ClaudeCode, Codex, LLMError, OpenRouter

PROVIDERS = {"claude": "Claude Code", "codex": "Codex", "openrouter": "OpenRouter"}
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
    return {"claude": settings.llm_model, "codex": settings.codex_model, "openrouter": settings.openrouter_model}[provider]


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
          task: str | None = None) -> LLM:
    """The AI for a task (chosen), set up for one piece of work: how hard it thinks, and how long it may take.
    `model_name` is another of its models to use for this work instead (e.g. a cheaper one for a backfill)."""
    with Session(get_engine(settings)) as s:
        choice = chosen(s, settings, task)
    if model_name:
        choice = Choice(choice.provider, model_name)
    return backend(settings, choice, effort, timeout_s)


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
    return ClaudeCode(bin=settings.claude_bin, model=choice.model, effort=effort, timeout_s=timeout_s)


def problem(settings: Settings, choice: Choice) -> str:
    """What stops the chosen AI from working, in a sentence for the app; empty if nothing does."""
    if choice.provider == "openrouter":
        return "" if settings.openrouter_api_key else "It needs CLIPPER_OPENROUTER_API_KEY in the server's .env."
    llm = backend(settings, choice)
    if isinstance(llm, Codex) and not llm.signed_in():
        return ("Codex isn't signed in on the server: run "
                "docker compose run --rm --entrypoint codex worker login --device-auth there.")
    if isinstance(llm, ClaudeCode) and not llm.signed_in():
        return "Claude Code isn't signed in on the server: run docker compose run --rm --entrypoint claude worker there."
    return ""
