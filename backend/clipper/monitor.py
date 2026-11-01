"""Watches channels on Kick, Twitch, YouTube and Rumble, accounts on X, and folders of TV episodes on the server, and
queues their new videos for processing.

A new video is queued once it's finished: not live, and the same length on two checks in a row,
because Twitch doesn't say whether a VOD is still recording. A video posted on X is finished as soon as
it's seen, and a file in a folder once it hasn't changed for FILE_SETTLE, so one still being copied in waits.
Videos that were already on a channel when it was added are listed as "older" and only processed if you ask
for them in the app, except a stream that was live at the time, which is treated as new.

A channel or folder in the Library (Channel.backlog) has its older videos worked through by itself instead, a few a
day, in order (backlog_order): a folder's in season and episode order, a channel's oldest first. A channel's whole
history is listed for that, once, where the platform can list it (HISTORY_PLATFORMS).

A Rumble channel's videos are listed by the upload browser (rumble.py), without saying which is live or how
long it is, so of the newest few, those that haven't been settled yet are each read for that (list_rumble).
"""

import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import func
from sqlmodel import Session, col, or_, select
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, unsmuggle_url

from . import notify, rumble, xposts
from .config import Settings
from .db import Channel, ChannelVideo, Show, get_engine, queue_vod, utcnow
from .notify import Note
from .rumble import RumbleError

log = logging.getLogger(__name__)

RECENT = 15  # newest videos looked at on each listing
HISTORY_PLATFORMS = ("kick", "twitch", "youtube")  # can list every past video; Rumble's and X's pages only list the newest
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
RUMBLE_RECENT = 5  # Rumble's are each read on their own, so fewer
# Videos read per check at most, so Rumble isn't asked too often: enough for the other channels' live streams every
# page links plus the channel's own newest five. Later checks only read what's new.
RUMBLE_MAX_READS = 10
# A Twitch stream's preview image: .../cf_vods/<server>/<hash>_<channel>_<stream id>_<unix start time>//thumb/...
TWITCH_STARTED = re.compile(r"/cf_vods/[^/]+/[0-9a-f]+_\w+_\d+_(\d{10})/")
# x.com pages that aren't accounts
X_PAGES = {"i", "home", "explore", "search", "notifications", "messages", "settings", "compose", "login", "hashtag"}
# Files in a watched folder that are videos. A partial download's name ends in something else (.part, .!qB).
VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts", ".mpeg", ".mpg"}
FILE_SETTLE = timedelta(minutes=3)  # a file in a watched folder that changed more recently is still being copied in


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
    published: float | None = None  # when it was posted or streamed (unix time), if the platform says


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


def parse_folder(path: str, media_dir: Path | None) -> str:
    """The full path of a folder to watch, which must be inside the media folder (Settings.media_dir)."""
    if media_dir is None:
        raise ChannelUrlError("Set CLIPPER_HOST_MEDIA_DIR to the server's media folder to watch folders on it (README)")
    try:
        folder = Path(path.strip()).resolve(strict=True)
        inside = folder.is_dir() and folder.is_relative_to(media_dir.resolve(strict=True))
    except OSError:
        inside = False
    if not inside:
        raise ChannelUrlError(
            f"Use a folder inside {media_dir}, where the server's CLIPPER_HOST_MEDIA_DIR is, e.g. {media_dir}/TV/Show"
        )
    return str(folder)


def list_folder(folder: str, media_dir: Path | None) -> list[Listed]:
    """The video files in a watched folder and its subfolders (Season 01/…), in season and episode order. One that
    changed in the last FILE_SETTLE is listed as live: it's still being copied in. Hidden files (a Mac's ._ copies)
    and links, which could lead out of the media folder, are left out."""
    root = Path(parse_folder(folder, media_dir))
    now = time.time()
    found: list[Listed] = []
    for base, dirs, files in os.walk(root, onerror=_unreadable):  # doesn't go into links to folders
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            path = Path(base, name)
            if name.startswith(".") or path.suffix.lower() not in VIDEO_SUFFIXES or path.is_symlink():
                continue
            try:
                changed = path.stat().st_mtime
            except OSError:  # gone since the folder was read
                continue
            found.append(Listed(
                id=path.relative_to(root).as_posix(), url=str(path), title=path.stem, duration=None,
                live=now - changed < FILE_SETTLE.total_seconds(), final=True, published=changed,
            ))
    return sorted(found, key=lambda item: episode_order(item.id))


