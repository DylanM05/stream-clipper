"""Compilations: a stream's approved clips joined into one video, for a show that makes them."""

import pytest
from sqlmodel import Session, select

from clipper import compilation, cuts
from clipper import worker as worker_module
from clipper.config import Settings
from clipper.db import ClipRecord, Show, Vod, get_engine
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


# --- The worker -----------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", web_dir=tmp_path / "web")


def stream(settings, statuses, *, due=True, auto="review"):
    ws = Workspace.open(settings.vods_dir, "kickvod-1")
    write_json(ws.meta_json, {"source_path": str(ws.root / "source.mp4"), "upload_date": "20261020"})
    with Session(get_engine(settings)) as s:
        show = Show(name="Night Owls", title_template="NIGHT OWLS {date} - {title}", compilation=True,
                    auto_compilation=auto, clip_destination_id=None, clip_playlist_id="PLclips")
        s.add(show)
        s.commit()
        vod = Vod(url="https://kick.com/kino/videos/1", status="ready", workspace="kickvod-1", show_id=show.id,
                  title="Late night stream", compilation_due=due)
        s.add(vod)
        s.commit()
        for i, status in enumerate(statuses):
            s.add(ClipRecord(vod_id=vod.id, key=f"hl{i:02d}", kind="highlight", start=i * 100, end=i * 100 + 30,
                             title=f"NIGHT OWLS Oct 20, 2026 - Moment {i}", status=status))
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
