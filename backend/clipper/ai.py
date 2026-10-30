"""Which AI does the app's writing: picking the clips, preview titles and thumbnail ideas (llm/). It's chosen in the
app, or by CLIPPER_LLM_PROVIDER until it's changed there, with a model for each."""

from dataclasses import dataclass

from sqlmodel import Session

from .config import Settings
from .db import get_app_setting, get_engine
from .llm import LLM, ClaudeCode, Codex, LLMError, OpenRouter

PROVIDERS = {"claude": "Claude Code", "codex": "Codex", "openrouter": "OpenRouter"}
PROVIDER_KEY = "llm_provider"  # in the app's settings, with each provider's model under "<provider>_model"


@dataclass(frozen=True)
class Choice:
    provider: str
    model: str  # for Codex, empty is its own default


def default_model(settings: Settings, provider: str) -> str:
    return {"claude": settings.llm_model, "codex": settings.codex_model, "openrouter": settings.openrouter_model}[provider]


def chosen(s: Session, settings: Settings) -> Choice:
    provider = get_app_setting(s, PROVIDER_KEY) or settings.llm_provider
    if provider not in PROVIDERS:
        provider = "claude"
    return Choice(provider, get_app_setting(s, f"{provider}_model") or default_model(settings, provider))


def model(settings: Settings, effort: str | None, timeout_s: int) -> LLM:
    """The chosen AI, set up for one piece of work: how hard it thinks, and how long it may take."""
    with Session(get_engine(settings)) as s:
        choice = chosen(s, settings)
    return backend(settings, choice, effort, timeout_s)


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
