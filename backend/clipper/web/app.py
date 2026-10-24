"""JSON API for the review app, plus media streaming and the built phone app."""

import logging
import mimetypes
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlmodel import Session, col, select

from ..config import Settings, get_settings
from ..db import Channel, ChannelVideo, ClipRecord, Destination, Show, UploadJob, Vod, get_engine, queue_vod, utcnow
from ..models import Transcript
from ..monitor import ChannelUrlError, parse_channel
from ..studio import Studio, launch_desktop_browser, read_status, resolve_channel_id, write_status
from ..pipeline import render
from ..workspace import Workspace
from .auth import COOKIE, MAX_AGE_S, Auth

log = logging.getLogger(__name__)

mimetypes.add_type("application/manifest+json", ".webmanifest")

MAX_SHORT_S = 180  # YouTube's limit for Shorts
REPO_WEB_DIR = Path(__file__).resolve().parents[3] / "frontend" / "dist"
PUBLIC_PATHS = {"/api/session", "/api/login"}
Mode = Literal["segment", "whole", "both"]


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
    error: str
    has_video: bool
    has_short: bool
    video_size: int  # bytes, 0 until rendered
    short_size: int
    destination_id: int | None
    destination_name: str
    destination_url: str
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


class DestinationIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(default="", max_length=500)


class DestinationOut(BaseModel):
    id: int
    name: str
    url: str
    channel_id: str  # empty if it couldn't be found from the link
    studio_upload_url: str  # where to upload by hand


class UploadRequest(BaseModel):
    which: Literal["video", "short"] = "video"
    visibility: Literal["public", "unlisted", "private"] = "public"


class UploadJobOut(BaseModel):
    id: int
    clip_id: int
    which: str
    visibility: str
    status: str
    progress: float
    youtube_url: str
    error: str
    has_screenshot: bool
    created_at: str


class Uploaded(BaseModel):
    youtube_url: str = Field(default="", max_length=500)


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


class Login(BaseModel):
    password: str


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def studio_upload_url(channel_id: str) -> str:
    """YouTube Studio's upload page, straight on the right channel when its UC… id is known."""
    return f"https://studio.youtube.com/channel/{channel_id}/videos/upload?d=ud" if channel_id else "https://www.youtube.com/upload"


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
        return VodOut(**vod.model_dump(exclude={"retry_at", "created_at", "updated_at", "workspace"}),
                      show_name=show.name if show else "",
                      retry_at=iso(vod.retry_at), created_at=iso(vod.created_at), counts=counts)

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
        return ShowOut(**show.model_dump(exclude={"created_at"}),
                       channels=[channel_out(s, c) for c in channels], video_count=videos)

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
                              studio_upload_url=studio_upload_url(d.channel_id))

    def job_out(job: UploadJob) -> UploadJobOut:
        return UploadJobOut(**job.model_dump(exclude={"screenshot", "created_at", "updated_at"}),
                            has_screenshot=bool(job.screenshot) and Path(job.screenshot).exists(),
                            created_at=iso(job.created_at))

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
            dest.channel_id = resolve_channel_id(url) if url else ""
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
                    show.clip_destination_id = None
                if show.episode_destination_id == dest_id:
                    show.episode_destination_id = None
                s.add(show)
        for clip in s.exec(select(ClipRecord).where(ClipRecord.destination_id == dest_id)):
            clip.destination_id = None
            s.add(clip)
        s.flush()
        s.delete(dest)
        s.commit()

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
        # Links pasted from this channel belong to the show too.
        show = s.get(Show, show_id)
        term = url.split("://", 1)[1].removeprefix("www.")
        if term.lower() not in show.terms():
            show.match = "\n".join(filter(None, [show.match.strip(), term]))
            s.add(show)
        s.commit()
        s.refresh(ch)
        return channel_out(s, ch)

    @app.patch("/api/channels/{channel_id}")
    def update_channel(channel_id: int, patch: ChannelPatch, s: Session = Depends(db)) -> ChannelOut:
        ch = found(s, Channel, channel_id, "channel")
        for k, v in patch.model_dump(exclude_unset=True).items():
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
        return set_status(s, clip, vod, "approved")

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

    # --- Uploading through YouTube Studio (done by the worker; see studio.py) ---

    @app.get("/api/studio")
    def studio_status() -> dict:
        """What the worker last saw of the upload browser, and where to open it to sign in."""
        status = read_status(settings.studio_dir / "status.json")
        checked = status.get("checked_at")
        return {
            "browser": bool(status.get("browser")),
            "signed_in": bool(status.get("signed_in")),
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
        clip.status, clip.uploaded_at, clip.updated_at = "rendered", None, utcnow()
        s.add(clip)
        s.commit()
        s.refresh(clip)
        return clip_out(clip, vod)

    @app.post("/api/clips/{clip_id}/reject")
    def reject(clip_id: int, s: Session = Depends(db)) -> ClipOut:
        clip, vod = get_clip(s, clip_id)
        return set_status(s, clip, vod, "rejected")

    @app.post("/api/clips/{clip_id}/restore")
    def restore(clip_id: int, s: Session = Depends(db)) -> ClipOut:
        """Back to the inbox (undo for approve and reject)."""
        clip, vod = get_clip(s, clip_id)
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
