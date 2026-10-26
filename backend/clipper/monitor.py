"""Watches channels on Kick, Twitch, YouTube and Rumble, and accounts on X, and queues their new videos for
processing.

A new video is queued once it's finished: not live, and the same length on two checks in a row,
because Twitch doesn't say whether a VOD is still recording. A video posted on X is finished as soon as
it's seen. Videos that were already on a channel when it was added are listed as "older" and only
processed if you ask for them in the app, except a stream that was live at the time, which is treated
as new.

Rumble's channel page only links its videos, without saying which is live or how long it is, so the newest
few that haven't been settled yet are each read for that (list_rumble).
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlparse

from sqlmodel import Session, col, or_, select
from yt_dlp import YoutubeDL
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.utils import DownloadError, unsmuggle_url

from . import notify, xposts
from .config import Settings
from .db import Channel, ChannelVideo, Show, get_engine, queue_vod, utcnow
from .notify import Note
from .pipeline.download import site_options

log = logging.getLogger(__name__)

RECENT = 15  # newest videos looked at on each listing
RUMBLE_RECENT = 5  # Rumble's are each read on their own, so fewer
RUMBLE_MAX_READS = 8  # videos read per check at most, so Rumble isn't asked too often
RUMBLE_VIDEO = re.compile(r"rumble\.com/(v[0-9a-z]+)[-.]", re.IGNORECASE)  # rumble.com/v6abcd-the-title.html
# A link to a video on a Rumble page: the whole <a> tag, and its path.
RUMBLE_ANCHOR = re.compile(r'<a\b[^>]*?href="(/v[0-9a-z]+-[^"?#]*\.html)[^"]*"[^>]*>', re.IGNORECASE)
# x.com pages that aren't accounts
X_PAGES = {"i", "home", "explore", "search", "notifications", "messages", "settings", "compose", "login", "hashtag"}


class ChannelUrlError(ValueError):
    pass


@dataclass
class Listed:
    id: str
    url: str
    title: str
    duration: float | None
    live: bool
    final: bool = False  # a post, done as soon as it's up, rather than a stream's VOD


def parse_channel(url: str) -> tuple[str, str]:
    """The platform and canonical link for a channel, e.g. ("twitch", "https://www.twitch.tv/shroud")."""
    url = url.strip()
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.").removeprefix("mobile.")
    parts = [p for p in parsed.path.split("/") if p]
    if host == "kick.com" and parts:
        return "kick", f"https://kick.com/{parts[0].lower()}"
    if host == "twitch.tv" and parts:
        return "twitch", f"https://www.twitch.tv/{parts[0].lower()}"
    if host == "youtube.com" and parts:
        if parts[0].startswith("@"):
            return "youtube", f"https://www.youtube.com/{parts[0]}"
        if parts[0] in ("channel", "c", "user") and len(parts) > 1:
            return "youtube", f"https://www.youtube.com/{parts[0]}/{parts[1]}"
    if host in ("x.com", "twitter.com") and parts:
        handle = parts[0].removeprefix("@")
        if re.fullmatch(r"\w{1,15}", handle, re.ASCII) and handle.lower() not in X_PAGES:
            return "x", f"https://x.com/{handle.lower()}"
    if host == "rumble.com" and parts:
        if parts[0] in ("c", "user") and len(parts) > 1:
            return "rumble", f"https://rumble.com/{parts[0]}/{parts[1]}"
        if not parts[0].endswith(".html") and parts[0] not in ("embed", "videos", "browse", "search"):
            return "rumble", f"https://rumble.com/c/{parts[0]}"  # rumble.com/Name is the channel's short link
    raise ChannelUrlError(
        "Use a channel link like kick.com/name, twitch.tv/name, youtube.com/@name, rumble.com/c/name or x.com/name"
    )


def listing_urls(platform: str, channel_url: str) -> list[str]:
    return {
        "kick": [f"{channel_url}/videos"],
        "twitch": [f"{channel_url}/videos?filter=archives&sort=time"],  # past broadcasts
        "youtube": [f"{channel_url}/streams", f"{channel_url}/videos"],  # past streams and uploads
    }[platform]


def list_recent(platform: str, channel_url: str) -> list[Listed]:
    """The channel's newest videos, without downloading anything."""
    found: list[Listed] = []
    for listing in listing_urls(platform, channel_url):
        opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "playlistend": RECENT}
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(listing, download=False)
        except DownloadError as e:
            if platform == "youtube" and "does not have a" in str(e).lower():
                continue  # e.g. a channel that has never streamed has no Live tab
            raise
        for entry in info.get("entries") or []:
            if not entry or not entry.get("id"):
                continue
            found.append(Listed(
                id=str(entry["id"]),
                url=unsmuggle_url(entry.get("url") or "")[0] or entry.get("webpage_url") or "",
                title=entry.get("title") or "",
                duration=float(entry["duration"]) if entry.get("duration") else None,
                live=is_live(entry),
            ))
    return found


