from datetime import date

from clipper import titles

PRESHOW = "♥️♠️♦️♣️KINO CASINO PRE SHOW♣️♦️♠️♥️ {date} - {title}"


def test_dates():
    assert titles.format_date(date(2026, 10, 3)) == "Oct 3, 2026"
    assert titles.stream_date("20261023", date(2000, 1, 1)) == date(2026, 10, 23)
    assert titles.stream_date(None, date(2000, 1, 1)) == date(2000, 1, 1)
    assert titles.stream_date("not a date", date(2000, 1, 1)) == date(2000, 1, 1)


def test_build_fills_in_the_pattern():
    assert (
        titles.build(PRESHOW, "Jer's Huck Blend Grift Unleashed", "Oct 23, 2026")
        == "♥️♠️♦️♣️KINO CASINO PRE SHOW♣️♦️♠️♥️ Oct 23, 2026 - Jer's Huck Blend Grift Unleashed"
    )
    assert titles.build("{title}", "  Just   the title ", "Oct 23, 2026") == "Just the title"
    assert titles.build("KINO CASINO {date} - {title}", "Topic", "") == "KINO CASINO - Topic"


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
