"""Errors shared by the AI backends."""

from datetime import datetime


class LLMError(RuntimeError):
    """The model call failed or returned an unusable answer."""


class UsageLimitError(LLMError):
    """Your Claude plan's usage limit was reached. Retry after `resets_at` when it's known."""

    def __init__(self, message: str, resets_at: datetime | None = None):
        super().__init__(message)
        self.resets_at = resets_at
