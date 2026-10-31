"""Transcripts, and so Shorts' captions, timed against the video, even where a stream's sound jumps."""

import shutil
import subprocess

import pytest
from sqlmodel import Session, select
from typer.testing import CliRunner

from clipper import cli, timing
from clipper.config import Settings
from clipper.db import ClipRecord, UploadJob, Vod, get_engine
from clipper.models import Transcript, TranscriptSegment, Word
from clipper.pipeline import audio
from clipper.timing import Jump, Timing
from clipper.workspace import Workspace, write_json

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")

GAP = "asetpts='if(gte(T,1),PTS+3/TB,PTS)'"  # the stream dropped for 3 s after its first second
LATE = "asetpts=PTS+1.5/TB"  # the sound starts 1.5 s after the picture


def make_stream(path, sound):
    """5 s of picture, and 2 s of tone whose timestamps `sound` changes."""
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=5", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-filter_complex", f"[1:a]{sound}[a]", "-map", "0:v", "-map", "[a]", "-c:v", "mpeg4", "-c:a", "aac", str(path),
    ], check=True)
    return path


@needs_ffmpeg
def test_the_soundtrack_keeps_the_videos_timing_where_the_sound_jumps(tmp_path):
    ws = Workspace.open(tmp_path, "vod")
    write_json(ws.meta_json, {"source_path": str(make_stream(ws.root / "source.mp4", GAP))})
    audio.extract(ws)
    assert audio.duration(ws) == pytest.approx(5, abs=0.05)  # not 2 s: the gap stays, as silence


@needs_ffmpeg
@pytest.mark.parametrize("sound, at, shift", [(GAP, 1.0, 3.0), (LATE, 0.0, 1.5)])
def test_where_a_soundtrack_extracted_the_old_way_jumped(tmp_path, sound, at, shift):
    found = timing.measure(make_stream(tmp_path / "source.mp4", sound))
    assert [(j.at, j.shift) for j in found.jumps] == [(pytest.approx(at, abs=0.05), pytest.approx(shift, abs=0.05))]
    assert found.length == pytest.approx(2, abs=0.05)  # the sound alone, run together


def test_a_transcript_from_the_old_soundtrack_goes_on_the_videos_timing():
    found = Timing([Jump(0.0, 1.5), Jump(60.0, 4.5)], length=100.0)
    old = Transcript(language="en", duration=100.0, segments=[TranscriptSegment(
        start=10.0, end=70.0, text="before after",
        words=[Word(start=10.0, end=10.5, text="before"), Word(start=69.5, end=70.0, text="after")],
    )])
    assert found.made_the_old_way(old) is True
    fixed = found.fix(old)
    assert [(w.start, w.end) for w in fixed.words()] == [(11.5, 12.0), (74.0, 74.5)]
    assert (fixed.segments[0].start, fixed.segments[0].end, fixed.duration) == (11.5, 74.5, 104.5)
    assert found.made_the_old_way(fixed) is False  # so it's never moved twice
    assert Timing([Jump(10.0, 2.0), Jump(20.0, 0.0)], length=100.0).made_the_old_way(old) is None  # jumps that cancel out


def test_timing_check_fixes_a_transcript_and_the_clips_not_trimmed_by_hand(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    ws = Workspace.open(settings.vods_dir, "kick-1")
    (ws.root / "source.mp4").write_bytes(b"video")
    write_json(ws.meta_json, {"source_path": str(ws.root / "source.mp4")})
    ws.transcript_json.write_text(Transcript(language="en", duration=100.0, segments=[TranscriptSegment(
        start=10.0, end=70.5, text="hi there",
        words=[Word(start=10.0, end=10.5, text="hi"), Word(start=70.0, end=70.5, text="there")],
    )]).model_dump_json(), encoding="utf-8")
    ws.audio_wav.write_bytes(b"the old soundtrack")
    # Where Claude picked them; hl03 has since been trimmed by hand.
    write_json(ws.segments_json, [{"id": key, "start": 65, "end": 80} for key in ("hl01", "hl02", "hl03", "hl05")]
               + [{"id": "hl04", "start": 20, "end": 40}])
    with Session(get_engine(settings)) as s:
        vod = Vod(url="https://kick.com/q/videos/1", workspace="kick-1", status="ready", title="Q After Dark")
        s.add(vod)
        s.commit()
        clips = [
            ClipRecord(vod_id=vod.id, key="hl01", kind="highlight", start=65, end=80, title="In the inbox",
                       picked_start=65, picked_end=80),
            ClipRecord(vod_id=vod.id, key="hl02", kind="highlight", start=65, end=80, title="Ready to go", status="rendered",
                       make_short=True, short_path="/data/hl02-short.mp4"),
            ClipRecord(vod_id=vod.id, key="hl03", kind="highlight", start=67, end=84, title="Trimmed", status="rendered",
                       make_short=True, short_path="/data/hl03-short.mp4"),
            ClipRecord(vod_id=vod.id, key="hl04", kind="highlight", start=20, end=40, title="Before the jump", status="rendered",
                       make_short=True, short_path="/data/hl04-short.mp4"),
            # Claude's ends, but a part cut out in the app: edited by hand, so its cut doesn't drift off its words.
            ClipRecord(vod_id=vod.id, key="hl05", kind="highlight", start=65, end=80, title="Cut", cuts="[[70, 72]]"),
        ]
        s.add_all(clips)
        s.commit()
        s.add(UploadJob(clip_id=clips[1].id, which="short"))
        s.commit()
        vod_id = vod.id
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(timing, "measure", lambda source: Timing([Jump(60.0, 3.0)], length=100.0))

    out = CliRunner().invoke(cli.app, ["timing-check"]).output
    assert "From 00:01:03, words are timed 3.0 s early" in out
    assert f"clipper timing-check {vod_id} --fix" in out
    assert ws.audio_wav.exists()  # only checked

    out = CliRunner().invoke(cli.app, ["timing-check", str(vod_id), "--fix"]).output
    fixed = Transcript.model_validate_json(ws.transcript_json.read_text(encoding="utf-8"))
    assert [(w.start, w.end) for w in fixed.words()] == [(10.0, 10.5), (73.0, 73.5)]
    assert not ws.audio_wav.exists()
    with Session(get_engine(settings)) as s:
        clips = {c.key: (c.start, c.end, c.status) for c in s.exec(select(ClipRecord))}
        assert not s.exec(select(UploadJob)).all()  # taken out of the queue while it renders again
    assert clips == {
        "hl01": (68.0, 83.0, "proposed"),
        "hl02": (68.0, 83.0, "approved"),  # moved, and rendering again
        "hl03": (67, 84, "approved"),  # trimmed by hand: kept, but its Short's captions render again
        "hl04": (20, 40, "rendered"),  # before the sound jumped: right as it was
        "hl05": (65, 80, "proposed"),
    }
    assert "hl02  Ready to go" in out.split("upload them again")[1]
    with Session(get_engine(settings)) as s:  # the AI's pick moved with it: still not changed by hand (learning.py)
        moved = s.exec(select(ClipRecord).where(ClipRecord.key == "hl01")).one()
        assert (moved.picked_start, moved.picked_end) == (68.0, 83.0)

    out = CliRunner().invoke(cli.app, ["timing-check", "--fix"]).output
    assert "Lines up with the video" in out
