"""Transcribing in windows, against a stand-in for faster-whisper."""

import sys
import wave
from types import SimpleNamespace

import numpy as np

from clipper.config import Settings
from clipper.pipeline import audio, transcribe
from clipper.workspace import Workspace

LINES = range(0, 28, 4)  # someone says a 3-second line every 4 seconds


class FakeWhisper:
    """Hears the lines that fit whole in the audio it's given. The test audio holds each second's number,
    so it can tell where in the stream that audio starts."""

    calls: list[float] = []

    def __init__(self, *args, **kwargs):
        pass

    def transcribe(self, samples, **kwargs):
        start = round(samples[0] * 32768)
        length = len(samples) / audio.SAMPLE_RATE
        FakeWhisper.calls.append(length)
        pieces = [
            SimpleNamespace(start=line - start, end=line - start + 3, text=f" line {line}",
                            words=[SimpleNamespace(start=line - start, end=line - start + 1, word=" line")])
            for line in LINES if 0 <= line - start and line - start + 3 <= length
        ]
        return iter(pieces), SimpleNamespace(language="en", duration=length)


def test_long_audio_is_transcribed_in_windows_without_losing_or_repeating_lines(tmp_path, monkeypatch):
    ws = Workspace.open(tmp_path, "vod")
    seconds = 28
    with wave.open(str(ws.audio_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(audio.SAMPLE_RATE)
        w.writeframes(np.repeat(np.arange(seconds, dtype="<i2"), audio.SAMPLE_RATE).tobytes())
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeWhisper))
    monkeypatch.setattr(transcribe, "WINDOW_S", 10)
    monkeypatch.setattr(transcribe, "OVERLAP_S", 3)
    FakeWhisper.calls = []
    progress = []

    result = transcribe.run(ws, Settings(language="en"), on_progress=progress.append)

    assert [s.text for s in result.segments] == [f"line {n}" for n in LINES]
    assert [(s.start, s.end) for s in result.segments] == [(n, n + 3) for n in LINES]
    assert [w.start for s in result.segments for w in s.words] == list(LINES)
    assert result.duration == seconds
    assert max(FakeWhisper.calls) <= 13  # never more than a window and its overlap at once
    assert progress == sorted(progress) and progress[-1] == 1.0
    assert ws.transcript_json.exists()


def test_part_of_the_audio_can_be_loaded(tmp_path):
    ws = Workspace.open(tmp_path, "vod")
    with wave.open(str(ws.audio_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(audio.SAMPLE_RATE)
        w.writeframes(np.arange(5 * audio.SAMPLE_RATE, dtype="<i2").tobytes())
    part = audio.load(ws, start_s=1, duration_s=2)
    assert len(part) == 2 * audio.SAMPLE_RATE and round(part[0] * 32768) == audio.SAMPLE_RATE
    assert len(audio.load(ws, start_s=4, duration_s=10)) == audio.SAMPLE_RATE  # cut at the end
    assert audio.duration(ws) == 5
