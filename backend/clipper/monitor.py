"""Watches channels on Kick, Twitch and YouTube and queues their new videos for processing.

A new video is queued once it's finished: not live, and the same length on two checks in a row,
because Twitch doesn't say whether a VOD is still recording. Videos that were already on a channel
when it was added are listed as "older" and only processed if you ask for them in the app, except a
stream that was live at the time, which is treated as new.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlparse

from sqlmodel import Session, col, or_, select
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, unsmuggle_url

from .config import Settings
from .db import Channel, ChannelVideo, Show, get_engine, queue_vod, utcnow

log = logging.getLogger(__name__)

RECENT = 15  # newest videos looked at on each listing


class ChannelUrlError(ValueError):
    pass


@dataclass
class Listed:
    id: str
    url: str
    title: str
    duration: float | None
    live: bool


def parse_channel(url: str) -> tuple[str, str]:
    """The platform and canonical link for a channel, e.g. ("twitch", "https://www.twitch.tv/shroud")."""
    url = url.strip()
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")
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
    raise ChannelUrlError("Use a channel link like kick.com/name, twitch.tv/name or youtube.com/@name")


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


def is_live(entry: dict) -> bool:
    return (
        bool(entry.get("is_live"))
        or entry.get("live_status") in ("is_live", "is_upcoming", "post_live")
        or "/404_processing_" in (entry.get("thumbnail") or "")  # Twitch: a VOD still recording
    )


def skip_reason(channel: Channel, item: Listed) -> str:
    """Why the channel's filters leave this video out, or "" to process it."""
    title = item.title.lower()
    keywords = [k.strip().lower() for k in channel.keywords.split(",") if k.strip()]
    exclude = [x.strip().lower() for x in channel.exclude.split(",") if x.strip()]
    if keywords and not any(k in title for k in keywords):
        return "The title has none of the keywords"
    if hit := next((x for x in exclude if x in title), None):
        return f'The title has "{hit}"'
    if channel.min_minutes and (item.duration or 0) < channel.min_minutes * 60:
        return f"Shorter than {channel.min_minutes:g} minutes"
    return ""


class Monitor:
    def __init__(self, settings: Settings, lister: Callable[[str, str], list[Listed]] = list_recent):
        self.settings = settings
        self.engine = get_engine(settings)
        self.lister = lister

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
            platform, url = channel.platform, channel.url
        try:
            listed = self.lister(platform, url)
        except Exception as e:
            log.warning("Checking %s failed: %s", url, e)
            self._finish(channel_id, last_error=f"{type(e).__name__}: {e}"[:500])
            return

        with Session(self.engine) as s:
            channel = s.get(Channel, channel_id)
            show = s.get(Show, channel.show_id)
            queued = 0
            for item in listed:
                row = s.exec(
                    select(ChannelVideo).where(ChannelVideo.channel_id == channel_id, ChannelVideo.video_id == item.id)
                ).first()
                if row is None:
                    # A stream that's live when the channel is added still counts as new: its VOD isn't done yet.
                    if channel.baseline_done or item.live or not item.duration:
                        s.add(ChannelVideo(
                            channel_id=channel_id, video_id=item.id, url=item.url, title=item.title,
                            duration=item.duration, status="waiting", reason=_waiting_reason(item),
                        ))
                    else:
                        s.add(ChannelVideo(
                            channel_id=channel_id, video_id=item.id, url=item.url, title=item.title,
                            duration=item.duration, status="older", reason="Already on the channel when you added it",
                        ))
                    continue
                if row.status != "waiting":
                    continue
                row.title, row.updated_at = item.title, utcnow()
                if item.live or not item.duration or row.duration != item.duration:
                    row.duration, row.reason = item.duration, _waiting_reason(item)
                elif reason := skip_reason(channel, item):
                    row.status, row.reason = "skipped", reason
                else:
                    row.vod_id = queue_vod(s, row.url, show=show).id
                    row.status, row.reason = "queued", ""
                    queued += 1
                s.add(row)
            s.commit()
        if queued:
            log.info("Queued %d new video(s) from %s", queued, url)
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


def _waiting_reason(item: Listed) -> str:
    return "Still live" if item.live or not item.duration else "Making sure the stream has ended"
