"""JSON API for the review app, plus media streaming and the built phone app."""

import html
import ipaddress
import json
import logging
import mimetypes
import re
import shutil
import time
from datetime import datetime, timezone
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

from .. import ffmpeg, notify
from ..config import Settings, get_settings
from ..db import (
    PAUSABLE, Channel, ChannelVideo, ClipRecord, Destination, Show, UploadJob, Vod,
    get_app_setting, get_engine, is_paused, queue_vod, set_app_setting, utcnow,
)
from ..models import Transcript
from ..monitor import ChannelUrlError, parse_channel
from ..overlay import Overlay, save as save_overlay_image
from ..studio import Studio, launch_desktop_browser, read_status, resolve_channel_id, write_status
from ..pipeline import render
from ..workspace import Workspace
from ..youtube import CALLBACK_PATH, Google, YouTubeError, sign_in_url
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
Auto = Literal["review", "render", "upload"]
Visibility = Literal["public", "unlisted", "private"]
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
    youtube_url: str
    updated_at: str


class VodDetail(BaseModel):
    vod: VodOut
    clips: list[ClipOut]


class Line(BaseModel):
    start: float
    end: float
    text: str


class NewVod(BaseModel):
    url: str = Field(min_length=4, max_length=2000)
    mode: Mode | None = None  # picked from the show or the link when missing
    notes: str = Field(default="", max_length=5000)


class ShowIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    match: str = Field(default="", max_length=2000)
    mode: Mode = "segment"
    title_template: str = Field(default="{title}", max_length=200)
    episode_template: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=10000)
    clip_guidance: str = Field(default="", max_length=10000)
    clip_destination_id: int | None = None
    episode_destination_id: int | None = None
    clip_playlist_id: str = Field(default="", max_length=64)
    episode_playlist_id: str = Field(default="", max_length=64)
    max_chapters: int = Field(default=5, ge=0, le=100)
    max_shorts: int = Field(default=5, ge=0, le=100)
    auto_episode: Auto = "review"
    auto_chapters: Auto = "review"
    auto_shorts: Auto = "review"
    auto_visibility: Visibility = "public"
    short_video_top: float | None = Field(default=None, ge=0, le=0.9)
    short_video_width: float = Field(default=1.0, ge=0.4, le=1)
    overlay_episodes: bool = False
    overlay_x: float | None = Field(default=None, ge=0, le=1)
    overlay_y: float | None = Field(default=None, ge=0, le=1)
    overlay_scale: float = Field(default=1.0, ge=0.1, le=5)


class ShowPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    match: str | None = Field(default=None, max_length=2000)
    mode: Mode | None = None
    title_template: str | None = Field(default=None, max_length=200)
    episode_template: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=10000)
    clip_guidance: str | None = Field(default=None, max_length=10000)
    clip_destination_id: int | None = None
    episode_destination_id: int | None = None
    clip_playlist_id: str | None = Field(default=None, max_length=64)
    episode_playlist_id: str | None = Field(default=None, max_length=64)
    max_chapters: int | None = Field(default=None, ge=0, le=100)
    max_shorts: int | None = Field(default=None, ge=0, le=100)
    auto_episode: Auto | None = None
    auto_chapters: Auto | None = None
    auto_shorts: Auto | None = None
    auto_visibility: Visibility | None = None
    short_video_top: float | None = Field(default=None, ge=0, le=0.9)  # null centres it
    short_video_width: float | None = Field(default=None, ge=0.4, le=1)
    overlay_episodes: bool | None = None
    overlay_x: float | None = Field(default=None, ge=0, le=1)  # null puts the logo back where it was drawn
    overlay_y: float | None = Field(default=None, ge=0, le=1)
    overlay_scale: float | None = Field(default=None, ge=0.1, le=5)


class DestinationIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(default="", max_length=500)


class DestinationOut(BaseModel):
    id: int
    name: str
    url: str
    channel_id: str  # empty if it couldn't be found from the link
    studio_upload_url: str  # where to upload by hand
    api_connected: bool  # uploads go through the YouTube API
    api_account: str  # the channel name Google reported when it was connected
    api_error: str  # why connecting failed, or why the sign-in stopped working


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


class UploadJobOut(BaseModel):
    id: int
    clip_id: int
    which: str
    visibility: str
    status: str
    method: str  # "api" or "studio"; empty until it starts
    progress: float
    youtube_url: str
    error: str
    has_screenshot: bool
    created_at: str


