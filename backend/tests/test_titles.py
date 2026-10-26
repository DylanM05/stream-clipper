from datetime import date, datetime, timezone

import pytest

from clipper import titles
from clipper.config import Settings

PRESHOW = "♥️♠️♦️♣️KINO CASINO PRE SHOW♣️♦️♠️♥️ {date} - {title}"


def test_dates():
    assert titles.format_date(date(2026, 10, 3)) == "Oct 3, 2026"
    assert titles.stream_date({"upload_date": "20261023"}, date(2000, 1, 1)) == date(2026, 10, 23)
    assert titles.stream_date({}, date(2000, 1, 1)) == date(2000, 1, 1)
    assert titles.stream_date({"upload_date": "not a date"}, date(2000, 1, 1)) == date(2000, 1, 1)


def started(*utc) -> dict:
    """What yt-dlp says about a Kick VOD: its start time, and that time's day in UTC."""
    at = datetime(*utc, tzinfo=timezone.utc)
    return {"timestamp": at.timestamp(), "upload_date": at.strftime("%Y%m%d")}


@pytest.mark.parametrize("meta, day", [
    (started(2026, 10, 27, 0, 30), date(2026, 10, 26)),  # Monday 8:30 PM Eastern is Tuesday in UTC
    (started(2026, 10, 27, 5, 15), date(2026, 10, 26)),  # a late start, 1:15 AM Tuesday: still Monday's show
    (started(2026, 10, 27, 11, 0), date(2026, 10, 27)),  # 7 AM Tuesday: Tuesday's
    ({**started(2026, 10, 27, 12, 0), "release_timestamp": started(2026, 10, 27, 0, 30)["timestamp"]}, date(2026, 10, 26)),
])
def test_a_streams_day_is_when_it_started_where_its_creators_are(meta, day):
    assert titles.stream_date(meta, date(2000, 1, 1), "America/New_York", 6) == day


def test_the_time_zone_must_be_a_real_one():
    assert Settings(timezone="Europe/London").timezone == "Europe/London"
    with pytest.raises(ValueError, match="unknown time zone"):
        Settings(timezone="Eastern")


def test_build_fills_in_the_pattern():
    assert (
        titles.build(PRESHOW, "Jer's Huck Blend Grift Unleashed", "Oct 23, 2026")
        == "♥️♠️♦️♣️KINO CASINO PRE SHOW♣️♦️♠️♥️ Oct 23, 2026 - Jer's Huck Blend Grift Unleashed"
    )
    assert titles.build("{title}", "  Just   the title ", "Oct 23, 2026") == "Just the title"
    assert titles.build("KINO CASINO {date} - {title}", "Topic", "") == "KINO CASINO - Topic"


def test_the_part_claude_wrote_comes_back_out_of_a_full_title():
    template = "{title} | Don Flurgundy & Channel 404 News Clips | {date}"
    full = titles.build(template, "TheQuartering Segment: Lawsuit", "Oct 25, 2026")
    assert titles.own_part(template, full) == "TheQuartering Segment: Lawsuit"
    assert titles.own_part("KINO CASINO {date} - {title}", "KINO CASINO Oct 1, 2026 - Jer (Finally) Wins?") == "Jer (Finally) Wins?"
    assert titles.own_part(template, "Edited by hand") == "Edited by hand"
    assert titles.own_part("{title}", "Anything") == "Anything"


def test_chapter_list_for_youtube():
    assert titles.chapter_list([(125, "Ethan Klein news"), (952, "Tonight's lineup"), (3725, "Late show")]) == (
        "Chapters\n0:00 Start\n2:05 Ethan Klein news\n15:52 Tonight's lineup\n1:02:05 Late show"
    )
    # A first chapter just after 0:00 moves to 0:00; chapters under 10 seconds apart are merged.
    assert titles.chapter_list([(3, "Intro"), (300, "Main"), (305, "Too close"), (900, "Outro")]) == (
        "Chapters\n0:00 Intro\n5:00 Main\n15:00 Outro"
    )
    assert titles.chapter_list([(0, "Only"), (60, "Two")]) == ""  # YouTube needs at least three


def test_emoji_count_double_and_long_titles_are_shortened_to_fit():
    assert titles.youtube_length("♥️") == 2 and titles.youtube_length("🎰") == 2
    room = titles.room_for_title(PRESHOW, "Oct 23, 2026")
    full = titles.build(PRESHOW, "word " * 40, "Oct 23, 2026")
    assert titles.youtube_length(full) <= titles.MAX_TITLE
    assert full.endswith("…")
    assert titles.youtube_length(full.split(" - ", 1)[1]) <= room
    assert titles.room_for_title("{title}", "Oct 23, 2026") == titles.MAX_TITLE
