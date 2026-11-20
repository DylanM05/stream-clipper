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


@pytest.mark.parametrize("video_type, prompt, noun, highlights", [
    ("streams", "segment.md", "stream", 3),
    ("episodes", "segment_episode.md", "episode", 8),  # the Shorts offered, and some to spare
    ("other", "segment_other.md", "video", 8),
])
def test_each_kind_of_video_gets_its_own_instructions(tmp_path, monkeypatch, video_type, prompt, noun, highlights):
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

    assert asked["system_prompt"] == segment.PROMPTS_DIR.joinpath(prompt).read_text(encoding="utf-8")
    assert asked["task"].startswith(f"Split this 00:22:00 {noun} into chapters and pick up to {highlights} highlights. ")
    assert asked["stdin_text"].startswith(f"# {noun.capitalize()}\nTitle: S01E01\n")
    if video_type != "streams":
        assert "given permission" not in asked["system_prompt"]


def test_compilation_uses_its_own_prompt_and_cache(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    ws = Workspace.open(settings.vods_dir, "local-abc")
    write_json(ws.meta_json, {"title": "Long stream"})
    write_json(ws.segmentation_json, Segmentation(chapters=[], highlights=[], episode_title="Standard").model_dump())
    asked = []

    class FakeAI:
        name = "Fake"

        def structured(self, **kwargs):
            asked.append(kwargs)
            return Segmentation(chapters=[], highlights=[], episode_title="Compilation")

    monkeypatch.setattr(segment.ai, "model", lambda *a, **k: FakeAI())
    t = transcript(line(5, "Hello there."), duration=7200)
    for _ in range(2):
        result = segment.run(ws, settings, t, Signals(), compilation_mode=True, track_record="Past reviews")
        assert result.episode_title == "Compilation"
    assert len(asked) == 1
    assert "no quota or per-hour cap" in asked[0]["system_prompt"]
    assert "several minutes when the moment itself needs that" in asked[0]["system_prompt"]
    assert "pick up to" not in asked[0]["task"]
    assert "Past reviews" in asked[0]["stdin_text"]
    assert segment.run(ws, settings, t, Signals()).episode_title == "Standard"
