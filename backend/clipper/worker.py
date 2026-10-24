"""Background worker: checks watched channels, processes queued videos, renders approved clips and
uploads them, through the YouTube API or YouTube Studio. Run with `clipper worker`.

Each job runs on its own thread, so approving a clip doesn't wait for an 8-hour video to finish
transcribing. The heavy work happens in ffmpeg and Whisper, outside Python's GIL.

Downloads, processing, renders and uploads can each be paused in the app, and so can one video or clip.
Work that's running stops at its next progress report (a download keeps what it has; a step like
transcribing starts over) and carries on when resumed. Claude's step and the audio analysis can't be
stopped partway, so a pause takes effect after them.
"""

import hashlib
import html
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from sqlmodel import Session, col, or_, and_, select

from . import notify, titles
from .config import Settings
from .db import (
    CLIP_DONE, VOD_ACTIVE, ClipRecord, Destination, Show, UploadJob, Vod, find_show, get_app_setting, get_engine,
    is_paused, utcnow,
)
from .ffmpeg import probe_duration
from .llm import LLMError, UsageLimitError
from .models import Clip, Segmentation, Transcript
from .monitor import Monitor
from .notify import Note
from .pipeline import audio, download, refine, render, segment, signals, transcribe
from .studio import Studio, StudioError, Upload, write_status
from .timecode import to_seconds
from .workspace import Workspace, disk_free_gb
from .youtube import Google, Video, YouTubeError

log = logging.getLogger(__name__)

USAGE_LIMIT_RETRY = timedelta(minutes=30)  # when Claude doesn't say when the limit resets
LOW_SPACE_RETRY = timedelta(minutes=30)
CHECK_EVERY_S = 2  # how often running work records its progress and looks for a pause


class Paused(Exception):
    """Raised from a progress report to stop work that was paused, or taken back, in the app."""


class Preview(threading.Thread):
    """Makes a video's preview copy alongside its transcription and Claude's step, which don't need it. NVDEC
    and NVENC are separate parts of the GPU from what Whisper uses, so the two barely slow each other down."""

    def __init__(self, worker: "Worker", vod_id: int, ws: Workspace):
        super().__init__(name=f"preview-{vod_id}", daemon=True)
        self.worker, self.vod_id, self.ws = worker, vod_id, ws
        self.fraction = 0.0
        self.error: BaseException | None = None
        self.stop = threading.Event()  # set when the video is paused
        self._checked = 0.0

    def run(self) -> None:
        try:
            render.render_proxy(self.ws, self.worker.settings, on_progress=self.report)
            self.fraction = 1.0
        except BaseException as e:  # passed on to the video's own thread
            self.error = e

    def report(self, fraction: float) -> None:
        self.fraction = fraction
        if self.stop.is_set():
            raise Paused
        if time.monotonic() - self._checked >= CHECK_EVERY_S:
            self._checked = time.monotonic()
            self.worker._check_paused(self.vod_id, "processing")


