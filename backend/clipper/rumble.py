"""Rumble channels and videos.

Rumble sits behind Cloudflare's bot check, and its pages have moved on from what yt-dlp expects (Oct 26, 2026):
- A channel's list of videos is built by JavaScript in the browser. The page a plain request gets holds one of
  the channel's videos at most, plus the "live now" strip of other channels' streams that every Rumble page has.
  So the list is read from the upload browser (studio.py, the same one that reads X), where the page runs as
  it does for you.
- A video's page carries the channel's newest (or featured) video's player before its own. yt-dlp takes the
  first one, and so does Rumble's oEmbed API, so both report, and download, the wrong video: the "9 Thugs" page
  came back as the "$1000 Viewer Event", and once a newer stream was up, the Viewer Event's page as that. Each
  video's own player is read from the page's description of its own video instead (player_url; the page's
  id, like v7h973m, isn't the player's), and yt-dlp is given that.
- The requests yt-dlp makes to Rumble pass themselves off as Chrome (with curl-cffi), which gets round the bot
  check. That was chosen knowingly on Oct 26, 2026, and stops working if Cloudflare catches on.
"""

import json
import logging
import re
import time
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright
from yt_dlp import YoutubeDL
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.networking.impersonate import ImpersonateTarget

from .studio import NOT_RUNNING

log = logging.getLogger(__name__)

VIDEO_PAGE = re.compile(r"rumble\.com/(v[0-9a-z]+)[-.]", re.IGNORECASE)  # rumble.com/v6abcd-the-title.html
PLAYER = re.compile(r"rumble\.com/embed/(?:[0-9a-z]+\.)?([0-9a-z]+)", re.IGNORECASE)  # rumble.com/embed/v5xyz/
# Rumble leaves the quotes off its attributes where it can: <script type=application/ld+json>
JSON_LD = re.compile(r"<script[^>]*type=[\"']?application/ld\+json[\"']?[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)
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
    """The video's own player (rumble.com/embed/…), which yt-dlp reads correctly. A player link is returned as it is.

    The page describes its own video for search engines (a JSON-LD VideoObject) with the page's link and its
    player's. Only a description whose link is this page counts, so another video's player on the page can't be
    taken for it. If there's none, it says so rather than guess.
    """
    if PLAYER.search(video_url):
        return video_url
    page = VIDEO_PAGE.search(video_url)
    if not page:
        raise RumbleError(f"{video_url} isn't a Rumble video's page.")
    try:
        html = ydl.urlopen(video_url).read().decode("utf-8", "replace")
    except HTTPError as e:
        raise RumbleError(refusal(e.status) if e.status in (403, 429) else
                          f"Rumble answered {e.status} for {video_url}.") from e
    for item in _described(html):
        own = VIDEO_PAGE.search(str(item.get("url") or ""))
        player = PLAYER.search(str(item.get("embedUrl") or ""))
        if item.get("@type") == "VideoObject" and own and own[1].lower() == page[1].lower() and player:
            return f"https://rumble.com/embed/{player[1]}/"
    raise RumbleError(f"Couldn't tell which player on {video_url} is its own: the page doesn't describe its video "
                      "the way Rumble's pages did (Oct 2026), so rumble.py needs updating.")


def _described(html: str) -> list[dict]:
    """Everything the page describes in its JSON-LD blocks, flattened."""
    found: list[dict] = []

    def walk(node) -> None:
        if isinstance(node, list):
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            found.append(node)
            walk(node.get("@graph"))

    for block in JSON_LD.findall(html):
        try:
            walk(json.loads(block))
        except ValueError:
            continue  # a broken block; the others may still say
    return found


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
