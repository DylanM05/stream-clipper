"""JSON API for the review app, plus media streaming and the built phone app."""

import asyncio
import gzip
import html
import ipaddress
import json
import logging
import mimetypes
import os
import re
import shutil
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import lru_cache, partial
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlmodel import Session, col, select
from starlette.concurrency import run_in_threadpool

from .. import ai, cuts, ffmpeg, learning, notify, stats, titles
from ..config import Settings, get_settings
from ..db import (
    PAUSABLE, VOD_ACTIVE, Channel, ChannelVideo, ClipRecord, Destination, Show, UploadJob, Vod,
    get_app_setting, get_engine, is_paused, queue_vod, set_app_setting, sites_name, upload_sites, utcnow,
)
from ..monitor import (
    VIDEO_SUFFIXES, ChannelUrlError, backlog_order, episode_order, list_folder, older_reason, parse_channel, parse_folder,
)
from ..overlay import Overlay, save as save_overlay_image
from ..schedule import Turn, upload_turns
from ..studio import Studio, launch_desktop_browser, read_status, resolve_channel_id, write_status
from ..pipeline import audio, render
from ..rumble_upload import CATEGORIES as RUMBLE_CATEGORIES, LICENCE as RUMBLE_LICENCE
from ..workspace import Workspace, remove
from ..youtube import CALLBACK_PATH, Google, YouTubeError, sign_in_url, video_id
from .auth import COOKIE, MAX_AGE_S, Auth

log = logging.getLogger(__name__)

mimetypes.add_type("application/manifest+json", ".webmanifest")

MAX_SHORT_S = 180  # YouTube's limit for Shorts
MAX_BACKGROUND_BYTES = 30_000_000
REPO_WEB_DIR = Path(__file__).resolve().parents[3] / "frontend" / "dist"
# Google's sign-in returns to the callback in whatever browser it ran in, which on an iPhone isn't the
# home-screen app and has no login cookie. The signed `state` it carries stands in for the login there.
PUBLIC_PATHS = {"/api/session", "/api/login", CALLBACK_PATH}
SIGN_IN_MAX_AGE_S = 3600
Mode = Literal["segment", "whole", "both"]
VideoType = Literal["streams", "episodes", "other"]  # db.VIDEO_TYPES
BacklogOrder = Literal["first", "latest", "random"]  # db.BACKLOG_ORDERS
Auto = Literal["review", "render", "upload"]
Visibility = Literal["public", "unlisted", "private"]
Sites = Literal["", "youtube", "rumble", "both"]  # "" goes by the channel's own setting
ACTIVE_CLIP = ("approved", "rendering", "rendered", "failed")  # statuses with a render queued, running or done


# --- API shapes ----------------------------------------------------------------


class VodOut(BaseModel):
    id: int
    url: str
    mode: str
    notes: str
    show_id: int | None
    show_name: str
    title: str
    channel: str
    platform: str
    upload_date: str
    duration: float
    status: str
    progress: float
    error: str
    retry_at: str | None
    paused: bool
    downloaded: bool  # so it only waits for processing
    cleaned: bool  # its video files were deleted to free space
    created_at: str
    counts: dict[str, int]  # clips per status


class ClipOut(BaseModel):
    id: int
    vod_id: int
    vod_title: str
    vod_duration: float
    key: str
    kind: str
    start: float
    end: float
    title: str
    description: str
    why: str
    score: float
    manual: bool  # made in the app, so it has no score from Claude
    cuts: list[tuple[float, float]]  # parts taken out of the middle, in the stream's seconds (cuts.py)
    vertical_ok: bool
    make_short: bool
    status: str
    paused: bool  # its render is held
    error: str
    has_video: bool
    has_short: bool
    video_size: int  # bytes, 0 until rendered
    short_size: int
    destination_id: int | None
    destination_name: str
    destination_url: str
    playlist_id: str
    upload_to: str  # where its uploads go unless one is sent elsewhere: "youtube", "rumble" or "both" (db.upload_sites)
    youtube_url: str
    rumble_url: str
    reuploading: bool  # rendering again, to be uploaded again once it's done (reupload)
    thumbnail_status: str  # "" (none), queued, making, done or failed
    thumbnail_url: str  # "" until there's one
    thumbnail_hint: str
    thumbnail_idea: str
    thumbnail_error: str
    thumbnail_prompt: str  # the image prompt the thumbnail it has was drawn from, "" if that wasn't kept
    thumbnail_redrawing: bool  # it's queued or being drawn again from a prompt, rather than a new idea from Claude
    # Where its uploads stand (upload_states): "", uploading, queued, scheduled, retrying or failed; and when
    # a scheduled one goes (it waits for its turn, schedule.py) or a failed one tries again by itself.
    upload_state: str
    next_upload: str | None
    updated_at: str


class ThumbnailRequest(BaseModel):
    hint: str = Field(default="", max_length=1000)  # your idea for it
    prompt: str = Field(default="", max_length=4000)  # draw again from this image prompt, without asking Claude


class VodDetail(BaseModel):
    vod: VodOut
    clips: list[ClipOut]


class Line(BaseModel):
    start: float
    end: float
    text: str


class NewClip(BaseModel):
    """A clip made in the app from any part of a stream."""

    kind: Literal["chapter", "highlight"]
    start: float
    end: float
    cuts: list[tuple[float, float]] = Field(default=[], max_length=500)
    title: str = Field(max_length=100)
    description: str = Field(default="", max_length=5000)
    make_short: bool = False  # highlights only
    destination_id: int | None = None
    playlist_id: str = Field(default="", max_length=64)
    approve: bool = False  # straight to rendering, rather than into the inbox


class NewClipInfo(BaseModel):
    title_pattern: str  # the show's pattern for clips, with the stream's date in it and {title} for the part you write
    title_room: int  # how long that part can be, for the whole title to fit YouTube's 100 characters
    destination_id: int | None  # the show's channel for clips
    playlist_id: str


class NewVod(BaseModel):
    url: str = Field(min_length=4, max_length=2000)
    mode: Mode | None = None  # picked from the show or the link when missing
    notes: str = Field(default="", max_length=5000)


class ShowIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    match: str = Field(default="", max_length=2000)
    mode: Mode = "segment"
    video_type: VideoType = "streams"
    title_template: str = Field(default="{title}", max_length=200)
    episode_template: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=10000)
    clip_guidance: str = Field(default="", max_length=10000)
    clip_destination_id: int | None = None
    episode_destination_id: int | None = None
    clip_playlist_id: str = Field(default="", max_length=64)
    episode_playlist_id: str = Field(default="", max_length=64)
    clip_sites: Sites = ""
    episode_sites: Sites = ""
    max_chapters: int = Field(default=5, ge=0, le=100)
    max_shorts: int = Field(default=5, ge=0, le=100)
    auto_episode: Auto = "review"
    auto_chapters: Auto = "review"
    auto_shorts: Auto = "review"
    auto_upload_score: float = Field(default=0, ge=0, le=1)
    auto_visibility: Visibility = "public"
    upload_gap_hours: float = Field(default=0, ge=0, le=168)
    chapter_thumbnails: bool = False
    episode_thumbnails: bool = False
    thumbnail_guidance: str = Field(default="", max_length=2000)
    learning_notes: str = Field(default="", max_length=4000)
    save_transcripts: bool = False
    breakdown: str = Field(default="", max_length=4000)
    short_video_top: float | None = Field(default=None, ge=0, le=0.9)
    short_video_width: float = Field(default=1.0, ge=0.4, le=1)
    short_layout: Literal["", "crop", "blur"] = ""  # "" goes by CLIPPER_SHORT_LAYOUT
    overlay_episodes: bool = False
    overlay_x: float | None = Field(default=None, ge=0, le=1)
    overlay_y: float | None = Field(default=None, ge=0, le=1)
    overlay_scale: float = Field(default=1.0, ge=0.1, le=5)


class ShowPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    match: str | None = Field(default=None, max_length=2000)
    mode: Mode | None = None
    video_type: VideoType | None = None
    title_template: str | None = Field(default=None, max_length=200)
    episode_template: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=10000)
    clip_guidance: str | None = Field(default=None, max_length=10000)
    clip_destination_id: int | None = None
    episode_destination_id: int | None = None
    clip_playlist_id: str | None = Field(default=None, max_length=64)
    episode_playlist_id: str | None = Field(default=None, max_length=64)
    clip_sites: Sites | None = None  # "" goes by the channel's own setting
    episode_sites: Sites | None = None
    max_chapters: int | None = Field(default=None, ge=0, le=100)
    max_shorts: int | None = Field(default=None, ge=0, le=100)
    auto_episode: Auto | None = None
    auto_chapters: Auto | None = None
    auto_shorts: Auto | None = None
    auto_upload_score: float | None = Field(default=None, ge=0, le=1)  # 0 is off
    auto_visibility: Visibility | None = None
    upload_gap_hours: float | None = Field(default=None, ge=0, le=168)  # 0 uploads clips as soon as they're ready
    chapter_thumbnails: bool | None = None
    episode_thumbnails: bool | None = None
    thumbnail_guidance: str | None = Field(default=None, max_length=2000)  # what every thumbnail must have
    learning_notes: str | None = Field(default=None, max_length=4000)  # what you've noticed works (learning.py)
    save_transcripts: bool | None = None  # also saved as files, for use outside the app (transcripts.py)
    breakdown: str | None = Field(default=None, max_length=4000)  # categories the AI sorts streams' time into
    short_video_top: float | None = Field(default=None, ge=0, le=0.9)  # null centres it
    short_video_width: float | None = Field(default=None, ge=0.4, le=1)
    short_layout: Literal["crop", "blur"] | None = None
    overlay_episodes: bool | None = None
    overlay_x: float | None = Field(default=None, ge=0, le=1)  # null puts the logo back where it was drawn
    overlay_y: float | None = Field(default=None, ge=0, le=1)
    overlay_scale: float | None = Field(default=None, ge=0.1, le=5)


class DestinationIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(default="", max_length=500)
    # Where its uploads go, one or both; each unchanged if left out.
    youtube: bool | None = None
    rumble: bool | None = None
    rumble_category: str | None = None  # one of rumble_upload.CATEGORIES


class DestinationOut(BaseModel):
    id: int
    name: str
    url: str
    channel_id: str  # empty if it couldn't be found from the link
    studio_upload_url: str  # where to upload by hand
    api_connected: bool  # uploads go through the YouTube API
    api_account: str  # the channel name Google reported when it was connected
    api_error: str  # why connecting failed, or why the sign-in stopped working
    youtube: bool  # its uploads go to YouTube
    rumble: bool  # and/or to Rumble, through the upload browser
    rumble_category: str


class ConnectRequest(BaseModel):
    origin: str = Field(max_length=200)  # the app's address in your browser, e.g. https://server.tailnet.ts.net


class PlaylistOut(BaseModel):
    id: str
    title: str
    privacy: str
    count: int


class NewPlaylist(BaseModel):
    title: str = Field(min_length=1, max_length=150)
    privacy: Visibility = "public"


class UploadRequest(BaseModel):
    which: Literal["video", "short"] = "video"
    visibility: Literal["public", "unlisted", "private"] = "public"
    site: Literal["youtube", "rumble"] = "youtube"


class UploadJobOut(BaseModel):
    id: int
    clip_id: int
    which: str
    site: str  # "youtube" or "rumble"
    visibility: str
    status: str
    method: str  # "api", "studio" or (Rumble) "browser"; empty until it starts
    progress: float
    youtube_url: str  # the link once it's up, on whichever site
    error: str
    has_screenshot: bool
    rush: bool  # sent with "Upload now"
    retry_at: str | None  # a failed upload that's tried again by itself then
    starts_at: str | None  # when it goes, if it's waiting for its turn
    wait_reason: str  # why: "gap" (the show spaces its uploads) or "thumbnail"
    views: int | None
    likes: int | None
    comments: int | None
    views_day1: int | None
    views_week1: int | None
    created_at: str
    finished_at: str | None


class StatsUpload(BaseModel):
    """One finished upload and how it's doing."""

    job_id: int
    clip_id: int
    show_id: int | None
    title: str
    kind: str
    which: str
    score: float
    manual: bool  # made in the app: no score from Claude
    youtube_url: str
    finished_at: str | None
    views: int | None
    likes: int | None
    comments: int | None
    views_day1: int | None
    views_week1: int | None


class StatsBand(BaseModel):
    label: str
    count: int
    day1: float | None
    week1: float | None
    views: float | None


class StatsOut(BaseModel):
    setup_needed: str  # why the numbers can't be read, or "" when they can
    checked_at: str | None  # the latest reading
    bands: list[StatsBand]
    uploads: list[StatsUpload]


