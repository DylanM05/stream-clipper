"""Reading how uploads do on YouTube, and what the app makes of it."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from clipper import stats
from clipper import worker as worker_module
from clipper.config import Settings
from clipper.db import ClipRecord, Destination, Show, UploadJob, Vod, get_app_setting, get_engine, set_app_setting, utcnow
from clipper.web import app as web_app
from clipper.youtube import Stats

NOW = utcnow()


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, discord_webhook_url="https://discord.example/webhook", google_client_id="client",
                    google_client_secret="secret")


@pytest.fixture
def show(settings):
    with Session(get_engine(settings)) as s:
        s.add_all([Destination(name="Clips", youtube_token="refresh"), Destination(name="Studio channel"),
                   Show(name="Don", title_template="{title} | Don Clips | {date}")])
        s.commit()
        s.add(Vod(url="https://kick.com/don/videos/1", show_id=1, status="ready", duration=3600))
        s.commit()


def add_upload(settings, title="A | Don Clips | Oct 25, 2026", score=0.8, kind="chapter", dest=1, age=timedelta(hours=1),
               **fields) -> int:
    with Session(get_engine(settings)) as s:
        clip = ClipRecord(vod_id=1, key="ch01", kind=kind, start=0, end=60, title=title, score=score, destination_id=dest,
                          status="uploaded")
        s.add(clip)
        s.commit()
        job = UploadJob(clip_id=clip.id, status="done", youtube_url=f"https://youtu.be/vid{clip.id:08d}",
                        finished_at=NOW - age, **fields)
        s.add(job)
        s.commit()
        return job.id


def job(settings, job_id) -> UploadJob:
    with Session(get_engine(settings)) as s:
        return s.get(UploadJob, job_id)


class FakeStatsGoogle:
    def __init__(self, views=100):
        self.views = views
        self.asked = []

    def video_stats(self, video_ids, refresh_token="", api_key=""):
        self.asked.append((sorted(video_ids), refresh_token, api_key))
        return {v: Stats(self.views, 5, 2) for v in video_ids}


def test_views_are_kept_at_a_day_and_a_week_old():
    job_ = UploadJob(clip_id=1, finished_at=NOW - timedelta(hours=25))
    assert stats.due(job_, NOW)
    stats.record(job_, Stats(500, 20, 4), NOW)
    assert (job_.views, job_.views_day1, job_.views_week1, job_.likes) == (500, 500, None, 20)
    assert not stats.due(job_, NOW + timedelta(hours=1))  # read again in a few hours
    assert stats.due(job_, NOW + stats.REFRESH)

    week = NOW + timedelta(days=6, hours=1)
    stats.record(job_, Stats(2000, 60, 9), week)
    assert (job_.views_day1, job_.views_week1) == (500, 2000)

    old = UploadJob(clip_id=1, finished_at=NOW - timedelta(days=3))  # from before views were read
    stats.record(old, Stats(900, 1, 1), NOW)
    assert (old.views, old.views_day1, old.views_week1) == (900, None, None)
    assert not stats.due(UploadJob(clip_id=1, finished_at=NOW - timedelta(days=40), stats_at=NOW - timedelta(days=2)), NOW)


def test_the_worker_reads_uploads_as_their_channel_or_with_the_api_key(settings, show):
    connected = add_upload(settings)
    studio = add_upload(settings, dest=2)
    fresh = add_upload(settings, stats_at=NOW - timedelta(hours=1))  # read an hour ago: not due
    w = worker_module.Worker(settings)
    w.youtube = FakeStatsGoogle()
    w.check_stats()

    # Without a key, the Studio upload is read with the connected channel's sign-in, in the same request.
    assert w.youtube.asked == [(["vid00000001", "vid00000002"], "refresh", "")]
    assert job(settings, connected).views == 100 and job(settings, studio).views == 100
    assert job(settings, fresh).views is None

    settings.youtube_api_key = "key123"
    add_upload(settings, dest=2)
    w.youtube.asked.clear()
    w.check_stats()
    assert w.youtube.asked == [(["vid00000004"], "", "key123")]


def test_the_monday_summary_goes_once_a_week(settings, show, monkeypatch):
    sent = []
    monkeypatch.setattr(worker_module.notify, "send", lambda settings_, note: sent.append(note))
    add_upload(settings, title="Huck Blend Is Back", views=12_300)
    add_upload(settings, title="Jer Wins", views=800)
    add_upload(settings, title="Old one", views=99_000, age=timedelta(days=9))
    w = worker_module.Worker(settings)
    tz = ZoneInfo(settings.timezone)

    w.send_digest(datetime(2026, 10, 25, 10, tzinfo=tz))  # a Sunday
    w.send_digest(datetime(2026, 10, 26, 8, tzinfo=tz))  # Monday, too early
    assert sent == []
    w.send_digest(datetime(2026, 10, 26, 9, 30, tzinfo=tz))
    w.send_digest(datetime(2026, 10, 26, 12, tzinfo=tz))
    [note] = sent
    assert note.title == "Last week on YouTube" and note.link == "stats"
    assert note.message == "2 uploads · 13.1K views so far\n\nMost viewed:\n1. Huck Blend Is Back: 12.3K\n2. Jer Wins: 800"


def test_the_monday_summary_switched_off_is_skipped_and_still_due(settings, show, monkeypatch):
    sent = []
    monkeypatch.setattr(worker_module.notify, "send", lambda settings_, note: sent.append(note))
    add_upload(settings, title="Huck Blend Is Back", views=12_300)
    w = worker_module.Worker(settings)
    monday = datetime(2026, 10, 26, 9, 30, tzinfo=ZoneInfo(settings.timezone))
    with Session(get_engine(settings)) as s:
        set_app_setting(s, "notify_digest", "0")
        s.commit()
    w.send_digest(monday)
    with Session(get_engine(settings)) as s:
        assert get_app_setting(s, "stats_digest_week") is None
        set_app_setting(s, "notify_digest", "1")
        s.commit()
    assert sent == []
    w.send_digest(monday)  # switched back on the same morning: it still goes
    assert [n.kind for n in sent] == ["digest"]

def test_views_by_score_band_and_the_stats_page(settings, show, monkeypatch):
    add_upload(settings, score=0.95, views=10_000, views_day1=4_000)
    add_upload(settings, score=0.92, views=6_000, views_day1=2_000)
    add_upload(settings, score=0.7, views=500, views_day1=100)
    add_upload(settings, score=1, kind="whole", views=1)
    client = TestClient(web_app.create_app(settings))
    body = client.get("/api/stats").json()

    assert body["setup_needed"] == ""
    assert [(b["label"], b["count"], b["day1"], b["views"]) for b in body["bands"]] == [
        ("0.9 and up", 2, 3000, 8000), ("0.8 to 0.89", 0, None, None), ("Under 0.8", 1, 100, 500),
    ]
    assert len(body["uploads"]) == 4 and body["uploads"][0]["kind"] == "whole"  # newest first
    assert client.get("/api/stats", params={"show_id": 2}).json()["uploads"] == []

    settings.google_client_id = ""
    unset = TestClient(web_app.create_app(settings)).get("/api/stats").json()
    assert "CLIPPER_YOUTUBE_API_KEY" in unset["setup_needed"]


def test_big_numbers_read_short():
    assert [stats.compact(n) for n in (999, 1000, 1250, 13_100, 2_500_000)] == ["999", "1K", "1.2K", "13.1K", "2.5M"]
