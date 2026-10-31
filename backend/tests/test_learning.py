"""What a show's past clips teach the AI that picks the next stream's: how they did, and how the owner changed them."""

import json
from datetime import timedelta

import pytest
from conftest import line, transcript
from fastapi.testclient import TestClient
from sqlmodel import Session, select

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
                          **{"picked_start": start, "picked_end": end, "picked_title": title, **fields})
        s.add(clip)
        s.commit()
        return clip.id


def add_upload(settings, clip_id, which="video", **views) -> None:
    with Session(get_engine(settings)) as s:
        s.add(UploadJob(clip_id=clip_id, which=which, status="done", youtube_url="https://youtu.be/x", finished_at=NOW,
                        views=views.get("views_week1") or views.get("views_day1"), **views))
        s.commit()


def record(settings) -> str:
    with Session(get_engine(settings)) as s:
        return learning.track_record(s, settings, s.get(Show, 1))


def test_nothing_to_go_on_says_nothing(settings):
    assert record(settings) == ""
    clip = add_clip(settings)
    add_upload(settings, clip, views_week1=500)  # one upload has no usual to compare with
    assert "past uploads" not in record(settings)


def test_uploads_against_the_shows_usual_by_kind_length_and_score_with_the_best_and_worst(settings):
    for title, minutes, score, views in [
        ("Slow start", 5, 0.6, 100), ("Mid one", 15, 0.7, 200), ("Fine one", 15, 0.82, 300),
        ("Good one", 25, 0.85, 400), ("The big one", 25, 0.95, 2000),
    ]:
        clip = add_clip(settings, title, end=minutes * 60.0, score=score, why=f"{title} has a hook. More detail.")
        add_upload(settings, clip, views_week1=views)
    for views in (1000, 1000, 1000, 5000):
        clip = add_clip(settings, "Short moment", kind="highlight", end=45.0, score=0.9)
        add_upload(settings, clip, which="short", views_week1=views)
    made = add_clip(settings, "Owner's own", kind="highlight", end=40.0, manual=True)
    add_upload(settings, made, which="short", views_day1=50)  # only a day's views: not compared with a week's

    text = record(settings)
    assert "- Clips: 300 views a week after going up (5 uploads)" in text
    assert "- Shorts: 1K views a week after going up (4 uploads)" in text
    assert "- Clips by length: 10–20 min 0.8× (2), 20–40 min 4.0× (2)" in text
    assert "- By the score you gave them: 0.9 and up 1.0× (5), 0.8 to 0.89 1.2× (2), under 0.8 0.5× (2)" in text
    best = text.split("Did best:\n")[1].split("\n\n")[0].splitlines()
    assert best[0] == '- "The big one" (clip, 25 min, scored 0.95): 6.7× usual, 2K views. The big one has a hook'
    assert best[1] == '- "Short moment" (Short, 45 s, scored 0.90): 5.0× usual, 5K views'
    worst = text.split("Did worst:\n")[1].splitlines()
    assert worst[0].startswith('- "Slow start" (clip, 5 min, scored 0.60): 0.3× usual, 100 views')
    assert "Owner's own" not in text


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

    text = record(settings)
    assert text.startswith("# How the channel owner changed your earlier picks")
    assert ("Of the last 3 of your picks they approved, they moved the start of 1 (later in 1, by a median of 20 s) "
            "and the end of 1 (later in 1, by a median of 2.2 s), and cut parts out of 1.") in text
    assert ('- Highlight "He walks out": the end moved 2.2 s later: yours ended on "…asked him about it and then he '
            'just", and they kept going through "walks out of the room".') in text
    assert ('- Clip "The main topic": the start moved 20 s later, leaving out "okay welcome back everyone"; they cut out '
            '10 s: "a quick word from the sponsor".') in text
    assert 'Titles they rewrote, yours and then theirs:\n- "Main topic, take one" → "The main topic"' in text
    assert ("They turned down 1 of the last 4 of your picks they looked at, for example:\n"
            '- "Not this one" (clip, 15 min, scored 0.55). Mostly logistics') in text
    assert "Went ahead by itself" not in text


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
        assert (new.picked_start, new.picked_end, new.picked_title) == (10, 40, "Big moment")
        assert s.exec(select(ClipRecord).where(ClipRecord.key == "full")).one().picked_start is None

    # Clips from before kept their picks only in the stream's segments.json.
    ws = Workspace(settings.vods_dir / "vod-1")
    write_text(ws.segments_json, json.dumps([
        {"id": "ch01", "start": 0.0, "end": 600.0, "title": "First pick"},
        {"id": "ch02", "start": 600.0, "end": 900.0, "title": "Second pick"},
    ]))
    with Session(get_engine(settings)) as s:
        s.add_all([
            ClipRecord(vod_id=1, key="ch01", kind="chapter", start=0.0, end=640.0, title="First"),  # end moved
            ClipRecord(vod_id=1, key="ch02", kind="chapter", start=601.5, end=901.5, title="Second"),  # the timing fix
            ClipRecord(vod_id=1, key="my01", kind="chapter", start=0.0, end=60.0, title="Mine", manual=True),
        ])
        s.commit()
        assert learning.backfill_picks(s, settings) == 1
        old = {c.key: c for c in s.exec(select(ClipRecord).where(ClipRecord.key.in_(["ch01", "ch02", "my01"])))}
        assert (old["ch01"].picked_start, old["ch01"].picked_end, old["ch01"].picked_title) == (0.0, 600.0, "First pick")
        assert old["ch02"].picked_start is None and old["my01"].picked_start is None
