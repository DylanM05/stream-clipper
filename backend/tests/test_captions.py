from conftest import line

from clipper.pipeline.captions import HIGHLIGHT, _ass_time, build_ass


def dialogue(ass: str) -> list[str]:
    return [l for l in ass.splitlines() if l.startswith("Dialogue:")]


def test_words_are_grouped_and_the_spoken_word_is_highlighted():
    words = line(100.0, "This is how {the} chicken won. Again").words
    events = dialogue(build_ass(words, 100.0, 110.0, font="DejaVu Sans"))

    # Groups of at most 3 words, ending at sentence ends: [this is how] [(the) chicken won.] [again]
    texts = [e.split(",,", 1)[1].split(",", 4)[-1] for e in events]
    assert len(events) == 7
    assert texts[0] == f"{{\\c{HIGHLIGHT}}}THIS{{\\r}} IS HOW"
    assert texts[4] == f"(THE) {{\\c{HIGHLIGHT}}}CHICKEN{{\\r}} WON."
    assert texts[6] == f"{{\\c{HIGHLIGHT}}}AGAIN{{\\r}}"
    # Times are relative to the clip start.
    assert events[0].startswith("Dialogue: 0,0:00:00.00,0:00:00.35,")


def test_words_outside_the_clip_are_left_out():
    words = line(0.0, "one two three four five six").words
    ass = build_ass(words, 0.7, 1.4, font="X")
    assert "ONE" not in ass and "SIX" not in ass
    assert "THREE" in ass


def test_ass_time_format():
    assert _ass_time(0) == "0:00:00.00"
    assert _ass_time(3725.456) == "1:02:05.46"
