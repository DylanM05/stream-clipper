import pytest
from conftest import line, transcript

from clipper import cuts
from clipper.models import (
    CompilationSegmentation, LLMChapter, LLMCompilationPiece, LLMHighlight, LLMPieceCut, Segmentation,
)
from clipper.pipeline.refine import (
    LEAD_IN_S, MAX_COMPILATION_PIECE_S, MAX_HIGHLIGHT_S, MIN_TAIL_S, TAIL_S, Snapper, refine,
)


def chapter(start, end, title="Chapter", category="content", confidence=0.8):
    return LLMChapter(start=start, end=end, category=category, title=title, summary="Summary.", confidence=confidence)


def highlight(start, end, title="Moment", score=0.7, vertical_ok=True):
    return LLMHighlight(start=start, end=end, title=title, description="d", why="w", score=score, vertical_ok=vertical_ok)


def piece(start, end, *gaps, title="Moment"):
    return LLMCompilationPiece(start=start, end=end, title=title, description="d", why="w", score=0.7,
                               vertical_ok=False, cuts=[LLMPieceCut(after=a, before=b) for a, b in gaps])


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


def test_chapters_are_scored_by_how_well_they_would_do_or_their_confidence_in_older_answers():
    scored = chapter("00:02:00", "00:10:00", confidence=0.9)
    scored.score = 0.35
    older = chapter("00:10:30", "00:18:40", confidence=0.7)  # saved before chapters were scored
    clips = refine(Segmentation(chapters=[scored, older], highlights=[]), _chapters_transcript())
    assert [c.score for c in clips] == [0.35, 0.7]


def test_a_chapter_keeps_the_ais_reason_and_older_answers_have_none():
    reasoned = chapter("00:02:00", "00:10:00")
    reasoned.why = " A guest everyone asks about. "
    older = chapter("00:10:30", "00:18:40")  # saved before chapters had a reason
    clips = refine(Segmentation(chapters=[reasoned, older], highlights=[]), _chapters_transcript())
    assert [c.why for c in clips] == ["A guest everyone asks about.", ""]


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


def test_adjacent_compilation_pieces_survive_overlapping_sentence_padding():
    t = transcript(
        line(5, "Here is the setup."),
        line(20, "What a comeback."),
        line(21.1, "Here's the next setup."),
        line(38, "That was unexpected."),
        duration=50,
    )
    seg = Segmentation(chapters=[], highlights=[
        highlight("00:00:05", "00:00:20", title="Comeback", score=0.9),
        highlight("00:00:21", "00:00:38", title="Surprise", score=0.6),
    ])
    snap = Snapper(t)
    assert snap.end(20) > snap.start(21)
    first, second = refine(seg, t, compilation_mode=True)
    assert (first.title, second.title) == ("Comeback", "Surprise")
    assert first.end == second.start == 21.1
    assert first.start < first.end < second.end


def test_compilation_keeps_a_quick_moment_below_the_legacy_highlight_minimum():
    t = transcript(line(3, "Here comes the setup."), line(8, "Payoff."), duration=20)
    seg = Segmentation(chapters=[], highlights=[highlight("00:00:03", "00:00:08")])
    assert refine(seg, t) == []
    [piece] = refine(seg, t, compilation_mode=True)
    assert 1 <= piece.duration < 10


def test_duplicate_compilation_proposals_for_the_same_line_are_deduplicated():
    t = transcript(line(5, "This one line has the entire joke."), duration=15)
    seg = Segmentation(chapters=[], highlights=[
        highlight("00:00:05", "00:00:05", title="Best title", score=0.9),
        highlight("00:00:05", "00:00:05", title="Duplicate", score=0.6),
    ])
    assert [clip.title for clip in refine(seg, t, compilation_mode=True)] == ["Best title"]


def test_a_compilation_piece_can_run_past_the_shorts_limit_and_cut_its_filler():
    t = transcript(
        line(10, "Here's how the heist started."),
        line(200, "Okay, quick break, back in a bit."),
        line(500, "We're back, where were we."),
        line(520, "So the guard walks in."),
        line(900, "And that's how we got away."),
        duration=1000,
    )
    seg = CompilationSegmentation(chapters=[], highlights=[piece("00:00:10", "00:15:00", ("00:03:20", "00:08:40"))])
    [clip] = refine(seg, t, compilation_mode=True)
    assert clip.start < 10 and clip.end > 900
    [(a, b)] = clip.cuts
    # The line before the gap and the line after it both stay.
    assert 200 < a < 500 and b < 520 and b >= 500 - LEAD_IN_S - 0.01
    assert clip.duration > MAX_HIGHLIGHT_S


def test_a_compilation_cut_with_bad_or_unknown_timecodes_is_skipped():
    t = transcript(line(10, "Setup."), line(30, "Middle."), line(60, "Payoff."), duration=80)
    seg = CompilationSegmentation(chapters=[], highlights=[
        piece("00:00:10", "00:01:00", ("soon", "00:00:30"), ("00:00:30", "00:00:10"), ("00:00:15", "00:00:30")),
    ])
    [clip] = refine(seg, t, compilation_mode=True)
    assert clip.cuts == []


def test_a_compilation_piece_is_capped_at_20_minutes_of_kept_footage():
    lines = [line(60.0 * m, f"Minute {m} of the story.") for m in range(0, 41)]
    t = transcript(*lines, duration=2500)
    # 40 minutes long with a 15 minute break cut out: 25 minutes kept, so it ends 20 minutes of footage in.
    seg = CompilationSegmentation(chapters=[], highlights=[piece("00:00:00", "00:40:00", ("00:05:00", "00:20:00"))])
    [clip] = refine(seg, t, compilation_mode=True)
    assert len(clip.cuts) == 1
    assert MAX_COMPILATION_PIECE_S - 60 <= clip.duration <= MAX_COMPILATION_PIECE_S
    assert clip.end > 20 * 60
    assert cuts.kept_length(clip.start, clip.end, clip.cuts) == pytest.approx(clip.duration)
