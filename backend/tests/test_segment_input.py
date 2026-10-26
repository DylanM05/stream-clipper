from conftest import line, transcript

from clipper.models import LoudMoment, PlatformChapter, Signals, Span
from clipper.pipeline.segment import build_input


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
    assert "# What the chapters and highlights should be\nOne chapter per guest." in text
    assert "[00:00:00] Just Chatting" in text
    assert "00:01:00-00:01:35" in text
    assert "01:06:40 (+12 dB)" in text
    assert "# Big visual changes" not in text  # empty hint sections are left out
    assert "most-viewed" not in text
    assert text.endswith("# Transcript\n[00:00:05] Hello chat.\n[01:02:05] Okay let's start the podcast.\n")


def test_the_channels_most_viewed_titles_come_before_the_hints():
    t = transcript(line(5.0, "Hello chat."))
    text = build_input({}, t, Signals(), top_titles=["TheQuartering Segment: Lawsuit", "Jer Wins"])
    assert "# The channel's most-viewed clip titles, most viewed first\n- TheQuartering Segment: Lawsuit\n- Jer Wins\n" in text