class Uploaded(BaseModel):
    youtube_url: str = Field(default="", max_length=500)


class AppSettings(BaseModel):
    download_limit_mbps: float  # 0 = no limit
    cleanup_after_days: float  # delete a finished stream's big files this long after its last change


class AppSettingsPatch(BaseModel):
    download_limit_mbps: float | None = Field(default=None, ge=0, le=100_000)
    cleanup_after_days: float | None = Field(default=None, ge=0, le=3650)


APP_SETTING_KEYS = ("download_limit_mbps", "cleanup_after_days")


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


class ChannelPatch(BaseModel):
    keywords: str | None = Field(default=None, max_length=1000)
    exclude: str | None = Field(default=None, max_length=1000)
    min_minutes: float | None = Field(default=None, ge=0, le=1440)
    enabled: bool | None = None
    show_id: int | None = None  # move it to another show; what it has already seen stays seen


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


class ShowOut(BaseModel):
    id: int
    name: str
    match: str
    mode: str
    title_template: str
    episode_template: str
    notes: str
    clip_guidance: str
    clip_destination_id: int | None
    episode_destination_id: int | None
    clip_playlist_id: str
    episode_playlist_id: str
    max_chapters: int
    max_shorts: int
    auto_episode: str
    auto_chapters: str
    auto_shorts: str
    auto_visibility: str
    short_video_top: float | None
    short_video_width: float
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
    first_seen_at: str


class ClipPatch(BaseModel):
    title: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=5000)
    start: float | None = None
    end: float | None = None
    make_short: bool | None = None
    destination_id: int | None = None
    playlist_id: str | None = Field(default=None, max_length=64)  # cleared when the channel changes, unless given


