"""Extract the soundtrack as 16 kHz mono WAV, the format Whisper and the loudness analysis both use."""

import logging
import wave

import numpy as np

from .. import ffmpeg
from ..workspace import Workspace

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
# Silence fills a gap in the soundtrack's timestamps (a stream that dropped and came back) and a start after
# the picture's, so a time in the WAV, and so in the transcript, is that moment in the video, where clips are
# cut. A WAV keeps no timestamps: without this, every word after a gap was timed early (see timing.py).
VIDEO_TIMING = "aresample=async=1:first_pts=0"


def extract(ws: Workspace) -> None:
    if ws.audio_wav.exists():
        return
    log.info("Extracting audio")
    tmp = ws.audio_wav.with_name("audio.tmp.wav")
    ffmpeg.run(["-i", str(ws.source), "-vn", "-af", VIDEO_TIMING, "-ac", "1", "-ar", str(SAMPLE_RATE),
                "-c:a", "pcm_s16le", str(tmp)])
    tmp.replace(ws.audio_wav)


def duration(ws: Workspace) -> float:
    with _open(ws) as w:
        return w.getnframes() / SAMPLE_RATE


def load(ws: Workspace, start_s: float = 0, duration_s: float | None = None) -> np.ndarray:
    """The soundtrack, or `duration_s` of it from `start_s`, as float32 samples in [-1, 1], which Whisper
    takes directly. All of an 8-hour stream is about 1.8 GB, so transcription loads it a part at a time.

    Read in chunks so the peak is the final array's size, not double.
    """
    with _open(ws) as w:
        first = min(w.getnframes(), round(start_s * SAMPLE_RATE))
        count = w.getnframes() - first
        if duration_s is not None:
            count = min(count, round(duration_s * SAMPLE_RATE))
        w.setpos(first)
        samples = np.empty(count, dtype=np.float32)
        pos = 0
        while pos < count and (frames := w.readframes(min(SAMPLE_RATE * 600, count - pos))):
            chunk = np.frombuffer(frames, dtype="<i2")
            samples[pos : pos + len(chunk)] = chunk
            pos += len(chunk)
    samples /= 32768
    return samples[:pos]


FULL_READ_SAMPLES = 32_000_000  # up to about half an hour is read whole; longer stretches are sampled


def peaks(ws: Workspace, start_s: float, end_s: float, count: int) -> list[float]:
    """How loud the soundtrack is across `count` equal parts of `start_s` to `end_s`, from 0 to 1: the loudest
    sample in each, for drawing its waveform. Over a long stretch (zoomed out) each part's loudness is taken from
    its first stretch of samples, so it stays quick; past the end of the soundtrack it's 0."""
    with _open(ws) as w:
        total = w.getnframes()
        first = max(0, round(start_s * SAMPLE_RATE))
        span = max(0, round(end_s * SAMPLE_RATE) - first)
        if count <= 0 or span == 0:
            return [0.0] * max(0, count)
        per = span / count
        found = []
        if span <= FULL_READ_SAMPLES:
            w.setpos(min(first, total))
            data = np.abs(np.frombuffer(w.readframes(span), dtype="<i2").astype(np.int32))
            for i in range(count):
                part = data[int(i * per) : max(int(i * per) + 1, int((i + 1) * per))]
                found.append(int(part.max()) if part.size else 0)
        else:
            chunk = max(1, min(int(per), FULL_READ_SAMPLES // count))
            for i in range(count):
                at = first + int(i * per)
                if at >= total:
                    found.append(0)
                    continue
                w.setpos(at)
                part = np.frombuffer(w.readframes(chunk), dtype="<i2")
                found.append(int(np.abs(part.astype(np.int32)).max()) if part.size else 0)
    return [round(min(1.0, v / 32768), 3) for v in found]


def _open(ws: Workspace) -> wave.Wave_read:
    w = wave.open(str(ws.audio_wav), "rb")
    if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != SAMPLE_RATE:
        w.close()
        raise ValueError(f"{ws.audio_wav} must be 16-bit mono at {SAMPLE_RATE} Hz")
    return w
