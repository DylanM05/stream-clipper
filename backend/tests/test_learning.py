"""What a show's past clips teach the AI that picks the next stream's: how they did, and how the owner changed them."""

import json
from datetime import timedelta

import pytest
from conftest import line, transcript
from fastapi.testclient import TestClient
from sqlmodel import Session, col, select

from clipper import learning
from clipper import worker as worker_module
from clipper.config import Settings
from clipper.db import ClipRecord, Show, UploadJob, Vod, get_engine, utcnow
from clipper.models import Clip
from clipper.web import app as web_app
from clipper.workspace import Workspace, write_text

PATTERN = "{title} | Example Clips | {date}"
NOW = utcnow()


@pytest.fixture
def settings(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    ws = Workspace.open(settings.vods_dir, "vod-1")
    write_text(ws.transcript_json, transcript(
        line(5.0, "okay welcome back everyone"),
        line(52.0, "a quick word from the sponsor"),
        line(90.0, "I asked him about it"),
        line(100.0, "and then he just walks out of the room"),
        duration=3600,
    ).model_dump_json())
    with Session(get_engine(settings)) as s:
        s.add(Show(name="Example", title_template=PATTERN))
        s.commit()
        s.add(Vod(url="https://kick.com/example/videos/1", show_id=1, status="ready", duration=3600, workspace="vod-1"))
        s.commit()
    return settings


def add_clip(settings, title="A moment", kind="chapter", start=0.0, end=600.0, status="uploaded", looked=True, **fields) -> int:
    """A pick of the AI's, offered as it is unless picked_* say otherwise; `looked` means the owner reviewed it."""
    with Session(get_engine(settings)) as s:
        clip = ClipRecord(vod_id=1, key=f"k{utcnow().timestamp()}", kind=kind, start=start, end=end, status=status,
                          title=f"{title} | Example Clips | Oct 20, 2026", created_at=NOW,
                          reviewed_at=NOW + (timedelta(hours=1) if looked else timedelta()),
                          **{"picked_start": start, "picked_end": end, "picked_title": title, "was_extra": False, **fields})
        s.add(clip)
        s.commit()
        return clip.id


def add_upload(settings, clip_id, which="video", ago=timedelta(), **fields) -> None:
    with Session(get_engine(settings)) as s:
        views = fields.pop("views", None) or fields.get("views_week1") or fields.get("views_day1")
        s.add(UploadJob(clip_id=clip_id, which=which, status="done", youtube_url="https://youtu.be/x",
                        finished_at=NOW - ago, views=views, **fields))
        s.commit()


def record(settings) -> str:
    with Session(get_engine(settings)) as s:
        return learning.track_record(s, settings, s.get(Show, 1))


def section(text: str, heading: str) -> list[str]:
    """The lines of a list under `heading`, up to the next blank line."""
    return text.split(heading + "\n")[1].split("\n\n")[0].splitlines()


def test_nothing_to_go_on_says_nothing(settings):
    assert record(settings) == ""
    clip = add_clip(settings)
    add_upload(settings, clip, views_week1=500)  # one upload has no usual to compare with
    assert "past uploads" not in record(settings)


def test_uploads_against_the_shows_usual_by_kind_length_score_engagement_and_staying_power(settings):
    for title, minutes, score, week1, day1, likes in [
        ("Slow start", 5, 0.6, 100, 80, 2), ("Mid one", 15, 0.9, 200, 150, 4), ("Fine one", 15, 0.82, 300, 100, 60),
        ("Good one", 25, 0.7, 400, 300, 8), ("The big one", 25, 0.95, 2000, 1800, 50),
    ]:
        clip = add_clip(settings, title, end=minutes * 60.0, score=score, why=f"{title} has a hook. More detail.")
        add_upload(settings, clip, views_week1=week1, views_day1=day1, likes=likes, comments=0)
    for views in (1000, 1000, 1000, 5000):
        clip = add_clip(settings, "Short moment", kind="highlight", end=45.0, score=0.9)
        add_upload(settings, clip, which="short", views_week1=views)
    made = add_clip(settings, "Owner's own", kind="highlight", end=40.0, manual=True)
    add_upload(settings, made, which="short", views_day1=50)  # only a day's views: not compared with a week's

    text = record(settings)
    # Each upload against the others of its kind (not itself), around the same time.
    assert "- Clips: lately 300 views a week after going up (5 uploads so far)" in text
    assert "- Shorts: lately 1K views a week after going up (4 uploads so far)" in text
    assert "- Clips: a week's views are usually 1.3× the first day's" in text
    assert "- Clips by length: 10–20 min 0.8× (2), 20–40 min 4.8× (2)" in text
    assert "- By the score you gave them: 0.9 and up 1.0× (6), under 0.8 0.9× (2)" in text
    assert section(text, "Did best:") == [
        '- "The big one" (clip, 25 min, scored 0.95): 8.0× usual, 2K views. The big one has a hook',
        '- "Short moment" (Short, 45 s, scored 0.90): 5.0× usual, 5K views',
        '- "Good one" (clip, 25 min, scored 0.70): 1.6× usual, 400 views. Good one has a hook',
    ]
    assert section(text, "Did worst:")[0].startswith('- "Slow start" (clip, 5 min, scored 0.60): 0.3× usual, 100 views')
    assert section(text, "Scored high but did badly:") == [
        '- "Mid one" (clip, 15 min, scored 0.90): 0.6× usual, 200 views. Mid one has a hook']
    assert section(text, "Scored low but did well:") == [
        '- "Good one" (clip, 25 min, scored 0.70): 1.6× usual, 400 views. Good one has a hook']
    assert section(text, "Liked and commented on most for their views:") == [
        '- "Fine one" (clip, 15 min): 60 likes and 0 comments on 300 views, 10.0× the usual for its kind']
    assert section(text, "Kept getting views after their first day:") == [
        '- "Fine one" (clip, 15 min): 300 views in a week, 3.0× its first day\'s (usually 1.3×)']
    assert "Owner's own" not in text.split("# Moments")[0]


def test_an_upload_is_compared_with_others_from_around_the_same_time(settings):
    for _ in range(4):  # from when the channel was small
        add_upload(settings, add_clip(settings, "Early days", score=0.8), ago=timedelta(days=90), views_week1=10)
    for title, views in (("Recent", 300), ("Recent", 300), ("Recent", 300), ("Newest hit", 600)):
        add_upload(settings, add_clip(settings, title, score=0.8), views_week1=views)
    text = record(settings)
    assert '- "Newest hit" (clip, 10 min, scored 0.80): 2.0× usual, 600 views' in text  # not 60× the early ones


def test_how_the_owner_changed_the_ais_picks_with_the_words_it_made_a_difference_to(settings):
    # A highlight that stopped mid-sentence, let run to the end of it.
    add_clip(settings, "He walks out", kind="highlight", start=90.0, end=103.5, status="approved",
             picked_start=90.0, picked_end=101.3)
    # A clip started later, past the welcome, with the sponsor read cut out.
    add_clip(settings, "The main topic", start=20.0, end=600.0, status="rendered", picked_start=0.0, picked_end=600.0,
             cuts=json.dumps([[50.0, 60.0]]), picked_title="Main topic, take one")
    add_clip(settings, "Untouched", start=700.0, end=900.0, status="approved")
    add_clip(settings, "Went ahead by itself", start=900.0, end=1000.0, status="approved", looked=False,
             picked_start=800.0)  # not looked at by the owner, so not counted
    add_clip(settings, "Not this one", start=1000.0, end=1900.0, status="rejected", score=0.55,
             why="Mostly logistics.")
    add_clip(settings, "Below the cut", start=1900.0, end=2500.0, status="approved", score=0.4, was_extra=True)
    add_clip(settings, "Tall one", kind="highlight", start=200.0, end=230.0, status="approved", make_short=True)
    add_clip(settings, "Wide one", kind="highlight", start=300.0, end=330.0, status="approved", vertical_ok=True)

    text = record(settings)
    assert text.startswith("# How the channel owner changed your earlier picks")
    assert ("Of the last 6 of your picks they approved, they moved the start of 1 (later in 1, by a median of 20 s) "
            "and the end of 1 (later in 1, by a median of 2.2 s), and cut parts out of 1.") in text
    assert ('- Highlight "He walks out": the end moved 2.2 s later: yours ended on "…asked him about it and then he '
            'just", and they kept going through "walks out of the room".') in text
    assert ('- Clip "The main topic": the start moved 20 s later, leaving out "okay welcome back everyone"; they cut out '
            '10 s: "a quick word from the sponsor".') in text
    assert 'Titles they rewrote, yours and then theirs:\n- "Main topic, take one" → "The main topic"' in text
    assert ("Extras they approved anyway: 1 of your picks you'd scored below the show's best few, for example:\n"
            '- "Below the cut" (clip, 10 min, scored 0.40)') in text
    assert ("Of the highlights they approved, they made Shorts of 1 you'd said don't work vertically (for example "
            '"Tall one") and turned the Short off for 1 you\'d said work vertically (for example "Wide one").') in text
    assert ("They turned down 1 of the last 7 of your picks they looked at, for example:\n"
            '- "Not this one" (clip, 15 min, scored 0.55). Mostly logistics') in text
    assert "Went ahead by itself" not in text


def test_moments_the_owner_clipped_themselves_and_whether_the_ai_had_them(settings):
    add_clip(settings, "Main pick", start=0.0, end=600.0, status="approved")
    add_clip(settings, "Missed moment", kind="highlight", start=100.0, end=104.0, manual=True)
    add_clip(settings, "Owner's cut", start=0.0, end=60.0, manual=True)

    lines = section(record(settings), "# Moments the channel owner clipped themselves, newest first")
    assert ('- "Missed moment" (highlight, 4.0 s, no highlight of yours covered it): it begins "and then he just walks '
            'out of the room…" and ends "…and then he just walks out of the room"') in lines
    assert any(l.startswith('- "Owner\'s cut" (clip, 1 min, overlapping your pick "Main pick"): it begins "okay welcome')
               for l in lines)


def test_the_owners_own_notes_come_first(settings):
    with Session(get_engine(settings)) as s:
        show = s.get(Show, 1)
        show.learning_notes = "Openers that start on the joke hold viewers best."
        s.add(show)
        s.commit()
    assert record(settings) == (
        "# What the channel owner has noticed about what works (follow this over the numbers)\n"
        "Openers that start on the joke hold viewers best."
    )
    client = TestClient(web_app.create_app(settings))
    assert client.get("/api/shows/1/track-record").json()["text"].endswith("Openers that start on the joke hold viewers best.")
    assert client.patch("/api/shows/1", json={"learning_notes": "Shorter is better."}).json()["learning_notes"] == "Shorter is better."


def test_new_clips_keep_the_ais_pick_and_older_ones_find_it_in_their_stream(settings):
    with Session(get_engine(settings)) as s:
        show = s.get(Show, 1)
        s.expunge(show)
    w = worker_module.Worker(settings)
    w._add_clips(1, [
        Clip(id="hl01", kind="highlight", start=10, end=40, title="Big moment | Example Clips | Oct 20, 2026",
             description="", score=0.9, llm_start="", llm_end=""),
        Clip(id="full", kind="whole", start=0, end=3600, title="Full show", description="", score=1, llm_start="",
             llm_end=""),
    ], show)
    with Session(get_engine(settings)) as s:
        new = s.exec(select(ClipRecord).where(ClipRecord.key == "hl01")).one()
        assert (new.picked_start, new.picked_end, new.picked_title, new.was_extra) == (10, 40, "Big moment", False)
        full = s.exec(select(ClipRecord).where(ClipRecord.key == "full")).one()
        assert full.picked_start is None and full.was_extra is None

    # Clips from before kept their picks only in the stream's segments.json, and whether each was an extra only in
    # how its score ranked (with the show offering its best one).
    ws = Workspace(settings.vods_dir / "vod-1")
    write_text(ws.segments_json, json.dumps([
        {"id": "ch01", "start": 0.0, "end": 600.0, "title": "First pick"},
        {"id": "ch02", "start": 600.0, "end": 900.0, "title": "Second pick"},
    ]))
    with Session(get_engine(settings)) as s:
        show = s.get(Show, 1)
        show.max_chapters = 1
        s.add_all([
            show,
            ClipRecord(vod_id=1, key="ch01", kind="chapter", start=0.0, end=640.0, title="First", score=0.9),  # end moved
            ClipRecord(vod_id=1, key="ch02", kind="chapter", start=601.5, end=901.5, title="Second", score=0.5,
                       status="approved"),  # moved by the timing fix; approved from the extras
            ClipRecord(vod_id=1, key="my01", kind="chapter", start=0.0, end=60.0, title="Mine", manual=True),
        ])
        s.commit()
        assert learning.backfill(s, settings) == 3
        old = {c.key: c for c in s.exec(select(ClipRecord).where(col(ClipRecord.key).in_(["ch01", "ch02", "my01"])))}
        assert (old["ch01"].picked_start, old["ch01"].picked_end, old["ch01"].picked_title) == (0.0, 600.0, "First pick")
        assert old["ch02"].picked_start is None and old["my01"].picked_start is None
        assert (old["ch01"].was_extra, old["ch02"].was_extra, old["my01"].was_extra) == (False, True, None)
        assert learning.backfill(s, settings) == 0  # nothing left to fill in


def add_compilation(settings, start, end, cut_list, picked_cuts, **fields) -> int:
    """The stream's compilation as the owner left it, made by the app from `picked_cuts` (the gaps between its clips)."""
    return add_clip(settings, "The compilation", kind="compilation", start=start, end=end, status="proposed",
                    cuts=json.dumps(cut_list), picked_cuts=json.dumps(picked_cuts), **fields)


def changed_lines(text: str) -> list[str]:
    return section(text, "Picks they changed, newest first:") if "Picks they changed" in text else []


def test_a_kept_picks_cut_appears_in_the_changed_picks(settings):
    add_clip(settings, "Kept with a cut", start=0.0, end=600.0, status="kept", cuts=json.dumps([[50.0, 60.0]]))

    assert ('- Clip "Kept with a cut": they cut out 10 s: "a quick word from the sponsor".'
            in changed_lines(record(settings)))


def test_the_ais_own_cuts_in_a_pick_are_not_counted_as_the_owners(settings):
    add_clip(settings, "Kept as cut", start=0.0, end=600.0, status="kept",
             cuts=json.dumps([[50.0, 60.0]]), picked_cuts=json.dumps([[50.0, 60.0]]))
    add_clip(settings, "One more cut", start=0.0, end=600.0, status="kept",
             cuts=json.dumps([[50.0, 60.0], [100.0, 110.0]]), picked_cuts=json.dumps([[50.0, 60.0]]))

    lines = changed_lines(record(settings))
    assert len(lines) == 1 and lines[0].startswith('- Clip "One more cut": they cut out 10 s')


def test_a_cut_in_the_compilation_is_credited_to_the_clip_it_falls_in_and_not_the_gaps(settings):
    # Kept without review: only the cut the owner made in the compilation makes them changed.
    add_clip(settings, "Opening", start=0.0, end=60.0, status="kept", looked=False)
    add_clip(settings, "He walks out", kind="highlight", start=90.0, end=104.0, status="kept", looked=False)
    # The owner's cut runs from the sponsor read into the gap the app cut between the two, so tidy merged them.
    add_compilation(settings, 0.0, 104.0, [[50.0, 90.0]], [[60.0, 90.0]])

    lines = changed_lines(record(settings))
    assert lines == ['- Clip "Opening": in the compilation they cut out 10 s: "a quick word from the sponsor".']


def test_a_cut_across_two_clips_is_split_between_them(settings):
    add_clip(settings, "Opening", start=0.0, end=60.0, status="kept", looked=False)
    add_clip(settings, "He walks out", kind="highlight", start=90.0, end=104.0, status="kept", looked=False)
    add_clip(settings, "Untouched", start=700.0, end=900.0, status="approved")
    add_compilation(settings, 0.0, 900.0, [[50.0, 95.0]], [[60.0, 90.0], [104.0, 700.0]])

    text = record(settings)
    assert sorted(changed_lines(text)) == [
        '- Clip "Opening": in the compilation they cut out 10 s: "a quick word from the sponsor".',
        '- Highlight "He walks out": in the compilation they cut out 5.0 s: "I asked him about it".',
    ]
    assert "Of the last 3 of your picks they approved, they cut parts out of 2." in text


def test_an_overlap_goes_to_the_shorter_clip(settings):
    add_clip(settings, "The whole topic", start=0.0, end=600.0, status="kept", looked=False)
    add_clip(settings, "He walks out", kind="highlight", start=90.0, end=110.0, status="kept", looked=False)
    add_compilation(settings, 0.0, 600.0, [[100.0, 104.0]], [])

    assert changed_lines(record(settings)) == [
        '- Highlight "He walks out": in the compilation they cut out 4.0 s: "and then he just walks out of the room".',
    ]


def test_a_compilation_without_picked_start_is_skipped(settings):
    add_clip(settings, "Opening", start=0.0, end=60.0, status="kept", looked=False)
    add_clip(settings, "He walks out", kind="highlight", start=90.0, end=104.0, status="kept", looked=False)
    add_compilation(settings, 0.0, 104.0, [[50.0, 90.0]], [], picked_start=None, picked_end=None)

    assert "in the compilation" not in record(settings)


def test_a_sliver_of_a_clip_taken_with_a_gap_is_not_a_compilation_cut(settings):
    add_clip(settings, "Opening", start=0.0, end=60.0, status="kept", looked=False)
    add_clip(settings, "He walks out", kind="highlight", start=90.0, end=104.0, status="kept", looked=False)
    # The gap's start dragged a hair earlier in the editor: a slip, not a cut out of the clip.
    add_compilation(settings, 0.0, 104.0, [[59.996, 90.0]], [[60.0, 90.0]])

    assert "in the compilation" not in record(settings)


def test_kept_highlights_are_not_counted_as_shorts(settings):
    add_clip(settings, "Tall one", kind="highlight", start=200.0, end=230.0, status="approved", make_short=True)
    add_clip(settings, "Kept tall one", kind="highlight", start=300.0, end=330.0, status="kept", make_short=True)

    text = record(settings)
    assert ("Of the highlights they approved, they made Shorts of 1 you'd said don't work vertically (for example "
            '"Tall one").') in text
    assert "Kept tall one" not in text