class Uploaded(BaseModel):
    youtube_url: str = Field(default="", max_length=500)


class AppSettings(BaseModel):
    download_limit_mbps: float  # 0 = no limit
    full_speed_from: float  # hours of the day when downloads ignore the limit; the same for both is off
    full_speed_until: float
    cleanup_after_days: float  # delete a finished stream's big files this long after its last change
    timezone: str  # what the hours are in (CLIPPER_TIMEZONE)


class AppSettingsPatch(BaseModel):
    download_limit_mbps: float | None = Field(default=None, ge=0, le=100_000)
    full_speed_from: float | None = Field(default=None, ge=0, le=24)
    full_speed_until: float | None = Field(default=None, ge=0, le=24)
    cleanup_after_days: float | None = Field(default=None, ge=0, le=3650)


APP_SETTING_KEYS = ("download_limit_mbps", "full_speed_from", "full_speed_until", "cleanup_after_days")


class AiProvider(BaseModel):
    id: str  # "claude", "codex" or "openrouter" (ai.PROVIDERS)
    name: str
    model: str  # the model it uses; for Codex, "" is its own default
    default_model: str  # what it uses until another is set in the app (CLIPPER_LLM_MODEL and the like)


class AiSetup(BaseModel):
    """The AI that picks the clips and writes preview titles and thumbnail ideas (ai.py)."""

    provider: str
    providers: list[AiProvider]
    problem: str  # what stops the chosen one from working, such as a missing sign-in; "" if nothing does


class AiPatch(BaseModel):
    provider: Literal["claude", "codex", "openrouter"] | None = None
    model: str | None = Field(default=None, max_length=200)  # for the provider given, or else the chosen one; "" for its default


class Pauses(BaseModel):
    """Which parts of the work are paused (db.PAUSABLE)."""

    downloads: bool
    processing: bool  # preview copy, transcription and Claude's picks
    renders: bool
    uploads: bool


class PausesPatch(BaseModel):
    downloads: bool | None = None
    processing: bool | None = None
    renders: bool | None = None
    uploads: bool | None = None


class ChannelIn(BaseModel):
    url: str = Field(min_length=4, max_length=500)
    keywords: str = Field(default="", max_length=1000)
    exclude: str = Field(default="", max_length=1000)
    min_minutes: float = Field(default=0, ge=0, le=1440)
    # In the Library (Channel.backlog). For a channel the show already watches, this puts it in.
    backlog: bool = False
    backlog_per_day: int = Field(default=2, ge=1, le=100)
    backlog_order: BacklogOrder = "first"


class ChannelPatch(BaseModel):
    keywords: str | None = Field(default=None, max_length=1000)
    exclude: str | None = Field(default=None, max_length=1000)
    min_minutes: float | None = Field(default=None, ge=0, le=1440)
    enabled: bool | None = None
    show_id: int | None = None  # move it to another show; what it has already seen stays seen
    backlog: bool | None = None
    backlog_per_day: int | None = Field(default=None, ge=1, le=100)
    backlog_paused: bool | None = None
    backlog_order: BacklogOrder | None = None


class ChannelOut(BaseModel):
    id: int
    show_id: int
    url: str
    platform: str
    keywords: str
    exclude: str
    min_minutes: float
    enabled: bool
    last_checked_at: str | None
    last_error: str
    waiting: int  # new videos seen that aren't finished yet
    backlog: bool
    backlog_per_day: int
    backlog_paused: bool
    backlog_order: str
    history_listed: bool


class ShowOut(BaseModel):
    id: int
    name: str
    match: str
    mode: str
    video_type: str
    title_template: str
    episode_template: str
    notes: str
    clip_guidance: str
    clip_destination_id: int | None
    episode_destination_id: int | None
    clip_playlist_id: str
    episode_playlist_id: str
    clip_sites: str
    episode_sites: str
    max_chapters: int
    max_shorts: int
    auto_episode: str
    auto_chapters: str
    auto_shorts: str
    auto_upload_score: float
    auto_visibility: str
    upload_gap_hours: float
    chapter_thumbnails: bool
    episode_thumbnails: bool
    thumbnail_guidance: str
    learning_notes: str
    save_transcripts: bool
    breakdown: str
    short_video_top: float | None
    short_video_width: float
    short_layout: str  # "crop" or "blur", as its Shorts are made
    short_background: str  # the image's address in the app, or "" for none
    overlay: str  # the overlay image's address in the app, or "" for none
    overlay_box: list[int] | None  # x, y, w, h of its visible part, as drawn (in its 1920x1080)
    overlay_screen: list[int] | None  # x, y, w, h of a frame's screen, where clips go
    overlay_episodes: bool
    overlay_x: float | None
    overlay_y: float | None
    overlay_scale: float
    channels: list[ChannelOut]
    video_count: int


class ChannelVideoOut(BaseModel):
    id: int
    channel_id: int
    url: str
    title: str
    duration: float | None
    status: str
    reason: str
    vod_id: int | None
    published_at: str | None  # when it was posted or streamed, if the platform says (roughly, on YouTube)
    first_seen_at: str


class FolderOut(BaseModel):
    """A folder in the media folder, for choosing one to watch."""

    path: str
    parent: str | None  # None in the media folder itself
    folders: list[str]  # the folders in it, by name
    videos: int  # video files in it, not counting its folders'


class FolderVideosOut(BaseModel):
    """What watching a folder would find: its video files and its folders', in the order a backlog takes them."""

    videos: int
    first: list[str]  # the first few, by their path in the folder


class BacklogOut(BaseModel):
    """A channel or folder in the Library, and how far through its videos it is."""

    channel: ChannelOut
    show_name: str
    video_type: str
    name: str  # the folder's or channel's name
    total: int  # its videos, not counting skipped ones
    done: int  # processed: their clips are in the inbox, or past it
    working: int  # queued or being processed
    failed: int
    left: int  # older ones still to go
    skipped: int
    copying: int  # waiting: files being copied in, or streams not finished
    next: str  # the next one it takes, by title; "" when none are left
    today: int  # how many it took in the last 24 hours, of its backlog_per_day


class BacklogItemOut(BaseModel):
    id: int  # the ChannelVideo's
    title: str
    url: str
    group: str  # a folder's subfolder ("Season 1"), or the month a channel's video was posted
    state: Literal["done", "working", "failed", "left", "skipped", "copying"]
    reason: str
    published_at: str | None
    vod_id: int | None
    vod_status: str  # "" when it hasn't been queued
    vod_progress: float
    counts: dict[str, int]  # its clips by status
    place: int | None  # one still to go: its place in the line, 1 being next


class BacklogDetailOut(BacklogOut):
    # Every video, in the order it works through them; when that's at random, in season and episode order (or oldest
    # first), with each one's place in the line.
    items: list[BacklogItemOut]


class BacklogSkip(BaseModel):
    ids: list[int] = Field(max_length=5000)
    skip: bool  # false puts them back


class ClipPatch(BaseModel):
    title: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=5000)
    start: float | None = None
    end: float | None = None
    make_short: bool | None = None
    cuts: list[tuple[float, float]] | None = Field(default=None, max_length=500)  # parts taken out, in the stream's seconds
    destination_id: int | None = None
    playlist_id: str | None = Field(default=None, max_length=64)  # cleared when the channel changes, unless given


class Login(BaseModel):
    password: str


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def plan_prompt(plan: str) -> str:
    """The image prompt in a thumbnail's saved plan (ClipRecord.thumbnail_plan)."""
    try:
        return str(json.loads(plan).get("prompt") or "") if plan else ""
    except (ValueError, AttributeError):
        return ""


def studio_upload_url(channel_id: str) -> str:
    """YouTube Studio's upload page, straight on the right channel when its UC… id is known."""
    return f"https://studio.youtube.com/channel/{channel_id}/videos/upload?d=ud" if channel_id else "https://www.youtube.com/upload"


def google_accepts(origin: str) -> bool:
    """Whether Google sends its sign-in back to this address: https on a real domain, or localhost."""
    url = urlparse(origin)
    if url.hostname in ("localhost", "127.0.0.1"):
        return True
    try:
        ipaddress.ip_address(url.hostname or "")
        return False
    except ValueError:
        return url.scheme == "https" and "." in (url.hostname or "")


def sign_in_page(message: str, ok: bool) -> HTMLResponse:
    """What the browser shows after Google's sign-in, which may not be the app's window."""
    color = "#6ee7b7" if ok else "#fca5a5"
    return HTMLResponse(
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><title>YouTube sign-in</title></head>'
        '<body style="background:#09090b;color:#e4e4e7;font:16px system-ui,sans-serif;padding:24px;max-width:32rem;margin:auto">'
        f'<p style="color:{color}">{html.escape(message)}</p>'
        '<p><a href="/#/shows/youtube" style="color:#c4b5fd">Back to the app</a></p></body></html>',
        status_code=200 if ok else 400,
    )


