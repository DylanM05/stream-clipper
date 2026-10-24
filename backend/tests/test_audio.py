import wave

import numpy as np

from clipper.pipeline import audio
from clipper.workspace import Workspace


def test_load_returns_float_samples(tmp_path):
    ws = Workspace.open(tmp_path, "vod")
    samples = np.array([0, 16384, -32768, 32767], dtype="<i2")
    with wave.open(str(ws.audio_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(audio.SAMPLE_RATE)
        w.writeframes(samples.tobytes())

    loaded = audio.load(ws)
    assert loaded.dtype == np.float32
    assert loaded.tolist() == [0.0, 0.5, -1.0, 32767 / 32768]
