"""Background worker: checks watched channels, processes queued videos, renders approved clips and
uploads them when their turn comes, through the YouTube API or YouTube Studio. It also makes thumbnails
and reads how uploads are doing. Run with `clipper worker`.

Each job runs on its own thread, so approving a clip doesn't wait for an 8-hour video to finish
transcribing. The heavy work happens in ffmpeg and Whisper, outside Python's GIL.

Downloads, processing, renders and uploads can each be paused in the app, and so can one video or clip.
Work that's running stops at its next progress report (a download keeps what it has; a step like
transcribing starts over) and carries on when resumed. The AI's step and the audio analysis can't be
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
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlmodel import Session, col, or_, and_, select

from . import cuts, ffmpeg, learning, notify, rumble_upload, stats, thumbnails, titles, transcripts
from .config import Settings
from .db import (
    CLIP_DONE, VOD_ACTIVE, ChannelVideo, ClipRecord, Destination, Show, UploadJob, Vod, deploy_held, find_show,
    get_app_setting, get_engine, is_paused, set_app_setting, upload_sites, utcnow,
)
from .ffmpeg import probe_duration
from .llm import LLMError, UsageLimitError
from .models import Clip, Segmentation, Transcript
from .monitor import MEMBERS_ONLY, Monitor
from .notify import Note
from .overlay import Overlay
from .pipeline import audio, download, refine, render, segment, signals, transcribe
from .rumble_upload import RumbleUpload, RumbleUploadError
from .schedule import upload_turns
from .studio import Studio, StudioError, Upload, write_status
from .thumbnails import ThumbnailError
from .timecode import to_seconds
from .workspace import Workspace, disk_free_gb
from .youtube import Google, Video, YouTubeError, video_id

log = logging.getLogger(__name__)

USAGE_LIMIT_RETRY = timedelta(minutes=30)  # when the AI doesn't say when its limit resets
LOW_SPACE_RETRY = timedelta(minutes=30)
CHECK_EVERY_S = 2  # how often running work records its progress and looks for a pause
STATS_EVERY_S = 1800
DIGEST_HOUR = 9  # Monday, in CLIPPER_TIMEZONE: the week's uploads and their views
MAX_AUTO_RETRIES = 3  # nights a failed upload is tried again by itself
QUOTA_TIMEZONE = "America/Los_Angeles"  # the YouTube API quota resets at midnight here


class Paused(Exception):
    """Raised from a progress report to stop work that was paused, or taken back, in the app."""


class UploadCancelled(Exception):
    """Raised from an upload's progress report once it's been cancelled in the app (UploadJob.cancel_requested)."""


class LimitChanged(Exception):
    """Raised from a download's progress report when the speed limit was changed in the app."""