def file_name(title: str) -> str:
    """A download name from the title. YouTube Studio pre-fills the title from the file name."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", title)
    return " ".join(name.split()).strip(" .") or "clip"


# A transcript line, light to keep: start, end, text, and its words as (start, end, text).
TranscriptLine = tuple[float, float, str, tuple[tuple[float, float, str], ...]]


@lru_cache(maxsize=2)
def _transcript_lines(path: str, _modified: float) -> tuple[TranscriptLine, ...]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return tuple(
        (round(seg["start"], 2), round(seg["end"], 2), seg["text"].strip(),
         tuple((round(w["start"], 2), round(w["end"], 2), w["text"].strip()) for w in seg.get("words") or () if w["text"].strip()))
        for seg in data["segments"] if seg["text"].strip()
    )


_transcript_lock = threading.Lock()


def transcript_lines(path: Path) -> tuple[TranscriptLine, ...]:
    """A stream's transcript lines, kept while the file stays the same: the editor asks for parts of a long one as it
    pans. Read as plain JSON, which is quicker and smaller than the full models, and only once at a time, so the
    editor's first requests don't each parse it."""
    with _transcript_lock:
        return _transcript_lines(str(path), path.stat().st_mtime)


# Stills made at once for the editor's timeline, when the worker's one-every-10-s pass hasn't got them: each run of
# ffmpeg reads the whole preview's index first (about 180 MB for 8 hours), so one at a time, and a minute's worth each.
FRAME_JOBS = 1


def packed(request: Request, data) -> Response:
    """JSON, compressed when the browser takes that and it's worth it: the editor's transcript and waveform add up
    on a slow connection."""
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    if len(body) > 2000 and "gzip" in request.headers.get("accept-encoding", ""):
        return Response(gzip.compress(body, 5), media_type="application/json",
                        headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
    return Response(body, media_type="application/json")


def _size(path: str) -> int:
    p = Path(path) if path else None
    return p.stat().st_size if p and p.exists() else 0


# --- App ---------------------------------------------------------------------------


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = get_engine(settings)
    auth = Auth(settings)
    app = FastAPI(title="Stream Clipper", docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    if not auth.required:
        log.warning("CLIPPER_APP_PASSWORD isn't set: anyone who can reach the app can use it")

    def db():
        with Session(engine) as s:
            yield s

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        if path.startswith(("/api/", "/media/")) and path not in PUBLIC_PATHS and not auth.valid(request.cookies.get(COOKIE)):
            return JSONResponse({"detail": "Not signed in"}, status_code=401)
        return await call_next(request)

    # --- Helpers ---

    def looked_up(read):
        """What `read` finds in the database, with the connection given back straight away. Media can take minutes to
        send (a preview streams for as long as it's watched): holding a connection all that time left none for the
        rest of the app once a few were streaming, and every request waited (30 s, then failed)."""
        with Session(engine) as s:
            return read(s)

    # Stills for the timeline and clips' pictures are made on a thread of their own, and the waveform read on another,
    # so a burst of either (zooming asks for dozens of stills) can't take the threads every other request needs.
    frame_pool = ThreadPoolExecutor(max_workers=FRAME_JOBS, thread_name_prefix="frames")
    frame_slots = asyncio.Semaphore(FRAME_JOBS)
    waveform_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="waveform")

    def workspace(vod: Vod) -> Workspace:
        if not vod.workspace:
            raise HTTPException(409, "This video hasn't been downloaded yet")
        return Workspace(settings.vods_dir / vod.workspace)

    def vod_out(s: Session, vod: Vod) -> VodOut:
        counts = dict(s.exec(
            select(ClipRecord.status, func.count()).where(ClipRecord.vod_id == vod.id).group_by(ClipRecord.status)
        ).all())
        show = s.get(Show, vod.show_id) if vod.show_id else None
        return VodOut(**vod.model_dump(exclude={"retry_at", "created_at", "updated_at", "workspace", "cleaned_at"}),
                      show_name=show.name if show else "",
                      retry_at=iso(vod.retry_at), downloaded=vod.workspace is not None,
                      cleaned=vod.cleaned_at is not None, created_at=iso(vod.created_at), counts=counts)

    def channel_out(s: Session, ch: Channel) -> ChannelOut:
        waiting = s.exec(
            select(func.count()).select_from(ChannelVideo)
            .where(ChannelVideo.channel_id == ch.id, ChannelVideo.status == "waiting")
        ).one()
        return ChannelOut(**ch.model_dump(exclude={"last_checked_at", "created_at", "baseline_done"}),
                          last_checked_at=iso(ch.last_checked_at), waiting=waiting)

    def show_out(s: Session, show: Show) -> ShowOut:
        channels = s.exec(select(Channel).where(Channel.show_id == show.id).order_by(col(Channel.id)))
        videos = s.exec(select(func.count()).select_from(Vod).where(Vod.show_id == show.id)).one()
        overlay = Overlay.load(settings.show_overlay(show.id), show.overlay_box, show.overlay_screen)
        background = settings.short_background(show.id)
        return ShowOut(**show.model_dump(exclude={"created_at", "overlay_box", "overlay_screen", "short_layout"}),
                       short_layout="crop" if show.short_crop(settings.short_layout, background.exists()) else "blur",
                       short_background=image_url(background, f"/media/shows/{show.id}/short-background.png"),
                       overlay=image_url(settings.show_overlay(show.id), f"/media/shows/{show.id}/overlay.png") if overlay else "",
                       overlay_box=list(overlay.box) if overlay else None,
                       overlay_screen=list(overlay.screen) if overlay and overlay.screen else None,
                       channels=[channel_out(s, c) for c in channels], video_count=videos)

    def image_url(path: Path, url: str) -> str:
        """A show's image's address, with the file's age in it so a new image isn't hidden by the browser's
        copy of the old one; "" if there's none."""
        return f"{url}?v={int(path.stat().st_mtime)}" if path.exists() else ""

    def channel_video_out(item: ChannelVideo) -> ChannelVideoOut:
        return ChannelVideoOut(**item.model_dump(exclude={"published_at", "first_seen_at", "updated_at"}),
                               published_at=iso(item.published_at), first_seen_at=iso(item.first_seen_at))

    def found(s: Session, model, id_: int, what: str):
        row = s.get(model, id_)
        if not row:
            raise HTTPException(404, f"No such {what}")
        return row

    def valid_template(template: str, optional: bool = False) -> str:
        template = " ".join(template.split())
        if not (optional and not template) and "{title}" not in template:
            raise HTTPException(422, "A title pattern needs {title} in it, for the part the AI writes")
        return template

    def remove_channel(s: Session, ch: Channel) -> None:
        for item in s.exec(select(ChannelVideo).where(ChannelVideo.channel_id == ch.id)):
            s.delete(item)
        s.flush()
        s.delete(ch)

    def clip_out(clip: ClipRecord, vod: Vod, upload: tuple[str, datetime | None] | None = None) -> ClipOut:
        upload_state, next_upload = upload or ("", None)
        video_size, short_size = _size(clip.video_path), _size(clip.short_path)
        with Session(engine) as s:
            dest = s.get(Destination, clip.destination_id) if clip.destination_id else None
            show = s.get(Show, vod.show_id) if vod.show_id else None
        thumbnail = Path(clip.thumbnail_path) if clip.thumbnail_path else None
        return ClipOut(
            **clip.model_dump(exclude={"video_path", "short_path", "thumbnail_path", "thumbnail_plan", "thumbnail_redraw",
                                       "reupload", "cuts", "created_at", "updated_at", "reviewed_at", "uploaded_at"}),
            cuts=cuts.parse(clip.cuts),
            reuploading=bool(clip.reupload),
            thumbnail_prompt=plan_prompt(clip.thumbnail_plan),
            thumbnail_redrawing=bool(clip.thumbnail_redraw) and clip.thumbnail_status in ("queued", "making"),
            vod_title=vod.title or vod.url,
            vod_duration=vod.duration,
            has_video=video_size > 0,
            has_short=short_size > 0,
            video_size=video_size,
            short_size=short_size,
            destination_name=dest.name if dest else "",
            destination_url=dest.url if dest else "",
            upload_to=sites_name(upload_sites(show, clip.kind, dest)),
            thumbnail_url=image_url(thumbnail, f"/media/clips/{clip.id}/thumbnail.jpg") if thumbnail else "",
            upload_state=upload_state,
            next_upload=iso(next_upload),
            updated_at=iso(clip.updated_at),
        )

    def upload_states(s: Session) -> dict[int, tuple[str, datetime | None]]:
        """Where each clip's uploads stand, by the latest of each of its files, with a time for some: "uploading",
        "queued" (goes once the upload running is done), "scheduled" (waits for its turn until then), "retrying"
        (failed, and tries again by itself then) or "failed". Clips with none of those aren't listed."""
        latest: dict[tuple[int, str, str], UploadJob] = {}
        for job in s.exec(select(UploadJob).order_by(col(UploadJob.id))):
            latest[job.clip_id, job.which, job.site] = job
        turns = upload_turns(s) if any(j.status == "queued" for j in latest.values()) else {}
        rank = ["uploading", "queued", "scheduled", "retrying", "failed"]  # what a clip with two files shows
        found: dict[int, tuple[str, datetime | None]] = {}
        for job in latest.values():
            if job.status in ("uploading", "failed"):
                state = (job.status, None) if job.status == "uploading" or not job.retry_at else ("retrying", job.retry_at)
            elif job.status == "queued":
                turn = turns.get(job.id)
                state = ("scheduled", turn.at) if turn and turn.reason else ("queued", None)
            else:
                continue
            have = found.get(job.clip_id)
            if not have or rank.index(state[0]) < rank.index(have[0]) or (
                    state[0] == have[0] and state[1] and have[1] and state[1] < have[1]):
                found[job.clip_id] = state
        return found

    def destination_out(d: Destination) -> DestinationOut:
        return DestinationOut(id=d.id, name=d.name, url=d.url, channel_id=d.channel_id,
                              studio_upload_url=studio_upload_url(d.channel_id),
                              api_connected=bool(d.youtube_token), api_account=d.youtube_account, api_error=d.youtube_error,
                              youtube=d.youtube, rumble=d.rumble, rumble_category=d.rumble_category)

    def set_sites(dest: Destination, body: DestinationIn) -> None:
        """Where the channel's uploads go, and Rumble's category for them."""
        if body.rumble_category is not None and body.rumble_category not in RUMBLE_CATEGORIES:
            raise HTTPException(422, f"Rumble's categories are {', '.join(RUMBLE_CATEGORIES)}")
        youtube = dest.youtube if body.youtube is None else body.youtube
        rumble = dest.rumble if body.rumble is None else body.rumble
        if not (youtube or rumble):
            raise HTTPException(422, "Its uploads have to go to YouTube, Rumble or both")
        dest.youtube, dest.rumble = youtube, rumble
        if body.rumble_category is not None:
            dest.rumble_category = body.rumble_category

    def job_out(job: UploadJob, turn: Turn | None = None) -> UploadJobOut:
        waits = turn is not None and bool(turn.reason)
        return UploadJobOut(**job.model_dump(exclude={"screenshot", "session_url", "session_key", "created_at", "updated_at",
                                                      "started_at", "finished_at", "stats_at", "retry_at"}),
                            has_screenshot=bool(job.screenshot) and Path(job.screenshot).exists(),
                            retry_at=iso(job.retry_at),
                            starts_at=iso(turn.at) if waits else None, wait_reason=turn.reason if waits else "",
                            created_at=iso(job.created_at), finished_at=iso(job.finished_at))

    def google() -> Google:
        if not (settings.google_client_id and settings.google_client_secret):
            raise HTTPException(409, "The YouTube API isn't set up yet: see Shows → YouTube channels")
        return Google(settings.google_client_id, settings.google_client_secret)

    def forget_sign_in(s: Session, dest: Destination) -> None:
        """Drop a channel's YouTube API sign-in. Revoking it at Google ends every sign-in of that account to
        the app, so it's skipped while another entry for the same channel is connected."""
        shared = s.exec(select(Destination).where(
            Destination.id != dest.id, Destination.channel_id == dest.channel_id, col(Destination.youtube_token) != "",
        )).first()
        if dest.youtube_token and not shared:
            Google(settings.google_client_id, settings.google_client_secret).revoke(dest.youtube_token)
        dest.youtube_token = dest.youtube_account = dest.youtube_error = ""

    def check_destinations(s: Session, *ids: int | None) -> None:
        for id_ in ids:
            if id_ is not None and not s.get(Destination, id_):
                raise HTTPException(422, "That YouTube channel doesn't exist anymore")

    def get_clip(s: Session, clip_id: int) -> tuple[ClipRecord, Vod]:
        clip = s.get(ClipRecord, clip_id)
        if not clip:
            raise HTTPException(404, "No such clip")
        return clip, s.get(Vod, clip.vod_id)

    def checked_cuts(
        kind: str, vod: Vod, start: float, end: float, cut_list: list[cuts.Cut], make_short: bool,
    ) -> tuple[float, float, list[cuts.Cut]]:
        """A clip's ends and the parts cut out of it, tidied (cuts.tidy) and checked."""
        start, end, cut_list = cuts.tidy(start, end, cut_list)
        if cut_list and kind == "whole":
            raise HTTPException(422, "A full episode goes up as the stream is, so parts can't be cut out of it")
        length = cuts.kept_length(start, end, cut_list)
        if start < 0 or end - start < 1 or length < 1 or (vod.duration and end > vod.duration + 0.5):
            raise HTTPException(422, "The clip must be at least 1 second long and inside the video")
        if make_short and length > MAX_SHORT_S:
            raise HTTPException(422, "Shorts can be at most 3 minutes long")
        return start, end, cut_list

    def set_status(s: Session, clip: ClipRecord, vod: Vod, status: str) -> ClipOut:
        clip.status = status
        clip.reviewed_at = clip.updated_at = utcnow()
        if status == "approved":
            clip.error = ""
        s.add(clip)
        s.commit()
        s.refresh(clip)
        return clip_out(clip, vod)

    def cancel_retries(s: Session, clip: ClipRecord) -> None:
        """Stop the clip's failed uploads from trying again by themselves."""
        for job in s.exec(select(UploadJob).where(UploadJob.clip_id == clip.id, col(UploadJob.retry_at).is_not(None))):
            job.retry_at = None
            s.add(job)

    def discard_render(s: Session, clip: ClipRecord, vod: Vod) -> None:
        """Take a clip out of the render and upload queues and delete its rendered files. A full episode's
        file is the stream itself and stays. A render that's running stops on its own (it sees the change)."""
        if s.exec(select(UploadJob).where(UploadJob.clip_id == clip.id, UploadJob.status == "uploading")).first():
            raise HTTPException(409, "It's uploading right now. Wait for that to finish.")
        for job in s.exec(select(UploadJob).where(UploadJob.clip_id == clip.id, UploadJob.status == "queued")):
            s.delete(job)
        cancel_retries(s, clip)
        clips_dir = (settings.vods_dir / vod.workspace / "clips").resolve() if vod.workspace else None
        for path in (clip.video_path, clip.short_path):
            if path and clips_dir and Path(path).parent.resolve() == clips_dir:
                Path(path).unlink(missing_ok=True)
        clip.video_path = clip.short_path = ""
        clip.paused = False
        clip.reupload = ""  # taken back: it isn't to go up again by itself

    # --- Session ---

    @app.get("/api/session")
    def session_info(request: Request) -> dict:
        return {"authenticated": auth.valid(request.cookies.get(COOKIE)), "password_required": auth.required}

    @app.post("/api/login", status_code=204)
    def login(body: Login, request: Request, response: Response) -> None:
        if not auth.check_password(body.password):
            time.sleep(1)  # slows down guessing
            raise HTTPException(401, "Wrong password")
        response.set_cookie(
            COOKIE, auth.new_token(), max_age=MAX_AGE_S, httponly=True, samesite="lax",
            secure=request.url.scheme == "https",
        )

    @app.post("/api/logout", status_code=204)
    def logout(response: Response) -> None:
        response.delete_cookie(COOKIE)

    @app.get("/api/summary")
    def summary(s: Session = Depends(db)) -> dict:
        to_review = s.exec(select(func.count()).select_from(ClipRecord).where(ClipRecord.status == "proposed")).one()
        to_upload = s.exec(select(func.count()).select_from(ClipRecord).where(ClipRecord.status == "rendered")).one()
        working = s.exec(
            select(func.count()).select_from(Vod).where(col(Vod.status).notin_(("ready", "failed")))
        ).one()
        return {"to_review": to_review, "to_upload": to_upload, "videos_in_progress": working}

    # --- Settings changed from the app ---

    def current_settings(s: Session) -> AppSettings:
        values = {}
        for key in APP_SETTING_KEYS:
            stored = get_app_setting(s, key)
            values[key] = float(stored) if stored is not None else float(getattr(settings, key))
        return AppSettings(**values, timezone=settings.timezone)

    @app.get("/api/settings")
    def get_settings_route(s: Session = Depends(db)) -> AppSettings:
        return current_settings(s)

    @app.patch("/api/settings")
    def update_settings(body: AppSettingsPatch, s: Session = Depends(db)) -> AppSettings:
        """A running download switches to a new speed limit (or full-speed hours) within a few seconds; cleanup
        uses its new days from its next round."""
        for key, value in body.model_dump(exclude_none=True).items():
            set_app_setting(s, key, str(value))
        s.commit()
        return current_settings(s)

    def ai_setup(s: Session) -> AiSetup:
        choice = ai.chosen(s, settings)
        providers = [
            AiProvider(id=p, name=name, model=get_app_setting(s, f"{p}_model") or ai.default_model(settings, p),
                       default_model=ai.default_model(settings, p))
            for p, name in ai.PROVIDERS.items()
        ]
        return AiSetup(provider=choice.provider, providers=providers, problem=ai.problem(settings, choice))

    @app.get("/api/ai")
    def get_ai(s: Session = Depends(db)) -> AiSetup:
        return ai_setup(s)

    @app.patch("/api/ai")
    def set_ai(body: AiPatch, s: Session = Depends(db)) -> AiSetup:
        """From the next piece of work on: what the AI is already working on finishes with the one it started with."""
        if body.provider:
            set_app_setting(s, ai.PROVIDER_KEY, body.provider)
        if body.model is not None:
            model = body.model.strip()
            if any(c.isspace() for c in model):
                raise HTTPException(422, "A model's name has no spaces in it")
            set_app_setting(s, f"{body.provider or ai.chosen(s, settings).provider}_model", model)
        s.commit()
        return ai_setup(s)

    def current_pauses(s: Session) -> Pauses:
        return Pauses(**{part: is_paused(s, part) for part in PAUSABLE})

    @app.get("/api/pauses")
    def get_pauses(s: Session = Depends(db)) -> Pauses:
        return current_pauses(s)

    @app.patch("/api/pauses")
    def set_pauses(body: PausesPatch, s: Session = Depends(db)) -> Pauses:
        """Work that's running stops within a few seconds (Claude's step finishes first) and carries on when resumed."""
        for part, value in body.model_dump(exclude_none=True).items():
            set_app_setting(s, f"paused_{part}", "1" if value else "0")
        s.commit()
        return current_pauses(s)

    @app.get("/api/notifications")
    def notification_setup() -> dict:
        return {"ntfy": bool(settings.ntfy_url), "discord": bool(settings.discord_webhook_url), "links": bool(settings.app_url)}

    @app.post("/api/notifications/test")
    def test_notification() -> dict:
        if not notify.configured(settings):
            raise HTTPException(409, "Notifications aren't set up: see CLIPPER_NTFY_URL and CLIPPER_DISCORD_WEBHOOK_URL in the README")
        errors = notify.deliver(settings, notify.Note("Test from Stream Clipper", "Notifications work.", link="videos"))
        if errors:
            raise HTTPException(502, "Couldn't send: " + "; ".join(errors))
        return {"sent": True}

    @app.get("/api/storage")
    def storage() -> dict:
        """Space on the data disk, and how much the streams' files take."""
        usage = shutil.disk_usage(settings.data_dir) if settings.data_dir.exists() else None
        used = sum(f.stat().st_size for f in settings.vods_dir.rglob("*") if f.is_file()) if settings.vods_dir.exists() else 0
        return {
            "free_gb": round(usage.free / 1e9, 1) if usage else None,
            "total_gb": round(usage.total / 1e9, 1) if usage else None,
            "videos_gb": round(used / 1e9, 1),
            "min_free_gb": settings.min_free_gb,
        }

    # --- Videos ---

    @app.get("/api/vods")
    def list_vods(s: Session = Depends(db)) -> list[VodOut]:
        return [vod_out(s, v) for v in s.exec(select(Vod).order_by(col(Vod.created_at).desc()).limit(200))]

    @app.post("/api/vods", status_code=201)
    def add_vod(body: NewVod, s: Session = Depends(db)) -> VodOut:
        url = body.url.strip()
        if not url.startswith(("http://", "https://")):
            raise HTTPException(422, "Enter a full link starting with https://")
        vod = queue_vod(s, url, mode=body.mode, notes=body.notes.strip())
        s.commit()
        s.refresh(vod)
        return vod_out(s, vod)

    @app.get("/api/vods/{vod_id}")
    def get_vod(vod_id: int, s: Session = Depends(db)) -> VodDetail:
        vod = s.get(Vod, vod_id)
        if not vod:
            raise HTTPException(404, "No such video")
        clips = s.exec(select(ClipRecord).where(ClipRecord.vod_id == vod_id).order_by(col(ClipRecord.kind), col(ClipRecord.start)))
        return VodDetail(vod=vod_out(s, vod), clips=[clip_out(c, vod) for c in clips])

    @app.post("/api/vods/{vod_id}/retry")
    def retry_vod(vod_id: int, s: Session = Depends(db)) -> VodOut:
        vod = s.get(Vod, vod_id)
        if not vod:
            raise HTTPException(404, "No such video")
        if vod.status not in ("failed", "waiting"):
            raise HTTPException(409, "Only failed or waiting videos can be retried")
        vod.status, vod.error, vod.retry_at, vod.updated_at = "queued", "", None, utcnow()
        s.add(vod)
        s.commit()
        s.refresh(vod)
        return vod_out(s, vod)

    @app.post("/api/vods/{vod_id}/pause")
    def pause_vod(vod_id: int, s: Session = Depends(db)) -> VodOut:
        """Hold a video: what it's doing stops within a few seconds, and it waits until resumed."""
        vod = found(s, Vod, vod_id, "video")
        if vod.status in ("ready", "failed"):
            raise HTTPException(409, "It isn't being processed")
        vod.paused, vod.updated_at = True, utcnow()
        s.add(vod)
        s.commit()
        s.refresh(vod)
        return vod_out(s, vod)

    @app.post("/api/vods/{vod_id}/resume")
    def resume_vod(vod_id: int, s: Session = Depends(db)) -> VodOut:
        vod = found(s, Vod, vod_id, "video")
        vod.paused, vod.updated_at = False, utcnow()
        s.add(vod)
        s.commit()
        s.refresh(vod)
        return vod_out(s, vod)

    @app.post("/api/vods/{vod_id}/delete-files")
    def delete_vod_files(vod_id: int, s: Session = Depends(db)) -> VodOut:
        """Delete a stream's video files now. Its clips that aren't uploaded can't be cut any more, so they're rejected."""
        vod = found(s, Vod, vod_id, "video")
        if vod.status not in ("ready", "failed") or not vod.workspace:
            raise HTTPException(409, "Only the files of a video that's done processing (or failed) can be deleted")
        clips = s.exec(select(ClipRecord).where(ClipRecord.vod_id == vod_id)).all()
        if any(c.status == "rendering" for c in clips):
            raise HTTPException(409, "A clip from it is rendering right now. Pause rendering first.")
        for clip in clips:
            if clip.status not in ("uploaded", "rejected"):
                discard_render(s, clip, vod)
                clip.status, clip.updated_at = "rejected", utcnow()
                s.add(clip)
        freed = Workspace(settings.vods_dir / vod.workspace).delete_media()
        vod.cleaned_at = vod.updated_at = utcnow()
        s.add(vod)
        s.commit()
        s.refresh(vod)
        log.info("Deleted the files of video %d (%s) from the app: freed %.1f GB", vod_id, vod.title, freed / 1e9)
        return vod_out(s, vod)

    @app.delete("/api/vods/{vod_id}", status_code=204)
    def delete_vod(vod_id: int, s: Session = Depends(db)) -> None:
        """Remove a video from the app, with its clips and files. A channel that's watched doesn't queue it again."""
        vod = found(s, Vod, vod_id, "video")
        if vod.status in VOD_ACTIVE:
            raise HTTPException(409, "It's being worked on right now. Pause it, then delete it once it has stopped.")
        clips = s.exec(select(ClipRecord).where(ClipRecord.vod_id == vod_id)).all()
        if any(c.status == "uploaded" for c in clips):
            raise HTTPException(409, "Some of its clips are on YouTube, so it stays for their history. "
                                     "Delete its video files instead to free the space.")
        if any(c.status == "rendering" or c.thumbnail_status == "making" for c in clips):
            raise HTTPException(409, "One of its clips is rendering or getting a thumbnail right now. Try again in a minute.")
        for clip in clips:
            discard_render(s, clip, vod)  # refuses while one is uploading
            for job in s.exec(select(UploadJob).where(UploadJob.clip_id == clip.id)):
                s.delete(job)
            s.delete(clip)
        for seen in s.exec(select(ChannelVideo).where(ChannelVideo.vod_id == vod_id)):
            seen.vod_id, seen.status, seen.reason, seen.updated_at = None, "skipped", "Deleted in the app", utcnow()
            s.add(seen)
        shared = vod.workspace and s.exec(select(Vod).where(Vod.workspace == vod.workspace, Vod.id != vod_id)).first()
        if vod.workspace and not shared:  # the same link added again after it failed has the same folder
            remove(settings.vods_dir / vod.workspace)
        s.delete(vod)
        s.commit()
        log.info("Deleted video %d (%s) from the app", vod_id, vod.title or vod.url)

    # --- Shows and the channels they watch ---

    @app.get("/api/shows")
    def list_shows(s: Session = Depends(db)) -> list[ShowOut]:
        return [show_out(s, show) for show in s.exec(select(Show).order_by(col(Show.name)))]

    @app.post("/api/shows", status_code=201)
    def add_show(body: ShowIn, s: Session = Depends(db)) -> ShowOut:
        check_destinations(s, body.clip_destination_id, body.episode_destination_id)
        show = Show(**body.model_dump())
        show.name, show.title_template = show.name.strip(), valid_template(show.title_template)
        show.episode_template = valid_template(show.episode_template, optional=True)
        s.add(show)
        s.commit()
        s.refresh(show)
        return show_out(s, show)

    @app.get("/api/shows/{show_id}")
    def get_show(show_id: int, s: Session = Depends(db)) -> ShowOut:
        return show_out(s, found(s, Show, show_id, "show"))

    @app.get("/api/shows/{show_id}/track-record")
    def show_track_record(show_id: int, s: Session = Depends(db)) -> dict:
        """What the show's past clips teach the AI, as it goes into its input for the next stream (learning.py)."""
        return {"text": learning.track_record(s, settings, found(s, Show, show_id, "show"))}

    @app.patch("/api/shows/{show_id}")
    def update_show(show_id: int, patch: ShowPatch, s: Session = Depends(db)) -> ShowOut:
        show = found(s, Show, show_id, "show")
        changes = patch.model_dump(exclude_unset=True)
        if "title_template" in changes:
            changes["title_template"] = valid_template(changes["title_template"] or "")
        if "episode_template" in changes:
            changes["episode_template"] = valid_template(changes["episode_template"] or "", optional=True)
        check_destinations(s, changes.get("clip_destination_id"), changes.get("episode_destination_id"))
        for kind in ("clip", "episode"):  # a playlist belongs to its channel
            field = f"{kind}_destination_id"
            if field in changes and changes[field] != getattr(show, field):
                changes.setdefault(f"{kind}_playlist_id", "")
        for k, v in changes.items():
            setattr(show, k, v.strip() if k == "name" else v)
        s.add(show)
        s.commit()
        s.refresh(show)
        return show_out(s, show)

    @app.delete("/api/shows/{show_id}", status_code=204)
    def delete_show(show_id: int, s: Session = Depends(db)) -> None:
        """Removes the show and its watched channels. Its videos and clips stay."""
        show = found(s, Show, show_id, "show")
        for ch in s.exec(select(Channel).where(Channel.show_id == show_id)):
            remove_channel(s, ch)
        for vod in s.exec(select(Vod).where(Vod.show_id == show_id)):
            vod.show_id = None
            s.add(vod)
        s.flush()
        s.delete(show)
        s.commit()
        settings.short_background(show_id).unlink(missing_ok=True)
        settings.show_overlay(show_id).unlink(missing_ok=True)

    @app.put("/api/shows/{show_id}/short-background")
    async def upload_short_background(show_id: int, request: Request) -> ShowOut:
        """The request's body is the image. It's scaled to fill a Short (1080x1920) and cropped to fit."""
        data = await request.body()
        if len(data) > MAX_BACKGROUND_BYTES:
            raise HTTPException(413, "That image is too big: 30 MB at most")
        return await run_in_threadpool(save_short_background, show_id, data)

    def save_short_background(show_id: int, data: bytes) -> ShowOut:
        with Session(engine) as s:
            show = found(s, Show, show_id, "show")
            image = settings.short_background(show_id)
            image.parent.mkdir(parents=True, exist_ok=True)
            upload, tmp = image.with_suffix(".upload"), image.with_suffix(".tmp.png")
            upload.write_bytes(data)
            size = f"{render.SHORT_W}:{render.SHORT_H}"
            try:
                ffmpeg.run(["-i", str(upload), "-vf", f"scale={size}:force_original_aspect_ratio=increase,crop={size}",
                            "-frames:v", "1", "-update", "1", str(tmp)])
            except ffmpeg.FFmpegError:
                raise HTTPException(422, "That file isn't an image the app can read. Use a PNG or JPG.") from None
            finally:
                upload.unlink(missing_ok=True)
            tmp.replace(image)
            return show_out(s, show)

    @app.delete("/api/shows/{show_id}/short-background")
    def delete_short_background(show_id: int, s: Session = Depends(db)) -> ShowOut:
        """Its Shorts go back to a blurred copy of the stream behind the video."""
        show = found(s, Show, show_id, "show")
        settings.short_background(show_id).unlink(missing_ok=True)
        return show_out(s, show)

    @app.put("/api/shows/{show_id}/overlay")
    async def upload_overlay(show_id: int, request: Request) -> ShowOut:
        """The request's body is the image: a PNG with transparent parts, saved at 1920x1080 (overlay.py)."""
        data = await request.body()
        if len(data) > MAX_BACKGROUND_BYTES:
            raise HTTPException(413, "That image is too big: 30 MB at most")
        return await run_in_threadpool(save_overlay, show_id, data)

    def save_overlay(show_id: int, data: bytes) -> ShowOut:
        with Session(engine) as s:
            show = found(s, Show, show_id, "show")
            image = settings.show_overlay(show_id)
            image.parent.mkdir(parents=True, exist_ok=True)
            upload = image.with_suffix(".upload")
            upload.write_bytes(data)
            try:
                saved = save_overlay_image(upload, image)
            except ValueError as e:
                raise HTTPException(422, str(e)) from None
            finally:
                upload.unlink(missing_ok=True)
            show.overlay_box = json.dumps(saved.box)
            show.overlay_screen = json.dumps(saved.screen) if saved.screen else ""
            show.overlay_x = show.overlay_y = None  # a new design starts where it was drawn
            show.overlay_scale = 1.0
            s.add(show)
            s.commit()
            s.refresh(show)
            return show_out(s, show)

    @app.delete("/api/shows/{show_id}/overlay")
    def delete_overlay(show_id: int, s: Session = Depends(db)) -> ShowOut:
        show = found(s, Show, show_id, "show")
        settings.show_overlay(show_id).unlink(missing_ok=True)
        show.overlay_box = show.overlay_screen = ""
        s.add(show)
        s.commit()
        s.refresh(show)
        return show_out(s, show)

    @app.get("/media/shows/{show_id}/overlay.png")
    def overlay_image(show_id: int) -> FileResponse:
        image = settings.show_overlay(show_id)
        if not image.exists():
            raise HTTPException(404, "No overlay")
        return FileResponse(image, media_type="image/png", headers={"Cache-Control": "max-age=31536000"})

    @app.get("/media/shows/{show_id}/short-background.png")
    def short_background_image(show_id: int) -> FileResponse:
        image = settings.short_background(show_id)
        if not image.exists():
            raise HTTPException(404, "No background")
        # Its address changes with the image (show_out), so it can be kept.
        return FileResponse(image, media_type="image/png", headers={"Cache-Control": "max-age=31536000"})

    # --- YouTube channels to upload to ---

    @app.get("/api/destinations")
    def list_destinations(s: Session = Depends(db)) -> list[DestinationOut]:
        return [destination_out(d) for d in s.exec(select(Destination).order_by(col(Destination.name)))]

    @app.get("/api/rumble")
    def rumble_setup() -> dict:
        """What Rumble uploads use: the primary categories to pick from, and the licence they go up under."""
        return {"categories": list(RUMBLE_CATEGORIES), "licence": RUMBLE_LICENCE}

    @app.post("/api/destinations", status_code=201)
    def add_destination(body: DestinationIn, s: Session = Depends(db)) -> DestinationOut:
        url = body.url.strip()
        dest = Destination(name=body.name.strip(), url=url, channel_id=resolve_channel_id(url) if url else "")
        set_sites(dest, body)
        s.add(dest)
        s.commit()
        s.refresh(dest)
        return destination_out(dest)

    @app.patch("/api/destinations/{dest_id}")
    def update_destination(dest_id: int, body: DestinationIn, s: Session = Depends(db)) -> DestinationOut:
        dest = found(s, Destination, dest_id, "YouTube channel")
        url = body.url.strip()
        if url != dest.url or not dest.channel_id:
            channel_id = resolve_channel_id(url) if url else ""
            if channel_id != dest.channel_id and dest.channel_id:
                forget_sign_in(s, dest)  # it's for the old channel
            dest.channel_id = channel_id
        dest.name, dest.url = body.name.strip(), url
        set_sites(dest, body)
        s.add(dest)
        s.commit()
        s.refresh(dest)
        return destination_out(dest)

    @app.delete("/api/destinations/{dest_id}", status_code=204)
    def delete_destination(dest_id: int, s: Session = Depends(db)) -> None:
        """Shows and clips that pointed at it are left without a channel to pick again."""
        dest = found(s, Destination, dest_id, "YouTube channel")
        for show in s.exec(select(Show)):
            if dest_id in (show.clip_destination_id, show.episode_destination_id):
                if show.clip_destination_id == dest_id:
                    show.clip_destination_id, show.clip_playlist_id = None, ""
                if show.episode_destination_id == dest_id:
                    show.episode_destination_id, show.episode_playlist_id = None, ""
                s.add(show)
        for clip in s.exec(select(ClipRecord).where(ClipRecord.destination_id == dest_id)):
            clip.destination_id, clip.playlist_id = None, ""
            s.add(clip)
        forget_sign_in(s, dest)
        s.flush()
        s.delete(dest)
        s.commit()

    # --- Uploading through the YouTube API (youtube.py) ---

    @app.get("/api/youtube")
    def youtube_setup() -> dict:
        """Whether the app has a Google OAuth client, and the path to register as its redirect URI."""
        return {"configured": bool(settings.google_client_id and settings.google_client_secret), "callback_path": CALLBACK_PATH}

    @app.post("/api/destinations/{dest_id}/connect")
    def connect_destination(dest_id: int, body: ConnectRequest, s: Session = Depends(db)) -> dict:
        """Google's sign-in page for this channel. It comes back to the callback below on the same address."""
        found(s, Destination, dest_id, "YouTube channel")
        client = google()
        origin = body.origin.rstrip("/")
        if not re.fullmatch(r"https?://[\w.:-]+", origin):
            raise HTTPException(422, "That isn't the app's address")
        if not google_accepts(origin):
            raise HTTPException(409, f"Google won't sign in to {urlparse(origin).netloc}: it needs an https address on a "
                                     "real domain. Open the app at its https://….ts.net address and connect from there.")
        redirect_uri = origin + CALLBACK_PATH
        state = auth.sign_data({"dest": dest_id, "redirect_uri": redirect_uri})
        return {"url": sign_in_url(client.client_id, redirect_uri, state)}

    @app.get(CALLBACK_PATH, include_in_schema=False)
    def youtube_callback(state: str = "", code: str = "", error: str = "") -> HTMLResponse:
        data = auth.read_data(state, SIGN_IN_MAX_AGE_S)
        if not data:
            return sign_in_page("This sign-in has expired. Go back to the app and tap Connect again.", ok=False)
        with Session(engine) as s:
            dest = s.get(Destination, data["dest"])
            if not dest:
                return sign_in_page("That channel isn't in the app any more.", ok=False)
            try:
                if error or not code:
                    raise YouTubeError("Google sign-in was cancelled." if error == "access_denied" else f"Google sign-in failed ({error}).")
                client = google()
                refresh_token, access_token = client.exchange_code(code, data["redirect_uri"])
                channel_id, channel_name = client.channel(access_token)
                if dest.channel_id and channel_id != dest.channel_id:
                    # Not revoked: that would also end the sign-in of an entry that channel is connected to.
                    raise YouTubeError(
                        f"You picked “{channel_name}”, but this is {dest.name}. Tap Connect again and pick "
                        f"{dest.name}'s channel when Google asks which account to use."
                    )
                dest.channel_id = channel_id
                dest.youtube_token, dest.youtube_account, dest.youtube_error = refresh_token, channel_name, ""
                message, ok = f"Connected {dest.name} (“{channel_name}”). Uploads to it now go through the YouTube API.", True
            except YouTubeError as e:
                dest.youtube_error, message, ok = str(e), str(e), False
            s.add(dest)
            s.commit()
        return sign_in_page(message, ok)

    @app.post("/api/destinations/{dest_id}/disconnect")
    def disconnect_destination(dest_id: int, s: Session = Depends(db)) -> DestinationOut:
        """Uploads to it go back to YouTube Studio."""
        dest = found(s, Destination, dest_id, "YouTube channel")
        forget_sign_in(s, dest)
        s.add(dest)
        s.commit()
        s.refresh(dest)
        return destination_out(dest)

    def connected(s: Session, dest_id: int) -> Destination:
        dest = found(s, Destination, dest_id, "YouTube channel")
        if not dest.youtube_token:
            raise HTTPException(409, "Playlists need this channel connected to the YouTube API (Shows → YouTube channels)")
        return dest

    @app.get("/api/destinations/{dest_id}/playlists")
    def list_playlists(dest_id: int, s: Session = Depends(db)) -> list[PlaylistOut]:
        dest = connected(s, dest_id)
        try:
            return [PlaylistOut(**vars(p)) for p in google().playlists(dest.youtube_token)]
        except YouTubeError as e:
            raise HTTPException(502, str(e)) from None

    @app.post("/api/destinations/{dest_id}/playlists", status_code=201)
    def create_playlist(dest_id: int, body: NewPlaylist, s: Session = Depends(db)) -> PlaylistOut:
        dest = connected(s, dest_id)
        try:
            return PlaylistOut(**vars(google().create_playlist(dest.youtube_token, body.title.strip(), body.privacy)))
        except YouTubeError as e:
            raise HTTPException(502, str(e)) from None

    @app.post("/api/shows/{show_id}/channels", status_code=201)
    def add_channel(show_id: int, body: ChannelIn, s: Session = Depends(db)) -> ChannelOut:
        found(s, Show, show_id, "show")
        try:
            if body.url.strip().startswith("/"):  # a folder on the server
                platform, url = "folder", parse_folder(body.url, settings.media_dir)
            else:
                platform, url = parse_channel(body.url)
        except ChannelUrlError as e:
            raise HTTPException(422, str(e)) from None
        backlog = dict(backlog=True, backlog_per_day=body.backlog_per_day, backlog_order=body.backlog_order) if body.backlog else {}
        if ch := s.exec(select(Channel).where(Channel.show_id == show_id, Channel.url == url)).first():
            if not backlog:
                raise HTTPException(409, "This show already watches that channel")
            for k, v in backlog.items():  # working through the past videos of a channel it already watches
                setattr(ch, k, v)
            ch.last_checked_at = None  # checked now, which lists its history
            s.add(ch)
            s.commit()
            s.refresh(ch)
            return channel_out(s, ch)
        ch = Channel(show_id=show_id, url=url, platform=platform, keywords=body.keywords.strip(),
                     exclude=body.exclude.strip(), min_minutes=body.min_minutes, **backlog)
        s.add(ch)
        add_term(s, s.get(Show, show_id), url)
        s.commit()
        s.refresh(ch)
        return channel_out(s, ch)

    def channel_term(url: str) -> str:
        """A channel's link without https://www., or a folder's path, which a file from it is added by."""
        return url.split("://", 1)[-1].removeprefix("www.")

    def add_term(s: Session, show: Show, url: str) -> None:
        """Links pasted from a watched channel belong to its show too."""
        term = channel_term(url)
        if term.lower() not in show.terms():
            show.match = "\n".join(filter(None, [show.match.strip(), term]))
            s.add(show)

    def drop_term(s: Session, show: Show, url: str) -> None:
        term = channel_term(url).lower()
        show.match = "\n".join(t.strip() for t in re.split(r"[\n,]+", show.match) if t.strip() and t.strip().lower() != term)
        s.add(show)

    @app.patch("/api/channels/{channel_id}")
    def update_channel(channel_id: int, patch: ChannelPatch, s: Session = Depends(db)) -> ChannelOut:
        ch = found(s, Channel, channel_id, "channel")
        changes = patch.model_dump(exclude_unset=True)
        if (show_id := changes.pop("show_id", None)) and show_id != ch.show_id:
            show = found(s, Show, show_id, "show")
            if s.exec(select(Channel).where(Channel.show_id == show_id, Channel.url == ch.url)).first():
                raise HTTPException(409, f"{show.name} already watches that channel")
            drop_term(s, s.get(Show, ch.show_id), ch.url)
            add_term(s, show, ch.url)
            ch.show_id = show_id
        for k, v in changes.items():
            setattr(ch, k, v.strip() if isinstance(v, str) else v)
        if changes.keys() & {"backlog", "backlog_per_day", "backlog_paused", "backlog_order"}:
            ch.last_checked_at = None  # so it takes effect on a check within seconds
        s.add(ch)
        s.commit()
        s.refresh(ch)
        return channel_out(s, ch)

    @app.delete("/api/channels/{channel_id}", status_code=204)
    def delete_channel(channel_id: int, s: Session = Depends(db)) -> None:
        remove_channel(s, found(s, Channel, channel_id, "channel"))
        s.commit()

    @app.post("/api/channels/{channel_id}/check")
    def check_channel_now(channel_id: int, s: Session = Depends(db)) -> ChannelOut:
        """Makes the channel due, so the worker checks it within a few seconds."""
        ch = found(s, Channel, channel_id, "channel")
        ch.last_checked_at = None
        s.add(ch)
        s.commit()
        s.refresh(ch)
        return channel_out(s, ch)

    @app.get("/api/channels/{channel_id}/videos")
    def channel_videos(channel_id: int, s: Session = Depends(db)) -> list[ChannelVideoOut]:
        found(s, Channel, channel_id, "channel")
        rows = s.exec(  # newest first, by when they were posted where that's known
            select(ChannelVideo).where(ChannelVideo.channel_id == channel_id)
            .order_by(func.coalesce(ChannelVideo.published_at, ChannelVideo.first_seen_at).desc(), col(ChannelVideo.id))
            .limit(50)
        )
        return [channel_video_out(item) for item in rows]

    @app.post("/api/channel-videos/{item_id}/process")
    def process_channel_video(item_id: int, s: Session = Depends(db)) -> ChannelVideoOut:
        """Queue a video the watcher left out: older, skipped by a filter, or not confirmed finished."""
        item = found(s, ChannelVideo, item_id, "video")
        show = s.get(Show, s.get(Channel, item.channel_id).show_id)
        item.vod_id = queue_vod(s, item.url, show=show).id
        item.status, item.reason, item.updated_at = "queued", "", utcnow()
        s.add(item)
        s.commit()
        s.refresh(item)
        return channel_video_out(item)

    # --- The Library: channels and folders whose older videos are worked through ---

    def media_folder(path: str | None) -> Path:
        """A folder in the media folder, by default the media folder itself."""
        try:
            return Path(parse_folder(path or str(settings.media_dir or ""), settings.media_dir))
        except ChannelUrlError as e:
            raise HTTPException(422, str(e)) from None

    @app.get("/api/folders")
    def list_folders(path: str | None = None) -> FolderOut:
        folder = media_folder(path)
        folders, videos = [], 0
        try:
            for entry in os.scandir(folder):  # like a watched folder, without hidden files and links
                if entry.name.startswith(".") or entry.is_symlink():
                    continue
                if entry.is_dir():
                    folders.append(entry.name)
                elif Path(entry.name).suffix.lower() in VIDEO_SUFFIXES:
                    videos += 1
        except OSError as e:
            raise HTTPException(422, f"Can't read {folder}: {e.strerror}") from None
        top = folder == settings.media_dir.resolve()
        return FolderOut(path=str(folder), parent=None if top else str(folder.parent),
                         folders=sorted(folders, key=episode_order), videos=videos)

    @app.get("/api/folders/videos")
    def folder_videos(path: str) -> FolderVideosOut:
        """What watching the folder would find, in the order its backlog would take them."""
        try:
            listed = list_folder(str(media_folder(path)), settings.media_dir)
        except OSError as e:
            raise HTTPException(422, f"Can't read {e.filename}: {e.strerror}") from None
        return FolderVideosOut(videos=len(listed), first=[item.id for item in listed[:5]])

    def backlog_items(s: Session, ch: Channel) -> list[tuple[ChannelVideo, Vod | None]]:
        """The channel's videos, with what became of each, in the order its backlog works through them, or for one
        that goes at random, in season and episode order (or oldest first)."""
        pairs = s.exec(
            select(ChannelVideo, Vod).outerjoin(Vod, ChannelVideo.vod_id == Vod.id).where(ChannelVideo.channel_id == ch.id)
        ).all()
        shown = "first" if ch.backlog_order == "random" else ch.backlog_order
        place = {item.id: i for i, item in enumerate(backlog_order(ch, [item for item, _ in pairs], shown))}
        return sorted(pairs, key=lambda pair: place[pair[0].id])

    def backlog_line(ch: Channel, pairs: list[tuple[ChannelVideo, Vod | None]]) -> list[ChannelVideo]:
        """The ones still to go, in the order the backlog takes them."""
        return backlog_order(ch, [item for item, _ in pairs if item.status == "older"])

    def item_state(item: ChannelVideo, vod: Vod | None) -> str:
        if item.status == "queued":
            return "done" if vod and vod.status == "ready" else "failed" if vod and vod.status == "failed" else "working"
        return {"older": "left", "waiting": "copying"}.get(item.status, "skipped")

    def backlog_summary(s: Session, ch: Channel, pairs: list[tuple[ChannelVideo, Vod | None]]) -> dict:
        show = s.get(Show, ch.show_id)
        states = Counter(item_state(item, vod) for item, vod in pairs)
        up_next = next(iter(backlog_line(ch, pairs)), None)
        day_ago = utcnow() - timedelta(days=1)
        return dict(
            channel=channel_out(s, ch), show_name=show.name, video_type=show.video_type,
            name=Path(ch.url).name if ch.platform == "folder" else ch.url.rstrip("/").rsplit("/", 1)[-1],
            total=len(pairs) - states["skipped"], done=states["done"], working=states["working"], failed=states["failed"],
            left=states["left"], skipped=states["skipped"], copying=states["copying"],
            next=(up_next.title or up_next.video_id) if up_next else "",
            today=sum(1 for item, _ in pairs if item.backlog_at and item.backlog_at >= day_ago),
        )

    @app.get("/api/library")
    def library(s: Session = Depends(db)) -> list[BacklogOut]:
        channels = s.exec(select(Channel).where(col(Channel.backlog)).order_by(col(Channel.id)))
        return [BacklogOut(**backlog_summary(s, ch, backlog_items(s, ch))) for ch in channels]

    @app.get("/api/channels/{channel_id}/backlog")
    def channel_backlog(channel_id: int, s: Session = Depends(db)) -> BacklogDetailOut:
        ch = found(s, Channel, channel_id, "channel")
        pairs = backlog_items(s, ch)
        counts: dict[int, dict[str, int]] = defaultdict(dict)
        for vod_id, status, n in s.exec(
            select(ClipRecord.vod_id, ClipRecord.status, func.count())
            .join(ChannelVideo, col(ChannelVideo.vod_id) == ClipRecord.vod_id).where(ChannelVideo.channel_id == ch.id)
            .group_by(ClipRecord.vod_id, ClipRecord.status)
        ):
            counts[vod_id][status] = n

        def group(item: ChannelVideo) -> str:
            if ch.platform == "folder":
                return item.video_id.rpartition("/")[0]
            return item.published_at.strftime("%B %Y") if item.published_at else ""

        line = {item.id: i for i, item in enumerate(backlog_line(ch, pairs), 1)}
        items = [BacklogItemOut(
            id=item.id, title=item.title or item.video_id, url=item.url, group=group(item), state=item_state(item, vod),
            reason=item.reason, published_at=iso(item.published_at), vod_id=vod.id if vod else None,
            vod_status=vod.status if vod else "", vod_progress=vod.progress if vod else 0,
            counts=counts.get(vod.id, {}) if vod else {}, place=line.get(item.id),
        ) for item, vod in pairs]
        return BacklogDetailOut(**backlog_summary(s, ch, pairs), items=items)

    @app.post("/api/channels/{channel_id}/backlog/skip", status_code=204)
    def skip_in_backlog(channel_id: int, body: BacklogSkip, s: Session = Depends(db)) -> None:
        """Skip older videos, so the backlog passes over them, or put skipped ones back in it."""
        ch = found(s, Channel, channel_id, "channel")
        for item in s.exec(select(ChannelVideo).where(ChannelVideo.channel_id == ch.id, col(ChannelVideo.id).in_(body.ids))):
            if body.skip and item.status == "older":
                item.status, item.reason = "skipped", "Skipped in the Library"
            elif not body.skip and item.status == "skipped":
                item.status, item.reason = "older", older_reason(ch)
            else:
                continue
            item.updated_at = utcnow()
            s.add(item)
        s.commit()

    # --- Clips ---

    @app.get("/api/clips")
    def list_clips(status: list[str] = Query(default=["proposed"]), s: Session = Depends(db)) -> list[ClipOut]:
        rows = s.exec(
            select(ClipRecord, Vod)
            .join(Vod)
            .where(col(ClipRecord.status).in_(status))
            .order_by(col(Vod.created_at).desc(), col(ClipRecord.kind), col(ClipRecord.start))
            .limit(500)
        )
        uploads = upload_states(s)
        return [clip_out(c, v, uploads.get(c.id)) for c, v in rows]

    @app.get("/api/clips/{clip_id}")
    def get_clip_route(clip_id: int, s: Session = Depends(db)) -> ClipOut:
        clip, vod = get_clip(s, clip_id)
        return clip_out(clip, vod, upload_states(s).get(clip.id))

    @app.patch("/api/clips/{clip_id}")
    def update_clip(clip_id: int, patch: ClipPatch, s: Session = Depends(db)) -> ClipOut:
        clip, vod = get_clip(s, clip_id)
        changes = patch.model_dump(exclude_unset=True)
        if "title" in changes:
            changes["title"] = " ".join((changes["title"] or "").split())
            if not changes["title"]:
                raise HTTPException(422, "The title can't be empty")
        cut_list = changes.pop("cuts", None)
        start, end, cut_list = checked_cuts(
            clip.kind, vod, changes.get("start", clip.start), changes.get("end", clip.end),
            cuts.parse(clip.cuts) if cut_list is None else cut_list, changes.get("make_short", clip.make_short),
        )
        changes.update(start=start, end=end, cuts=cuts.dump(cut_list))
        check_destinations(s, changes.get("destination_id"))
        if changes.get("destination_id", clip.destination_id) != clip.destination_id:
            changes.setdefault("playlist_id", "")  # a playlist belongs to its channel
        changed = {k: v for k, v in changes.items() if getattr(clip, k) != v}
        if not changed:
            return clip_out(clip, vod)
        for k, v in changed.items():
            setattr(clip, k, v)
        clip.updated_at = utcnow()
        # New cut points or a new Short mean a new render; titles and channels don't change the video.
        if changed.keys() & {"start", "end", "cuts", "make_short"} and clip.status in ("approved", "rendering", "rendered", "failed"):
            clip.status, clip.reviewed_at = "approved", utcnow()
        s.add(clip)
        s.commit()
        s.refresh(clip)
        return clip_out(clip, vod)

    @app.post("/api/clips/{clip_id}/approve")
    def approve(clip_id: int, s: Session = Depends(db)) -> ClipOut:
        clip, vod = get_clip(s, clip_id)
        if clip.status in ("approved", "rendering", "rendered", "uploaded"):
            return clip_out(clip, vod)
        if vod.cleaned_at:
            raise HTTPException(409, "This stream's video files were deleted to free space, so it can't be cut any more")
        return set_status(s, clip, vod, "approved")

    @app.post("/api/clips/{clip_id}/pause")
    def pause_clip(clip_id: int, s: Session = Depends(db)) -> ClipOut:
        """Hold a clip's render: a render that's running stops within a few seconds."""
        clip, vod = get_clip(s, clip_id)
        if clip.status not in ("approved", "rendering"):
            raise HTTPException(409, "It isn't waiting to render")
        clip.paused, clip.updated_at = True, utcnow()
        s.add(clip)
        s.commit()
        s.refresh(clip)
        return clip_out(clip, vod)

    @app.post("/api/clips/{clip_id}/resume")
    def resume_clip(clip_id: int, s: Session = Depends(db)) -> ClipOut:
        clip, vod = get_clip(s, clip_id)
        clip.paused, clip.updated_at = False, utcnow()
        s.add(clip)
        s.commit()
        s.refresh(clip)
        return clip_out(clip, vod)

    @app.post("/api/clips/{clip_id}/uploaded")
    def mark_uploaded(clip_id: int, body: Uploaded, s: Session = Depends(db)) -> ClipOut:
        clip, vod = get_clip(s, clip_id)
        if clip.status not in ("rendered", "uploaded"):
            raise HTTPException(409, "Only rendered clips can be marked as uploaded")
        clip.status, clip.youtube_url = "uploaded", body.youtube_url.strip()
        clip.uploaded_at = clip.updated_at = utcnow()
        cancel_retries(s, clip)  # it's on YouTube: a failed upload trying again would post it twice
        s.add(clip)
        s.commit()
        s.refresh(clip)
        return clip_out(clip, vod)

    # --- Uploads, done by the worker; the upload browser is for channels not connected to the API ---

    @app.get("/api/studio")
    def studio_status() -> dict:
        """What the worker last saw of the upload browser (signed in to YouTube, and to X for watching
        accounts there), and where to open it to sign in."""
        status = read_status(settings.studio_dir / "status.json")
        checked = status.get("checked_at")
        return {
            "browser": bool(status.get("browser")),
            "signed_in": bool(status.get("signed_in")),
            "x_signed_in": bool(status.get("x_signed_in")),
            "checked_at": iso(datetime.fromtimestamp(checked, timezone.utc)) if checked else None,
            "browser_view_url": settings.browser_view_url,
        }

    @app.post("/api/studio/open")
    def open_upload_browser() -> dict:
        """Open the upload browser on this machine, for signing in. Only works where a desktop browser is installed."""
        try:
            launch_desktop_browser(settings.browser_profile_dir, urlparse(settings.studio_cdp_url).port or 9222)
        except FileNotFoundError:
            raise HTTPException(409, "There's no browser to open here. Use the upload browser's own view to sign in.") from None
        return {"opened": True}

    @app.post("/api/studio/check")
    def check_upload_browser() -> dict:
        """Check the browser now instead of waiting for the worker. If this process can't reach it
        (the web container in Docker can't), the worker's last report stands."""
        status = Studio(settings.studio_cdp_url, settings.studio_dir).status()
        if status["browser"]:
            write_status(settings.studio_dir / "status.json", status)
        return studio_status()

    @app.get("/api/clips/{clip_id}/uploads")
    def clip_uploads(clip_id: int, s: Session = Depends(db)) -> list[UploadJobOut]:
        get_clip(s, clip_id)
        jobs = s.exec(select(UploadJob).where(UploadJob.clip_id == clip_id).order_by(col(UploadJob.created_at).desc())).all()
        turns = upload_turns(s) if any(j.status == "queued" for j in jobs) else {}
        return [job_out(j, turns.get(j.id)) for j in jobs]

    @app.post("/api/clips/{clip_id}/uploads", status_code=201)
    def start_upload(clip_id: int, body: UploadRequest, s: Session = Depends(db)) -> UploadJobOut:
        clip, vod = get_clip(s, clip_id)
        path = clip.short_path if body.which == "short" else clip.video_path
        if clip.status not in ("rendered", "uploaded") or not path or not Path(path).exists():
            raise HTTPException(409, "Render the clip before uploading it")
        if body.site == "rumble":
            dest = s.get(Destination, clip.destination_id) if clip.destination_id else None
            show = s.get(Show, vod.show_id) if vod.show_id else None
            if not (dest and (dest.rumble or "rumble" in upload_sites(show, clip.kind, dest))):
                raise HTTPException(409, "Set its show's uploads, or its channel's under Shows → YouTube channels, to go to "
                                         "Rumble (or both) first")
        busy = s.exec(select(UploadJob).where(
            UploadJob.clip_id == clip_id, UploadJob.which == body.which, UploadJob.site == body.site,
            col(UploadJob.status).in_(("queued", "uploading")),
        )).first()
        if busy:
            raise HTTPException(409, f"That file is already being uploaded to {'Rumble' if body.site == 'rumble' else 'YouTube'}")
        job = UploadJob(clip_id=clip_id, which=body.which, site=body.site, visibility=body.visibility)
        s.add(job)
        s.commit()
        s.refresh(job)
        return job_out(job, upload_turns(s).get(job.id))

    @app.post("/api/upload-jobs/{job_id}/retry")
    def retry_upload(job_id: int, s: Session = Depends(db)) -> UploadJobOut:
        job = found(s, UploadJob, job_id, "upload")
        if job.status != "failed":
            raise HTTPException(409, "Only failed uploads can be retried")
        job.status, job.progress, job.error, job.retry_at, job.updated_at = "queued", 0, "", None, utcnow()
        s.add(job)
        s.commit()
        s.refresh(job)
        return job_out(job, upload_turns(s).get(job.id))

    @app.post("/api/upload-jobs/{job_id}/now")
    def upload_now(job_id: int, s: Session = Depends(db)) -> UploadJobOut:
        """Skip the wait for its turn (the show's spacing, or its thumbnail)."""
        job = found(s, UploadJob, job_id, "upload")
        if job.status != "queued":
            raise HTTPException(409, "It isn't waiting")
        job.rush, job.updated_at = True, utcnow()
        s.add(job)
        s.commit()
        s.refresh(job)
        return job_out(job, upload_turns(s).get(job.id))

    @app.delete("/api/upload-jobs/{job_id}", status_code=204)
    def cancel_upload(job_id: int, s: Session = Depends(db)) -> None:
        job = found(s, UploadJob, job_id, "upload")
        if job.status == "uploading":
            raise HTTPException(409, "It's uploading right now and can't be cancelled from here")
        s.delete(job)
        s.commit()

    @app.get("/media/upload-jobs/{job_id}/screenshot.png")
    def upload_screenshot(job_id: int) -> FileResponse:
        job = looked_up(lambda s: found(s, UploadJob, job_id, "upload"))
        if not job.screenshot or not Path(job.screenshot).exists():
            raise HTTPException(404, "No screenshot")
        return FileResponse(job.screenshot, media_type="image/png")

    @app.post("/api/clips/{clip_id}/not-uploaded")
    def unmark_uploaded(clip_id: int, s: Session = Depends(db)) -> ClipOut:
        clip, vod = get_clip(s, clip_id)
        if clip.status != "uploaded":
            raise HTTPException(409, "This clip isn't marked as uploaded")
        # Uploaded files are deleted to save space; then it renders again.
        rendered = bool(clip.video_path) and Path(clip.video_path).exists()
        clip.status, clip.uploaded_at, clip.updated_at = "rendered" if rendered else "approved", None, utcnow()
        if not rendered:
            clip.reviewed_at = utcnow()
        s.add(clip)
        s.commit()
        s.refresh(clip)
        return clip_out(clip, vod)

    @app.post("/api/clips/{clip_id}/reupload")
    def reupload(clip_id: int, s: Session = Depends(db)) -> ClipOut:
        """Render an uploaded clip again, then upload it again wherever the app uploaded it, with the same visibility:
        for an upload that went wrong on the site's side, like a video YouTube won't play. The broken one stays up
        until it's deleted there."""
        clip, vod = get_clip(s, clip_id)
        if clip.status != "uploaded":
            raise HTTPException(409, "Only an uploaded clip can be uploaded again")
        if vod.cleaned_at or not vod.workspace:
            raise HTTPException(409, "This stream's video files were deleted, so it can't be rendered again")
        jobs = s.exec(select(UploadJob).where(UploadJob.clip_id == clip_id).order_by(col(UploadJob.id))).all()
        if any(j.status in ("queued", "uploading") for j in jobs):
            raise HTTPException(409, "One of its uploads is still going. Wait for it to finish first.")
        done = {(j.which, j.site): j.visibility for j in jobs if j.status == "done"}  # the latest of each
        if not done:
            raise HTTPException(409, "The app didn't upload it, so there's nothing to repeat. Tap Not uploaded yet, "
                                     "then upload it again.")
        clip.reupload = json.dumps([[which, site, visibility] for (which, site), visibility in done.items()])
        if any(site == "youtube" for _, site in done):
            clip.youtube_url = ""
        if any(site == "rumble" for _, site in done):
            clip.rumble_url = ""
        cancel_retries(s, clip)
        clip.status, clip.error, clip.uploaded_at = "approved", "", None
        clip.reviewed_at = clip.updated_at = utcnow()
        s.add(clip)
        s.commit()
        s.refresh(clip)
        return clip_out(clip, vod)

    @app.post("/api/clips/{clip_id}/reject")
    def reject(clip_id: int, s: Session = Depends(db)) -> ClipOut:
        """Also deletes a render, so a clip that's ready to upload can be thrown away."""
        clip, vod = get_clip(s, clip_id)
        if clip.status == "uploaded":
            raise HTTPException(409, "It's on YouTube already")
        if clip.status in ACTIVE_CLIP:
            discard_render(s, clip, vod)
        return set_status(s, clip, vod, "rejected")

    @app.post("/api/clips/{clip_id}/restore")
    def restore(clip_id: int, s: Session = Depends(db)) -> ClipOut:
        """Back to the inbox: undo for approve and reject, or taking back an approved clip (its render is deleted)."""
        clip, vod = get_clip(s, clip_id)
        if clip.status == "uploaded":
            raise HTTPException(409, "It's on YouTube already")
        if clip.status in ACTIVE_CLIP:
            discard_render(s, clip, vod)
        return set_status(s, clip, vod, "proposed")

    @app.get("/api/clips/{clip_id}/transcript")
    def clip_transcript(clip_id: int, s: Session = Depends(db)) -> list[Line]:
        """Transcript lines from a minute before the clip to a minute after, for trimming."""
        clip, vod = get_clip(s, clip_id)
        ws = workspace(vod)
        if clip.kind == "whole" or not ws.transcript_json.exists():
            return []
        return [
            Line(start=a, end=b, text=text)
            for a, b, text, _ in transcript_lines(ws.transcript_json)
            if b > clip.start - 60 and a < clip.end + 60
        ]

    # --- The editor: a stream's transcript, waveform and frames, and clips made from any part of it ---

    @app.get("/api/vods/{vod_id}/transcript")
    def vod_transcript(
        vod_id: int, request: Request, start: float | None = None, end: float | None = None, words: bool = False,
    ) -> Response:
        """The stream's transcript lines, all of them or those between `start` and `end`, with each word's timing
        as [start, end, text] if `words` (to snap cuts to). Compressed when the browser takes that: all of a long
        stream's lines come to several hundred KB."""
        ws = workspace(looked_up(lambda s: found(s, Vod, vod_id, "video")))
        lo = float("-inf") if start is None else start
        hi = float("inf") if end is None else end
        lines = []
        if ws.transcript_json.exists():
            for a, b, text, said in transcript_lines(ws.transcript_json):
                if b > lo and a < hi:
                    lines.append({"start": a, "end": b, "text": text, **({"words": said} if words else {})})
        return packed(request, lines)

    @app.get("/api/vods/{vod_id}/peaks")
    async def vod_peaks(
        vod_id: int, request: Request, start: float = Query(ge=0), end: float = Query(gt=0),
        count: int = Query(default=1000, ge=1, le=4000),
    ) -> Response:
        """How loud the stream is across `count` parts of `start` to `end` (0 to 1), to draw its waveform. Read on a
        thread of its own: the first zoomed-out look at a stream the worker hasn't made the overview of yet makes it,
        and requests meanwhile wait there rather than holding threads the rest of the app needs."""
        ws = workspace(await run_in_threadpool(looked_up, lambda s: found(s, Vod, vod_id, "video")))
        if end <= start:
            raise HTTPException(422, "The end has to be after the start")
        if not ws.audio_wav.exists():
            raise HTTPException(404, "The stream's soundtrack isn't there: it's deleted with its video files")
        peaks = await asyncio.get_running_loop().run_in_executor(waveform_pool, audio.peaks, ws, start, end, count)
        return packed(request, {"start": start, "end": end, "peaks": peaks})

    @app.get("/api/vods/{vod_id}/new-clip")
    def new_clip_info(vod_id: int, s: Session = Depends(db)) -> NewClipInfo:
        """What a clip made in the app starts with: the show's title pattern (with the stream's date, as Claude's
        clips have it) and its channel and playlist for clips."""
        vod = found(s, Vod, vod_id, "video")
        show = s.get(Show, vod.show_id) if vod.show_id else None
        template = show.title_template if show else "{title}"
        ws = workspace(vod)
        meta = ws.meta() if ws.meta_json.exists() else {}
        day = titles.format_date(titles.stream_date(meta, vod.created_at.date(), settings.timezone, settings.day_starts_at))
        return NewClipInfo(
            title_pattern=titles.apply(template, "{title}", day),
            title_room=titles.room_for_title(template, day),
            destination_id=show.clip_destination_id if show else None,
            playlist_id=show.clip_playlist_id if show else "",
        )

    @app.post("/api/vods/{vod_id}/clips", status_code=201)
    def make_clip(vod_id: int, body: NewClip, s: Session = Depends(db)) -> ClipOut:
        """A clip made in the app from any part of the stream: into the inbox, or with `approve` straight to
        rendering. It's marked as made by hand (manual), since it has no score from Claude."""
        vod = found(s, Vod, vod_id, "video")
        if vod.cleaned_at:
            raise HTTPException(409, "This stream's video files were deleted to free space, so it can't be cut any more")
        if vod.status != "ready" or not vod.workspace:
            raise HTTPException(409, "Clips can be made once the video is ready")
        title = " ".join(body.title.split())
        if not title:
            raise HTTPException(422, "The title can't be empty")
        make_short = body.make_short and body.kind == "highlight"
        start, end, cut_list = checked_cuts(body.kind, vod, body.start, body.end, body.cuts, make_short)
        check_destinations(s, body.destination_id)
        keys = set(s.exec(select(ClipRecord.key).where(ClipRecord.vod_id == vod.id)))
        number = next(n for n in range(1, len(keys) + 2) if f"my{n:02d}" not in keys)
        clip = ClipRecord(
            vod_id=vod.id, key=f"my{number:02d}", kind=body.kind, start=start, end=end, cuts=cuts.dump(cut_list),
            title=title, description=body.description.strip(), vertical_ok=body.kind == "highlight", make_short=make_short,
            destination_id=body.destination_id, playlist_id=body.playlist_id if body.destination_id else "",
            manual=True, status="approved" if body.approve else "proposed", reviewed_at=utcnow() if body.approve else None,
        )
        s.add(clip)
        s.commit()
        s.refresh(clip)
        return clip_out(clip, vod)

    # --- Thumbnails (thumbnails.py), made by the worker, which has Claude ---

    @app.get("/api/thumbnails")
    def thumbnail_setup() -> dict:
        return {"configured": bool(settings.openrouter_api_key), "model": settings.thumbnail_model}

    @app.post("/api/clips/{clip_id}/thumbnail")
    def request_thumbnail(clip_id: int, body: ThumbnailRequest, s: Session = Depends(db)) -> ClipOut:
        """Have the worker make a thumbnail (a new one, if it has one), built on your idea if you give one; or, with
        a prompt, draw the one it has again from that prompt (the one it was drawn from, or your changes to it)."""
        clip, vod = get_clip(s, clip_id)
        if not settings.openrouter_api_key:
            raise HTTPException(409, "Thumbnails need an OpenRouter key: set CLIPPER_OPENROUTER_API_KEY (see the README)")
        if clip.thumbnail_status in ("queued", "making"):
            raise HTTPException(409, "One is being made already")
        if vod.cleaned_at or not vod.workspace:
            raise HTTPException(409, "This stream's video files were deleted, so there are no frames to draw the people from")
        if prompt := body.prompt.strip():
            clip.thumbnail_redraw = prompt
        else:
            clip.thumbnail_hint, clip.thumbnail_redraw = body.hint.strip(), ""
        clip.thumbnail_status, clip.thumbnail_error = "queued", ""
        s.add(clip)
        s.commit()
        s.refresh(clip)
        return clip_out(clip, vod)

    @app.post("/api/clips/{clip_id}/thumbnail/apply")
    def apply_thumbnail(clip_id: int, s: Session = Depends(db)) -> dict:
        """Put the clip's thumbnail on its video that's already on YouTube, for channels connected to the API: the
        video the app uploaded, else the one whose link you gave when marking it uploaded by hand."""
        clip, _ = get_clip(s, clip_id)
        path = Path(clip.thumbnail_path) if clip.thumbnail_path else None
        if clip.thumbnail_status != "done" or not path or not path.is_file():
            raise HTTPException(409, "Make a thumbnail first")
        job = s.exec(select(UploadJob).where(
            UploadJob.clip_id == clip_id, UploadJob.which == "video", UploadJob.site == "youtube", UploadJob.status == "done",
        ).order_by(col(UploadJob.created_at).desc())).first()
        link = job.youtube_url if job else clip.youtube_url
        if not job and "/shorts/" in link.lower():
            raise HTTPException(409, "The link it was marked uploaded with is a Short's, and Shorts don't use custom thumbnails. "
                                     "Mark it uploaded again with its 16:9 video's link.")
        vid = video_id(link)
        if not vid:
            raise HTTPException(409, "There's no link to its video on YouTube. If you uploaded it yourself, mark it uploaded "
                                     "with the 16:9 video's link (Shorts don't use thumbnails).")
        dest = s.get(Destination, clip.destination_id) if clip.destination_id else None
        if not (dest and dest.youtube_token):
            raise HTTPException(409, "Only channels connected to the YouTube API take it from here. Download it and set it in YouTube Studio.")
        try:
            google().set_thumbnail(dest.youtube_token, vid, path)
        except YouTubeError as e:
            # A link you gave can be to a video on another channel than the clip's, which YouTube turns down too.
            mine = "" if job else f" Also check the link it was marked uploaded with is a video on {dest.name}."
            raise HTTPException(502, f"{e}{mine}") from None
        return {"applied": True}

    # --- How uploads are doing on YouTube (stats.py) ---

    @app.get("/api/stats")
    def upload_stats(show_id: int | None = None, s: Session = Depends(db)) -> StatsOut:
        query = (
            select(UploadJob, ClipRecord, Vod)
            .join(ClipRecord, UploadJob.clip_id == ClipRecord.id)
            .join(Vod, ClipRecord.vod_id == Vod.id)
            .where(UploadJob.status == "done", UploadJob.youtube_url != "", UploadJob.site == "youtube")
        )
        if show_id is not None:
            query = query.where(Vod.show_id == show_id)
        rows = s.exec(query.order_by(col(UploadJob.finished_at).desc(), col(UploadJob.id).desc()).limit(500)).all()
        connected = s.exec(select(Destination).where(Destination.youtube_token != "")).first() is not None
        setup = "" if settings.youtube_api_key or (settings.google_client_id and connected) else (
            "Views can't be read yet: connect a channel to the YouTube API under Shows → YouTube channels, or set "
            "CLIPPER_YOUTUBE_API_KEY (see the README)."
        )
        checked = max((j.stats_at for j, _, _ in rows if j.stats_at), default=None)
        return StatsOut(
            setup_needed=setup,
            checked_at=iso(checked),
            bands=[StatsBand(**vars(b)) for b in stats.score_bands([(j, c) for j, c, _ in rows])],
            uploads=[
                StatsUpload(job_id=j.id, clip_id=c.id, show_id=v.show_id, title=c.title, kind=c.kind, which=j.which,
                            score=c.score, manual=c.manual, youtube_url=j.youtube_url, finished_at=iso(j.finished_at), views=j.views,
                            likes=j.likes, comments=j.comments, views_day1=j.views_day1, views_week1=j.views_week1)
                for j, c, v in rows
            ],
        )

    # --- Media ---

    @app.get("/media/clips/{clip_id}/thumbnail.jpg")
    def clip_thumbnail(clip_id: int, download: bool = False) -> FileResponse:
        clip, _ = looked_up(lambda s: get_clip(s, clip_id))
        if not clip.thumbnail_path or not Path(clip.thumbnail_path).is_file():
            raise HTTPException(404, "No thumbnail")
        name = f"{file_name(clip.title)} (thumbnail).jpg"
        return FileResponse(clip.thumbnail_path, media_type="image/jpeg", filename=name if download else None)

    @app.get("/media/vods/{vod_id}/proxy.mp4")
    def proxy(vod_id: int) -> FileResponse:
        path = workspace(looked_up(lambda s: found(s, Vod, vod_id, "video"))).proxy_mp4
        if not path.exists():
            raise HTTPException(404, "The preview copy isn't ready yet")
        return FileResponse(path, media_type="video/mp4")

    @app.get("/media/vods/{vod_id}/frame.jpg")
    async def vod_frame(vod_id: int, request: Request, t: float = Query(ge=0)) -> Response:
        """A small still of the stream at `t`, to the tenth of a second, for the editor's timeline. Kept once
        made, and deleted with the stream's files. One the browser stopped wanting while it waited (the timeline
        moved on) isn't made."""
        vod = await run_in_threadpool(looked_up, lambda s: found(s, Vod, vod_id, "video"))
        ws = workspace(vod)
        if not ws.proxy_mp4.exists():
            raise HTTPException(404, "The preview copy isn't ready yet")
        # On the 2 s grid of the preview's keyframes, which is all a still can show.
        moment = int(min(t, max(0.0, vod.duration - 0.5) if vod.duration else t) // 2 * 2)
        out = ws.frames_dir / f"{moment * 10}.jpg"
        coarse = moment % render.STILL_EVERY_S == 0
        if not out.exists() and coarse and ws.stills_done.exists():
            raise HTTPException(404, "No still there")  # the worker's pass made every one there is
        if not out.exists():
            async with frame_slots:
                if await request.is_disconnected():
                    return Response(status_code=204)
                if not out.exists():
                    if coarse:  # zoomed out, before the worker's pass: just this one
                        make = partial(render.render_frame, ws.proxy_mp4, moment, out)
                    else:  # zoomed in: the minute around it, in one run
                        block = moment // render.STILL_BLOCK_S * render.STILL_BLOCK_S
                        make = partial(render.render_stills, ws.proxy_mp4, ws.frames_dir, 2, block, render.STILL_BLOCK_S)
                    try:
                        await asyncio.get_running_loop().run_in_executor(frame_pool, make)
                    except ffmpeg.FFmpegError as e:
                        raise HTTPException(404, "No still there") from e
                if not out.exists():
                    raise HTTPException(404, "No still there")
        return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "max-age=604800, immutable"})

    @app.get("/media/clips/{clip_id}/thumb.jpg")
    async def thumbnail(clip_id: int, request: Request) -> Response:
        """A clip's picture in the lists. Made one at a time, with the timeline's stills: a list of new clips asks for
        all of theirs at once, and each run of ffmpeg reads the stream's whole index (180 MB for 8 hours), so a few
        dozen together took the server's memory. One the browser stopped wanting while it waited isn't made."""
        clip, vod = await run_in_threadpool(looked_up, lambda s: get_clip(s, clip_id))
        ws = workspace(vod)
        t = clip.start + min(3.0, (clip.end - clip.start) * 0.2)
        out = ws.thumbs_dir / f"{clip.key}-{int(t * 10)}.jpg"
        if not out.exists():
            async with frame_slots:
                if await request.is_disconnected():
                    return Response(status_code=204)
                if not out.exists():
                    video = ws.proxy_mp4 if ws.proxy_mp4.exists() else ws.source
                    try:
                        await asyncio.get_running_loop().run_in_executor(frame_pool, render.render_thumbnail, video, t, out)
                    except ffmpeg.FFmpegError as e:
                        raise HTTPException(404, "No picture there") from e
        return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    @app.get("/media/clips/{clip_id}/{which}.mp4")
    def clip_file(clip_id: int, which: str, download: bool = False) -> FileResponse:
        clip, _ = looked_up(lambda s: get_clip(s, clip_id))
        path = {"video": clip.video_path, "short": clip.short_path}.get(which)
        if not path or not Path(path).exists():
            raise HTTPException(404, "Not rendered yet")
        name = f"{file_name(clip.title)}{' (Short)' if which == 'short' else ''}.mp4"
        return FileResponse(path, media_type="video/mp4", filename=name if download else None)

    # --- Phone app ---

    web_dir = (settings.web_dir or REPO_WEB_DIR).resolve()
    if (web_dir / "index.html").exists():
        if (web_dir / "assets").exists():
            app.mount("/assets", StaticFiles(directory=web_dir / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def phone_app(path: str) -> FileResponse:
            if path.startswith(("api/", "media/")):
                raise HTTPException(404)
            file = (web_dir / path).resolve()
            if path and file.is_file() and web_dir in file.parents:
                return FileResponse(file)
            return FileResponse(web_dir / "index.html", headers={"Cache-Control": "no-cache"})
    else:
        log.warning("Phone app not built (no %s); only the API is available", web_dir / "index.html")

    return app
