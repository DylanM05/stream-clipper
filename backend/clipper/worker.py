"""Background worker: checks watched channels, processes queued videos, renders approved clips and
uploads them through YouTube Studio. Run with `clipper worker`.

Each job runs on its own thread, so approving a clip doesn't wait for an 8-hour video to finish
transcribing. The heavy work happens in ffmpeg and Whisper, outside Python's GIL.
"""

import html
import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from sqlmodel import Session, col, or_, and_, select

from . import titles
from .config import Settings
from .db import VOD_ACTIVE, ClipRecord, Destination, Show, UploadJob, Vod, find_show, get_engine, utcnow
from .ffmpeg import probe_duration
from .llm import LLMError, UsageLimitError
from .models import Clip, Segmentation, Transcript
from .monitor import Monitor
from .pipeline import audio, download, refine, render, segment, signals, transcribe
from .studio import Studio, StudioError, Upload, write_status
from .timecode import to_seconds
from .workspace import Workspace

log = logging.getLogger(__name__)

USAGE_LIMIT_RETRY = timedelta(minutes=30)  # when Claude doesn't say when the limit resets


class Worker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.engine = get_engine(settings)
        self.monitor = Monitor(settings)
        self.studio = Studio(settings.studio_cdp_url, settings.studio_dir, desktop_profile=settings.browser_profile_dir)
        self.stopping = threading.Event()
        self._studio_checked = 0.0

    def run(self) -> None:
        self.recover()
        threads = [
            threading.Thread(target=self._loop, args=(self.process_next_vod,), name="videos", daemon=True),
            threading.Thread(target=self._loop, args=(self.render_next_clip,), name="renders", daemon=True),
            threading.Thread(target=self._loop, args=(self.monitor.check_next, 15), name="channels", daemon=True),
            threading.Thread(target=self._loop, args=(self.upload_next, 10), name="uploads", daemon=True),
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
            vod = s.exec(
                select(Vod)
                .where(or_(Vod.status == "queued", and_(Vod.status == "waiting", col(Vod.retry_at) <= utcnow())))
                .order_by(col(Vod.created_at))
            ).first()
            if not vod:
                return False
            vod_id = vod.id
        self.process_vod(vod_id)
        return True

    def process_vod(self, vod_id: int) -> None:
        settings = self.settings
        vod = self._get(Vod, vod_id)
        log.info("Processing video %d (%s): %s", vod_id, vod.mode, vod.url)
        try:
            self._update_vod(vod_id, status="downloading", progress=0, error="", retry_at=None)
            ws = download.fetch(vod.url, settings, on_progress=self._progress(vod_id))
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
            clip_template = show.title_template if show else "{title}"
            episode_template = show.full_episode_template if show else "{title}"
            day = titles.format_date(titles.stream_date(meta.get("upload_date"), vod.created_at.date()))
            context = "\n\n".join(n.strip() for n in (show.notes if show else "", vod.notes) if n.strip())

            self._update_vod(vod_id, status="preparing", progress=0)
            render.render_proxy(ws, settings, on_progress=self._progress(vod_id))

            clips: list[Clip] = []
            episode_title = episode_summary = chapters = ""
            if mode in ("segment", "both"):
                audio.extract(ws)
                self._update_vod(vod_id, status="transcribing", progress=0)
                transcript = transcribe.run(ws, settings, on_progress=self._progress(vod_id))
                self._update_vod(vod_id, status="analysing", progress=0)
                hints = signals.run(ws, settings)
                self._update_vod(vod_id, status="segmenting", progress=0)
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

            self._add_clips(vod_id, clips, show)
            self._update_vod(vod_id, status="ready", progress=1)
            log.info("Video %d ready with %d clips", vod_id, len(clips))
        except UsageLimitError as e:
            retry_at = e.resets_at or utcnow() + USAGE_LIMIT_RETRY
            log.warning("Claude usage limit reached; video %d retries at %s", vod_id, retry_at)
            self._update_vod(vod_id, status="waiting", retry_at=retry_at, error=str(e)[:500])
        except Exception as e:
            log.exception("Video %d failed", vod_id)
            self._update_vod(vod_id, status="failed", error=f"{type(e).__name__}: {e}"[:2000])

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

    def _add_clips(self, vod_id: int, clips: list[Clip], show: Show | None) -> None:
        """Add clips the database doesn't have yet, so re-running a video never duplicates them."""
        with Session(self.engine) as s:
            existing = set(s.exec(select(ClipRecord.key).where(ClipRecord.vod_id == vod_id)))
            for c in clips:
                if c.id in existing:
                    continue
                destination = (show.episode_destination_id if c.kind == "whole" else show.clip_destination_id) if show else None
                s.add(ClipRecord(
                    vod_id=vod_id, key=c.id, kind=c.kind, start=c.start, end=c.end, title=c.title,
                    description=c.description, why=c.why, score=c.score, vertical_ok=c.vertical_ok,
                    make_short=c.kind == "highlight" and c.vertical_ok, destination_id=destination,
                ))
            s.commit()

    def _progress(self, vod_id: int) -> Callable[[float], None]:
        """A progress callback that writes to the database at most every 2 seconds."""
        last = [0.0]

        def report(fraction: float) -> None:
            now = time.monotonic()
            if now - last[0] >= 2 or fraction >= 1:
                last[0] = now
                self._update_vod(vod_id, progress=round(fraction, 3))

        return report

    # --- Renders ------------------------------------------------------------

    def render_next_clip(self) -> bool:
        with Session(self.engine) as s:
            clip = s.exec(
                select(ClipRecord).where(ClipRecord.status == "approved").order_by(col(ClipRecord.reviewed_at))
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
                render.render_landscape(source, pipeline_clip, video, settings)
                short = None
                if clip.make_short:
                    words = Transcript.model_validate_json(ws.transcript_json.read_text(encoding="utf-8")).words()
                    short = ws.clips_dir / f"{stem}-short.mp4"
                    render.render_short(source, pipeline_clip, words, short, settings)
            self._finish_render(clip_id, status="rendered", video_path=str(video), short_path=str(short or ""), error="")
        except Exception as e:
            log.exception("Rendering clip %d failed", clip_id)
            self._finish_render(clip_id, status="failed", error=f"{type(e).__name__}: {e}"[:2000])

    def _finish_render(self, clip_id: int, **fields) -> None:
        with Session(self.engine) as s:
            clip = s.get(ClipRecord, clip_id)
            if clip.status != "rendering":
                return  # edited, re-approved or rejected while rendering: leave the new status alone
            for k, v in fields.items():
                setattr(clip, k, v)
            clip.updated_at = utcnow()
            s.add(clip)
            s.commit()

    # --- Uploads through YouTube Studio ------------------------------------------

    def upload_next(self) -> bool:
        with Session(self.engine) as s:
            job = s.exec(select(UploadJob).where(UploadJob.status == "queued").order_by(col(UploadJob.created_at))).first()
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
            upload = Upload(
                file=Path(path).resolve() if path else Path(),
                title=clip.title,
                description=clip.description,
                visibility=job.visibility,
                channel_id=dest.channel_id if dest else "",
            )
        log.info("Uploading clip %d (%s) as %s", job.clip_id, job.which, job.visibility)
        try:
            if not path or not upload.file.exists():
                raise StudioError("The file isn't rendered (any more)")
            url = self.studio.upload(upload, on_progress=self._upload_progress(job_id))
        except StudioError as e:
            log.warning("Upload %d failed: %s", job_id, e)
            self._update_job(job_id, status="failed", error=str(e), screenshot=e.screenshot, youtube_url=e.link)
            return
        self._update_job(job_id, status="done", progress=1, youtube_url=url, error="")
        with Session(self.engine) as s:
            clip = s.get(ClipRecord, job.clip_id)
            if job.which == "video" or not clip.youtube_url:
                clip.youtube_url = url
            clip.status, clip.uploaded_at, clip.updated_at = "uploaded", utcnow(), utcnow()
            s.add(clip)
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

    # --- Helpers --------------------------------------------------------------

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