def listing_urls(platform: str, channel_url: str) -> list[str]:
    return {
        "kick": [f"{channel_url}/videos"],
        "twitch": [f"{channel_url}/videos?filter=archives&sort=time"],  # past broadcasts
        "youtube": [f"{channel_url}/streams", f"{channel_url}/videos"],  # past streams and uploads
    }[platform]


def list_recent(platform: str, channel_url: str, limit: int | None = RECENT) -> list[Listed]:
    """The channel's newest `limit` videos (None: all of them), without downloading anything."""
    found: list[Listed] = []
    for listing in listing_urls(platform, channel_url):
        opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "playlistend": limit,
                # YouTube's lists only say "3 days ago"; this turns that into a date.
                "extractor_args": {"youtubetab": {"approximate_date": [""]}}}
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
                published=published(entry),
            ))
    return found


def published(entry: dict) -> float | None:
    """When a listed video was posted or streamed, as a unix time. Twitch's list doesn't say, but a past stream's
    preview image is stored under a name ending in when the stream started."""
    if when := entry.get("timestamp") or entry.get("release_timestamp"):
        return float(when)
    if started := TWITCH_STARTED.search(entry.get("thumbnail") or ""):
        return float(started[1])
    return None


def list_rumble(cdp_url: str, channel_url: str, settled: set[str] = frozenset(), others: set[str] | None = None,
                skipped: list[tuple[str, str]] | None = None) -> list[Listed]:
    """The channel's newest RUMBLE_RECENT videos (rumble.py), each read on its own for whether it's live, how
    long it is and whose it is, except those already `settled` (queued, skipped or older), which the monitor has
    no more use for. Older videos further down the page are never read: they were there before.

    Every Rumble page also links other channels' live streams, so only videos Rumble says are this channel's count.
    The ids of other channels' videos go in `others`, so a later check doesn't read them again, and their titles
    and owners in `skipped` (for `clipper channel-check`).
    """
    others = set() if others is None else others
    found: list[Listed] = []
    problems: list[RumbleError] = []
    links = rumble.channel_videos(cdp_url, channel_url)
    with YoutubeDL({"quiet": True, "no_warnings": True, **rumble.ytdlp_options(channel_url)}) as ydl:
        reads = own = 0
        for url in links:
            vid = rumble.VIDEO_PAGE.search(url)[1]
            if vid in others:
                continue
            if own >= RUMBLE_RECENT or reads >= RUMBLE_MAX_READS:
                break
            own += 1
            # A settled video still takes its place among the newest few. Otherwise each check would reach one
            # further back, and the channel's past streams would come up as new, five at a time.
            if vid in settled:
                continue
            reads += 1
            try:
                video = ydl.extract_info(rumble.player_url(url, ydl), download=False)
            except DownloadError as e:
                if "429" in str(e):
                    raise RumbleError(rumble.refusal(429)) from e
                log.info("Couldn't read %s: %s", url, e)
                continue
            except RumbleError as e:
                if any(code in str(e) for code in ("(429", "(403")):
                    raise
                log.info("Couldn't read %s: %s", url, e)
                problems.append(e)
                continue
            if not rumble.owner_is(video, channel_url):
                own -= 1
                others.add(vid)
                if skipped is not None:
                    owner = video.get("channel_url") or video.get("channel") or video.get("uploader") or "nobody said"
                    skipped.append((video.get("title") or url, owner))
                continue
            found.append(Listed(
                id=vid, url=url, title=video.get("title") or "",
                duration=float(video["duration"]) if video.get("duration") else None, live=is_live(video),
                published=published(video),
            ))
    if problems and not found and not (skipped if skipped is not None else others):
        raise problems[0]  # nothing could be read: say why, rather than look like an empty channel
    return found


