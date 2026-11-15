"""SQLite database of shows, watched channels, videos and clips.

The web app and the worker share it; WAL mode lets both use it at once.
"""

import json
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic_core import PydanticUndefined
from sqlalchemy import Engine, event
from sqlmodel import Field, Session, SQLModel, col, create_engine, select

from .config import Settings
from .models import Box

# Video statuses, in pipeline order. "waiting" means it can't go on yet (the AI's usage limit, or not
# enough disk space); `error` says why, and it retries at retry_at.
VOD_ACTIVE = ("downloading", "preparing", "transcribing", "analysing", "segmenting")
VOD_STATUSES = ("queued", *VOD_ACTIVE, "waiting", "ready", "failed")

# Clip statuses: proposed -> approved -> rendering -> rendered -> uploaded, or rejected; failed renders can be retried.
# "extra" is a pick beyond the show's best few per stream: kept out of the inbox unless you add it.
CLIP_STATUSES = ("proposed", "extra", "approved", "rejected", "rendering", "rendered", "uploaded", "failed")
# A stream whose clips all have one of these needs its video files no more (see Worker.clean_up_next).
CLIP_DONE = ("uploaded", "rejected", "extra")

# What a video becomes: "segment" is chapters and highlights, "whole" is the full episode as one
# video, "both" is the full episode plus the chapters and highlights.
MODES = ("segment", "whole", "both")

# What happens to a show's new full episodes, chapters or highlights: wait in the inbox for review,
# render without review, or render and upload without review.
AUTO_MODES = ("review", "render", "upload")

# Parts of the work that can be paused from the app. What's running stops and carries on when resumed.
PAUSABLE = ("downloads", "processing", "renders", "uploads")

# Posts on these sites are short and get uploaded as they are, not split into clips.
WHOLE_VIDEO_HOSTS = ("x.com", "twitter.com")

# The order a backlog takes its videos in (monitor.backlog_order): from a folder's first episode or a channel's oldest
# video, from the latest, or at random.
BACKLOG_ORDERS = ("first", "latest", "random")

# What a show's videos are, for the instructions the AI picks clips with (segment.py): livestreams, episodes of a TV
# show, or anything else (podcasts, vlogs, lectures).
VIDEO_TYPES = ("streams", "episodes", "other")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Destination(SQLModel, table=True):
    """A YouTube channel that videos are uploaded to."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    url: str = ""  # the channel's YouTube link
    channel_id: str = ""  # UC…, looked up from the link; picks the channel in YouTube Studio
    # Connected to the YouTube API (youtube.py): uploads to it go through the API instead of Studio.
    youtube_token: str = ""  # Google refresh token for the channel
    youtube_account: str = ""  # the channel's name as Google reported it when connecting
    youtube_error: str = ""  # why connecting failed, or why the sign-in stopped working
    # Where its uploads go: YouTube, and/or Rumble (rumble_upload.py), to the channel of the Rumble account the
    # upload browser is signed in to, under rumble_category. At least one of the two.
    youtube: bool = True
    rumble: bool = False
    rumble_category: str = "Entertainment"
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def sites(self) -> list[str]:
        """Where its uploads go, as UploadJob.site values."""
        return [site for site in ("youtube", "rumble") if getattr(self, site)]


class UploadJob(SQLModel, table=True):
    """One file of a clip uploaded by the worker, through the YouTube API or YouTube Studio."""

    __tablename__ = "upload_job"

    id: int | None = Field(default=None, primary_key=True)
    clip_id: int = Field(foreign_key="clip.id", index=True)
    which: str = "video"  # "video" or "short"
    site: str = "youtube"  # "youtube" or "rumble"
    visibility: str = "public"  # public, unlisted, private
    status: str = Field(default="queued", index=True)  # queued, uploading, done, failed
    method: str = ""  # "api", "studio" or (on Rumble) "browser", set when it starts
    progress: float = 0
    progress_at: datetime | None = None  # when progress last moved, so a stuck upload can be told apart (cli deploy-wait)
    cancel_requested: bool = False  # cancelled in the app while uploading: the worker stops it at its next progress check
    youtube_url: str = ""  # the video's link once it's up, on whichever site it went to
    error: str = ""  # why it failed; on a finished upload, a note (e.g. YouTube changed the visibility)
    screenshot: str = ""  # of Studio when it failed
    # The API's resumable upload session, so a retry carries on from the bytes YouTube already has.
    # session_key fingerprints what was sent; anything changed since means a fresh upload.
    session_url: str = ""
    session_key: str = ""
    rush: bool = False  # "Upload now": doesn't wait for its turn (schedule.py)
    # A failure that could clear up by itself (a daily limit, YouTube having trouble) is queued again at 12:01 AM,
    # up to Worker.MAX_AUTO_RETRIES times.
    retry_at: datetime | None = None
    auto_retries: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # How it's doing on YouTube (stats.py): the latest counts, and the views a day and a week after it went up.
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    views_day1: int | None = None
    views_week1: int | None = None
    stats_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


def parse_box(text: str) -> Box | None:
    """A part of the picture saved as JSON [x, y, w, h]; None for "" or anything that isn't one."""
    try:
        return _box(json.loads(text))
    except ValueError:
        return None