class Preview(threading.Thread):
    """Makes a video's preview copy in the background while the AI picks clips (and, with
    CLIPPER_PREVIEW_WITH_TRANSCRIPTION, while it's transcribed), since neither needs it."""

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
        # A preview still being made for a video that stopped for another reason (e.g. the AI's usage limit)
        # carries on, and the video's next attempt picks it up rather than starting another.
        self._previews: dict[int, Preview] = {}
        self._prepared: set[int] = set()  # videos the editor's overviews are made for (or couldn't be), this run

    def run(self) -> None:
        self.recover()
        try:
            with Session(self.engine) as s:
                if filled := learning.backfill(s, self.settings):
                    log.info("Filled in how %d older clips were picked, to learn from what was changed in them", filled)
        except Exception:
            log.exception("Couldn't look up the AI's picks of older clips")
        threads = [
            threading.Thread(target=self._loop, args=(self.process_next_vod,), name="videos", daemon=True),
            threading.Thread(target=self._loop, args=(self.render_next_clip,), name="renders", daemon=True),
            threading.Thread(target=self._loop, args=(self.monitor.check_next, 15), name="channels", daemon=True),
            threading.Thread(target=self._loop, args=(self.upload_next, 10), name="uploads", daemon=True),
            threading.Thread(target=self._loop, args=(self.make_next_thumbnail, 10), name="thumbnails", daemon=True),
            threading.Thread(target=self._loop, args=(self.check_stats, STATS_EVERY_S), name="stats", daemon=True),
            threading.Thread(target=self._loop, args=(self.clean_up_next, 120), name="cleanup", daemon=True),
            threading.Thread(target=self._loop, args=(self.prepare_next_for_editor, 300), name="editor", daemon=True),
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
            for clip in s.exec(select(ClipRecord).where(ClipRecord.thumbnail_status == "making")):
                clip.thumbnail_status = "queued"
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
                ws = self._download(vod_id, vod.url)
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
            day = titles.format_date(
                titles.stream_date(meta, vod.created_at.date(), settings.timezone, settings.day_starts_at)
            )
            context = "\n\n".join(n.strip() for n in (show.notes if show else "", vod.notes) if n.strip())

            # A whole video has nothing else to do while its preview is made.
            early = settings.preview_with_transcription or mode == "whole"
            preview = self._preview(vod_id, ws) if early else None
            clips: list[Clip] = []
            episode_title = episode_summary = chapters = ""
            if mode in ("segment", "both"):
                self._start(vod_id, "transcribing", "processing")
                audio.extract(ws)
                transcript = transcribe.run(ws, settings, on_progress=self._progress(vod_id, "processing"))
                preview = preview or self._preview(vod_id, ws)
                self._start(vod_id, "analysing", "processing")
                video_type = show.video_type if show else "streams"
                # A TV episode or an edited video changes shot every few seconds, so its scene cuts would only be noise
                # to the AI. A stream's mark its BRB screens and game changes.
                hints = signals.run(ws, settings, detect_scenes=video_type == "streams")
                self._start(vod_id, "segmenting", "processing")
                segmentation = segment.run(
                    ws, settings, transcript, hints,
                    context=context,
                    guidance=show.clip_guidance if show else "",
                    title_room=titles.room_for_title(clip_template, day),
                    title_prefixed=clip_template != "{title}",
                    episode_room=titles.room_for_title(episode_template, day),
                    track_record=self._track_record(show),
                    breakdown=show.breakdown_categories() if show else None,
                    video_type=video_type,
                    offered_shorts=show.max_shorts if show else 5,
                )
                if show and show.save_transcripts:
                    self._save_transcript(show, vod, ws, transcript)
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

            # The clips don't need the preview: ones going ahead without review start rendering now, and the
            # inbox lists the rest. Only playing them in the app waits for it.
            added = self._add_clips(vod_id, clips, show)
            if summary := _added_summary(added):
                if preview.is_alive():
                    summary += "\nThe preview to watch them in the app is still being made."
                self._notify(Note(f"Ready: {name}", summary, link=f"videos/{vod_id}"))
            self._wait_for_preview(vod_id, preview)
            self._update_vod(vod_id, status="ready", progress=1)
            log.info("Video %d ready with %d clips", vod_id, len(clips))
        except Paused:
            log.info("Video %d paused", vod_id)
            if preview := self._previews.pop(vod_id, None):
                preview.stop.set()
                preview.join()  # so it's stopped before the video can start again
            self._update_vod(vod_id, status="queued", progress=0)
        except download.MembersOnly as e:
            self._drop_members_only(vod_id, str(e))
        except UsageLimitError as e:
            retry_at = e.resets_at or utcnow() + USAGE_LIMIT_RETRY
            log.warning("%s (%s); video %d retries at %s", e.note, e, vod_id, retry_at)
            self._update_vod(vod_id, status="waiting", retry_at=retry_at, error=e.note)
            if not was_waiting:
                self._notify(Note(f"Waiting for the AI: {name}", f"{e.note} It carries on by itself after that.",
                                  link=f"videos/{vod_id}", tone="quiet"))
        except Exception as e:
            log.exception("Video %d failed", vod_id)
            error = f"{type(e).__name__}: {e}"[:2000]
            self._update_vod(vod_id, status="failed", error=error)
            self._notify(Note(f"Failed: {name}", error[:500], link=f"videos/{vod_id}", tone="bad"))

    def _drop_members_only(self, vod_id: int, error: str) -> None:
        """A video for its channel's paying members, which the account downloading can't get. One a watched channel
        queued is removed without a word, and the channel lists it as skipped (it isn't queued again, nor does it use
        up a day's place in the backlog). One added by hand fails, so you see why, but nothing is sent about it."""
        with Session(self.engine) as s:
            vod = s.get(Vod, vod_id)
            seen = s.exec(select(ChannelVideo).where(ChannelVideo.vod_id == vod_id)).all()
            if not seen:
                vod.status, vod.error, vod.updated_at = "failed", f"Members only: {error}"[:2000], utcnow()
                s.add(vod)
                s.commit()
                log.info("Video %d is members-only: %s", vod_id, vod.url)
                return
            for row in seen:
                row.vod_id, row.status, row.reason, row.backlog_at, row.updated_at = None, "skipped", MEMBERS_ONLY, None, utcnow()
                s.add(row)
            s.delete(vod)  # not downloaded, so it has no folder or clips
            s.commit()
            log.info("Skipped video %d, members-only: %s", vod_id, vod.url)

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

    def download_limit(self, now: datetime | None = None) -> float:
        """Mbit/s cap for downloads; 0 means none, as it is during the full-speed hours."""
        local = now or datetime.now(ZoneInfo(self.settings.timezone))
        start, end = self.app_setting("full_speed_from"), self.app_setting("full_speed_until")
        if in_hours(local.hour + local.minute / 60, start, end):
            return 0
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

    def _save_transcript(self, show: Show, vod: Vod, ws: Workspace, transcript: Transcript) -> None:
        """Save the transcript, with the AI's breakdown if the show has one, for use outside the app. That's not what
        the stream is processed for, so a failure is only logged."""
        try:
            log.info("Saved video %d's transcript to %s", vod.id, transcripts.save(self.settings, show, vod, ws, transcript))
        except Exception:
            log.exception("Couldn't save video %d's transcript", vod.id)

    def _track_record(self, show: Show | None) -> str:
        """What the show's past clips teach the AI (learning.py); empty if there's nothing yet, or working it out
        went wrong, which shouldn't hold up the video."""
        if not show:
            return ""
        try:
            with Session(self.engine) as s:
                return learning.track_record(s, self.settings, show)
        except Exception:
            log.exception("Couldn't work out what the show's past clips teach the AI")
            return ""

    def _preview_title(self, text: str, room: int, notes: str) -> str:
        """The AI's short title for a whole video; empty if the AI isn't available right now."""
        if not text:
            return ""
        try:
            return titles.preview_title(text, room, self.settings, notes)
        except LLMError as e:
            log.warning("No title preview from the AI (%s); using the post's first line", e)
            return ""

    def _add_clips(self, vod_id: int, clips: list[Clip], show: Show | None) -> dict[str, int]:
        """Add clips the database doesn't have yet, so re-running a video never duplicates them.

        The full episode and the show's best chapters and highlights go to the inbox, or straight to
        rendering if the show takes their kind (or their score) without review; the rest are kept as
        extras. Returns how many were added with each status.
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
                elif show and show.auto_for_clip(c.kind, c.score) != "review":
                    status = "approved"
                else:
                    status = "proposed"
                # The AI's pick as offered, to learn from what the owner changes (learning.py).
                picked = c.kind != "whole"
                s.add(ClipRecord(
                    vod_id=vod_id, key=c.id, kind=c.kind, start=c.start, end=c.end, title=c.title,
                    description=c.description, why=c.why, score=c.score, vertical_ok=c.vertical_ok,
                    make_short=c.kind == "highlight" and c.vertical_ok, destination_id=destination, playlist_id=playlist,
                    status=status, reviewed_at=utcnow() if status == "approved" else None,
                    picked_start=c.start if picked else None, picked_end=c.end if picked else None,
                    picked_title=titles.own_part(show.title_template if show else "{title}", c.title) if picked else "",
                    was_extra=status == "extra" if picked else None,
                ))
                added[status] = added.get(status, 0) + 1
            s.commit()
        return added

    def _progress(self, vod_id: int, part: str, limit_mbps: float | None = None) -> Callable[[float], None]:
        """A progress callback that writes to the database at most every 2 seconds, and stops the work (by
        raising Paused) once the video or `part` of the work is paused. For a download at `limit_mbps`, it
        also stops it (raising LimitChanged) once the app's speed limit is something else."""
        last = [0.0]

        def report(fraction: float) -> None:
            now = time.monotonic()
            if now - last[0] >= CHECK_EVERY_S or fraction >= 1:
                last[0] = now
                self._update_vod(vod_id, progress=round(fraction, 3))
                self._check_paused(vod_id, part)
                if limit_mbps is not None and self.download_limit() != limit_mbps:
                    raise LimitChanged

        return report

    def _download(self, vod_id: int, url: str) -> Workspace:
        """Download at the app's speed limit. yt-dlp fixes the limit when it starts, so a change in the app
        stops the download, and it starts again at the new speed, carrying on from what it already has."""
        while True:
            limit = self.download_limit()
            try:
                return download.fetch(url, self.settings, on_progress=self._progress(vod_id, "downloads", limit),
                                      limit_mbps=limit)
            except LimitChanged:
                new = self.download_limit()
                log.info("Download speed limit changed to %s; carrying on at that", f"{new:g} Mbit/s" if new else "none")

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
            show = self._get(Show, vod.show_id) if vod.show_id else None
            overlay = Overlay.load(
                settings.show_overlay(show.id), show.overlay_box, show.overlay_screen,
                show.overlay_x, show.overlay_y, show.overlay_scale,
            ) if show else None
            if clip.kind == "whole" and not (overlay and show.overlay_episodes):
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
                    cuts=cuts.parse(clip.cuts),
                )
                video = ws.clips_dir / f"{stem}.mp4"
                render.render_landscape(source, pipeline_clip, video, settings, on_progress=check, overlay=overlay)
                short = None
                if clip.make_short:
                    words = Transcript.model_validate_json(ws.transcript_json.read_text(encoding="utf-8")).words()
                    short = ws.clips_dir / f"{stem}-short.mp4"
                    background = settings.short_background(show.id) if show else None
                    background = background if background and background.exists() else None
                    crop = show.short_crop(settings.short_layout, bool(background)) if show else settings.short_layout == "crop"
                    render.render_short(
                        source, pipeline_clip, words, short, settings, on_progress=check, crop=crop,
                        background=None if crop else background,
                        video_top=show.short_video_top if show else None,
                        video_width=show.short_video_width if show else 1.0,
                    )
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
            self._auto_thumbnail(clip_id)
            self._reupload(clip_id)
            self._auto_upload(clip_id)

    def _reupload(self, clip_id: int) -> None:
        """Queue the uploads a clip rendered again for its broken ones (app.reupload) is to make again."""
        with Session(self.engine) as s:
            clip = s.get(ClipRecord, clip_id)
            if not clip.reupload or clip.status != "rendered":
                return
            for which, site, visibility in json.loads(clip.reupload):
                s.add(UploadJob(clip_id=clip_id, which=which, site=site, visibility=visibility))
            clip.reupload = ""
            s.add(clip)
            s.commit()

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

    def _auto_thumbnail(self, clip_id: int) -> None:
        """Queue a thumbnail for a freshly rendered chapter or full episode whose show makes them for its kind,
        unless it has one."""
        if not self.settings.openrouter_api_key:
            return
        with Session(self.engine) as s:
            clip = s.get(ClipRecord, clip_id)
            vod = s.get(Vod, clip.vod_id)
            show = s.get(Show, vod.show_id) if vod.show_id else None
            wanted = show and {"chapter": show.chapter_thumbnails, "whole": show.episode_thumbnails}.get(clip.kind)
            if wanted and not clip.thumbnail_status:
                clip.thumbnail_status = "queued"
                s.add(clip)
                s.commit()

    def _auto_upload(self, clip_id: int) -> None:
        """Queue the upload of a freshly rendered clip whose show uploads its kind or score without review:
        for a highlight its Short if it has one, else the video. It goes where its channel's uploads go: YouTube,
        Rumble or both."""
        with Session(self.engine) as s:
            clip = s.get(ClipRecord, clip_id)
            vod = s.get(Vod, clip.vod_id)
            show = s.get(Show, vod.show_id) if vod.show_id else None
            if not show or show.auto_for_clip(clip.kind, clip.score) != "upload" or clip.status != "rendered":
                return
            which = "short" if clip.short_path else "video"
            if s.exec(select(UploadJob).where(UploadJob.clip_id == clip_id, UploadJob.which == which)).first():
                return  # uploaded or tried before: what happens next is up to you
            if clip.destination_id:
                dest = s.get(Destination, clip.destination_id)
                for site in upload_sites(show, clip.kind, dest):
                    s.add(UploadJob(clip_id=clip_id, which=which, site=site, visibility=show.auto_visibility))
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
        """Start the first queued upload whose turn has come (schedule.py), after queueing failed uploads whose
        automatic retry is due: their retry time has come, and their turn in the queue has too."""
        with Session(self.engine) as s:
            turns = upload_turns(s)
            due = [j for j in s.exec(select(UploadJob).where(UploadJob.status == "failed", col(UploadJob.retry_at).is_not(None)))
                   if (turn := turns.get(j.id)) and turn.at <= utcnow()]
            for failed in due:
                failed.retry_at, failed.updated_at = None, utcnow()
                clip = s.get(ClipRecord, failed.clip_id)
                # Not if that file went up (or is going up) another way since, or the clip was taken back.
                another = s.exec(select(UploadJob).where(
                    UploadJob.clip_id == failed.clip_id, UploadJob.which == failed.which, UploadJob.site == failed.site,
                    UploadJob.id != failed.id, col(UploadJob.status).in_(("queued", "uploading", "done")),
                )).first()
                if clip and clip.status in ("rendered", "uploaded") and not another:
                    log.info("Trying upload %d again by itself", failed.id)
                    failed.status, failed.error, failed.progress = "queued", "", 0
                    failed.auto_retries += 1
                s.add(failed)
            s.commit()
            job = None
            # Not while a deploy is about to restart the worker (cli deploy-wait).
            if not is_paused(s, "uploads") and not deploy_held(s):
                now = utcnow()
                job_id = next((jid for jid, turn in upload_turns(s, now).items() if turn.at <= now), None)
                job = s.get(UploadJob, job_id) if job_id else None
            if job:
                job.status, job.started_at, job.updated_at, job.progress_at, job.cancel_requested = "uploading", now, now, now, False
                s.add(job)
                s.commit()
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
        on_rumble = job.site == "rumble"
        through_api = not on_rumble and bool(self.youtube and dest and dest.youtube_token)
        method = "browser" if on_rumble else "api" if through_api else "studio"
        self._update_job(job_id, method=method)
        log.info("Uploading clip %d (%s) as %s %s", job.clip_id, job.which, job.visibility,
                 {"browser": "to Rumble", "api": "through the YouTube API", "studio": "through YouTube Studio"}[method])
        if not file or not file.exists():
            self._upload_failed(job, clip, "The file isn't rendered (any more)")
            return
        note = ""
        try:
            if on_rumble:
                url = self._upload_to_rumble(job, clip, dest, file)
            elif through_api:
                url, note = self._upload_through_api(job, clip, dest, file)
            else:
                upload = Upload(file=file, title=clip.title, description=clip.description, visibility=job.visibility,
                                channel_id=dest.channel_id if dest else "")
                url = self.studio.upload(upload, on_progress=self._upload_progress(job_id))
                missing = [what for what, wanted in (("playlist", clip.playlist_id), ("thumbnail", clip.thumbnail_status == "done"))
                           if wanted and job.which != "short"]
                if len(missing) == 1:
                    note = (f"The {missing[0]} only goes on by itself for channels connected to the YouTube API, so add "
                            "it in YouTube Studio.")
                elif missing:
                    note = ("The playlist and thumbnail only go on by themselves for channels connected to the YouTube "
                            "API, so add them in YouTube Studio.")
        except UploadCancelled:
            self._was_cancelled(job_id)
            return
        # The browser uploads turn whatever stopped them into their own error, a cancel included.
        except StudioError as e:
            if self._was_cancelled(job_id):
                return
            log.warning("Upload %d failed: %s", job_id, e)
            self._upload_failed(job, clip, str(e), retry="tomorrow" if e.retry else "", screenshot=e.screenshot,
                                youtube_url=e.link)
            return
        except RumbleUploadError as e:
            if self._was_cancelled(job_id):
                return
            log.warning("Upload %d failed: %s", job_id, e)
            self._upload_failed(job, clip, str(e), retry="tomorrow" if e.retry else "", screenshot=e.screenshot)
            return
        except YouTubeError as e:
            if self._was_cancelled(job_id):
                return
            log.warning("Upload %d failed: %s", job_id, e)
            if e.signed_out:
                self._update_destination(dest.id, youtube_error=str(e))
            self._upload_failed(job, clip, str(e), retry=e.retry)
            return
        self._update_job(job_id, status="done", progress=1, youtube_url=url, error=note, finished_at=utcnow())
        if through_api and dest.youtube_error:
            self._update_destination(dest.id, youtube_error="")  # e.g. a cancelled reconnect: the sign-in works
        with Session(self.engine) as s:
            row = s.get(ClipRecord, job.clip_id)
            field = "rumble_url" if on_rumble else "youtube_url"
            if job.which == "video" or not getattr(row, field):
                setattr(row, field, url)
            row.status, row.uploaded_at, row.updated_at = "uploaded", utcnow(), utcnow()
            # The file goes once it's up everywhere it was asked to go: the other site's upload may still need it.
            if not s.exec(select(UploadJob).where(UploadJob.clip_id == row.id, UploadJob.which == job.which,
                                                  UploadJob.status != "done")).first():
                self._delete_render(row, s.get(Vod, row.vod_id), job.which)
            s.add(row)
            s.commit()
        what = "Short" if job.which == "short" else "Full episode" if clip.kind == "whole" else "Video"
        where = " on Rumble" if on_rumble else f" on {dest.name}" if dest else ""
        self._notify(Note(f"Uploaded: {clip.title}", f"{what}, {job.visibility}{where}. {note}".strip(), link=url, tone="good"))

    def _upload_failed(self, job: UploadJob, clip: ClipRecord, error: str, retry: str = "", **fields) -> None:
        """Record a failed upload. One that could work later (`retry`, see youtube.RETRY_LATER) is tried again by
        itself at the next 12:01 AM, a few nights at most."""
        retry_at = retry_time(self.settings.timezone if retry != "quota" else QUOTA_TIMEZONE) if (
            retry and job.auto_retries < MAX_AUTO_RETRIES) else None
        self._update_job(job.id, status="failed", error=error, retry_at=retry_at, **fields)
        if retry_at:
            local = retry_at.astimezone(ZoneInfo(self.settings.timezone))
            error += f" It tries again by itself at {local.strftime('%I:%M %p').lstrip('0')} {local.tzname()}."
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
        """Upload with the channel's Google sign-in, then, unless it's the Short (Shorts never go in playlists or
        get thumbnails), add it to the clip's playlist and give it the clip's thumbnail. Returns the link, and a
        note if YouTube didn't use the visibility asked for (what Google does to uploads from projects it hasn't
        audited), or the playlist or thumbnail didn't work."""
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
        if clip.playlist_id and job.which != "short":
            try:
                self.youtube.add_to_playlist(dest.youtube_token, clip.playlist_id, result.video_id)
            except YouTubeError as e:
                log.warning("Couldn't add upload %d to its playlist: %s", job.id, e)
                notes.append(f"It isn't in the playlist: {e}")
        if job.which == "video" and clip.thumbnail_status == "done" and Path(clip.thumbnail_path).is_file():
            try:
                self.youtube.set_thumbnail(dest.youtube_token, result.video_id, Path(clip.thumbnail_path))
            except YouTubeError as e:
                log.warning("Couldn't set upload %d's thumbnail: %s", job.id, e)
                notes.append(f"It doesn't have its thumbnail: {e}")
        return result.url, " ".join(notes)

    def _upload_to_rumble(self, job: UploadJob, clip: ClipRecord, dest: Destination | None, file: Path) -> str:
        """Upload through Rumble's upload page, to the channel the upload browser is signed in to. Its video gets the
        clip's thumbnail if it has one, else a frame from a third of the way in, as does a Short."""
        own = Path(clip.thumbnail_path) if (job.which == "video" and clip.thumbnail_status == "done"
                                            and clip.thumbnail_path and Path(clip.thumbnail_path).is_file()) else None
        frame = None if own else file.with_name(f"{file.stem}.rumble-thumbnail.jpg")
        try:
            if frame:
                try:
                    ffmpeg.run(["-ss", f"{probe_duration(file) / 3:.3f}", "-i", str(file), "-frames:v", "1", "-q:v", "2",
                                str(frame)])
                except ffmpeg.FFmpegError as e:
                    raise RumbleUploadError(f"Couldn't take a frame of the video for its thumbnail: {e}"[:500], retry=True) from e
            return rumble_upload.upload(
                self.settings.studio_cdp_url,
                RumbleUpload(file=file, title=clip.title, description=clip.description, visibility=job.visibility,
                             category=dest.rumble_category if dest else "Entertainment", thumbnail=own or frame),
                self.settings.studio_dir,
                on_progress=self._upload_progress(job.id),
            )
        finally:
            if frame:
                frame.unlink(missing_ok=True)

    def _update_destination(self, dest_id: int, **fields) -> None:
        with Session(self.engine) as s:
            dest = s.get(Destination, dest_id)
            for k, v in fields.items():
                setattr(dest, k, v)
            s.add(dest)
            s.commit()

    def _upload_progress(self, job_id: int) -> Callable[[float], None]:
        """Records an upload's progress every few seconds, and when it last moved; stops it (UploadCancelled) once
        it's been cancelled in the app."""
        last = [0.0]

        def report(fraction: float) -> None:
            if time.monotonic() - last[0] >= 5 or fraction >= 1:
                last[0] = time.monotonic()
                with Session(self.engine) as s:
                    job = s.get(UploadJob, job_id)
                    if job.cancel_requested:
                        raise UploadCancelled(job_id)
                    if round(fraction, 3) != job.progress:
                        job.progress, job.progress_at = round(fraction, 3), utcnow()
                    job.updated_at = utcnow()
                    s.add(job)
                    s.commit()

        return report

    def _was_cancelled(self, job_id: int) -> bool:
        """Whether the upload was cancelled in the app while it was going up; if so, it's taken out of the queue."""
        with Session(self.engine) as s:
            job = s.get(UploadJob, job_id)
            if not (job and job.cancel_requested):
                return False
            s.delete(job)
            s.commit()
        log.info("Upload %d cancelled from the app", job_id)
        return True

    def _update_job(self, job_id: int, **fields) -> None:
        with Session(self.engine) as s:
            job = s.get(UploadJob, job_id)
            for k, v in fields.items():
                setattr(job, k, v)
            job.updated_at = utcnow()
            s.add(job)
            s.commit()

    # --- Thumbnails (thumbnails.py) -------------------------------------------------

    def make_next_thumbnail(self) -> bool:
        with Session(self.engine) as s:
            clip = s.exec(
                select(ClipRecord).where(ClipRecord.thumbnail_status == "queued").order_by(col(ClipRecord.updated_at))
            ).first()
            if not clip:
                return False
            clip.thumbnail_status, clip.thumbnail_error = "making", ""
            s.add(clip)
            s.commit()
            clip_id = clip.id
        self.make_thumbnail(clip_id)
        return True

    def make_thumbnail(self, clip_id: int) -> None:
        clip = self._get(ClipRecord, clip_id)
        vod = self._get(Vod, clip.vod_id)
        show = self._get(Show, vod.show_id) if vod.show_id else None
        notes = "\n\n".join(n.strip() for n in (show.notes if show else "", vod.notes) if n.strip())
        redraw = clip.thumbnail_redraw.strip()
        log.info("%s a thumbnail for clip %d (%r)", "Drawing again" if redraw else "Making", clip_id, clip.title)
        try:
            if not vod.workspace:
                raise ThumbnailError("This video hasn't been downloaded.")
            plan = None
            if redraw:  # the saved plan, with the prompt as you left it; the AI isn't asked again
                plan = (thumbnails.Idea.model_validate_json(clip.thumbnail_plan) if clip.thumbnail_plan
                        else thumbnails.Idea(people=[], concept="Drawn from your prompt.", text="", prompt=""))
                plan.prompt = redraw
            path, idea, cast = thumbnails.make(self.settings, clip, Workspace(self.settings.vods_dir / vod.workspace), notes,
                                               channel_picture=self._channel_picture, idea=plan,
                                               rules=show.thumbnail_guidance if show else "",
                                               on_idea=lambda idea: self._update_clip(
                                                   clip_id, thumbnail_plan=idea.model_dump_json()))
        except Exception as e:
            log.warning("No thumbnail for clip %d: %s", clip_id, e)
            error = str(e) if isinstance(e, (ThumbnailError, LLMError)) else f"{type(e).__name__}: {e}"
            # A clip that has a thumbnail keeps it, so a new one that didn't work out doesn't cost it the old.
            kept = bool(clip.thumbnail_path) and Path(clip.thumbnail_path).is_file()
            self._update_clip(clip_id, thumbnail_status="done" if kept else "failed", thumbnail_error=error[:1000],
                              thumbnail_redraw="")
            return
        text = f' Text: "{idea.text}".' if idea.text.strip() else ""
        people = f" People: {cast}." if cast else ""
        self._update_clip(clip_id, thumbnail_status="done", thumbnail_path=str(path),
                          thumbnail_idea=f"{idea.concept}{text}{people}", thumbnail_plan=idea.model_dump_json(),
                          thumbnail_redraw="")

    def prepare_next_for_editor(self) -> bool:
        """For a video ready to edit, newest first, make what the editor's zoomed-out timeline shows of it: the
        soundtrack's loudness overview and a still every 10 s of the preview. Each is a pass over the whole stream,
        so they're made here, once, rather than while the editor waits; stills for a zoomed-out view were each a run
        of ffmpeg reading the whole preview's index (about 180 MB for 8 hours), which on a small server froze it.
        Returns whether it made something."""
        with Session(self.engine) as s:
            vods = s.exec(
                select(Vod.id, Vod.workspace)
                .where(Vod.status == "ready", col(Vod.cleaned_at).is_(None), col(Vod.workspace).is_not(None))
                .order_by(col(Vod.created_at).desc())
            ).all()
        for vod_id, folder in vods:
            if vod_id in self._prepared:
                continue
            self._prepared.add(vod_id)
            ws = Workspace(self.settings.vods_dir / folder)
            made = False
            try:
                if ws.audio_wav.exists() and not audio.overview_ready(ws):
                    audio.overview(ws)
                    made = True
                if ws.proxy_mp4.exists() and not ws.stills_done.exists():
                    count = render.render_stills(ws.proxy_mp4, ws.frames_dir, render.STILL_EVERY_S)
                    ws.stills_done.write_text(f"{count}\n", encoding="utf-8")
                    log.info("Made the editor's overview of video %d (%d stills)", vod_id, count)
                    made = True
            except Exception as e:
                log.warning("Couldn't make the editor's overview of video %d: %s", vod_id, e)
            if made:
                return True
        return False

    def _channel_picture(self, name: str) -> tuple[str, str] | None:
        """For thumbnails: the picture of the YouTube channel a search for someone finds first. Uses the API key,
        else a connected channel's sign-in; None without either, or if the search fails."""
        key = self.settings.youtube_api_key
        with Session(self.engine) as s:
            token = "" if key or not self.youtube else next(
                (d.youtube_token for d in s.exec(select(Destination).where(Destination.youtube_token != ""))), "")
        if not (key or token):
            return None
        try:
            return (self.youtube or Google("", "")).channel_picture(name, refresh_token=token, api_key=key)
        except YouTubeError as e:
            log.info("Couldn't search YouTube for %s: %s", name, e)
            return None

    def _update_clip(self, clip_id: int, **fields) -> None:
        """Change a clip without touching its updated_at, which marks changes to the clip itself."""
        with Session(self.engine) as s:
            clip = s.get(ClipRecord, clip_id)
            if clip is None:
                return
            for k, v in fields.items():
                setattr(clip, k, v)
            s.add(clip)
            s.commit()

    # --- How uploads do on YouTube (stats.py) ------------------------------------------

    def check_stats(self) -> bool:
        """Read the numbers of the uploads that are due, then send the weekly summary if it's time."""
        now = utcnow()
        with Session(self.engine) as s:
            dests = {d.id: d for d in s.exec(select(Destination))}
            done = s.exec(select(UploadJob, ClipRecord).join(ClipRecord).where(
                UploadJob.status == "done", UploadJob.youtube_url != "", UploadJob.site == "youtube",
            )).all()
        any_token = next((d.youtube_token for d in dests.values() if d.youtube_token), "") if self.youtube else ""
        # Read as the clip's own channel if it's connected, else with the API key, else as any connected channel.
        groups: dict[tuple[str, str], dict[str, list[int]]] = {}
        for job, clip in done:
            vid = video_id(job.youtube_url)
            if not vid or not stats.due(job, now):
                continue
            dest = dests.get(clip.destination_id)
            own = dest.youtube_token if dest and self.youtube else ""
            key = (own, "") if own else ("", self.settings.youtube_api_key) if self.settings.youtube_api_key else (any_token, "")
            if any(key):
                groups.setdefault(key, {}).setdefault(vid, []).append(job.id)
        google = self.youtube or Google("", "")
        for (token, api_key), by_video in groups.items():
            try:
                found = google.video_stats(list(by_video), refresh_token=token, api_key=api_key)
            except YouTubeError as e:
                log.warning("Couldn't read how uploads are doing: %s", e)
                continue
            with Session(self.engine) as s:
                for vid, job_ids in by_video.items():
                    for job_id in job_ids:
                        job = s.get(UploadJob, job_id)
                        if vid in found:
                            stats.record(job, found[vid], now)
                        else:
                            job.stats_at = now  # deleted or made private: check less often
                        s.add(job)
                s.commit()
        self.send_digest()
        return False

    def send_digest(self, local: datetime | None = None) -> None:
        """On Monday mornings, a notification with the past week's uploads and how they're doing."""
        local = local or datetime.now(ZoneInfo(self.settings.timezone))
        if local.weekday() != 0 or local.hour < DIGEST_HOUR or not notify.configured(self.settings):
            return
        week = local.strftime("%G-W%V")
        with Session(self.engine) as s:
            if get_app_setting(s, "stats_digest_week") == week:
                return
            set_app_setting(s, "stats_digest_week", week)
            s.commit()
            rows = s.exec(select(UploadJob, ClipRecord).join(ClipRecord).where(
                UploadJob.status == "done", UploadJob.site == "youtube",
                col(UploadJob.finished_at) >= utcnow() - timedelta(days=7),
            )).all()
        if rows:
            self._notify(Note("Last week on YouTube", stats.digest(rows), link="stats"))

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
                    .where(ClipRecord.vod_id == vod.id, or_(col(UploadJob.status).in_(("queued", "uploading")),
                                                            col(UploadJob.retry_at).is_not(None)))
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
        """Delete a stream's source, preview, audio and rendered clips; keep its transcript and the AI's picks."""
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


def retry_time(timezone: str, now: datetime | None = None) -> datetime:
    """The next 12:01 AM in `timezone`."""
    local = (now or utcnow()).astimezone(ZoneInfo(timezone))
    at = local.replace(hour=0, minute=1, second=0, microsecond=0)
    if at <= local:
        at += timedelta(days=1)  # by the clock on the wall, so a change to or from daylight saving time is allowed for
    return at.astimezone(ZoneInfo("UTC"))


def in_hours(hour: float, start: float, end: float) -> bool:
    """Whether `hour` of the day is from `start` up to `end`, which can run past midnight (22 to 6). The same
    hour for both means never."""
    if start == end:
        return False
    return start <= hour < end if start < end else hour >= start or hour < end


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
    """Every chapter the AI found (pre-show and breaks too), for the full episode's chapter list."""
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
