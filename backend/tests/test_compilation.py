"""Compilations: a stream's approved clips joined into one video, for a show that makes them."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from clipper import compilation, cuts
from clipper import worker as worker_module
from clipper.config import Settings
from clipper.db import ClipRecord, Destination, Show, Vod, get_engine, utcnow
from clipper.web import app as web_app
from clipper.workspace import Workspace, write_json


def clip(key, start, end, status="approved", kind="highlight", title=None, cut=""):
    return ClipRecord(vod_id=1, key=key, kind=kind, start=start, end=end, title=title or f"SHOW - {key}",
                      status=status, cuts=cut)


def test_it_joins_the_approved_picks_in_stream_order_and_leaves_out_the_rest():
    found = compilation.plan([
        clip("hl02", 500, 530),
        clip("hl01", 100, 130),
        clip("hl03", 700, 760, status="rejected"),
        clip("hl04", 900, 930, status="extra"),
        clip("full", 0, 3600, kind="whole"),
        clip("ch01", 1000, 1600, kind="chapter", status="uploaded"),
    ], "SHOW - {title}")
    assert [c.key for c in found.picks] == ["hl01", "hl02", "ch01"]
    assert (found.start, found.end) == (100, 1600)
    assert found.cuts == [(130, 500), (530, 1000)]
    assert cuts.kept_length(found.start, found.end, found.cuts) == 30 + 30 + 600
    assert found.description == "Chapters\n0:00 hl01\n0:30 hl02\n1:00 ch01"


def test_a_highlight_inside_a_chapter_goes_in_once_and_cut_parts_stay_out():
    found = compilation.plan([
        clip("ch01", 100, 400, kind="chapter", cut="[[200, 250]]"),
        clip("hl01", 150, 180),
        clip("hl02", 1000, 1020),
    ], "{title}")
    assert (found.start, found.end) == (100, 1020)
    assert found.cuts == [(200, 250), (400, 1000)]


@pytest.mark.parametrize("statuses", [("approved",), ("approved", "rejected", "proposed")])
def test_fewer_than_two_approved_clips_make_no_compilation(statuses):
    assert compilation.plan([clip(f"hl{i}", i * 100, i * 100 + 20, s) for i, s in enumerate(statuses)], "{title}") is None


def test_kept_clips_are_included_in_the_compilation():
    found = compilation.plan([clip("hl01", 100, 130, status="kept"), clip("hl02", 500, 530, status="kept")], "{title}")
    assert [c.key for c in found.picks] == ["hl01", "hl02"]


def test_where_each_clip_starts_skips_the_parts_before_it():
    parts = [(100, 130), (500, 530), (1000, 1600)]
    assert compilation._video_time(parts, 500) == 30
    assert compilation._video_time(parts, 1100) == 160
    assert cuts.stream_time(parts, 160) == 1100


# --- What the owner removed from a compilation ------------------------------------------


def test_removed_parts_include_a_new_cut_that_merged_into_a_gap():
    # the owner cut 500-520 right after the gap the app cut, and tidy merged the two
    assert cuts.removed(100, 1600, [(130, 520)], 100, 1600, [(130, 500)]) == [(500, 520)]


def test_removed_parts_include_a_moved_start_and_end():
    assert cuts.removed(110, 1500, [(130, 500), (700, 720)], 100, 1600, [(130, 500)]) == [
        (100, 110), (700, 720), (1500, 1600)]


def test_a_gap_the_owner_put_back_is_not_removed_and_touching_parts_are_merged():
    # 130-500 was given back; the start moved to 110 and 110-120 was cut right after it
    assert cuts.removed(110, 1600, [(110, 120), (800, 900)], 100, 1600, [(130, 500)]) == [(100, 120), (800, 900)]


def test_removed_is_empty_when_nothing_changed():
    assert cuts.removed(100, 1600, [(130, 500)], 100, 1600, [(130, 500)]) == []


# --- The worker -----------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", web_dir=tmp_path / "web")


def long_ago():
    """A decision old enough that the inbox's Undo can't take it back any more."""
    return utcnow() - timedelta(seconds=worker_module.COMPILE_DELAY_S + 1)


def stream(settings, statuses, *, due=True, auto="review", reviewed_at=None, n=1, compiling=True):
    ws = Workspace.open(settings.vods_dir, f"kickvod-{n}")
    write_json(ws.meta_json, {"source_path": str(ws.root / "source.mp4"), "upload_date": "20261020"})
    with Session(get_engine(settings)) as s:
        show = Show(name="Night Owls", title_template="NIGHT OWLS {date} - {title}", compilation=True,
                    auto_compilation=auto, clip_destination_id=None, clip_playlist_id="PLclips")
        s.add(show)
        s.commit()
        vod = Vod(url=f"https://kick.com/kino/videos/{n}", status="ready", workspace=f"kickvod-{n}", show_id=show.id,
                  title="Late night stream", compilation_due=due, makes_compilation=compiling)
        s.add(vod)
        s.commit()
        for i, status in enumerate(statuses):
            s.add(ClipRecord(vod_id=vod.id, key=f"hl{i:02d}", kind="highlight", start=i * 100, end=i * 100 + 30,
                             title=f"NIGHT OWLS Oct 20, 2026 - Moment {i}", status=status, reviewed_at=reviewed_at))
        s.commit()
        return vod.id


def clips(settings, vod_id):
    with Session(get_engine(settings)) as s:
        return s.exec(select(ClipRecord).where(ClipRecord.vod_id == vod_id).order_by(ClipRecord.id)).all()


def due(settings, vod_id):
    with Session(get_engine(settings)) as s:
        return s.get(Vod, vod_id).compilation_due


def test_it_waits_until_every_offered_clip_is_reviewed(settings):
    vod_id = stream(settings, ["approved", "proposed", "approved"])
    w = worker_module.Worker(settings)
    assert w.compile_next() is False
    with Session(get_engine(settings)) as s:
        c = s.exec(select(ClipRecord).where(ClipRecord.key == "hl01")).one()
        c.status = "rejected"
        s.add(c)
        s.commit()
    assert w.compile_next() is True
    made = clips(settings, vod_id)[-1]
    assert (made.kind, made.key, made.status) == ("compilation", "comp", "proposed")
    assert made.title == "NIGHT OWLS Oct 20, 2026 - Best of Late night stream"
    assert (made.start, made.end, cuts.parse(made.cuts)) == (0, 230, [(30, 200)])
    assert made.playlist_id == "PLclips"
    assert made.description == "- Moment 0\n- Moment 2"
    assert due(settings, vod_id) is False
    assert w.compile_next() is False  # made once


def test_a_show_that_takes_it_without_review_sends_it_to_render(settings):
    vod_id = stream(settings, ["approved", "uploaded"], auto="upload")
    assert worker_module.Worker(settings).compile_next() is True
    assert clips(settings, vod_id)[-1].status == "approved"


def test_with_one_approved_clip_there_is_none_and_it_stops_waiting(settings):
    vod_id = stream(settings, ["approved", "rejected"])
    assert worker_module.Worker(settings).compile_next() is True
    assert [c.kind for c in clips(settings, vod_id)] == ["highlight", "highlight"]
    assert due(settings, vod_id) is False


def test_streams_not_due_one_get_none(settings):
    vod_id = stream(settings, ["approved", "approved"], due=False)
    assert worker_module.Worker(settings).compile_next() is False
    assert len(clips(settings, vod_id)) == 2


def test_the_streams_files_stay_until_its_compilation_is_made(settings):
    stream(settings, ["uploaded", "uploaded"])
    w = worker_module.Worker(settings)
    w.app_setting = lambda key: 0  # no grace period
    assert w.clean_up_next() is False
    with Session(get_engine(settings)) as s:
        s.get(Vod, 1).compilation_due = False
        s.commit()
    assert w.clean_up_next() is True


def notes(monkeypatch) -> list:
    sent = []
    monkeypatch.setattr(worker_module.notify, "send", lambda settings, note: sent.append(note))
    return sent


def set_clip(settings, key, vod_id=1, **fields):
    with Session(get_engine(settings)) as s:
        c = s.exec(select(ClipRecord).where(ClipRecord.key == key, ClipRecord.vod_id == vod_id)).one()
        for k, v in fields.items():
            setattr(c, k, v)
        s.add(c)
        s.commit()


def test_compile_waits_ten_seconds_after_the_last_decision(settings):
    vod_id = stream(settings, ["kept", "kept", "rejected"], reviewed_at=long_ago())
    set_clip(settings, "hl02", reviewed_at=utcnow())  # just rejected: Undo can still take it back
    w = worker_module.Worker(settings)
    assert w.compile_next() is False
    assert due(settings, vod_id) is True
    set_clip(settings, "hl02", reviewed_at=long_ago())
    assert w.compile_next() is True
    assert clips(settings, vod_id)[-1].kind == "compilation"


def test_compilation_records_what_it_was_first_made_as(settings):
    vod_id = stream(settings, ["kept", "approved", "kept"], reviewed_at=long_ago())
    assert worker_module.Worker(settings).compile_next() is True
    made = clips(settings, vod_id)[-1]
    assert (made.start, made.end, cuts.parse(made.cuts)) == (0, 230, [(30, 100), (130, 200)])
    assert (made.picked_start, made.picked_end, cuts.parse(made.picked_cuts)) == (0, 230, [(30, 100), (130, 200)])


def test_one_kept_clip_makes_no_compilation_and_says_so(settings, monkeypatch):
    sent = notes(monkeypatch)
    vod_id = stream(settings, ["kept", "rejected"], reviewed_at=long_ago())
    w = worker_module.Worker(settings)
    assert w.compile_next() is True
    assert [c.status for c in clips(settings, vod_id)] == ["kept", "rejected"]
    assert [(n.title, n.message, n.link) for n in sent] == [(
        "Only one clip kept: Late night stream",
        "Only one clip was kept from Late night stream. Keep another, or post this one by itself.",
        f"videos/{vod_id}",
    )]
    assert due(settings, vod_id) is False
    assert w.compile_next() is False
    assert len(sent) == 1


def test_one_clip_posted_by_itself_sends_no_note(settings, monkeypatch):
    sent = notes(monkeypatch)
    stream(settings, ["approved", "rejected"], reviewed_at=long_ago())
    assert worker_module.Worker(settings).compile_next() is True
    assert sent == []


def test_keeping_another_after_one_makes_the_compilation(settings, monkeypatch):
    notes(monkeypatch)
    vod_id = stream(settings, ["kept", "rejected"], reviewed_at=long_ago())
    w = worker_module.Worker(settings)
    assert w.compile_next() is True
    set_clip(settings, "hl01", status="kept")  # as the Keep button does, a while ago
    with Session(get_engine(settings)) as s:
        vod = s.get(Vod, vod_id)
        compilation.mark_due(vod, s.exec(select(ClipRecord).where(ClipRecord.vod_id == vod_id)).all())
        s.add(vod)
        s.commit()
    assert due(settings, vod_id) is True
    assert w.compile_next() is True
    assert [c.kind for c in clips(settings, vod_id)] == ["highlight", "highlight", "compilation"]


def test_none_kept_does_nothing(settings, monkeypatch):
    sent = notes(monkeypatch)
    vod_id = stream(settings, ["rejected", "rejected"], reviewed_at=long_ago())
    assert worker_module.Worker(settings).compile_next() is True
    assert len(clips(settings, vod_id)) == 2 and sent == []
    assert due(settings, vod_id) is False


# --- Out of date, and Rebuild -----------------------------------------------------------


def made(settings, statuses=("kept", "kept", "kept"), auto="review"):
    """A stream whose compilation the worker has made, at 0-230 with the gaps (30, 100) and (130, 200)."""
    vod_id = stream(settings, list(statuses), reviewed_at=long_ago(), auto=auto)
    assert worker_module.Worker(settings).compile_next() is True
    return vod_id, clips(settings, vod_id)[-1].id


def changed(settings, vod_id):
    found = clips(settings, vod_id)
    made_one = next(c for c in found if c.kind == "compilation")
    return compilation.is_changed(made_one, compilation.plan(found, "{title}"))


def test_a_compilation_is_changed_once_a_kept_clip_is_rejected(settings):
    vod_id, _ = made(settings)
    assert changed(settings, vod_id) is False
    set_clip(settings, "hl01", status="rejected")
    assert changed(settings, vod_id) is True
    set_clip(settings, "hl00", status="rejected")  # one left: no compilation would be made now
    assert changed(settings, vod_id) is True


def test_one_made_before_it_recorded_its_plan_is_never_changed(settings):
    vod_id, _ = made(settings)
    set_clip(settings, "comp", picked_start=None, picked_end=None, picked_cuts="")
    set_clip(settings, "hl01", status="rejected")
    assert changed(settings, vod_id) is False


def test_it_is_not_changed_by_its_own_edits(settings):
    vod_id, _ = made(settings)
    set_clip(settings, "comp", start=10, end=220, cuts=cuts.dump([(30, 100), (110, 120), (130, 200)]))
    assert changed(settings, vod_id) is False
    set_clip(settings, "hl01", end=125)  # but a kept clip's edit does
    assert changed(settings, vod_id) is True


@pytest.fixture
def client(settings):
    return TestClient(web_app.create_app(settings))


def test_rebuild_takes_the_new_clips_and_keeps_the_owners_cuts(settings, client):
    vod_id, comp_id = made(settings, ("kept", "kept", "kept", "rejected"))
    # The owner started it later and cut a bit out of the third clip; then kept the fourth.
    set_clip(settings, "comp", start=10, cuts=cuts.dump([(30, 100), (130, 200), (210, 220)]))
    set_clip(settings, "hl03", status="kept")
    assert client.get(f"/api/clips/{comp_id}").json()["compilation_changed"] is True
    rebuilt = client.post(f"/api/clips/{comp_id}/rebuild")
    assert rebuilt.status_code == 200
    out = rebuilt.json()
    assert (out["start"], out["end"], out["cuts"]) == (10, 330, [[30, 100], [130, 200], [210, 220], [230, 300]])
    assert out["status"] == "proposed" and out["compilation_clips"] == 4 and out["compilation_changed"] is False
    # Its chapters start where each clip does after the owner's cuts (10-30 runs 20 s, 200-230 lost 210-220).
    assert out["description"] == "Chapters\n0:00 Moment 0\n0:20 Moment 1\n0:50 Moment 2\n1:10 Moment 3"
    c = next(c for c in clips(settings, vod_id) if c.id == comp_id)
    assert (c.picked_start, c.picked_end, cuts.parse(c.picked_cuts)) == (0, 330, [(30, 100), (130, 200), (230, 300)])
    assert "4 " in c.why


def test_rebuild_keeps_title_channel_and_playlist(settings, client):
    vod_id, comp_id = made(settings, ("kept", "kept", "kept"), auto="upload")
    with Session(get_engine(settings)) as s:
        dest = Destination(name="Clips channel", url="https://youtube.com/@clips", channel_id="UCclips")
        s.add(dest)
        s.commit()
        dest_id = dest.id
    video = settings.vods_dir / "kickvod-1" / "clips" / "comp.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    set_clip(settings, "comp", title="My best of", destination_id=dest_id, playlist_id="PLmine",
             status="rendered", video_path=str(video), approved_in_review=True)
    set_clip(settings, "hl01", status="rejected")
    out = client.post(f"/api/clips/{comp_id}/rebuild").json()
    assert (out["title"], out["destination_id"], out["playlist_id"]) == ("My best of", dest_id, "PLmine")
    assert out["status"] == "approved" and not out["has_video"] and not video.exists()  # renders again
    assert out["description"] == "- Moment 0\n- Moment 2"


def test_rebuild_refusals(settings, client):
    vod_id, comp_id = made(settings, ("kept", "kept"))
    assert client.post(f"/api/clips/{clips(settings, vod_id)[0].id}/rebuild").status_code == 422
    set_clip(settings, "hl01", status="rejected")
    one = client.post(f"/api/clips/{comp_id}/rebuild")
    assert one.status_code == 409
    assert one.json()["detail"] == "Only one clip is kept now. Post it by itself, or reject the compilation."
    set_clip(settings, "hl01", status="kept")
    set_clip(settings, "comp", status="uploaded")
    assert client.post(f"/api/clips/{comp_id}/rebuild").status_code == 409
    set_clip(settings, "comp", status="proposed")
    with Session(get_engine(settings)) as s:
        s.get(Vod, vod_id).cleaned_at = utcnow()
        s.commit()
    assert client.post(f"/api/clips/{comp_id}/rebuild").status_code == 409


@pytest.mark.parametrize(("statuses", "standing", "kept", "to_review", "count"), [
    (["kept", "proposed", "approved"], "waiting", [[0, 30], [200, 230]], [[100, 130]], 2),
    (["kept", "rejected", "kept"], "making", [[0, 30], [200, 230]], [], 2),
    (["kept", "rejected"], "one", [[0, 30]], [], 1),
    (["rejected", "extra"], "none", [], [], 0),
])
def test_compilation_out_lists_kept_and_to_review_ranges_and_its_standing(
        settings, client, statuses, standing, kept, to_review, count):
    vod_id = stream(settings, statuses)
    with Session(get_engine(settings)) as s:
        s.get(Vod, vod_id).duration = 3600.0
        s.commit()
    detail = client.get(f"/api/vods/{vod_id}").json()
    assert detail["vod"]["compilation"] == {
        "compilation_mode": False, "vod_id": vod_id, "vod_title": "Late night stream", "vod_duration": 3600.0, "kept": kept, "to_review": to_review, "clips": count,
        "length": 30.0 * len(kept), "clip_id": None, "status": standing, "changed": False,
    }
    assert {c["in_compilation"] for c in detail["clips"]} == {True}


def test_once_made_compilation_out_is_the_compilations_own(settings, client):
    vod_id, comp_id = made(settings)
    set_clip(settings, "comp", cuts=cuts.dump([(10, 20), (30, 100), (130, 200)]))
    out = client.get(f"/api/vods/{vod_id}").json()["vod"]["compilation"]
    assert (out["clip_id"], out["status"], out["changed"], out["clips"]) == (comp_id, "proposed", False, 3)
    assert out["kept"] == [[0, 10], [20, 30], [100, 130], [200, 230]] and out["length"] == 80
    set_clip(settings, "hl01", status="rejected")
    out = client.get(f"/api/vods/{vod_id}").json()["vod"]["compilation"]
    assert (out["changed"], out["clips"]) == (True, 2)
    comp = client.get(f"/api/clips/{comp_id}").json()
    assert (comp["in_compilation"], comp["compilation_clips"], comp["compilation_changed"]) == (False, 2, True)
    set_clip(settings, "comp", status="uploaded")  # too late to matter
    assert client.get(f"/api/vods/{vod_id}").json()["vod"]["compilation"]["changed"] is False
    assert client.get(f"/api/clips/{comp_id}").json()["compilation_changed"] is False


def test_a_stream_that_makes_no_compilation_has_none(settings, client):
    vod_id = stream(settings, ["approved"], compiling=False)
    detail = client.get(f"/api/vods/{vod_id}").json()
    assert detail["vod"]["compilation"] is None
    assert detail["clips"][0]["in_compilation"] is False and detail["clips"][0]["compilation_clips"] == 0


def add_compilation(settings, vod_id, status):
    with Session(get_engine(settings)) as s:
        s.add(ClipRecord(vod_id=vod_id, key="comp", kind="compilation", start=0, end=230, title="Best of",
                         status=status, cuts=cuts.dump([(30, 200)]), picked_start=0, picked_end=230,
                         picked_cuts=cuts.dump([(30, 200)])))
        s.commit()


def test_compilations_endpoint_only_returns_streams_with_something_to_say(settings, client):
    stream(settings, ["kept", "proposed"], n=1)  # picks to review
    stream(settings, ["kept", "kept"], n=2)  # being made
    stream(settings, ["kept", "rejected"], n=3)  # one kept: keep another or post it
    stream(settings, ["rejected"], n=4)  # nothing to say
    stream(settings, ["kept", "rejected", "kept"], n=5, due=False)
    add_compilation(settings, 5, "proposed")  # to review
    stream(settings, ["kept", "rejected", "kept"], n=6, due=False)
    add_compilation(settings, 6, "rendered")  # past the inbox
    stream(settings, ["approved", "rejected"], n=7)  # its one clip is posted by itself
    stream(settings, ["proposed"], n=8, compiling=False)  # makes none
    found = client.get("/api/compilations").json()
    assert sorted((c["vod_id"], c["status"]) for c in found) == [
        (1, "waiting"), (2, "making"), (3, "one"), (5, "proposed")]
    assert not any(c["changed"] for c in found)  # its plan was stored as whole seconds, the stream's comes as floats


@pytest.mark.parametrize("status", ["rendered", "uploaded", "rejected"])
def test_the_compilations_endpoint_doesnt_work_out_streams_past_the_inbox(settings, client, monkeypatch, status):
    # The inbox asks every 10 s, so months of finished compilations mustn't each be planned again.
    stream(settings, ["kept", "kept", "rejected"], due=False)
    add_compilation(settings, 1, status)
    stream(settings, ["approved", "uploaded"], n=2, due=False)
    add_compilation(settings, 2, "uploaded")

    def planned(*args):
        raise AssertionError("planned a compilation past the inbox")

    monkeypatch.setattr(compilation, "plan", planned)
    monkeypatch.setattr(compilation, "kept_parts", planned)
    assert client.get("/api/compilations").json() == []


def test_compilation_mode_reviews_every_piece_and_rebuilds_with_the_same_gap(settings, client):
    vod_id = stream(settings, ["proposed", "proposed", "proposed"])
    with Session(get_engine(settings)) as s:
        s.get(Vod, vod_id).mode = "compilation"
        s.commit()
    set_clip(settings, "hl01", start=31, end=61, cuts="[[45,46]]")
    first, second, third = clips(settings, vod_id)
    worker = worker_module.Worker(settings)

    assert client.post(f"/api/clips/{first.id}/approve").status_code == 422
    first_url = f"/api/clips/{first.id}"
    version = client.get(first_url).json()["updated_at"]
    assert client.post(f"{first_url}/keep", params={"version": version}).json()["status"] == "kept"
    assert client.post(f"{first_url}/reject", params={"version": version}).status_code == 409
    assert client.post(f"/api/clips/{second.id}/keep").json()["status"] == "kept"
    assert worker.compile_next() is False
    decision = client.post(f"/api/clips/{third.id}/reject").json()
    assert decision["status"] == "rejected"
    assert worker.compile_next() is False
    undo = f"/api/clips/{third.id}/restore"
    assert client.post(undo, params={"review_undo": decision["updated_at"]}).json()["status"] == "proposed"
    set_clip(settings, "hl02", reviewed_at=long_ago())
    assert worker.compile_next() is False  # Undo restored the last pending piece.
    newer = client.post(f"/api/clips/{third.id}/reject").json()
    assert client.post(undo, params={"review_undo": decision["updated_at"]}).status_code == 409
    for c in (first, second, third):
        set_clip(settings, c.key, reviewed_at=long_ago())
    assert worker.compile_next() is True
    assert client.post(undo, params={"review_undo": newer["updated_at"]}).status_code == 409

    made = clips(settings, vod_id)[-1]
    out = client.get(f"/api/clips/{made.id}").json()
    assert out["end"] == 61
    assert out["cuts"] == [[45, 46]]  # Joined the one-second gap but respected the editor cut.
    assert not out["compilation_changed"]
    assert client.patch(f"/api/clips/{made.id}", json={"cuts": [[45, 46], [48, 52]]}).status_code == 200
    assert client.post(f"/api/clips/{third.id}/restore").status_code == 200
    rebuild = f"/api/clips/{made.id}/rebuild"
    assert client.post(rebuild).status_code == 409
    assert client.post(f"/api/clips/{third.id}/reject").status_code == 200
    assert client.post(rebuild).status_code == 409
    set_clip(settings, "hl02", reviewed_at=long_ago())
    set_clip(settings, "hl01", end=62)
    assert client.get(f"/api/clips/{made.id}").json()["compilation_changed"]
    rebuilt = client.post(f"/api/clips/{made.id}/rebuild").json()
    assert rebuilt["cuts"] == [[45, 46], [48, 52]]
    assert rebuilt["end"] == 62
    assert not rebuilt["compilation_changed"]


def test_compilation_mode_does_not_fill_a_small_gap_over_a_trimmed_piece_end():
    first = clip("hl01", 0, 30, status="kept")
    first.picked_end, first.picked_start = 31, 0
    second = clip("hl02", 31, 50, status="kept")
    plan = compilation.plan([first, second], "", gap=1.5)
    assert plan.cuts == [(30, 31)]


def test_compilation_mode_can_publish_a_single_kept_piece_after_undo_closes(settings, client):
    vod_id = stream(settings, ["proposed", "rejected"])
    with Session(get_engine(settings)) as s:
        s.get(Vod, vod_id).mode = "compilation"
        s.commit()
    first = clips(settings, vod_id)[0]
    assert client.post(f"/api/clips/{first.id}/keep").status_code == 200
    assert client.get(f"/api/vods/{vod_id}").json()["vod"]["compilation"]["status"] == "making"
    worker = worker_module.Worker(settings)
    assert not worker.compile_next()
    set_clip(settings, first.key, reviewed_at=long_ago())
    assert worker.compile_next()
    made = clips(settings, vod_id)[-1]
    assert (made.kind, made.status, made.start, made.end) == ("compilation", "proposed", 0, 30)
    assert not client.get(f"/api/clips/{made.id}").json()["compilation_changed"]
    assert client.patch(f"/api/clips/{first.id}", json={"start": 15.06, "end": 16.06}).status_code == 200
    set_clip(settings, first.key, reviewed_at=long_ago())
    rebuilt = client.post(f"/api/clips/{made.id}/rebuild")
    assert rebuilt.status_code == 200
    assert (rebuilt.json()["start"], rebuilt.json()["end"]) == (15.06, 16.06)
