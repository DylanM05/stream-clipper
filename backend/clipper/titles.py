"""Video titles from a show's pattern, such as "NIGHT OWLS {date} - {title}".

The AI writes only the {title} part, sized to fit the room the pattern leaves within YouTube's limit.
"""

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from .config import Settings
from . import ai
from .timecode import to_seconds

MAX_TITLE = 100  # YouTube's limit
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def youtube_length(text: str) -> int:
    """Length counted in UTF-16 units, so emoji count double. That's at least what YouTube counts."""
    return len(text.encode("utf-16-le")) // 2


def format_date(day: date | None) -> str:
    """Oct 23, 2026"""
    return f"{day:%b} {day.day}, {day.year}" if day else ""


def stream_date(meta: dict, fallback: date, timezone: str = "UTC", day_starts_at: int = 0) -> date:
    """The day of a stream or post as its creators would say it: when it started, in their `timezone`, with
    the hours before `day_starts_at` counting as the day before.

    yt-dlp's own upload_date is that time's day in UTC, which for an evening stream in America is often
    already the next day; it's only used when there's no time. Then `fallback`.
    """
    if started := meta.get("release_timestamp") or meta.get("timestamp"):
        local = datetime.fromtimestamp(started, ZoneInfo(timezone))
        return (local - timedelta(hours=day_starts_at)).date()
    try:
        return datetime.strptime(meta.get("upload_date") or "", "%Y%m%d").date()
    except ValueError:
        return fallback


def apply(template: str, title: str, day: str) -> str:
    text = (template or "{title}").replace("{date}", day).replace("{title}", title)
    return " ".join(text.split())


def room_for_title(template: str, day: str) -> int:
    """How long the {title} part may be so the whole title fits YouTube's limit."""
    fixed = youtube_length(apply(template, "", day))
    return max(20, MAX_TITLE - fixed - (1 if fixed else 0))


def build(template: str, title: str, day: str) -> str:
    """The full title, with the {title} part shortened at a word boundary if it doesn't fit."""
    return apply(template, fit(" ".join(title.split()), room_for_title(template, day)), day)


def own_part(template: str, title: str) -> str:
    """The {title} part of a full title made with `template`; the whole title if it doesn't match (e.g. edited)."""
    template = " ".join((template or "{title}").split())
    pattern = re.escape(template).replace(re.escape("{date}"), ".*?")
    pattern = pattern.replace(re.escape("{title}"), "(.+?)", 1).replace(re.escape("{title}"), ".+?")
    m = re.fullmatch(pattern, title)
    return m[1].strip() if m and m.groups() else title


def fit(text: str, limit: int) -> str:
    if youtube_length(text) <= limit:
        return text
    kept = ""
    for word in text.split():
        candidate = f"{kept} {word}".strip()
        if youtube_length(candidate) + 1 > limit:
            break
        kept = candidate
    return (kept or text[: max(1, limit - 1)]) + "…"


def chapter_list(chapters: list[tuple[float, str]]) -> str:
    """Chapter lines for a full episode's description, which YouTube turns into video chapters.

    YouTube needs at least three, the first at 0:00, each at least 10 seconds long; otherwise "".
    """
    entries = sorted(chapters)
    if entries and entries[0][0] > 0:
        entries = [(0.0, "Start"), *entries] if entries[0][0] >= 10 else [(0.0, entries[0][1]), *entries[1:]]
    kept: list[tuple[float, str]] = []
    for t, title in entries:
        if not kept or t - kept[-1][0] >= 10:
            kept.append((t, title))
    if len(kept) < 3:
        return ""
    return "Chapters\n" + "\n".join(f"{_stamp(t)} {title}" for t, title in kept)


_STAMPED_LINE = re.compile(r"^([ \t]*)((?:\d+:)?\d{1,2}:\d{2})(?=[ \t])", re.MULTILINE)


def shift_chapters(text: str, seconds: float) -> str:
    """A description's chapter times moved on by `seconds`, for its video with an intro joined onto the start: every
    line that starts with a time, except 0:00, where YouTube needs the first chapter (the intro becomes part of it)."""
    if seconds <= 0:
        return text

    def moved(m: re.Match) -> str:
        at = to_seconds(m.group(2))
        return m.group(1) + (m.group(2) if at == 0 else _stamp(at + round(seconds)))

    return _STAMPED_LINE.sub(moved, text)


def _stamp(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


class _Preview(BaseModel):
    title: str = Field(description="The short title, without the show name or date")


def preview_title(post_text: str, room: int, settings: Settings, notes: str = "") -> str:
    """A short title previewing what a posted video is about, written by the AI from the post's text."""
    llm = ai.model(settings, effort="low", timeout_s=300, task="titles")
    stdin = f"# Post text\n{post_text.strip()}\n"
    if notes.strip():
        stdin += f"\n# Notes about the show\n{notes.strip()}\n"
    answer = llm.structured(
        system_prompt=(PROMPTS_DIR / "preview_title.md").read_text(encoding="utf-8"),
        task=f"Write the title preview, at most {room} characters.",
        stdin_text=stdin,
        schema=_Preview,
    )
    return " ".join(answer.title.split())
