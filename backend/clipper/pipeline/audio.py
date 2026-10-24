"""Extract the soundtrack as 16 kHz mono WAV, the format Whisper and the loudness analysis both use."""

import logging
import wave

import numpy as np

from .. import ffmpeg
from ..workspace import Workspace

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000


def extract(ws: Workspace) -> None:
    if ws.audio_wav.exists():
        return
    log.info("Extracting audio")
    tmp = ws.audio_wav.with_name("audio.tmp.wav")
    ffmpeg.run(["-i", str(ws.source), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(tmp)])
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


def _open(ws: Workspace) -> wave.Wave_read:
    w = wave.open(str(ws.audio_wav), "rb")
    if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != SAMPLE_RATE:
        w.close()
        raise ValueError(f"{ws.audio_wav} must be 16-bit mono at {SAMPLE_RATE} Hz")
    return w
