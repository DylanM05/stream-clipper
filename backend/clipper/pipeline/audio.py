"""Extract the soundtrack as 16 kHz mono WAV, the format Whisper and the loudness analysis both use."""

import logging
import os
import threading
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


OVERVIEW_RATE = 50  # loudness readings a second in the overview of a whole soundtrack
_overview_lock = threading.Lock()


def overview_ready(ws: Workspace) -> bool:
    """Whether the overview is there, and newer than the soundtrack."""
    return ws.peaks_u8.exists() and ws.peaks_u8.stat().st_mtime >= ws.audio_wav.stat().st_mtime


def overview(ws: Workspace) -> np.ndarray:
    """How loud the soundtrack is, 50 readings a second: each the loudest sample in its fiftieth of a second, as a
    byte from 0 to 255. Worked out once, a minute of sound at a time, and kept beside the soundtrack (a byte each, so
    an 8-hour stream's is under 1.5 MB), and again if the soundtrack is extracted again."""
    with _overview_lock:
        if not overview_ready(ws):
            block = SAMPLE_RATE // OVERVIEW_RATE
            levels = []
            with _open(ws) as w:
                while frames := w.readframes(SAMPLE_RATE * 60):
                    data = np.abs(np.frombuffer(frames, dtype="<i2").astype(np.int32))
                    whole = len(data) // block * block
                    loudest = data[:whole].reshape(-1, block).max(axis=1)
                    if whole < len(data):
                        loudest = np.append(loudest, data[whole:].max())
                    # Rounded up, so a sound that's there never shows as silence.
                    levels.append(np.minimum(255, (loudest * 255 + 32767) // 32768).astype(np.uint8))
            tmp = ws.peaks_u8.with_name("peaks.tmp.u8")
            (np.concatenate(levels) if levels else np.zeros(0, np.uint8)).tofile(tmp)
            os.replace(tmp, ws.peaks_u8)
    return np.fromfile(ws.peaks_u8, dtype=np.uint8)


def peaks(ws: Workspace, start_s: float, end_s: float, count: int) -> list[float]:
    """How loud the soundtrack is across `count` equal parts of `start_s` to `end_s`, from 0 to 1: the loudest
    sound in each, for drawing its waveform; past the end of the soundtrack it's 0. From the overview, unless the
    parts are shorter than its readings (zoomed right in), when that short stretch's own samples are read."""
    span = end_s - start_s
    if count <= 0 or span <= 0:
        return [0.0] * max(0, count)
    if span * OVERVIEW_RATE >= count:
        levels, rate, scale = overview(ws), OVERVIEW_RATE, 255
        offset = start_s * rate  # where start_s is in the readings
    else:  # shorter than count / 50 seconds: at most 80 s, a couple of MB of samples
        with _open(ws) as w:
            first = min(w.getnframes(), max(0, int(start_s * SAMPLE_RATE)))
            w.setpos(first)
            samples = np.frombuffer(w.readframes(int(np.ceil(span * SAMPLE_RATE)) + 1), dtype="<i2")
        levels, rate, scale = np.abs(samples.astype(np.int32)), SAMPLE_RATE, 32768
        offset = start_s * rate - first
    per = span * rate / count
    found = []
    for i in range(count):
        a = max(0, int(offset + i * per))
        part = levels[a : max(a + 1, int(np.ceil(offset + (i + 1) * per)))]
        found.append(round(min(1.0, int(part.max()) / scale), 3) if part.size else 0.0)
    return found


def _open(ws: Workspace) -> wave.Wave_read:
    w = wave.open(str(ws.audio_wav), "rb")
    if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != SAMPLE_RATE:
        w.close()
        raise ValueError(f"{ws.audio_wav} must be 16-bit mono at {SAMPLE_RATE} Hz")
    return w