class Worker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.engine = get_engine(settings)
        self.monitor = Monitor(settings)
        self.studio = Studio(settings.studio_cdp_url, settings.studio_dir, desktop_profile=settings.browser_profile_dir)
        self.youtube = (
            Google(settings.google_client_id, settings.google_client_secret) if settings.google_client_id else None
        )
        self.stopping = threading.Event()
        self._studio_checked = 0.0
        # A preview still being made for a video that stopped for another reason (e.g. Claude's usage limit)
        # carries on, and the video's next attempt picks it up rather than starting another.
        self._previews: dict[int, Preview] = {}

    def run(self) -> None:
        self.recover()
        threads = [
            threading.Thread(target=self._loop, args=(self.process_next_vod,), name="videos", daemon=True),
            threading.Thread(target=self._loop, args=(self.render_next_clip,), name="renders", daemon=True),
            threading.Thread(target=self._loop, args=(self.monitor.check_next, 15), name="channels", daemon=True),
            threading.Thread(target=self._loop, args=(self.upload_next, 10), name="uploads", daemon=True),
            threading.Thread(target=self._loop, args=(self.clean_up_next, 120), name="cleanup", daemon=True),
        ]
        for t in threads:
            t.start()
        log.info("Worker started")
        try:
            while any(t.is_alive() for t in threads):
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Stopping after the current step")
            self.stopping.set()

    def recover(self) -> None:
        """Requeue work a previous worker was in the middle of when it stopped."""
        with Session(self.engine) as s:
            for vod in s.exec(select(Vod).where(col(Vod.status).in_(VOD_ACTIVE))):
                vod.status, vod.progress = "queued", 0
                s.add(vod)
            for clip in s.exec(select(ClipRecord).where(ClipRecord.status == "rendering")):
                clip.status = "approved"
                s.add(clip)
            for job in s.exec(select(UploadJob).where(UploadJob.status == "uploading")):
                if job.method == "api":
                    # YouTube keeps what it received, and the next attempt carries on from there (or finds
                    # it finished), so it can't post twice.
                    job.status = "queued"
                else:
                    # Not retried automatically: it may have finished, and retrying would post it twice.
                    job.status = "failed"
                    job.error = "The worker stopped during this upload. Check YouTube Studio before retrying."
                s.add(job)
            s.commit()

    def _loop(self, step: Callable[[], bool], idle_s: float | None = None) -> None:
        while not self.stopping.is_set():
            try:
                worked = step()
            except Exception:
                log.exception("Worker step failed")
                worked = False
            if not worked:
                self.stopping.wait(idle_s or self.settings.worker_poll_s)

    # --- Videos -------------------------------------------------------------

    def process_next_vod(self) -> bool:
        with Session(self.engine) as s:
            query = select(Vod).where(
                or_(Vod.status == "queued", and_(Vod.status == "waiting", col(Vod.retry_at) <= utcnow())),
                col(Vod.paused).is_not(True),
            )
            # A video that's downloaded only needs processing; one that isn't needs downloading first.
            if is_paused(s, "downloads"):
                query = query.where(col(Vod.workspace).is_not(None))
            if is_paused(s, "processing"):
                query = query.where(col(Vod.workspace).is_(None))
            vod = s.exec(query.order_by(col(Vod.created_at))).first()
            if not vod:
                return False
            vod_id = vod.id
        self.process_vod(vod_id)
        return True

    def process_vod(self, vod_id: int) -> None:
        settings = self.settings
        vod = self._get(Vod, vod_id)
        name = vod.title or vod.url
        was_waiting = vod.status == "waiting"  # already notified about why
        ws = Workspace(settings.vods_dir / vod.workspace) if vod.workspace else None
        downloaded = bool(ws and ws.meta_json.exists() and ws.source.exists())
        if not downloaded and (free := disk_free_gb(settings.data_dir)) < settings.min_free_gb:
            log.warning("Video %d waits: only %.0f GB free", vod_id, free)
            error = (f"Only {free:.0f} GB free on the data disk (it needs {settings.min_free_gb:g} GB to start a "
                     "download). It starts once cleanup or you free up space.")
            self._update_vod(vod_id, status="waiting", retry_at=utcnow() + LOW_SPACE_RETRY, error=error)
            if not was_waiting:
                self._notify(Note(f"Waiting for disk space: {name}", error, link=f"videos/{vod_id}", tone="bad"))
            return
        log.info("Processing video %d (%s): %s", vod_id, vod.mode, vod.url)
        try:
            if not downloaded:
                self._start(vod_id, "downloading", "downloads")
                ws = download.fetch(vod.url, settings, on_progress=self._progress(vod_id, "downloads"),
                                    limit_mbps=self.download_limit())
            meta = ws.meta()
            duration = meta.get("duration") or probe_duration(ws.source)
            show = self._show_for(vod, meta)
            mode = show.mode if show and vod.mode_auto else vod.mode
            self._update_vod(
                vod_id,
                workspace=ws.root.name,
                title=meta.get("title") or "",
                channel=meta.get("channel") or meta.get("uploader") or "",
                platform=meta.get("extractor_key") or "",
                upload_date=meta.get("upload_date") or "",
                duration=duration,
                show_id=show.id if show else None,
                mode=mode,
            )
            name = meta.get("title") or name
            clip_template = show.title_template if show else "{title}"
            episode_template = show.full_episode_template if show else "{title}"
            day = titles.format_date(titles.stream_date(meta.get("upload_date"), vod.created_at.date()))
            context = "\n\n".join(n.strip() for n in (show.notes if show else "", vod.notes) if n.strip())

            preview = self._preview(vod_id, ws)
            clips: list[Clip] = []
            episode_title = episode_summary = chapters = ""
            if mode in ("segment", "both"):
                audio.extract(ws)
                self._start(vod_id, "transcribing", "processing")
                transcript = transcribe.run(ws, settings, on_progress=self._progress(vod_id, "processing"))
                self._start(vod_id, "analysing", "processing")
                hints = signals.run(ws, settings)
                self._start(vod_id, "segmenting", "processing")
                segmentation = segment.run(
                    ws, settings, transcript, hints,
                    context=context,
                    guidance=show.clip_guidance if show else "",
                    title_room=titles.room_for_title(clip_template, day),
                    title_prefixed=clip_template != "{title}",
                    episode_room=titles.room_for_title(episode_template, day),
                )
                clips = refine.run(ws, segmentation, transcript)
                for c in clips:
                    c.title = titles.build(clip_template, c.title, day)
                episode_title, episode_summary = segmentation.episode_title, segmentation.episode_summary
                chapters = titles.chapter_list(_chapter_starts(segmentation))

            if mode in ("whole", "both"):
                text = post_text(meta)
                short = (
                    episode_title
                    or self._preview_title(text, titles.room_for_title(episode_template, day), context)
                    or first_line(text)
                    or vod.url
                )
                clips.insert(0, Clip(
                    id="full", kind="whole", start=0, end=round(duration, 2), score=1, llm_start="", llm_end="",
                    title=titles.build(episode_template, short, day),
                    description="\n\n".join(p for p in (episode_summary or text, chapters) if p),
                ))

            self._wait_for_preview(vod_id, preview)
            added = self._add_clips(vod_id, clips, show)
            self._update_vod(vod_id, status="ready", progress=1)
            log.info("Video %d ready with %d clips", vod_id, len(clips))
            if summary := _added_summary(added):
                self._notify(Note(f"Ready: {name}", summary, link=f"videos/{vod_id}"))
        except Paused:
            log.info("Video %d paused", vod_id)
            if preview := self._previews.pop(vod_id, None):
                preview.stop.set()
                preview.join()  # so it's stopped before the video can start again
            self._update_vod(vod_id, status="queued", progress=0)
        except UsageLimitError as e:
            retry_at = e.resets_at or utcnow() + USAGE_LIMIT_RETRY
            log.warning("Claude usage limit reached; video %d retries at %s", vod_id, retry_at)
            self._update_vod(vod_id, status="waiting", retry_at=retry_at, error="Claude's usage limit was reached.")
            if not was_waiting:
                self._notify(Note(f"Claude's usage limit: {name}", "It carries on by itself once the limit resets.",
                                  link=f"videos/{vod_id}", tone="quiet"))
        except Exception as e:
            log.exception("Video %d failed", vod_id)
            error = f"{type(e).__name__}: {e}"[:2000]
            self._update_vod(vod_id, status="failed", error=error)
            self._notify(Note(f"Failed: {name}", error[:500], link=f"videos/{vod_id}", tone="bad"))

    def _start(self, vod_id: int, status: str, part: str) -> None:
        """Begin a step of a video's processing, unless the video or that part of the work is paused."""
        self._check_paused(vod_id, part)
        self._update_vod(vod_id, status=status, progress=0, error="", retry_at=None)

    def _preview(self, vod_id: int, ws: Workspace) -> Preview:
        """Start making the preview copy in the background, or pick up the one an earlier attempt left running."""
        self._check_paused(vod_id, "processing")
        preview = self._previews.get(vod_id)
        if not preview or not preview.is_alive() or preview.stop.is_set():
            preview = self._previews[vod_id] = Preview(self, vod_id, ws)
            preview.start()
        return preview

    def _wait_for_preview(self, vod_id: int, preview: Preview) -> None:
        """Everything else is done: show the preview's progress until it's finished too."""
        if preview.is_alive():
            self._start(vod_id, "preparing", "processing")
            while preview.is_alive():
                self._update_vod(vod_id, progress=round(preview.fraction, 3))
                preview.join(CHECK_EVERY_S)
        self._previews.pop(vod_id, None)
        if preview.error:
            raise preview.error

    def _check_paused(self, vod_id: int, part: str) -> None:
        with Session(self.engine) as s:
            if s.get(Vod, vod_id).paused or is_paused(s, part):
                raise Paused

    def app_setting(self, key: str) -> float:
        """A number set in the app, else its CLIPPER_* default (the Settings field of the same name)."""
        with Session(self.engine) as s:
            value = get_app_setting(s, key)
        return float(value) if value is not None else float(getattr(self.settings, key))

    def download_limit(self) -> float:
        """Mbit/s cap for downloads; 0 means none."""
        return self.app_setting("download_limit_mbps")

    def _show_for(self, vod: Vod, meta: dict) -> Show | None:
        """The video's show: the one it was added under, or the first whose terms match its link or channel."""
        with Session(self.engine) as s:
            if vod.show_id:
                show = s.get(Show, vod.show_id)
            else:
                names = (meta.get("channel"), meta.get("uploader"), meta.get("uploader_id"))
                show = find_show(s.exec(select(Show).order_by(col(Show.id))), vod.url, names)
            if show:
                s.expunge(show)
            return show

    def _preview_title(self, text: str, room: int, notes: str) -> str:
        """Claude's short title for a whole video; empty if Claude isn't available right now."""
        if not text:
            return ""
        try:
            return titles.preview_title(text, room, self.settings, notes)
        except LLMError as e:
            log.warning("No title preview from Claude (%s); using the post's first line", e)
            return ""

    def _add_clips(self, vod_id: int, clips: list[Clip], show: Show | None) -> dict[str, int]:
        """Add clips the database doesn't have yet, so re-running a video never duplicates them.

        The full episode and the show's best chapters and highlights go to the inbox, or straight to
        rendering if the show takes their kind without review; the rest are kept as extras. Returns how
        many were added with each status.
        """
        offered = best_picks(clips, show)
        added: dict[str, int] = {}
        with Session(self.engine) as s:
            existing = set(s.exec(select(ClipRecord.key).where(ClipRecord.vod_id == vod_id)))
            for c in clips:
                if c.id in existing:
                    continue
                destination = (show.episode_destination_id if c.kind == "whole" else show.clip_destination_id) if show else None
                playlist = (show.episode_playlist_id if c.kind == "whole" else show.clip_playlist_id) if show else ""
                if c.id not in offered:
                    status = "extra"
                elif show and show.auto_for(c.kind) != "review":
                    status = "approved"
                else:
                    status = "proposed"
                s.add(ClipRecord(
                    vod_id=vod_id, key=c.id, kind=c.kind, start=c.start, end=c.end, title=c.title,
                    description=c.description, why=c.why, score=c.score, vertical_ok=c.vertical_ok,
                    make_short=c.kind == "highlight" and c.vertical_ok, destination_id=destination, playlist_id=playlist,
                    status=status, reviewed_at=utcnow() if status == "approved" else None,
                ))
                added[status] = added.get(status, 0) + 1
            s.commit()
        return added

    def _progress(self, vod_id: int, part: str) -> Callable[[float], None]:
        """A progress callback that writes to the database at most every 2 seconds, and stops the work (by
        raising Paused) once the video or `part` of the work is paused."""
        last = [0.0]

        def report(fraction: float) -> None:
            now = time.monotonic()
            if now - last[0] >= CHECK_EVERY_S or fraction >= 1:
                last[0] = now
                self._update_vod(vod_id, progress=round(fraction, 3))
                self._check_paused(vod_id, part)

        return report

    # --- Renders ------------------------------------------------------------

    def render_next_clip(self) -> bool:
        with Session(self.engine) as s:
            if is_paused(s, "renders"):
                return False
            clip = s.exec(
                select(ClipRecord)
                .where(ClipRecord.status == "approved", col(ClipRecord.paused).is_not(True))
                .order_by(col(ClipRecord.reviewed_at))
            ).first()
            if not clip:
                return False
            clip.status = "rendering"
            s.add(clip)
            s.commit()
            clip_id = clip.id
        self.render_clip(clip_id)
        return True

    def render_clip(self, clip_id: int) -> None:
        settings = self.settings
        clip = self._get(ClipRecord, clip_id)
        vod = self._get(Vod, clip.vod_id)
        log.info("Rendering clip %d (%s %r)", clip_id, clip.key, clip.title)
        try:
            ws = Workspace(settings.vods_dir / vod.workspace)
            check = self._render_check(clip_id)
            if clip.kind == "whole":
                video, short = ws.source, None  # posted as it is: no re-encode
            else:
                # Remove files from an earlier render; the title (and so the file name) may have changed.
                for old in (clip.video_path, clip.short_path):
                    if old and Path(old).parent == ws.clips_dir:
                        Path(old).unlink(missing_ok=True)
                stem = f"{clip.key}-{render.slugify(clip.title)}"
                ws.clips_dir.mkdir(exist_ok=True)
                source = ws.source.resolve()
                pipeline_clip = Clip(
                    id=clip.key, kind=clip.kind, start=clip.start, end=clip.end, title=clip.title,
                    description=clip.description, score=clip.score, llm_start="", llm_end="",
                )
                video = ws.clips_dir / f"{stem}.mp4"
                render.render_landscape(source, pipeline_clip, video, settings, on_progress=check)
                short = None
                if clip.make_short:
                    words = Transcript.model_validate_json(ws.transcript_json.read_text(encoding="utf-8")).words()
                    short = ws.clips_dir / f"{stem}-short.mp4"
                    render.render_short(source, pipeline_clip, words, short, settings, on_progress=check)
            rendered = self._finish_render(clip_id, status="rendered", video_path=str(video), short_path=str(short or ""), error="")
        except Paused:
            log.info("Stopped rendering clip %d", clip_id)
            for unfinished in (settings.vods_dir / vod.workspace / "clips").glob(f"{clip.key}-*.tmp.*"):
                unfinished.unlink(missing_ok=True)
            self._finish_render(clip_id, status="approved")  # back in the queue, unless it was changed in the app
            return
        except Exception as e:
            log.exception("Rendering clip %d failed", clip_id)
            error = f"{type(e).__name__}: {e}"[:2000]
            if self._finish_render(clip_id, status="failed", error=error):
                self._notify(Note(f"Render failed: {clip.title}", error[:500], link=f"clip/{clip_id}", tone="bad"))
            return
        if rendered:
            self._auto_upload(clip_id)

    def _render_check(self, clip_id: int) -> Callable[[float], None]:
        """A progress callback that stops a render (by raising Paused) once it's paused, or the clip is
        edited or taken back, in the app."""
        last = [0.0]

        def check(_fraction: float) -> None:
            if time.monotonic() - last[0] < CHECK_EVERY_S:
                return
            last[0] = time.monotonic()
            with Session(self.engine) as s:
                clip = s.get(ClipRecord, clip_id)
                if clip is None or clip.status != "rendering" or clip.paused or is_paused(s, "renders"):
                    raise Paused

        return check

    def _finish_render(self, clip_id: int, **fields) -> bool:
        """Record how a render ended. False if the clip changed in the meantime, which leaves it alone."""
        with Session(self.engine) as s:
            clip = s.get(ClipRecord, clip_id)
            if clip is None or clip.status != "rendering":
                return False  # edited, re-approved or rejected while rendering: leave the new status alone
            for k, v in fields.items():
                setattr(clip, k, v)
            clip.updated_at = utcnow()
            s.add(clip)
            s.commit()
            return True

    def _auto_upload(self, clip_id: int) -> None:
        """Queue the upload of a freshly rendered clip whose show uploads its kind without review: for a
        highlight its Short if it has one, else the video."""
        with Session(self.engine) as s:
            clip = s.get(ClipRecord, clip_id)
            vod = s.get(Vod, clip.vod_id)
            show = s.get(Show, vod.show_id) if vod.show_id else None
            if not show or show.auto_for(clip.kind) != "upload" or clip.status != "rendered":
                return
            which = "short" if clip.short_path else "video"
            if s.exec(select(UploadJob).where(UploadJob.clip_id == clip_id, UploadJob.which == which)).first():
                return  # uploaded or tried before: what happens next is up to you
            if clip.destination_id:
                s.add(UploadJob(clip_id=clip_id, which=which, visibility=show.auto_visibility))
                s.commit()
                return
            title, show_name = clip.title, show.name
        self._notify(Note(
            f"Not uploaded: {title}",
            f"{show_name} uploads these without review, but there's no YouTube channel set for them. Pick one on the clip.",
            link=f"upload/{clip_id}", tone="bad",
        ))

    # --- Uploads: through the YouTube API for connected channels, else YouTube Studio ----------

    def upload_next(self) -> bool:
        with Session(self.engine) as s:
            job = None if is_paused(s, "uploads") else (
                s.exec(select(UploadJob).where(UploadJob.status == "queued").order_by(col(UploadJob.created_at))).first()
            )
            if job:
                job.status, job.updated_at = "uploading", utcnow()
                s.add(job)
                s.commit()
                job_id = job.id
        if not job:
            self.report_studio_status()
            return False
        self.run_upload(job_id)
        self.report_studio_status(force=True)
        return True

    def report_studio_status(self, force: bool = False) -> None:
        """Tell the app whether the browser is up and signed in, at most every two minutes."""
        if not force and time.monotonic() - self._studio_checked < 120:
            return
        self._studio_checked = time.monotonic()
        write_status(self.settings.studio_dir / "status.json", self.studio.status())

    def run_upload(self, job_id: int) -> None:
        with Session(self.engine) as s:
            job = s.get(UploadJob, job_id)
            clip = s.get(ClipRecord, job.clip_id)
            dest = s.get(Destination, clip.destination_id) if clip.destination_id else None
            path = clip.short_path if job.which == "short" else clip.video_path
        file = Path(path).resolve() if path else None
        through_api = bool(self.youtube and dest and dest.youtube_token)
        self._update_job(job_id, method="api" if through_api else "studio")
        log.info("Uploading clip %d (%s) as %s through %s", job.clip_id, job.which, job.visibility,
                 "the YouTube API" if through_api else "YouTube Studio")
        if not file or not file.exists():
            self._upload_failed(job, clip, "The file isn't rendered (any more)")
            return
        note = ""
        try:
            if through_api:
                url, note = self._upload_through_api(job, clip, dest, file)
            else:
                upload = Upload(file=file, title=clip.title, description=clip.description, visibility=job.visibility,
                                channel_id=dest.channel_id if dest else "")
                url = self.studio.upload(upload, on_progress=self._upload_progress(job_id))
                if clip.playlist_id:
                    note = ("Playlists only work for channels connected to the YouTube API, so add it to the playlist "
                            "in YouTube Studio.")
        except StudioError as e:
            log.warning("Upload %d failed: %s", job_id, e)
            self._upload_failed(job, clip, str(e), screenshot=e.screenshot, youtube_url=e.link)
            return
        except YouTubeError as e:
            log.warning("Upload %d failed: %s", job_id, e)
            if e.signed_out:
                self._update_destination(dest.id, youtube_error=str(e))
            self._upload_failed(job, clip, str(e))
            return
        self._update_job(job_id, status="done", progress=1, youtube_url=url, error=note)
        if through_api and dest.youtube_error:
            self._update_destination(dest.id, youtube_error="")  # e.g. a cancelled reconnect: the sign-in works
        with Session(self.engine) as s:
            row = s.get(ClipRecord, job.clip_id)
            if job.which == "video" or not row.youtube_url:
                row.youtube_url = url
            row.status, row.uploaded_at, row.updated_at = "uploaded", utcnow(), utcnow()
            self._delete_render(row, s.get(Vod, row.vod_id), job.which)
            s.add(row)
            s.commit()
        what = "Short" if job.which == "short" else "Full episode" if clip.kind == "whole" else "Video"
        where = f" on {dest.name}" if dest else ""
        self._notify(Note(f"Uploaded: {clip.title}", f"{what}, {job.visibility}{where}. {note}".strip(), link=url, tone="good"))

    def _upload_failed(self, job: UploadJob, clip: ClipRecord, error: str, **fields) -> None:
        self._update_job(job.id, status="failed", error=error, **fields)
        self._notify(Note(f"Upload failed: {clip.title}", error, link=f"upload/{clip.id}", tone="bad"))

    def _delete_render(self, clip: ClipRecord, vod: Vod, which: str) -> None:
        """Delete a file once it's uploaded, if it's a render. A full episode's file is the stream itself, which
        other clips may still need; it goes with the rest of the stream's files at cleanup."""
        field = "short_path" if which == "short" else "video_path"
        path = getattr(clip, field)
        if path and vod.workspace and Path(path).parent.resolve() == (self.settings.vods_dir / vod.workspace / "clips").resolve():
            Path(path).unlink(missing_ok=True)
            setattr(clip, field, "")
            log.info("Deleted %s now that it's uploaded", Path(path).name)

    def _upload_through_api(self, job: UploadJob, clip: ClipRecord, dest: Destination, file: Path) -> tuple[str, str]:
        """Upload with the channel's Google sign-in, then add it to the clip's playlist. Returns the link, and a
        note if YouTube didn't use the visibility asked for (what Google does to uploads from projects it hasn't
        audited) or the playlist couldn't take it."""
        video = Video(file=file, title=clip.title, description=clip.description, visibility=job.visibility,
                      category_id=self.settings.youtube_category_id, short=job.which == "short")
        # An earlier attempt's session only carries on if the same file is going to the same place, the same way.
        key = hashlib.sha256(json.dumps(
            [dest.id, str(file), file.stat().st_size, video.title, video.description, video.visibility]
        ).encode()).hexdigest()[:16]
        result = self.youtube.upload(
            dest.youtube_token, video,
            on_progress=self._upload_progress(job.id),
            session_url=job.session_url if job.session_key == key else "",
            on_session=lambda url: self._update_job(job.id, session_url=url, session_key=key),
        )
        notes = []
        if result.privacy != job.visibility:
            notes.append(f"YouTube made this video {result.privacy}, not {job.visibility}. If that happens to every API "
                         "upload, the Google Cloud project needs YouTube's API audit (see the README). Change it in "
                         "YouTube Studio for now.")
        if clip.playlist_id:
            try:
                self.youtube.add_to_playlist(dest.youtube_token, clip.playlist_id, result.video_id)
            except YouTubeError as e:
                log.warning("Couldn't add upload %d to its playlist: %s", job.id, e)
                notes.append(f"It isn't in the playlist: {e}")
        return result.url, " ".join(notes)

    def _update_destination(self, dest_id: int, **fields) -> None:
        with Session(self.engine) as s:
            dest = s.get(Destination, dest_id)
            for k, v in fields.items():
                setattr(dest, k, v)
            s.add(dest)
            s.commit()

    def _upload_progress(self, job_id: int) -> Callable[[float], None]:
        last = [0.0]

        def report(fraction: float) -> None:
            if time.monotonic() - last[0] >= 5 or fraction >= 1:
                last[0] = time.monotonic()
                self._update_job(job_id, progress=round(fraction, 3))

        return report

    def _update_job(self, job_id: int, **fields) -> None:
        with Session(self.engine) as s:
            job = s.get(UploadJob, job_id)
            for k, v in fields.items():
                setattr(job, k, v)
            job.updated_at = utcnow()
            s.add(job)
            s.commit()

    # --- Disk cleanup --------------------------------------------------------------

    def clean_up_next(self) -> bool:
        """Delete the big files of one finished stream that's due. True if one was cleaned up.

        A stream is finished when every clip is uploaded, rejected or an extra and no upload is pending;
        it's due `cleanup_after_days` after the last change to it or its clips.
        """
        cutoff = utcnow() - timedelta(days=self.app_setting("cleanup_after_days"))
        with Session(self.engine) as s:
            candidates = s.exec(select(Vod).where(
                Vod.status == "ready", col(Vod.cleaned_at).is_(None), col(Vod.workspace).is_not(None),
            ))
            for vod in candidates:
                clips = s.exec(select(ClipRecord).where(ClipRecord.vod_id == vod.id)).all()
                if any(c.status not in CLIP_DONE for c in clips):
                    continue
                pending = s.exec(
                    select(UploadJob).join(ClipRecord)
                    .where(ClipRecord.vod_id == vod.id, col(UploadJob.status).in_(("queued", "uploading")))
                ).first()
                if pending or max([vod.updated_at, *(c.updated_at for c in clips)]) > cutoff:
                    continue
                vod_id = vod.id
                break
            else:
                return False
        self.clean_up(vod_id)
        return True

    def clean_up(self, vod_id: int) -> None:
        """Delete a stream's source, preview, audio and rendered clips; keep its transcript and Claude's picks."""
        vod = self._get(Vod, vod_id)
        freed = Workspace(self.settings.vods_dir / vod.workspace).delete_media()
        self._update_vod(vod_id, cleaned_at=utcnow())
        log.info("Cleaned up video %d (%s): freed %.1f GB", vod_id, vod.title, freed / 1e9)

    # --- Helpers --------------------------------------------------------------

    def _notify(self, note: Note) -> None:
        notify.send(self.settings, note)

    def _get(self, model, id_: int):
        with Session(self.engine) as s:
            row = s.get(model, id_)
            s.expunge(row)
            return row

    def _update_vod(self, vod_id: int, **fields) -> None:
        with Session(self.engine) as s:
            vod = s.get(Vod, vod_id)
            for k, v in fields.items():
                setattr(vod, k, v)
            vod.updated_at = utcnow()
            s.add(vod)
            s.commit()


