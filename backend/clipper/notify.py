"""Notifications: to an ntfy topic (the ntfy phone app, or anything else that follows the topic) and to a
Discord channel through its webhook. Each is optional; see CLIPPER_NTFY_URL and CLIPPER_DISCORD_WEBHOOK_URL.
Each kind of notification (KINDS) can be switched off in the app.

ntfy can't post to Discord itself, so Discord gets its own copy straight from here.
"""

import logging
import threading
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from sqlmodel import Session

from .config import Settings
from .db import get_app_setting, get_engine

log = logging.getLogger(__name__)

TIMEOUT_S = 15
# How each tone looks: ntfy priority (1-5) and tags (tags named after an emoji show as it), and the
# colour of the Discord message's side bar.
TONES = {
    "good": (3, ["white_check_mark"], 0x10B981),
    "info": (3, ["clapper"], 0x8B5CF6),
    "quiet": (2, ["eyes"], 0x71717A),
    "bad": (4, ["warning"], 0xEF4444),  # something needs you: a failure, a sign-in to redo
}

# The kinds of notification, each with its own switch in Settings; all are on until switched off.
KINDS = {
    "review": "Ready to review",
    "new": "New on a watched channel",
    "uploaded": "Uploaded",
    "failure": "Failures",
    "heads_up": "Heads-ups",
    "digest": "Weekly YouTube summary",
}


@dataclass
class Note:
    title: str
    message: str = ""
    # Opened when the notification is tapped: a full link (e.g. the video on YouTube), or a page of the
    # app such as "videos/3", which needs CLIPPER_APP_URL.
    link: str = ""
    tone: str = "info"  # see TONES
    kind: str = ""  # see KINDS; only the test button sends one without


def configured(settings: Settings) -> bool:
    return bool(settings.ntfy_url or settings.discord_webhook_url)


def switch_key(kind: str) -> str:
    return f"notify_{kind}"


def enabled(settings: Settings, kind: str) -> bool:
    """Whether a kind of notification is switched on (KINDS)."""
    with Session(get_engine(settings)) as s:
        return get_app_setting(s, switch_key(kind)) != "0"


def send(settings: Settings, note: Note) -> None:
    """Send in the background, so a slow or unreachable service never holds up the work. Skipped when its kind is
    switched off."""
    if configured(settings) and (not note.kind or enabled(settings, note.kind)):
        threading.Thread(target=deliver, args=(settings, note), name="notify", daemon=True).start()


def deliver(settings: Settings, note: Note) -> list[str]:
    """Send now. Returns what went wrong, one line per service that failed."""
    link = full_link(settings, note.link)
    errors = []
    for name, url, post in (("ntfy", settings.ntfy_url, _ntfy), ("Discord", settings.discord_webhook_url, _discord)):
        if not url:
            continue
        try:
            post(settings, note, link)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("Couldn't notify through %s: %s", name, e)
            errors.append(f"{name}: {e}")
    return errors


def full_link(settings: Settings, link: str) -> str:
    if not link or link.startswith(("http://", "https://")):
        return link
    return f"{settings.app_url.rstrip('/')}/#/{link}" if settings.app_url else ""


def _ntfy(settings: Settings, note: Note, link: str) -> None:
    # Published as JSON to the server's root, since titles with emoji can't go in HTTP headers.
    url = urlparse(settings.ntfy_url)
    base, _, topic = url.path.rstrip("/").rpartition("/")
    if not (url.scheme and url.netloc and topic):
        raise ValueError("CLIPPER_NTFY_URL needs the topic at the end, like https://ntfy.sh/your-topic")
    priority, tags, _ = TONES[note.tone]
    body = {"topic": topic, "title": note.title, "message": note.message or note.title, "priority": priority, "tags": tags}
    if link:
        body["click"] = link
    headers = {"Authorization": f"Bearer {settings.ntfy_token}"} if settings.ntfy_token else {}
    httpx.post(f"{url.scheme}://{url.netloc}{base}/", json=body, headers=headers, timeout=TIMEOUT_S).raise_for_status()


def _discord(settings: Settings, note: Note, link: str) -> None:
    *_, color = TONES[note.tone]
    embed = {"title": note.title[:256], "description": note.message[:4000], "color": color}
    if link:
        embed["url"] = link
    body = {"username": "Stream Clipper", "embeds": [embed]}
    if note.link.startswith(("http://", "https://")):
        body["content"] = note.link  # e.g. a YouTube link, which Discord shows as the video
    httpx.post(settings.discord_webhook_url, json=body, timeout=TIMEOUT_S).raise_for_status()