class RumbleError(RuntimeError):
    """Rumble turned the server away; the message says why in plain words."""


def list_rumble(channel_url: str, settled: set[str] = frozenset(), others: set[str] | None = None,
                skipped: list[tuple[str, str]] | None = None) -> list[Listed]:
    """The channel's newest streams, each read on its own for whether it's live, how long it is and whose it is,
    except those already `settled` (queued, skipped or older), which the monitor has no more use for.

    The links come straight from the first page of the channel's Livestreams tab (its main page lists uploads), in
    one request. yt-dlp's own Rumble channel reader finds only one video on Rumble's current pages and then asks for
    the next page at once, which Rumble answers with "Too Many Requests" (Oct 26, 2026).

    That page also links other channels' live streams, so only streams Rumble says are this channel's count. The
    ids of other channels' streams go in `others`, so a later check doesn't read them again, and their titles and
    owners in `skipped` (for `clipper channel-check`).
    """
    others = set() if others is None else others
    found: list[Listed] = []
    with YoutubeDL({"quiet": True, "no_warnings": True, **site_options(channel_url)}) as ydl:
        try:
            page = ydl.urlopen(f"{channel_url}/livestreams").read().decode("utf-8", "replace")
        except HTTPError as e:
            raise RumbleError(_rumble_refusal(e.status)) from e
        links = rumble_links(page)
        if not links:
            raise RumbleError("Found no streams on the channel's Livestreams page. Rumble may have changed it; check the "
                              "link, and what `clipper channel-check` finds on the server.")
        reads = 0
        for path in links:
            url = f"https://rumble.com{path}"
            vid = RUMBLE_VIDEO.search(url)[1]
            if vid in settled or vid in others:
                continue
            if len(found) >= RUMBLE_RECENT or reads >= RUMBLE_MAX_READS:
                break
            reads += 1
            try:
                video = ydl.extract_info(url, download=False)
            except DownloadError as e:
                if "429" in str(e):
                    raise RumbleError(_rumble_refusal(429)) from e
                log.info("Couldn't read %s: %s", url, e)
                continue
            if not rumble_owner_is(video, channel_url):
                others.add(vid)
                if skipped is not None:
                    owner = video.get("channel_url") or video.get("channel") or video.get("uploader") or "nobody said"
                    skipped.append((video.get("title") or url, owner))
                continue
            found.append(Listed(
                id=vid, url=url, title=video.get("title") or "",
                duration=float(video["duration"]) if video.get("duration") else None, live=is_live(video),
            ))
    return found


def rumble_links(page: str) -> list[str]:
    """Video links on a Rumble page, in order and once each: those in the page's own video grid (class
    videostream__link) if it has any, since the rest of the page links other channels."""
    anchors = [(m[1], m[0]) for m in RUMBLE_ANCHOR.finditer(page)]
    grid = [href for href, tag in anchors if "videostream__link" in tag]
    return list(dict.fromkeys(grid or [href for href, _ in anchors]))


def rumble_owner_is(video: dict, channel_url: str) -> bool:
    """Whether Rumble says the video belongs to the channel: by its channel link (/c/Name or /user/Name), else by
    its channel's name. A video that says neither doesn't count, rather than risk another channel's stream."""
    want = channel_url.rstrip("/").rsplit("/", 1)[-1].lower()
    owner = (video.get("channel_url") or video.get("uploader_url") or "").rstrip("/")
    if owner:
        return owner.rsplit("/", 1)[-1].lower() == want
    names = {re.sub(r"\W", "", (video.get(k) or "").lower()) for k in ("channel", "uploader")}
    return re.sub(r"\W", "", want) in names


def _rumble_refusal(status: int) -> str:
    if status == 429:
        return ("Rumble says the server asked too often (429 Too Many Requests). It usually clears up within the hour; "
                "the channel is tried again at each check.")
    if status == 403:
        return ("Rumble's Cloudflare check turned the server away (403 Forbidden). A newer yt-dlp (rebuild the image) "
                "may get through again.")
    return f"Rumble answered {status}."


def is_live(entry: dict) -> bool:
    return (
        bool(entry.get("is_live"))
        or entry.get("live_status") in ("is_live", "is_upcoming", "post_live")
        or "/404_processing_" in (entry.get("thumbnail") or "")  # Twitch: a VOD still recording
    )


def skip_reason(channel: Channel, item: Listed) -> str:
    """Why the channel's filters leave this video out, or "" to process it.

    Keywords are comma-separated, and any one of them will do; one made of parts joined with "+" needs
    all its parts, e.g. "tonight on + kino casino". Case and repeated spaces don't matter.
    """
    title = _words(item.title)
    what = "post" if channel.platform == "x" else "title"
    keywords = [k.strip() for k in channel.keywords.split(",") if k.strip()]
    exclude = [x.strip() for x in channel.exclude.split(",") if x.strip()]
    if keywords and not any(_has(title, k) for k in keywords):
        return f"The {what} has none of the keywords"
    if hit := next((x for x in exclude if _has(title, x)), None):
        return f'The {what} has "{hit}"'
    if channel.min_minutes and (item.duration or 0) < channel.min_minutes * 60:
        return f"Shorter than {channel.min_minutes:g} minutes"
    return ""