class Login(BaseModel):
    password: str


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


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
        return ShowOut(**show.model_dump(exclude={"created_at", "overlay_box", "overlay_screen"}),
                       short_background=image_url(settings.short_background(show.id), f"/media/shows/{show.id}/short-background.png"),
                       overlay=image_url(settings.show_overlay(show.id), f"/media/shows/{show.id}/overlay.png") if overlay else "",
                       overlay_box=list(overlay.box) if overlay else None,
                       overlay_screen=list(overlay.screen) if overlay and overlay.screen else None,
                       channels=[channel_out(s, c) for c in channels], video_count=videos)

    def image_url(path: Path, url: str) -> str:
        """A show's image's address, with the file's age in it so a new image isn't hidden by the browser's
        copy of the old one; "" if there's none."""
        return f"{url}?v={int(path.stat().st_mtime)}" if path.exists() else ""

    def channel_video_out(item: ChannelVideo) -> ChannelVideoOut:
        return ChannelVideoOut(**item.model_dump(exclude={"first_seen_at", "updated_at"}),
                               first_seen_at=iso(item.first_seen_at))

    def found(s: Session, model, id_: int, what: str):
        row = s.get(model, id_)
        if not row:
            raise HTTPException(404, f"No such {what}")
        return row

    def valid_template(template: str, optional: bool = False) -> str:
        template = " ".join(template.split())
        if not (optional and not template) and "{title}" not in template:
            raise HTTPException(422, "A title pattern needs {title} in it, for the part Claude writes")
        return template

    def remove_channel(s: Session, ch: Channel) -> None:
        for item in s.exec(select(ChannelVideo).where(ChannelVideo.channel_id == ch.id)):
            s.delete(item)
        s.flush()
        s.delete(ch)

    def clip_out(clip: ClipRecord, vod: Vod) -> ClipOut:
        video_size, short_size = _size(clip.video_path), _size(clip.short_path)
        with Session(engine) as s:
            dest = s.get(Destination, clip.destination_id) if clip.destination_id else None
        return ClipOut(
            **clip.model_dump(exclude={"video_path", "short_path", "created_at", "updated_at", "reviewed_at", "uploaded_at"}),
            vod_title=vod.title or vod.url,
            vod_duration=vod.duration,
            has_video=video_size > 0,
            has_short=short_size > 0,
            video_size=video_size,
            short_size=short_size,
            destination_name=dest.name if dest else "",
            destination_url=dest.url if dest else "",
            updated_at=iso(clip.updated_at),
        )

    def destination_out(d: Destination) -> DestinationOut:
        return DestinationOut(id=d.id, name=d.name, url=d.url, channel_id=d.channel_id,
                              studio_upload_url=studio_upload_url(d.channel_id),
                              api_connected=bool(d.youtube_token), api_account=d.youtube_account, api_error=d.youtube_error)

    def job_out(job: UploadJob) -> UploadJobOut:
        return UploadJobOut(**job.model_dump(exclude={"screenshot", "session_url", "session_key", "created_at", "updated_at"}),
                            has_screenshot=bool(job.screenshot) and Path(job.screenshot).exists(),
                            created_at=iso(job.created_at))

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

    def set_status(s: Session, clip: ClipRecord, vod: Vod, status: str) -> ClipOut:
        clip.status = status
        clip.reviewed_at = clip.updated_at = utcnow()
        if status == "approved":
            clip.error = ""
        s.add(clip)
        s.commit()
        s.refresh(clip)
        return clip_out(clip, vod)

    def discard_render(s: Session, clip: ClipRecord, vod: Vod) -> None:
        """Take a clip out of the render and upload queues and delete its rendered files. A full episode's
        file is the stream itself and stays. A render that's running stops on its own (it sees the change)."""
        if s.exec(select(UploadJob).where(UploadJob.clip_id == clip.id, UploadJob.status == "uploading")).first():
            raise HTTPException(409, "It's uploading right now. Wait for that to finish.")
        for job in s.exec(select(UploadJob).where(UploadJob.clip_id == clip.id, UploadJob.status == "queued")):
            s.delete(job)
        clips_dir = (settings.vods_dir / vod.workspace / "clips").resolve() if vod.workspace else None
        for path in (clip.video_path, clip.short_path):
            if path and clips_dir and Path(path).parent.resolve() == clips_dir:
                Path(path).unlink(missing_ok=True)
        clip.video_path = clip.short_path = ""
        clip.paused = False

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
        return AppSettings(**values)

    @app.get("/api/settings")
    def get_settings_route(s: Session = Depends(db)) -> AppSettings:
        return current_settings(s)

    @app.patch("/api/settings")
    def update_settings(body: AppSettingsPatch, s: Session = Depends(db)) -> AppSettings:
        """A running download switches to a new speed limit within a few seconds; cleanup uses its new days
        from its next round."""
        for key, value in body.model_dump(exclude_none=True).items():
            set_app_setting(s, key, str(value))
        s.commit()
        return current_settings(s)

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

    @app.post("/api/destinations", status_code=201)
    def add_destination(body: DestinationIn, s: Session = Depends(db)) -> DestinationOut:
        url = body.url.strip()
        dest = Destination(name=body.name.strip(), url=url, channel_id=resolve_channel_id(url) if url else "")
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
            platform, url = parse_channel(body.url)
        except ChannelUrlError as e:
            raise HTTPException(422, str(e)) from None
        if s.exec(select(Channel).where(Channel.show_id == show_id, Channel.url == url)).first():
            raise HTTPException(409, "This show already watches that channel")
        ch = Channel(show_id=show_id, url=url, platform=platform, keywords=body.keywords.strip(),
                     exclude=body.exclude.strip(), min_minutes=body.min_minutes)
        s.add(ch)
        add_term(s, s.get(Show, show_id), url)
        s.commit()
        s.refresh(ch)
        return channel_out(s, ch)

    def channel_term(url: str) -> str:
        return url.split("://", 1)[1].removeprefix("www.")

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
        rows = s.exec(
            select(ChannelVideo).where(ChannelVideo.channel_id == channel_id)
            .order_by(col(ChannelVideo.first_seen_at).desc(), col(ChannelVideo.id)).limit(50)
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
        return [clip_out(c, v) for c, v in rows]

    @app.get("/api/clips/{clip_id}")
    def get_clip_route(clip_id: int, s: Session = Depends(db)) -> ClipOut:
        return clip_out(*get_clip(s, clip_id))

    @app.patch("/api/clips/{clip_id}")
    def update_clip(clip_id: int, patch: ClipPatch, s: Session = Depends(db)) -> ClipOut:
        clip, vod = get_clip(s, clip_id)
        changes = patch.model_dump(exclude_unset=True)
        if "title" in changes:
            changes["title"] = " ".join((changes["title"] or "").split())
            if not changes["title"]:
                raise HTTPException(422, "The title can't be empty")
        start, end = changes.get("start", clip.start), changes.get("end", clip.end)
        if start < 0 or end - start < 1 or (vod.duration and end > vod.duration + 0.5):
            raise HTTPException(422, "The clip must be at least 1 second long and inside the video")
        if changes.get("make_short", clip.make_short) and end - start > MAX_SHORT_S:
            raise HTTPException(422, "Shorts can be at most 3 minutes long")
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
        if changed.keys() & {"start", "end", "make_short"} and clip.status in ("approved", "rendering", "rendered", "failed"):
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
        jobs = s.exec(select(UploadJob).where(UploadJob.clip_id == clip_id).order_by(col(UploadJob.created_at).desc()))
        return [job_out(j) for j in jobs]

    @app.post("/api/clips/{clip_id}/uploads", status_code=201)
    def start_upload(clip_id: int, body: UploadRequest, s: Session = Depends(db)) -> UploadJobOut:
        clip, _ = get_clip(s, clip_id)
        path = clip.short_path if body.which == "short" else clip.video_path
        if clip.status not in ("rendered", "uploaded") or not path or not Path(path).exists():
            raise HTTPException(409, "Render the clip before uploading it")
        busy = s.exec(select(UploadJob).where(
            UploadJob.clip_id == clip_id, UploadJob.which == body.which, col(UploadJob.status).in_(("queued", "uploading")),
        )).first()
        if busy:
            raise HTTPException(409, "That file is already being uploaded")
        job = UploadJob(clip_id=clip_id, which=body.which, visibility=body.visibility)
        s.add(job)
        s.commit()
        s.refresh(job)
        return job_out(job)

    @app.post("/api/upload-jobs/{job_id}/retry")
    def retry_upload(job_id: int, s: Session = Depends(db)) -> UploadJobOut:
        job = found(s, UploadJob, job_id, "upload")
        if job.status != "failed":
            raise HTTPException(409, "Only failed uploads can be retried")
        job.status, job.progress, job.error, job.updated_at = "queued", 0, "", utcnow()
        s.add(job)
        s.commit()
        s.refresh(job)
        return job_out(job)

    @app.delete("/api/upload-jobs/{job_id}", status_code=204)
    def cancel_upload(job_id: int, s: Session = Depends(db)) -> None:
        job = found(s, UploadJob, job_id, "upload")
        if job.status == "uploading":
            raise HTTPException(409, "It's uploading right now and can't be cancelled from here")
        s.delete(job)
        s.commit()

    @app.get("/media/upload-jobs/{job_id}/screenshot.png")
    def upload_screenshot(job_id: int, s: Session = Depends(db)) -> FileResponse:
        job = found(s, UploadJob, job_id, "upload")
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
        transcript = Transcript.model_validate_json(ws.transcript_json.read_text(encoding="utf-8"))
        return [
            Line(start=seg.start, end=seg.end, text=seg.text)
            for seg in transcript.segments
            if seg.text and seg.end > clip.start - 60 and seg.start < clip.end + 60
        ]

    # --- Media ---

    @app.get("/media/vods/{vod_id}/proxy.mp4")
    def proxy(vod_id: int, s: Session = Depends(db)) -> FileResponse:
        vod = s.get(Vod, vod_id)
        if not vod:
            raise HTTPException(404, "No such video")
        path = workspace(vod).proxy_mp4
        if not path.exists():
            raise HTTPException(404, "The preview copy isn't ready yet")
        return FileResponse(path, media_type="video/mp4")

    @app.get("/media/clips/{clip_id}/thumb.jpg")
    def thumbnail(clip_id: int, s: Session = Depends(db)) -> FileResponse:
        clip, vod = get_clip(s, clip_id)
        ws = workspace(vod)
        t = clip.start + min(3.0, (clip.end - clip.start) * 0.2)
        out = ws.thumbs_dir / f"{clip.key}-{int(t * 10)}.jpg"
        if not out.exists():
            video = ws.proxy_mp4 if ws.proxy_mp4.exists() else ws.source
            render.render_thumbnail(video, t, out)
        return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    @app.get("/media/clips/{clip_id}/{which}.mp4")
    def clip_file(clip_id: int, which: str, download: bool = False, s: Session = Depends(db)) -> FileResponse:
        clip, _ = get_clip(s, clip_id)
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
