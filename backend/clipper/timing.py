"""Putting transcripts made before the soundtrack kept the video's timing (audio.VIDEO_TIMING) on it.

Until then the soundtrack was extracted with its pieces run together. In a stream that dropped and came
back (a gap in its timestamps), or whose sound started after its picture, every later word was timed
early: Shorts' captions ran ahead of the speech, and Claude's cut points came early by as much. That old
soundtrack is the decoded audio frames one after another, so counting their samples against their
timestamps shows where it jumped. `clipper timing-check` uses this.
"""

import bisect
from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg
from .models import Transcript, TranscriptSegment, Word

MIN_JUMP_S = 0.1  # less doesn't show in captions


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