class Monitor:
    def __init__(self, settings: Settings, lister: Callable[[str, str], list[Listed]] | None = None):
        self.settings = settings
        self.engine = get_engine(settings)
        self.lister = lister or self._list
        self._rumble_others: dict[str, set[str]] = {}  # other channels' streams seen on each Rumble channel's page

    def _list(self, platform: str, channel_url: str) -> list[Listed]:
        if platform == "rumble":
            with Session(self.engine) as s:
                settled = set(s.exec(
                    select(ChannelVideo.video_id).join(Channel)
                    .where(Channel.url == channel_url, ChannelVideo.status != "waiting")
                ))
            return list_rumble(channel_url, settled, self._rumble_others.setdefault(channel_url, set()))
        if platform != "x":
            return list_recent(platform, channel_url)
        posts = xposts.video_posts(self.settings.studio_cdp_url, channel_url.rsplit("/", 1)[-1])
        return [Listed(id=p.id, url=p.url, title=p.text[:1000], duration=p.duration, live=False, final=True) for p in posts]

    def check_next(self) -> bool:
        """Check the most overdue channel, if one is due. True if a channel was checked."""
        due = utcnow() - timedelta(seconds=self.settings.monitor_interval_s)
        with Session(self.engine) as s:
            channel = s.exec(
                select(Channel)
                .where(Channel.enabled, or_(col(Channel.last_checked_at).is_(None), col(Channel.last_checked_at) <= due))
                .order_by(col(Channel.last_checked_at).is_not(None), col(Channel.last_checked_at))
            ).first()
            if not channel:
                return False
            channel_id = channel.id
        self.check(channel_id)
        return True

    def check(self, channel_id: int) -> None:
        with Session(self.engine) as s:
            channel = s.get(Channel, channel_id)
            platform, url, was_failing = channel.platform, channel.url, bool(channel.last_error)
        try:
            listed = self.lister(platform, url)
        except Exception as e:
            log.warning("Checking %s failed: %s", url, e)
            error = str(e) if isinstance(e, (xposts.XError, RumbleError)) else f"{type(e).__name__}: {e}"
            self._finish(channel_id, last_error=error[:500])
            if not was_failing:  # once, not on every check until it's fixed
                notify.send(self.settings, Note(f"Can't check {url}", error[:500], link="shows", tone="bad"))
            return

        with Session(self.engine) as s:
            channel = s.get(Channel, channel_id)
            show = s.get(Show, channel.show_id)
            queued = 0
            new_titles = []
            for item in listed:
                row = s.exec(
                    select(ChannelVideo).where(ChannelVideo.channel_id == channel_id, ChannelVideo.video_id == item.id)
                ).first()
                if row is None:
                    # A stream that's live when the channel is added still counts as new: its VOD isn't done yet.
                    if not (channel.baseline_done or item.live or (not item.duration and not item.final)):
                        s.add(ChannelVideo(
                            channel_id=channel_id, video_id=item.id, url=item.url, title=item.title,
                            duration=item.duration, status="older", reason="Already on the channel when you added it",
                        ))
                        continue
                    row = ChannelVideo(channel_id=channel_id, video_id=item.id, url=item.url)
                elif row.status != "waiting":
                    continue
                finished = not item.live and (item.final or bool(item.duration and row.duration == item.duration))
                row.title, row.duration, row.updated_at = item.title, item.duration, utcnow()
                if not finished:
                    row.status, row.reason = "waiting", _waiting_reason(item)
                elif reason := skip_reason(channel, item):
                    row.status, row.reason = "skipped", reason
                else:
                    row.vod_id = queue_vod(s, row.url, show=show).id
                    row.status, row.reason = "queued", ""
                    queued += 1
                    new_titles.append((row.vod_id, row.title or row.url))
                s.add(row)
            s.commit()
            show_name = show.name
        if queued:
            log.info("Queued %d new video(s) from %s", queued, url)
        for vod_id, title in new_titles:
            notify.send(self.settings, Note(f"New for {show_name}", title[:300], link=f"videos/{vod_id}", tone="quiet"))
        self._finish(channel_id, last_error="", baseline_done=True)

    def _finish(self, channel_id: int, **fields) -> None:
        with Session(self.engine) as s:
            channel = s.get(Channel, channel_id)
            if channel is None:
                return  # removed while being checked
            for k, v in fields.items():
                setattr(channel, k, v)
            channel.last_checked_at = utcnow()
            s.add(channel)
            s.commit()


def _words(text: str) -> str:
    return " ".join(text.lower().split())


def _has(text: str, keyword: str) -> bool:
    return all(_words(part) in text for part in keyword.split("+") if part.strip())


def _waiting_reason(item: Listed) -> str:
    return "Still live" if item.live or not item.duration else "Making sure the stream has ended"
