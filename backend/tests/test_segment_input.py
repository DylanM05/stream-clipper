import pytest
from conftest import line, transcript

from clipper.config import Settings
from clipper.models import LoudMoment, PlatformChapter, Segmentation, Signals, Span
from clipper.pipeline import segment
from clipper.pipeline.segment import build_input
from clipper.workspace import Workspace, write_json


def test_input_has_stream_details_hints_notes_and_timestamped_transcript():
    t = transcript(line(5.0, "Hello chat."), line(3725.0, "Okay let's start the podcast."), duration=7200)
    signals = Signals(
        platform_chapters=[PlatformChapter(start=0, end=3600, title="Just Chatting")],
        long_silences=[Span(start=60, end=95)],
        loud_moments=[LoudMoment(t=4000, db_above=12.4)],
    )
    meta = {"title": "Big Stream", "channel": "Creator", "extractor_key": "TwitchVod", "upload_date": "20261020"}
    text = build_input(meta, t, signals, context="The guest is called Sam.", guidance="One chapter per guest.")

    assert "Title: Big Stream" in text
    assert "Date: 2026-10-20" in text
    assert "Length: 02:00:00" in text
    assert "# About the show\nThe guest is called Sam." in text
    assert '# What the chapters and highlights should be (chapters may be called "clips" here)\nOne chapter per guest.' in text
    assert "[00:00:00] Just Chatting" in text
    assert "00:01:00-00:01:35" in text
    assert "01:06:40 (+12 dB)" in text
    assert "# Big visual changes" not in text  # empty hint sections are left out
    assert "past uploads" not in text
    assert text.endswith("# Transcript\n[00:00:05] Hello chat.\n[01:02:05] Okay let's start the podcast.\n")


def test_the_shows_track_record_comes_before_the_hints():
    t = transcript(line(5.0, "Hello chat."))
    record = "# How the show's past uploads did\n\nTheir usual views:\n- Clips: 500 views a week after going up (6 uploads)"
    text = build_input({}, t, Signals(long_silences=[Span(start=60, end=95)]), track_record=record)
    assert record + "\n" in text and text.index(record) < text.index("# Long silences")


@pytest.mark.parametrize("video_type", ["streams", "episodes"])
def test_a_tv_episode_gets_its_own_instructions_and_more_highlights_asked_for(tmp_path, monkeypatch, video_type):
    settings = Settings(data_dir=tmp_path)
    ws = Workspace.open(settings.vods_dir, "local-abc")
    write_json(ws.meta_json, {"title": "S01E01", "extractor_key": "Local"})
    asked = {}

    class FakeAI:
        name = "Fake"

        def structured(self, **kwargs):
            asked.update(kwargs)
            return Segmentation(chapters=[], highlights=[])

    monkeypatch.setattr(segment.ai, "model", lambda *a, **k: FakeAI())
    t = transcript(line(5.0, "Hello there."), duration=22 * 60)
    segment.run(ws, settings, t, Signals(), video_type=video_type, offered_shorts=5)

    prompt = "segment_episode.md" if video_type == "episodes" else "segment.md"
    assert asked["system_prompt"] == segment.PROMPTS_DIR.joinpath(prompt).read_text(encoding="utf-8")
    if video_type == "episodes":  # the Shorts offered, and some to spare
        assert asked["task"].startswith("Split this 00:22:00 episode into chapters and pick up to 8 highlights. ")
        assert asked["stdin_text"].startswith("# Episode\nTitle: S01E01\n")
        assert "given permission" not in asked["system_prompt"]
    else:
        assert asked["task"].startswith("Split this 00:22:00 stream into chapters and pick up to 3 highlights. ")
        assert asked["stdin_text"].startswith("# Stream\n")
