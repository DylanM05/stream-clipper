"""Cheap hints about where segments start: long silences, loudness spikes, big visual changes and the
platform's own chapters (Twitch game changes). They go to Claude alongside the transcript."""

import logging
import re
import wave
from pathlib import Path

import numpy as np

from .. import ffmpeg
from ..config import Settings
from ..models import LoudMoment, PlatformChapter, SceneCut, Signals, Span
from ..workspace import Workspace, write_text

log = logging.getLogger(__name__)


def run(ws: Workspace, settings: Settings, *, detect_scenes: bool = True) -> Signals:
    if ws.signals_json.exists():
        return Signals.model_validate_json(ws.signals_json.read_text(encoding="utf-8"))

    log.info("Analysing loudness and scene changes")
    levels = loudness_per_second(ws.audio_wav)
    signals = Signals(
        platform_chapters=[
            PlatformChapter(start=c["start_time"], end=c["end_time"], title=c.get("title") or "")
            for c in ws.meta().get("chapters") or []
        ],
        long_silences=find_silences(levels),
        loud_moments=find_loud_moments(levels),
        scene_cuts=detect_scene_cuts(ws.source) if detect_scenes else [],
    )
    write_text(ws.signals_json, signals.model_dump_json(indent=2))
    log.info(
        "Found %d platform chapters, %d long silences, %d loud moments, %d scene cuts",
        len(signals.platform_chapters), len(signals.long_silences),
        len(signals.loud_moments), len(signals.scene_cuts),
    )
    return signals


def loudness_per_second(wav_path: Path) -> np.ndarray:
    """Loudness of each second of a 16-bit mono WAV, in dBFS."""
    rms: list[np.ndarray] = []
    with wave.open(str(wav_path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(f"{wav_path} must be 16-bit mono")
        rate = w.getframerate()
        while frames := w.readframes(rate * 600):  # ten minutes at a time
            x = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768
            n = len(x) // rate
            if n:
                rms.append(np.sqrt(np.mean(x[: n * rate].reshape(n, rate) ** 2, axis=1)))
            if len(x) > n * rate:
                rms.append(np.sqrt(np.mean(x[n * rate:] ** 2, keepdims=True)))
    levels = np.concatenate(rms) if rms else np.zeros(0, dtype=np.float32)
    return 20 * np.log10(np.maximum(levels, 1e-5))


def find_silences(db: np.ndarray, threshold_db: float = -45, min_seconds: int = 8) -> list[Span]:
    spans = []
    start = None
    for i, quiet in enumerate(db < threshold_db):
        if quiet and start is None:
            start = i
        elif not quiet and start is not None:
            if i - start >= min_seconds:
                spans.append(Span(start=start, end=i))
            start = None
    if start is not None and len(db) - start >= min_seconds:
        spans.append(Span(start=start, end=len(db)))
    return spans


def find_loud_moments(
    db: np.ndarray, *, min_excess_db: float = 6.0, window_s: int = 300, min_gap_s: int = 30, limit: int = 60
) -> list[LoudMoment]:
    """Seconds noticeably louder than the surrounding five minutes: shouting, laughter, hype."""
    if len(db) < 10:
        return []
    smooth = np.convolve(np.pad(db, 1, mode="edge"), np.ones(3) / 3, mode="valid")
    half = window_s // 2
    windows = np.lib.stride_tricks.sliding_window_view(np.pad(db, half, mode="edge"), 2 * half + 1)
    # The floor stops speech right after a long silence from counting as a spike.
    baseline = np.maximum(np.median(windows, axis=1), np.median(db) - 6)
    excess = smooth - baseline

    picks: list[int] = []
    for i in np.argsort(excess)[::-1]:
        if excess[i] < min_excess_db or len(picks) >= limit:
            break
        if all(abs(int(i) - j) >= min_gap_s for j in picks):
            picks.append(int(i))
    return [LoudMoment(t=float(i), db_above=round(float(excess[i]), 1)) for i in sorted(picks)]


_PTS = re.compile(r"pts_time:(\d+(?:\.\d+)?)")
_SCENE = re.compile(r"lavfi\.scene_score=(\d+(?:\.\d+)?)")


def detect_scene_cuts(video: Path, threshold: float = 0.4, limit: int = 300) -> list[SceneCut]:
    """Big visual changes (switching games, BRB screens). Only keyframes are decoded, so 8 hours takes a minute or two."""
    log_text = ffmpeg.run([
        "-skip_frame", "nokey", "-i", str(video), "-an", "-sn",
        "-vf", f"scale=160:-2,select='gt(scene,{threshold})',metadata=print:key=lavfi.scene_score",
        "-f", "null", "-",
    ])
    return thin_cuts(parse_scene_log(log_text), limit=limit)


def parse_scene_log(text: str) -> list[SceneCut]:
    """Pair each `pts_time:` line from ffmpeg's metadata filter with the scene score printed after it."""
    cuts = []
    t = None
    for line in text.splitlines():
        if m := _PTS.search(line):
            t = float(m[1])
        elif (m := _SCENE.search(line)) and t is not None:
            cuts.append(SceneCut(t=round(t, 2), score=round(float(m[1]), 3)))
            t = None
    return cuts


def thin_cuts(cuts: list[SceneCut], min_gap_s: float = 10.0, limit: int = 300) -> list[SceneCut]:
    """Keep the strongest cut in each cluster of nearby cuts, then the `limit` strongest overall."""
    kept: list[SceneCut] = []
    for cut in sorted(cuts, key=lambda c: c.t):
        if kept and cut.t - kept[-1].t < min_gap_s:
            if cut.score > kept[-1].score:
                kept[-1] = cut
        else:
            kept.append(cut)
    strongest = sorted(kept, key=lambda c: c.score, reverse=True)[:limit]
    return sorted(strongest, key=lambda c: c.t)