def parse_boxes(text: str) -> list[Box]:
    """Parts of the picture saved as JSON [[x, y, w, h], ...]; [] for "", leaving out anything that isn't one."""
    try:
        items = json.loads(text)
    except ValueError:
        return []
    return [box for item in items if (box := _box(item))] if isinstance(items, list) else []


def _box(value: object) -> Box | None:
    try:
        x, y, w, h = (float(v) for v in value)  # type: ignore[union-attr]
    except (ValueError, TypeError):
        return None
    return (x, y, w, h) if w > 0 and h > 0 else None


class Show(SQLModel, table=True):
    """A show's settings, applied to every video that matches it."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    # Links or channel names, one per line. A video belongs to the show if its link contains one of
    # them, or its channel or account is one of them.
    match: str = ""
    mode: str = "segment"  # see MODES
    video_type: str = "streams"  # see VIDEO_TYPES
    title_template: str = "{title}"  # clips, e.g. "NIGHT OWLS {date} - {title}"
    episode_template: str = ""  # full episodes; empty means the clip pattern
    notes: str = ""  # for Claude: about the show, who's on it, running jokes
    clip_guidance: str = ""  # for Claude: what the chapters and highlights should be
    clip_destination_id: int | None = Field(default=None, foreign_key="destination.id")
    episode_destination_id: int | None = Field(default=None, foreign_key="destination.id")
    # YouTube playlists (PL… ids) on those channels that uploads are added to; empty means none.
    clip_playlist_id: str = ""
    episode_playlist_id: str = ""
    # Where the uploads to those channels go: "youtube", "rumble" or "both" (see SITES); "" goes by the channel's own
    # setting (Destination.youtube and .rumble).
    clip_sites: str = ""
    episode_sites: str = ""
    # How many of each stream's chapters and highlights are offered, best-scored first; the rest are extras.
    max_chapters: int = 5
    max_shorts: int = 5
    # See AUTO_MODES; automatic uploads get auto_visibility.
    auto_episode: str = "review"
    auto_chapters: str = "review"
    auto_shorts: str = "review"
    # Offered chapters and highlights Claude scores at least this (0-1) are uploaded without review, whatever
    # auto_chapters and auto_shorts say; 0 is off.
    auto_upload_score: float = 0
    # What you approve yourself (from the inbox or the extras, or a clip you made) is uploaded once it's rendered, as
    # if its kind were set to upload; off, it waits in Ready to upload for you to tap Upload.
    upload_approved: bool = False
    auto_visibility: str = "public"
    # The show's chapters and highlights go up at least this many hours after the last clip upload to the same
    # channel (schedule.py); 0 uploads each as soon as it's ready. Full episodes never wait.
    upload_gap_hours: float = 0
    # Chapters (clips, in the app) and full episodes get a thumbnail made (thumbnails.py) once they're rendered.
    chapter_thumbnails: bool = False
    episode_thumbnails: bool = False
    thumbnail_guidance: str = ""  # for Claude: what every thumbnail of the show must have, e.g. words on it
    # Added under the description of everything the show uploads, such as the creators' links (youtube.with_footer).
    description_footer: str = ""
    # For the AI: what you've noticed works for the show, which it puts first among what its past clips teach it
    # (learning.py).
    learning_notes: str = ""
    # Each stream's transcript is also saved as files of its own, for use outside the app (transcripts.py), with
    # what the stream's time goes on when `breakdown` has categories: one per line, "Name: what it covers". The AI
    # sorts the stream into them in the same request as the clips (segment.py).
    save_transcripts: bool = False
    breakdown: str = ""
    # Shorts: where the video sits on the show's background image (Settings.short_background), as fractions
    # of the Short: its top edge (None: centred) and its width.
    short_video_top: float | None = None
    short_video_width: float = 1.0
    # Shorts: "crop" fills them with the middle of the picture instead; "blur" puts the whole picture on the background,
    # as above; "split" puts the streamers' webcams across the top and the game under it (short_split); "" goes by
    # CLIPPER_SHORT_LAYOUT (short_crop).
    short_layout: str = ""
    # For "split": where the webcams are in the picture, marked in the app, in the order they go across the Short (JSON
    # [[x, y, w, h], ...], parse_boxes), and the part of it the game is in ("" for all of it, parse_box). Each is in
    # fractions of the picture.
    short_webcams: str = ""
    short_game: str = ""
    # The overlay on its landscape videos (Settings.show_overlay, overlay.py): where its design and, for a
    # frame, its screen are (JSON [x, y, w, h], found when it's uploaded), and whether full episodes get it
    # too, which means re-encoding the whole stream.
    overlay_box: str = ""
    overlay_screen: str = ""
    overlay_episodes: bool = False
    # Where a logo (not a frame) goes, moved in the app: its top-left as fractions of the picture (None: where
    # it was drawn), and its size compared with as drawn.
    overlay_x: float | None = None
    overlay_y: float | None = None
    overlay_scale: float = 1.0
    # Its intro (Settings.show_intro, pipeline/intro.py): how long it is, and which of its videos get it at the start.
    intro_seconds: float = 0
    intro_clips: bool = True  # clips and highlights
    intro_episodes: bool = True  # full episodes
    intro_shorts: bool = False  # viewers swipe away in the first second or two
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def full_episode_template(self) -> str:
        return self.episode_template or self.title_template

    def auto_for(self, kind: str) -> str:
        return {"whole": self.auto_episode, "chapter": self.auto_chapters, "highlight": self.auto_shorts}[kind]

    def auto_for_clip(self, kind: str, score: float) -> str:
        """auto_for, except a chapter or highlight scored at least auto_upload_score is uploaded."""
        if kind != "whole" and self.auto_upload_score and score >= self.auto_upload_score:
            return "upload"
        return self.auto_for(kind)

    def short_crop(self, default: str, background: bool) -> bool:
        """Whether its Shorts are cropped to fill the screen: as the show is set, or else as CLIPPER_SHORT_LAYOUT
        (`default`) is, unless the show has a `background` image for them."""
        if self.short_layout:
            return self.short_layout == "crop"
        return default == "crop" and not background

    def short_split(self) -> tuple[list[Box], Box | None] | None:
        """Where its Shorts take the webcams and the game from, when they're split; None when they aren't, or no webcam
        has been marked."""
        webcams = parse_boxes(self.short_webcams)
        if self.short_layout != "split" or not webcams:
            return None
        return webcams, parse_box(self.short_game)

    def terms(self) -> list[str]:
        return [t.strip().lower() for t in re.split(r"[\n,]+", self.match) if t.strip()]

    def breakdown_categories(self) -> dict[str, str]:
        """The breakdown's categories by name, with what each covers; none unless transcripts are saved."""
        if not self.save_transcripts:
            return {}
        categories = {}
        for line in self.breakdown.splitlines():
            name, _, covers = line.partition(":")
            if name.strip():
                categories[name.strip()] = covers.strip()
        return categories


