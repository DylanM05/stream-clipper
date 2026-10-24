import pytest
from conftest import line, transcript

from clipper.models import LLMChapter, LLMHighlight, Segmentation
from clipper.pipeline.refine import LEAD_IN_S, MAX_HIGHLIGHT_S, MIN_TAIL_S, TAIL_S, Snapper, refine


def chapter(start, end, title="Chapter", category="content", confidence=0.8):
    return LLMChapter(start=start, end=end, category=category, title=title, summary="Summary.", confidence=confidence)


def highlight(start, end, title="Moment", score=0.7, vertical_ok=True):
    return LLMHighlight(start=start, end=end, title=title, description="d", why="w", score=score, vertical_ok=vertical_ok)


# Whisper split one sentence across two lines; the second line starts mid-sentence.
STORY = transcript(
    line(0.0, "Hello there everyone."),
    line(10.0, "So I was walking home yesterday"),
    line(12.2, "and then a chicken attacked me."),
    line(20.0, "It was honestly terrifying."),
    line(30.0, "Anyway let's play."),
)


def test_start_moves_back_to_the_start_of_the_sentence():
    snap = Snapper(STORY)
    # Claude named the mid-sentence line; the cut should include "So I was walking..."
    assert snap.start(12) == 10.0 - LEAD_IN_S


def test_end_runs_through_the_end_of_the_named_line_and_its_sentence():
    snap = Snapper(STORY)
    me = STORY.segments[2].words[-1]
    # Claude named the line "So I was walking home yesterday"; the sentence ends at "me."
    end = snap.end(10)
    assert end == pytest.approx(me.end + TAIL_S)


def test_end_of_a_complete_line_stops_before_the_next_line():
    snap = Snapper(STORY)
    end = snap.end(20)
    assert STORY.segments[3].end < end < STORY.segments[4].start


def test_end_never_drops_the_line_claude_named_when_speech_runs_on():
    # Real case: no punctuation, and the punchline runs straight into the next line. The last
    # break before it is the pause after "over"; cutting there would drop the punchline.
    t = transcript(
        line(0.0, "this is when he knew it was over"),
        line(4.0, "ethan is a total mess"),
        line(5.75, "it is like the last of the janitors laid down their brooms"),
    )
    # The next word starts right away, so the cut keeps the minimum tail after "mess".
    assert Snapper(t).end(4) == pytest.approx(t.segments[1].end + MIN_TAIL_S)


def test_start_never_skips_into_the_line_claude_named():
    # No sentence start within reach behind "well", and a pause right after it.
    t = transcript(
        line(0.0, "and so we were all sitting there waiting for the thing"),
        line(4.2, "well"),
        line(5.5, "guess what happened next"),
    )
    assert Snapper(t).start(4) == pytest.approx(t.segments[0].end)


def test_time_with_no_speech_nearby_is_kept():
    snap = Snapper(STORY)
    assert snap.start(5) == 5
    assert snap.end(27) == 27


def _chapters_transcript():
    return transcript(
        line(0.0, "Starting soon."),
        line(120.0, "Welcome back to the show."),
        line(300.0, "Let's talk about the new game."),
        line(600.0, "Quick break."),
        line(630.0, "Okay we're back with a guest."),
        line(1100.0, "Thanks for watching."),
        duration=1200,
    )


def test_chapters_skip_non_content_and_short_ones_and_never_overlap():
    seg = Segmentation(
        chapters=[
            chapter("00:00:00", "00:02:00", category="pre_stream"),
            chapter("00:02:00", "00:10:00", title="Show"),
            chapter("00:10:00", "00:10:30", title="Too short"),
            # Claude's end overlaps the next chapter; the cut must not.
            chapter("00:10:30", "00:18:40", title="Guest"),
            chapter("00:18:20", "00:20:00", title="Outro", category="outro"),
        ],
        highlights=[],
    )
    clips = refine(seg, _chapters_transcript())
    assert [c.title for c in clips] == ["Show", "Guest"]
    assert [c.id for c in clips] == ["ch01", "ch02"]
    show, guest = clips
    assert show.start == pytest.approx(120.0 - LEAD_IN_S)
    assert show.end < 600.0
    assert guest.start == pytest.approx(630.0 - LEAD_IN_S)
    assert guest.end < 1100.0


def test_overlapping_highlights_keep_the_higher_score():
    t = transcript(*[line(i * 10.0, f"Sentence number {i}.") for i in range(30)])
    seg = Segmentation(
        chapters=[],
        highlights=[
            highlight("00:00:50", "00:01:40", title="Weaker", score=0.6),
            highlight("00:01:00", "00:01:50", title="Better", score=0.9),
            highlight("00:03:00", "00:03:40", title="Separate", score=0.5),
        ],
    )
    clips = refine(seg, t)
    assert [c.title for c in clips] == ["Better", "Separate"]
    assert [c.id for c in clips] == ["hl01", "hl02"]


def test_highlight_length_limits():
    t = transcript(*[line(i * 10.0, f"Sentence number {i}.") for i in range(40)])
    seg = Segmentation(
        chapters=[],
        highlights=[
            highlight("00:00:10", "00:05:00", title="Too long"),
            highlight("00:06:00", "00:06:00", title="Too short"),
        ],
    )
    clips = refine(seg, t)
    assert [c.title for c in clips] == ["Too long"]
    assert clips[0].duration <= MAX_HIGHLIGHT_S


def test_titles_are_cleaned_and_capped_at_100_characters():
    long_title = "word " * 40
    seg = Segmentation(chapters=[], highlights=[highlight("00:00:10", "00:00:30", title=long_title, score=1.4)])
    clip = refine(seg, transcript(*[line(i * 10.0, f"Sentence {i}.") for i in range(5)]))[0]
    assert len(clip.title) <= 100
    assert clip.title.endswith("…")
    assert clip.score == 1.0


def test_bad_timecodes_are_skipped():
    seg = Segmentation(chapters=[], highlights=[highlight("soon", "later")])
    assert refine(seg, STORY) == []
