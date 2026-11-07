"""Spacing a show's uploads out, so a channel gets one clip every few hours."""

from datetime import timedelta

import pytest
from sqlmodel import Session

from clipper import worker as worker_module
from clipper.config import Settings
from clipper.db import ClipRecord, Destination, Show, UploadJob, Vod, get_engine, utcnow
from clipper.schedule import THUMBNAIL_WAIT, upload_turns

NOW = utcnow()


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path)


@pytest.fixture
def show(settings):
    """A show that spaces its clips 3 hours apart, with two channels."""
    with Session(get_engine(settings)) as s:
        s.add_all([Destination(name="Clips"), Destination(name="Other clips"), Show(name="Kino", upload_gap_hours=3)])
        s.commit()
        s.add(Vod(url="https://kick.com/kino/videos/1", show_id=1, status="ready", duration=3600))
        s.commit()


def add_job(settings, kind="chapter", dest=1, minutes_ago=10, **fields) -> int:
    with Session(get_engine(settings)) as s:
        clip = ClipRecord(vod_id=1, key=f"c{minutes_ago}", kind=kind, start=0, end=60, title="t", destination_id=dest,
                          status="rendered", thumbnail_status=fields.pop("thumbnail_status", ""))
        s.add(clip)
        s.commit()
        job = UploadJob(clip_id=clip.id, created_at=NOW - timedelta(minutes=minutes_ago), **fields)
        s.add(job)
        s.commit()
        return job.id


def turns(settings, now=NOW):
    with Session(get_engine(settings)) as s:
        return upload_turns(s, now)


def test_a_shows_clips_go_up_a_gap_apart_on_each_channel(settings, show):
    add_job(settings, status="done", started_at=NOW - timedelta(hours=1), minutes_ago=70)
    first, second = add_job(settings, minutes_ago=30), add_job(settings, minutes_ago=20)
    elsewhere = add_job(settings, dest=2, minutes_ago=10)

    t = turns(settings)
    assert (t[first].at, t[first].reason) == (NOW + timedelta(hours=2), "gap")  # 3 hours after the last one started
    assert (t[second].at, t[second].reason) == (NOW + timedelta(hours=5), "gap")
    assert (t[elsewhere].at, t[elsewhere].reason) == (NOW, "")  # another channel takes its own turns


def test_full_episodes_and_upload_now_dont_wait(settings, show):
    add_job(settings, status="done", started_at=NOW, minutes_ago=70)
    episode = add_job(settings, kind="whole", minutes_ago=40)
    rushed = add_job(settings, rush=True, minutes_ago=30)
    after = add_job(settings, minutes_ago=20)

    t = turns(settings)
    assert t[episode].at == NOW and t[rushed].at == NOW
    assert t[after].at == NOW + timedelta(hours=3)  # the rushed one still took a turn


def test_a_video_waits_a_while_for_its_thumbnail(settings, show):
    job = add_job(settings, thumbnail_status="making", minutes_ago=10)
    short = add_job(settings, dest=2, which="short", thumbnail_status="making", minutes_ago=5)

    t = turns(settings)
    assert (t[job].reason, t[job].at) == ("thumbnail", NOW - timedelta(minutes=10) + THUMBNAIL_WAIT)
    assert t[short].at == NOW  # Shorts don't get thumbnails
    assert turns(settings, NOW + THUMBNAIL_WAIT)[job].reason == ""  # it doesn't wait forever


def test_a_retried_upload_doesnt_wait_for_its_own_failed_attempt(settings, show):
    job = add_job(settings, started_at=NOW - timedelta(minutes=5), minutes_ago=30)  # failed, then retried
    add_job(settings, status="failed", started_at=NOW - timedelta(minutes=2), minutes_ago=20)
    assert turns(settings)[job].at == NOW


def test_without_spacing_everything_goes_as_it_comes(settings, show):
    with Session(get_engine(settings)) as s:
        s.get(Show, 1).upload_gap_hours = 0
        s.commit()
    add_job(settings, status="done", started_at=NOW, minutes_ago=70)
    jobs = add_job(settings, minutes_ago=10), add_job(settings, minutes_ago=5)
    assert [turns(settings)[j].at for j in jobs] == [NOW, NOW]


def test_the_worker_starts_only_uploads_whose_turn_has_come(settings, show, monkeypatch):
    started = []
    monkeypatch.setattr(worker_module.Worker, "run_upload", lambda self, job_id: started.append(job_id))
    first, second = add_job(settings, minutes_ago=30), add_job(settings, minutes_ago=20)
    w = worker_module.Worker(settings)

    assert w.upload_next() is True and started == [first]
    with Session(get_engine(settings)) as s:
        assert s.get(UploadJob, first).started_at is not None
    assert w.upload_next() is False  # the second waits 3 hours
    assert started == [first]


def test_failed_uploads_that_retry_keep_their_place_in_line_and_the_shows_spacing(settings, show):
    # Two clips failed on the channel's daily limit; both retry at midnight, which has come. A newer clip queued since.
    midnight = NOW - timedelta(minutes=1)
    first = add_job(settings, status="failed", retry_at=midnight, minutes_ago=300, error="Daily limit.")
    second = add_job(settings, status="failed", retry_at=midnight, minutes_ago=290, error="Daily limit.")
    newer = add_job(settings, minutes_ago=10)
    got = turns(settings)
    assert got[first].at == NOW
    assert (got[second].at, got[second].reason) == (NOW + timedelta(hours=3), "gap")  # spaced like any other clips
    assert got[newer].at == NOW + timedelta(hours=6)  # behind them


def test_a_retry_waits_for_its_retry_time(settings, show):
    later = NOW + timedelta(hours=5)
    job = add_job(settings, status="failed", retry_at=later, minutes_ago=30)
    gone = add_job(settings, status="failed", minutes_ago=20)  # not retried by itself: not in the line
    got = turns(settings)
    assert (got[job].at, got[job].reason) == (later, "retry") and gone not in got


def test_the_worker_retries_only_the_failed_uploads_whose_turn_has_come(settings, show, monkeypatch):
    started = []
    monkeypatch.setattr(worker_module.Worker, "run_upload", lambda self, job_id: started.append(job_id))
    midnight = utcnow() - timedelta(minutes=1)
    first = add_job(settings, status="failed", retry_at=midnight, minutes_ago=300)
    second = add_job(settings, status="failed", retry_at=midnight, minutes_ago=290)
    w = worker_module.Worker(settings)
    assert w.upload_next() is True and started == [first]
    with Session(get_engine(settings)) as s:
        job = s.get(UploadJob, second)
        assert (job.status, job.auto_retries) == ("failed", 0) and job.retry_at is not None  # waits its turn, 3 hours on
    assert w.upload_next() is False
