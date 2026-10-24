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


def load(ws: Workspace) -> np.ndarray:
    """The soundtrack as float32 samples in [-1, 1], which Whisper takes directly.

    Read in chunks so an 8-hour stream peaks at the final array's size (about 1.8 GB), not double.
    """
    with wave.open(str(ws.audio_wav), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != SAMPLE_RATE:
            raise ValueError(f"{ws.audio_wav} must be 16-bit mono at {SAMPLE_RATE} Hz")
        samples = np.empty(w.getnframes(), dtype=np.float32)
        pos = 0
        while frames := w.readframes(SAMPLE_RATE * 600):
            chunk = np.frombuffer(frames, dtype="<i2")
            samples[pos : pos + len(chunk)] = chunk
            pos += len(chunk)
    samples /= 32768
    return samples[:pos]