def best_picks(clips: list[Clip], show: Show | None) -> set[str]:
    """Keys of the clips offered for upload: the full episode, and the show's best-scored chapters and
    highlights (every one, for a video that isn't part of a show)."""
    picks = {c.id for c in clips if c.kind == "whole"}
    for kind, limit in (("chapter", show and show.max_chapters), ("highlight", show and show.max_shorts)):
        ranked = sorted((c for c in clips if c.kind == kind), key=lambda c: c.score, reverse=True)
        picks |= {c.id for c in ranked[:limit]}
    return picks


def _added_summary(added: dict[str, int]) -> str:
    """E.g. "5 to review · 1 on its way without review · 14 extras"."""
    parts = [
        f"{n} {label}" if n == 1 else f"{n} {label_n}"
        for status, label, label_n in (
            ("proposed", "to review", "to review"),
            ("approved", "on its way without review", "on their way without review"),
            ("extra", "extra", "extras"),
        )
        if (n := added.get(status))
    ]
    return " · ".join(parts)


def _chapter_starts(segmentation: Segmentation) -> list[tuple[float, str]]:
    """Every chapter Claude found (pre-show and breaks too), for the full episode's chapter list."""
    starts = []
    for ch in segmentation.chapters:
        try:
            starts.append((to_seconds(ch.start), ch.title))
        except ValueError:
            continue
    return starts


def post_text(meta: dict) -> str:
    """The text that came with a post's video, without links. X puts the full text in the description."""
    text = html.unescape(meta.get("description") or meta.get("title") or "")  # X text arrives as "&amp;"
    return re.sub(r"https?://\S+", "", text).strip()


def first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")
