"""Putting transcripts made before the soundtrack kept the video's timing (audio.VIDEO_TIMING) on it.

Until then the soundtrack was extracted with its pieces run together. In a stream that dropped and came
back (a gap in its timestamps), or whose sound started after its picture, every later word was timed
early: Shorts' captions ran ahead of the speech, and Claude's cut points came early by as much. That old
soundtrack is the decoded audio frames one after another, so counting their samples against their
timestamps shows where it jumped. `clipper timing-check` uses this.
"""

import bisect
from dataclasses import dataclass, field
from pathlib import Path

from sqlmodel import Session, col, select

from . import ffmpeg
from .db import ClipRecord, UploadJob, utcnow
from .models import Transcript, TranscriptSegment, Word

MIN_JUMP_S = 0.1  # less doesn't show in captions
RENDERED = ("approved", "rendering", "rendered", "failed")  # a render queued, running or done


@dataclass(frozen=True)
class Jump:
    at: float  # seconds into the old soundtrack
    shift: float  # seconds to add to times from there on, to get the video's


@dataclass(frozen=True)
class Timing:
    """How a soundtrack extracted the old way lines up with its video."""

    jumps: list[Jump]
    length: float  # of the old soundtrack, in seconds

    def video_time(self, t: float) -> float:
        i = bisect.bisect_right([j.at for j in self.jumps], t) - 1
        return round(t + self.jumps[i].shift, 2) if i >= 0 else t

    def made_the_old_way(self, transcript: Transcript) -> bool | None:
        """Whether `transcript` came from the old soundtrack, going by its length; None if that can't tell."""
        old, new = abs(transcript.duration - self.length), abs(transcript.duration - self.video_time(self.length))
        return None if abs(old - new) < MIN_JUMP_S else old < new

    def fix(self, transcript: Transcript) -> Transcript:
        """`transcript`, made from the old soundtrack, on the video's timing."""
        t = self.video_time
        return Transcript(language=transcript.language, duration=t(transcript.duration), segments=[
            TranscriptSegment(start=t(s.start), end=t(s.end), text=s.text,
                              words=[Word(start=t(w.start), end=t(w.end), text=w.text) for w in s.words])
            for s in transcript.segments
        ])


@dataclass
class ClipsFixed:
    moved: list[ClipRecord] = field(default_factory=list)  # to where Claude meant them
    rendering: list[ClipRecord] = field(default_factory=list)  # again, for their cut points or Short's captions
    unqueued: list[ClipRecord] = field(default_factory=list)  # their queued uploads were taken out of the queue
    uploading: list[ClipRecord] = field(default_factory=list)  # right now, so left as they were


def fix_clips(s: Session, vod_id: int, found: Timing, picked: dict[str, tuple[float, float]]) -> ClipsFixed:
    """Move a video's clips, which Claude cut early by as much as its transcript was, unless they've been
    trimmed by hand (they're no longer where Claude `picked` them, by key) or uploaded. The ones rendered, or
    queued to be, render again, and so do trimmed ones with a Short, for its captions. As with the app's To
    inbox, that takes their queued uploads out of the queue, so none starts on a file being rendered."""
    fixed = ClipsFixed()
    clips = s.exec(select(ClipRecord).where(
        ClipRecord.vod_id == vod_id, ClipRecord.kind != "whole", ClipRecord.status != "uploaded",
        ClipRecord.end > found.jumps[0].at,  # before the sound first jumped, the timing was right
    ))
    for clip in clips:
        was = picked.get(clip.key)
        # Parts cut out in the app make it edited by hand too, even where its ends are still Claude's.
        untouched = bool(was) and abs(was[0] - clip.start) < 0.01 and abs(was[1] - clip.end) < 0.01 and not clip.cuts
        if not untouched and not (clip.make_short and clip.status in RENDERED):
            continue
        jobs = s.exec(select(UploadJob).where(UploadJob.clip_id == clip.id,
                                              col(UploadJob.status).in_(("queued", "uploading")))).all()
        if any(job.status == "uploading" for job in jobs):
            fixed.uploading.append(clip)
            continue
        if untouched:
            clip.start, clip.end = found.video_time(clip.start), found.video_time(clip.end)
            fixed.moved.append(clip)
        if clip.status in RENDERED:
            for job in jobs:
                s.delete(job)
            if jobs:
                fixed.unqueued.append(clip)
            clip.status, clip.error = "approved", ""
            fixed.rendering.append(clip)
        clip.updated_at = utcnow()
        s.add(clip)
    s.commit()
    return fixed


def measure(source: Path) -> Timing:
    """Where the soundtrack extracted the old way from `source` jumps against the video."""
    info = ffmpeg.probe(source)
    rate = next((int(s["sample_rate"]) for s in info["streams"] if s.get("codec_type") == "audio"), 0)
    if not rate:
        raise ValueError(f"{source} has no sound")
    start = float(info["format"].get("start_time") or 0)  # where the video's own times count from
    jumps: list[Jump] = []
    samples, shift = 0, 0.0
    for pts, count in ffmpeg.audio_frames(source):
        at = samples / rate
        if pts is not None and abs(pts - start - at - shift) > MIN_JUMP_S:
            shift = pts - start - at
            jumps.append(Jump(round(at, 3), round(shift, 3)))
        samples += count
    return Timing(jumps, samples / rate)