def is_live(entry: dict) -> bool:
    return (
        bool(entry.get("is_live"))
        or entry.get("live_status") in ("is_live", "is_upcoming", "post_live")
        or "/404_processing_" in (entry.get("thumbnail") or "")  # Twitch: a VOD still recording
    )


def skip_reason(channel: Channel, item: Listed) -> str:
    """Why the channel's filters leave this video out, or "" to process it.

    Keywords are comma-separated, and any one of them will do; one made of parts joined with "+" needs
    all its parts, e.g. "tonight on + night owls". Case and repeated spaces don't matter.
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


def backlog_order(channel: Channel, rows: list[ChannelVideo]) -> list[ChannelVideo]:
    """A channel's videos in the order its backlog works through them: a folder's in season and episode order, a
    channel's oldest first, by when they were posted, or as the channel listed them where it doesn't say; the other
    way round when it goes newest first."""
    if channel.platform == "folder":
        return sorted(rows, key=lambda row: episode_order(row.video_id), reverse=channel.backlog_newest_first)
    # The channel lists newest first, so a later row is an older video.
    return sorted(rows, key=lambda row: (row.published_at or EPOCH, -(row.id or 0)), reverse=channel.backlog_newest_first)


def older_reason(channel: Channel) -> str:
    return f"Already {'in the folder' if channel.platform == 'folder' else 'on the channel'} when you added it"


class Monitor:
    def __init__(self, settings: Settings, lister: Callable[[str, str], list[Listed]] | None = None,
                 history: Callable[[str, str], list[Listed]] | None = None):
        """`lister` lists a channel's newest videos and `history` all of them (HISTORY_PLATFORMS only)."""
        self.settings = settings
        self.engine = get_engine(settings)
        self.lister = lister or self._list
        self.history = history or (lambda platform, url: list_recent(platform, url, limit=None))
        self._rumble_others: dict[str, set[str]] = {}  # other channels' streams seen on each Rumble channel's page

    def _list(self, platform: str, channel_url: str) -> list[Listed]:
        if platform == "folder":
            return list_folder(channel_url, self.settings.media_dir)
        if platform == "rumble":
            with Session(self.engine) as s:
                settled = set(s.exec(
                    select(ChannelVideo.video_id).join(Channel)
                    .where(Channel.url == channel_url, ChannelVideo.status != "waiting")
                ))
            return list_rumble(self.settings.studio_cdp_url, channel_url, settled,
                               self._rumble_others.setdefault(channel_url, set()))
        if platform != "x":
            return list_recent(platform, channel_url)
        posts = xposts.video_posts(self.settings.studio_cdp_url, channel_url.rsplit("/", 1)[-1])
        return [Listed(id=p.id, url=p.url, title=p.text[:1000], duration=p.duration, live=False, final=True,
                       published=p.posted) for p in posts]

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
            wants_history = channel.backlog and not channel.history_listed and platform in HISTORY_PLATFORMS
        try:
            listed = self.lister(platform, url)
            history = self.history(platform, url) if wants_history else []
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
                when = datetime.fromtimestamp(item.published, timezone.utc) if item.published else None
                row = s.exec(
                    select(ChannelVideo).where(ChannelVideo.channel_id == channel_id, ChannelVideo.video_id == item.id)
                ).first()
                if row is None:
                    # A stream that's live when the channel is added still counts as new: its VOD isn't done yet.
                    if not (channel.baseline_done or item.live or (not item.duration and not item.final)):
                        s.add(ChannelVideo(
                            channel_id=channel_id, video_id=item.id, url=item.url, title=item.title, published_at=when,
                            duration=item.duration, status="older", reason=older_reason(channel),
                        ))
                        continue
                    row = ChannelVideo(channel_id=channel_id, video_id=item.id, url=item.url)
                elif row.status != "waiting":
                    if when and not row.published_at:  # listed before dates were kept
                        row.published_at = when
                        s.add(row)
                    continue
                finished = not item.live and (item.final or bool(item.duration and row.duration == item.duration))
                row.title, row.duration, row.updated_at = item.title, item.duration, utcnow()
                row.published_at = when or row.published_at
                if not finished:
                    row.status, row.reason = "waiting", _waiting_reason(channel, item)
                elif reason := skip_reason(channel, item):
                    row.status, row.reason = "skipped", reason
                else:
                    row.vod_id = queue_vod(s, row.url, show=show).id
                    row.status, row.reason = "queued", ""
                    queued += 1
                    new_titles.append((row.vod_id, row.title or row.url))
                s.add(row)
            if wants_history:
                self._add_history(s, channel, history)
            if channel.backlog and not channel.backlog_paused and channel.backlog_per_day > 0:
                for row in self._work_through(s, channel, show, listed):
                    queued += 1
                    new_titles.append((row.vod_id, row.title or row.url))
            s.commit()
            show_name = show.name
        if queued:
            log.info("Queued %d new video(s) from %s", queued, url)
        for vod_id, title in new_titles:
            notify.send(self.settings, Note(f"New for {show_name}", title[:300], link=f"videos/{vod_id}", tone="quiet"))
        self._finish(channel_id, last_error="", baseline_done=True)

    @staticmethod
    def _add_history(s: Session, channel: Channel, history: list[Listed]) -> None:
        """The channel's past videos that weren't among its newest, for its backlog."""
        seen = set(s.exec(select(ChannelVideo.video_id).where(ChannelVideo.channel_id == channel.id)))
        for item in history:
            if item.id in seen or item.live:
                continue
            seen.add(item.id)
            when = datetime.fromtimestamp(item.published, timezone.utc) if item.published else None
            s.add(ChannelVideo(
                channel_id=channel.id, video_id=item.id, url=item.url, title=item.title, published_at=when,
                duration=item.duration, status="older", reason=older_reason(channel),
            ))
        channel.history_listed = True
        s.add(channel)

    @staticmethod
    def _work_through(s: Session, channel: Channel, show: Show, listed: list[Listed]) -> list[ChannelVideo]:
        """Queue the next of the backlog's older videos, so that it queues at most channel.backlog_per_day in 24
        hours. A folder's file that's gone, or is being copied in again, is left for later."""
        recent = s.exec(select(func.count()).select_from(ChannelVideo).where(
            ChannelVideo.channel_id == channel.id, col(ChannelVideo.backlog_at) >= utcnow() - timedelta(days=1),
        )).one()
        if recent >= channel.backlog_per_day:
            return []
        older = list(s.exec(select(ChannelVideo).where(ChannelVideo.channel_id == channel.id, ChannelVideo.status == "older")))
        if channel.platform == "folder":
            there = {item.id for item in listed if not item.live}
            older = [row for row in older if row.video_id in there]
        rows = backlog_order(channel, older)[:channel.backlog_per_day - recent]
        for row in rows:
            row.vod_id = queue_vod(s, row.url, show=show).id
            row.status, row.reason = "queued", ""
            row.updated_at = row.backlog_at = utcnow()
            s.add(row)
        return rows

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


def _waiting_reason(channel: Channel, item: Listed) -> str:
    if channel.platform == "folder":
        return "Still being copied in"
    return "Still live" if item.live or not item.duration else "Making sure the stream has ended"


def _unreadable(error: OSError) -> None:
    raise error  # a season that can't be read (its permissions) shouldn't look like it isn't there


def episode_order(path: str) -> tuple:
    """A file's path with its numbers compared as numbers: Season 2 before Season 10, S02E09 before S02E10."""
    return tuple(int(part) if i % 2 else part.casefold() for i, part in enumerate(re.split(r"(\d+)", path)))
