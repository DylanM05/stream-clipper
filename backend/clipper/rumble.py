"""Rumble channels and videos.

Rumble sits behind Cloudflare's bot check, and its pages have moved on from what yt-dlp expects (Oct 26, 2026):
- A channel's list of videos is built by JavaScript in the browser. The page a plain request gets holds one of
  the channel's videos at most, plus the "live now" strip of other channels' streams that every Rumble page has.
  So the list is read from the upload browser (studio.py, the same one that reads X), where the page runs as
  it does for you.
- A video's page carries another video's player before its own, and yt-dlp takes the first one, so it reports,
  and downloads, the wrong video. Each video's own player is looked up with Rumble's oEmbed API instead (the
  page's id, like v7h973m, isn't the player's), and yt-dlp is given that.
- The requests yt-dlp makes to Rumble pass themselves off as Chrome (with curl-cffi), which gets round the bot
  check. That was chosen knowingly on Oct 26, 2026, and stops working if Cloudflare catches on.
"""

import json
import logging
import re
import time
from urllib.parse import urlencode, urlparse

from playwright.sync_api import sync_playwright
from yt_dlp import YoutubeDL
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.networking.impersonate import ImpersonateTarget

from .studio import NOT_RUNNING

log = logging.getLogger(__name__)

OEMBED_URL = "https://rumble.com/api/Media/oembed.json"
VIDEO_PAGE = re.compile(r"rumble\.com/(v[0-9a-z]+)[-.]", re.IGNORECASE)  # rumble.com/v6abcd-the-title.html
PLAYER = re.compile(r"rumble\.com/embed/(?:[0-9a-z]+\.)?([0-9a-z]+)", re.IGNORECASE)  # rumble.com/embed/v5xyz/
GRID_WAIT_S = 30  # for the channel's page to show its videos in the browser


class RumbleError(RuntimeError):
    """Rumble, or the upload browser, didn't give what was needed; the message says why in plain words."""


def is_rumble(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "rumble.com" or host.endswith(".rumble.com")


def ytdlp_options(url: str) -> dict:
    """What yt-dlp needs to get anything from Rumble: requests that look like Chrome's. Nothing for other sites."""
    return {"impersonate": ImpersonateTarget.from_str("chrome")} if is_rumble(url) else {}


def channel_videos(cdp_url: str, channel_url: str) -> list[str]:
    """Links to the videos on the channel's Live tab, as the upload browser shows it, in page order. The "live now"
    strip of other channels' streams is among them: tell those apart by who Rumble says made each (owner_is)."""
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp(cdp_url, timeout=15_000)
            except Exception as e:
                raise RumbleError(NOT_RUNNING) from e
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()
            try:
                page.goto(f"{channel_url}/livestreams", wait_until="load", timeout=60_000)
                links = _wait_for_videos(page)
                if not links:
                    raise RumbleError(
                        f"The channel's page showed no videos in the upload browser (its title: {page.title()!r}). Open "
                        f"the upload browser and look at {channel_url}/livestreams; Rumble may want it to prove it's human."
                    )
                return links
            finally:
                page.close()
    except RumbleError:
        raise
    except Exception as e:
        raise RumbleError(f"Lost the upload browser ({e})".splitlines()[0][:500]) from e


def _wait_for_videos(page) -> list[str]:
    """The page's video links once the list has filled in: when no new ones turn up for two seconds."""
    deadline = time.monotonic() + GRID_WAIT_S
    links: list[str] = []
    steady_since = time.monotonic()
    while time.monotonic() < deadline:
        hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
        now = video_links(hrefs)
        if len(now) != len(links):
            links, steady_since = now, time.monotonic()
        elif links and time.monotonic() - steady_since >= 2:
            break
        page.wait_for_timeout(500)
    return links


def video_links(hrefs: list[str]) -> list[str]:
    """Rumble video pages among links, without their tracking (?e9s=…), in order, once each."""
    found = []
    for href in hrefs:
        path = urlparse(href).path
        if VIDEO_PAGE.search(f"rumble.com{path}") and path.endswith(".html"):
            found.append(f"https://rumble.com{path}")
    return list(dict.fromkeys(found))


def player_url(video_url: str, ydl: YoutubeDL) -> str:
    """The video's own player (rumble.com/embed/…), which yt-dlp reads correctly, from Rumble's oEmbed API. A
    player link is returned as it is."""
    if PLAYER.search(video_url):
        return video_url
    try:
        answer = json.loads(ydl.urlopen(f"{OEMBED_URL}?{urlencode({'url': video_url})}").read())
    except HTTPError as e:
        raise RumbleError(refusal(e.status) if e.status in (403, 429) else
                          f"Rumble wouldn't say which player is {video_url}'s (its oEmbed API answered {e.status}).") from e
    except ValueError as e:
        raise RumbleError(f"Rumble's oEmbed API didn't answer with JSON for {video_url}.") from e
    m = PLAYER.search(str(answer.get("html") or "")) if isinstance(answer, dict) else None
    if not m:
        raise RumbleError(f"Rumble's oEmbed API gave no player for {video_url}.")
    return f"https://rumble.com/embed/{m[1]}/"


def owner_is(video: dict, channel_url: str) -> bool:
    """Whether Rumble says the video belongs to the channel: by its channel link (/c/Name or /user/Name), else by
    its channel's name. A video that says neither doesn't count, rather than risk another channel's stream."""
    want = channel_url.rstrip("/").rsplit("/", 1)[-1].lower()
    owner = (video.get("channel_url") or video.get("uploader_url") or "").rstrip("/")
    if owner:
        return owner.rsplit("/", 1)[-1].lower() == want
    names = {re.sub(r"\W", "", (video.get(k) or "").lower()) for k in ("channel", "uploader")}
    return re.sub(r"\W", "", want) in names


def refusal(status: int) -> str:
    if status == 429:
        return ("Rumble says the server asked too often (429 Too Many Requests). It usually clears up within the hour; "
                "the channel is tried again at each check.")
    if status == 403:
        return ("Rumble's Cloudflare check turned the server away (403 Forbidden). A newer yt-dlp (rebuild the image) "
                "may get through again.")
    return f"Rumble answered {status}."
