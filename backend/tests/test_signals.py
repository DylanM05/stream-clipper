import wave

import numpy as np

from clipper.models import SceneCut
from clipper.pipeline.signals import find_loud_moments, find_silences, loudness_per_second, parse_scene_log, thin_cuts

RATE = 16000


def write_wav(path, *parts: tuple[float, float]) -> None:
    """Noise sections of (seconds, loudness in dBFS); -inf gives silence."""
    rng = np.random.default_rng(0)
    chunks = []
    for seconds, db in parts:
        n = int(seconds * RATE)
        amp = 0.0 if db == float("-inf") else 10 ** (db / 20) * np.sqrt(3)  # uniform noise RMS = amp/sqrt(3)
        chunks.append(rng.uniform(-amp, amp, n) if amp else np.zeros(n))
    samples = (np.concatenate(chunks) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.tobytes())


def test_loudness_silences_and_spikes(tmp_path):
    wav = tmp_path / "audio.wav"
    write_wav(wav, (20, -30), (10, float("-inf")), (3, -8), (20.5, -30))
    db = loudness_per_second(wav)

    assert len(db) == 54  # 53.5 s: the last half second is its own entry
    assert abs(db[5] + 30) < 1
    assert db[25] < -90

    silences = find_silences(db)
    assert [(s.start, s.end) for s in silences] == [(20, 30)]

    loud = find_loud_moments(db)
    assert [m.t for m in loud] == [31.0]
    assert loud[0].db_above > 15


def test_parse_scene_log_pairs_times_with_scores():
    log = """
[Parsed_metadata_2 @ 0x55] frame:0    pts:12012   pts_time:12.012
[Parsed_metadata_2 @ 0x55] lavfi.scene_score=0.512000
[Parsed_metadata_2 @ 0x55] frame:1    pts:99099   pts_time:99.099
[Parsed_metadata_2 @ 0x55] lavfi.scene_score=0.950000
"""
    assert parse_scene_log(log) == [SceneCut(t=12.01, score=0.512), SceneCut(t=99.1, score=0.95)]


def test_thin_cuts_keeps_the_strongest_cut_per_cluster():
    cuts = [SceneCut(t=10, score=0.5), SceneCut(t=12, score=0.9), SceneCut(t=100, score=0.45), SceneCut(t=200, score=0.6)]
    assert thin_cuts(cuts) == [SceneCut(t=12, score=0.9), SceneCut(t=100, score=0.45), SceneCut(t=200, score=0.6)]
    assert thin_cuts(cuts, limit=2) == [SceneCut(t=12, score=0.9), SceneCut(t=200, score=0.6)]