class Vod(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    url: str
    mode: str = "segment"  # see MODES
    mode_auto: bool = True  # the mode wasn't picked by hand, so the show's mode applies
    show_id: int | None = Field(default=None, foreign_key="show.id")
    notes: str = ""  # passed to Claude: names, running jokes, title style
    workspace: str | None = None  # folder under data/vods, once downloaded
    title: str = ""
    channel: str = ""
    platform: str = ""
    upload_date: str = ""  # YYYYMMDD of the stream or post
    duration: float = 0
    status: str = Field(default="queued", index=True)
    progress: float = 0  # 0-1 within the current status
    error: str = ""  # why it failed, or why it's waiting
    retry_at: datetime | None = None
    paused: bool = False  # held in the app; carries on from its last finished step when resumed
    cleaned_at: datetime | None = None  # when its big files were deleted (see Worker.clean_up)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ClipRecord(SQLModel, table=True):
    __tablename__ = "clip"

    id: int | None = Field(default=None, primary_key=True)
    vod_id: int = Field(foreign_key="vod.id", index=True)
    key: str  # ch01, hl03, full
    kind: str  # chapter, highlight, whole
    start: float
    end: float
    title: str
    description: str = ""
    why: str = ""  # why the AI picked it
    score: float = 0
    vertical_ok: bool = False
    make_short: bool = False  # also render a vertical Short
    status: str = Field(default="proposed", index=True)
    paused: bool = False  # its render is held in the app
    error: str = ""
    video_path: str = ""
    short_path: str = ""
    destination_id: int | None = Field(default=None, foreign_key="destination.id")  # where it gets uploaded
    playlist_id: str = ""  # a playlist on that channel to add it to, through the YouTube API
    youtube_url: str = ""  # once uploaded
    rumble_url: str = ""  # once uploaded to Rumble too
    uploaded_at: datetime | None = None
    # A thumbnail for its landscape video (thumbnails.py), made by the worker: "" (none asked for), queued,
    # making, done or failed. thumbnail_hint is your idea for it; thumbnail_idea is what Claude came up with.
    # thumbnail_plan is the plan the one it has was drawn from (thumbnails.Idea as JSON: who's in it and the image
    # prompt), so it can be drawn again; thumbnail_redraw is the prompt to draw it again with, while that's queued.
    thumbnail_status: str = ""
    thumbnail_path: str = ""
    thumbnail_hint: str = ""
    thumbnail_idea: str = ""
    thumbnail_error: str = ""
    thumbnail_plan: str = ""
    thumbnail_redraw: str = ""
    # Uploads to make again once it's rendered again, after one went wrong on the site's side (a video YouTube
    # won't play): JSON [[which, site, visibility], ...]; "" for none.
    reupload: str = ""
    # How long the show's intro at the start of its rendered video is (0 for none): its description's chapter times
    # are moved on by this as it's uploaded (titles.shift_chapters).
    intro_s: float = 0
    # Parts taken out of the middle in the app's editor (cuts.py): JSON [[start, end], ...] in the stream's
    # seconds, inside start-end; "" for none. The video jumps over them.
    cuts: str = ""
    manual: bool = False  # made in the app rather than picked by the AI, so its score means nothing
    # Approved by you in the app, rather than taken without review: what Show.upload_approved uploads.
    approved_in_review: bool = False
    # The AI's pick as it was first offered, before any change in the app: where it started and ended, and the part of
    # the title it wrote (without the show's pattern). What the owner changed teaches the AI (learning.py). Unset for
    # clips made in the app, full episodes, and older clips whose pick couldn't be found.
    picked_start: float | None = None
    picked_end: float | None = None
    picked_title: str = ""
    # Offered as an extra (beyond the show's best few, by score) rather than in the inbox: approving it anyway says the
    # AI scored it too low (learning.py). Unset for clips made in the app, full episodes, and older clips until it's
    # worked out from their scores.
    was_extra: bool | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    reviewed_at: datetime | None = None


class Channel(SQLModel, table=True):
    """A channel on Kick, Twitch or YouTube, an account on X, or a folder of TV episodes on the server, that the worker
    checks for new videos (see monitor.py)."""

    id: int | None = Field(default=None, primary_key=True)
    show_id: int = Field(foreign_key="show.id", index=True)
    url: str  # canonical channel link, e.g. https://kick.com/name, or a folder's full path
    platform: str  # kick, twitch, youtube, x, folder
    keywords: str = ""  # only titles (on X, post text) with one of these, comma-separated; empty means every video
    exclude: str = ""  # skip titles with any of these
    min_minutes: float = 0
    enabled: bool = True
    baseline_done: bool = False  # the videos already there when the channel was added have been listed
    # In the Library: its older videos (a folder's episodes, or a channel's past streams) are worked through by
    # themselves, backlog_per_day of them a day, in backlog_order (monitor.py). Newer ones never wait for them.
    backlog: bool = False
    backlog_per_day: int = 2
    backlog_paused: bool = False
    backlog_order: str = "first"  # see BACKLOG_ORDERS
    # The channel's whole history has been listed for its backlog, not just its newest videos. A folder's always is.
    history_listed: bool = False
    last_checked_at: datetime | None = None
    last_error: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class ChannelVideo(SQLModel, table=True):
    """A video seen on a watched channel, and what was decided about it."""

    __tablename__ = "channel_video"

    id: int | None = Field(default=None, primary_key=True)
    channel_id: int = Field(foreign_key="channel.id", index=True)
    video_id: str
    url: str
    title: str = ""
    duration: float | None = None
    status: str = "waiting"  # waiting (live, being copied in, or not confirmed finished), queued, skipped, older
    reason: str = ""
    vod_id: int | None = Field(default=None, foreign_key="vod.id")
    published_at: datetime | None = None  # when it was posted or streamed, if the platform says (roughly, on YouTube)
    backlog_at: datetime | None = None  # when working through the channel's backlog queued it
    first_seen_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class AppSetting(SQLModel, table=True):
    """A setting changed from the app, taking the place of its CLIPPER_* default."""

    __tablename__ = "app_setting"

    key: str = Field(primary_key=True)
    value: str


# A show's choice of where uploads go, as UploadJob.site values.
SITES = {"youtube": ["youtube"], "rumble": ["rumble"], "both": ["youtube", "rumble"]}


def upload_sites(show: Show | None, kind: str, dest: Destination | None) -> list[str]:
    """Where a clip's uploads go: its show's choice for full episodes or for clips (by `kind`), or else its channel's
    own setting; YouTube when there's neither."""
    chosen = (show.episode_sites if kind == "whole" else show.clip_sites) if show else ""
    if chosen in SITES:
        return SITES[chosen]
    return dest.sites if dest else ["youtube"]


def sites_name(sites: list[str]) -> str:
    """["youtube", "rumble"] as "both", one of them as itself."""
    return "both" if len(sites) > 1 else sites[0]


def get_app_setting(s: Session, key: str) -> str | None:
    row = s.get(AppSetting, key)
    return row.value if row else None


def set_app_setting(s: Session, key: str, value: str) -> None:
    row = s.get(AppSetting, key) or AppSetting(key=key, value=value)
    row.value = value
    s.add(row)


def is_paused(s: Session, part: str) -> bool:
    """Whether one of PAUSABLE is paused in the app."""
    return get_app_setting(s, f"paused_{part}") == "1"


# A deploy holds new uploads until then (an ISO time), while it waits for one through the browser to finish
# (cli deploy-wait). It lapses by itself, so a deploy that stops partway can't leave uploads held.
DEPLOY_HOLD = "deploy_hold_until"


def deploy_held(s: Session, now: datetime | None = None) -> bool:
    until = get_app_setting(s, DEPLOY_HOLD)
    try:
        return bool(until) and datetime.fromisoformat(until) > (now or utcnow())
    except ValueError:
        return False


def default_mode(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.").removeprefix("mobile.")
    return "whole" if host in WHOLE_VIDEO_HOSTS else "segment"


def find_show(shows: Iterable[Show], url: str, names: Iterable[str | None] = ()) -> Show | None:
    """The first show with a term that appears in the link or names the channel.

    A channel link like "twitch.tv/nightowls" also matches a channel named "nightowls", since
    Twitch VOD links (twitch.tv/videos/123) don't contain the channel.
    """
    url = url.lower()
    names = {n.lower().lstrip("@") for n in names if n}

    def matches(term: str) -> bool:
        return term in url or term.rstrip("/").rsplit("/", 1)[-1].lstrip("@") in names

    return next((s for s in shows if any(matches(t) for t in s.terms())), None)


def queue_vod(s: Session, url: str, mode: str | None = None, notes: str = "", show: Show | None = None) -> Vod:
    """Queue a video for the worker, or return it if it's already queued or done.

    Without a mode it's automatic: the show's mode, else whole for X posts and clips for everything else.
    Without a show, the first show whose terms appear in the link is used.
    """
    existing = s.exec(select(Vod).where(Vod.url == url, Vod.status != "failed")).first()
    if existing:
        return existing
    show = show or find_show(s.exec(select(Show).order_by(col(Show.id))), url)
    vod = Vod(
        url=url,
        mode=mode or (show.mode if show else default_mode(url)),
        mode_auto=mode is None,
        show_id=show.id if show else None,
        notes=notes,
    )
    s.add(vod)
    s.flush()
    return vod


def get_engine(settings: Settings) -> Engine:
    return _engine(str(settings.db_path.resolve()))


@lru_cache
def _engine(db_path: str) -> Engine:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    SQLModel.metadata.create_all(engine)
    _rename_columns(engine)
    _add_missing_columns(engine)
    return engine


# Columns renamed since a database was made, by (table, old name, new name): renamed in place, keeping their values.
_RENAMED = [("show", "auto_thumbnails", "chapter_thumbnails"), ("channel", "older_per_day", "backlog_per_day")]
# Columns that start out from another of the row's when they're added, by (table, column): SQL for the value, and the
# column it's worked out from, which an older database may not have. One setting that became two keeps doing what it
# did, the folders watched before the Library are in it, and a backlog going newest first still does.
_FILLED = {
    ("show", "episode_thumbnails"): ('"chapter_thumbnails"', "chapter_thumbnails"),
    ("channel", "backlog"): ("platform = 'folder'", "platform"),
    ("channel", "backlog_order"): ("CASE WHEN backlog_newest_first THEN 'latest' ELSE 'first' END", "backlog_newest_first"),
}


def _rename_columns(engine: Engine) -> None:
    with engine.begin() as conn:
        for table, old, new in _RENAMED:
            have = {row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{table}")')}
            if old in have and new not in have:
                conn.exec_driver_sql(f'ALTER TABLE "{table}" RENAME COLUMN "{old}" TO "{new}"')


def _add_missing_columns(engine: Engine) -> None:
    """Add columns that newer code expects to a database an older version created. Only ever adds."""
    with engine.begin() as conn:
        for model in (Destination, Show, Vod, ClipRecord, Channel, ChannelVideo, UploadJob, AppSetting):
            table = model.__table__
            have = {row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{table.name}")')}
            for column in table.columns:
                if column.name in have:
                    continue
                default = model.model_fields[column.name].default
                default_sql = "" if default is None or default is PydanticUndefined else f" DEFAULT {_sql_literal(default)}"
                conn.exec_driver_sql(
                    f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column.type.compile(engine.dialect)}{default_sql}'
                )
                have.add(column.name)
                value, source = _FILLED.get((table.name, column.name), ("", ""))
                if source in have:
                    conn.exec_driver_sql(f'UPDATE "{table.name}" SET "{column.name}" = {value}')


def _sql_literal(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"
