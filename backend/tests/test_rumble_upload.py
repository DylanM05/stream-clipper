"""Uploading to Rumble through its upload page, against a stand-in for the page (rumble_upload_page.html) in a real
browser, then the worker and app around it with the browser part replaced."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from clipper import rumble_upload
from clipper import worker as worker_module
from clipper.config import Settings
from clipper.db import ClipRecord, Destination, Show, UploadJob, Vod, get_engine
from clipper.rumble_upload import RumbleUpload, RumbleUploadError
from clipper.web import app as web_app

PAGE = (Path(__file__).parent / "rumble_upload_page.html").resolve().as_uri()


@pytest.fixture
def page():
    """A page in a real browser: the one Playwright installs, or else Edge or Chrome as they're installed."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        for channel in (None, "msedge", "chrome"):
            try:
                browser = p.chromium.launch(channel=channel) if channel else p.chromium.launch()
                break
            except Exception:
                continue
        else:
            pytest.skip("no browser for Playwright here")
        yield browser.new_page()
        browser.close()


def files(tmp_path):
    video, thumbnail = tmp_path / "hl01-a-moment.mp4", tmp_path / "hl01-thumbnail.jpg"
    video.write_bytes(b"video")
    thumbnail.write_bytes(b"jpeg")
    return video, thumbnail


def test_the_upload_page_is_filled_in_step_by_step(page, tmp_path):
    video, thumbnail = files(tmp_path)
    job = RumbleUpload(file=video, title="A Moment", description="From the stream.", visibility="unlisted",
                       category="Entertainment", thumbnail=thumbnail)
    progress, sent = [], [False]
    link = rumble_upload.fill_in(page, job, progress.append, sent, page_url=PAGE)

    assert link == "https://rumble.com/v6test1-a-moment.html" and sent == [True]
    assert page.evaluate("window.sent") == {
        "file": "hl01-a-moment.mp4", "title": "A Moment", "description": "From the stream.",
        "category": "15",  # Entertainment
        "thumbnail": "hl01-thumbnail.jpg", "visibility": "visibility_unlisted",
        "licence": "Rumble Only", "push": False,
    }
    assert progress and progress[-1] == 1.0
    assert page.url.startswith(PAGE.split("?")[0])  # the terms link inside the checkbox's label wasn't followed


def test_an_error_rumble_shows_is_what_the_upload_says(page, tmp_path):
    video, thumbnail = files(tmp_path)
    job = RumbleUpload(file=video, title="A Moment", description="", visibility="public", category="News",
                       thumbnail=thumbnail)
    sent = [False]
    with pytest.raises(RumbleUploadError, match="Title is too long"):
        rumble_upload.fill_in(page, job, lambda _: None, sent, page_url=PAGE + "?fail=Title%20is%20too%20long")
    assert sent == [True]  # the upload function then warns it may be on Rumble already, and doesn't retry


# --- The worker and the app, with the browser part replaced ---


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", web_dir=tmp_path / "web")


@pytest.fixture
def clip_id(settings):
    clips = settings.vods_dir / "vod-1" / "clips"
    clips.mkdir(parents=True)
    video = clips / "hl01-a-moment.mp4"
    video.write_bytes(b"video")
    with Session(get_engine(settings)) as s:
        dest = Destination(name="Kino Klips", channel_id="UCklips", rumble=True, rumble_category="Podcasts")
        vod = Vod(url="https://kick.com/kino/videos/1", status="ready", workspace="vod-1", duration=600)
        s.add_all([dest, vod])
        s.commit()
        clip = ClipRecord(vod_id=vod.id, key="hl01", kind="highlight", start=1, end=40, title="A Moment",
                          description="From the stream.", status="rendered", video_path=str(video), destination_id=dest.id)
        s.add(clip)
        s.commit()
        return clip.id


def get(settings, model, id_):
    with Session(get_engine(settings)) as s:
        return s.get(model, id_)


class FakeYouTube:
    def __init__(self):
        self.calls = []

    def upload(self, refresh_token, video, on_progress=lambda _: None, session_url="", on_session=lambda _: None):
        from clipper.youtube import Uploaded
        self.calls.append(video)
        return Uploaded("vid123", "https://youtu.be/vid123", video.visibility)


def test_the_worker_uploads_to_rumble_with_a_frame_as_thumbnail_and_keeps_the_file_for_youtube(settings, clip_id, monkeypatch):
    uploaded, frames = [], []

    def fake_upload(cdp_url, job, screenshots_dir, on_progress=lambda _: None):
        assert job.thumbnail.exists()  # a frame of the video: the clip has no thumbnail of its own
        uploaded.append(job)
        return "https://rumble.com/v6test1-a-moment.html"

    monkeypatch.setattr(worker_module.rumble_upload, "upload", fake_upload)
    monkeypatch.setattr(worker_module, "probe_duration", lambda path: 39.0)
    monkeypatch.setattr(worker_module.ffmpeg, "run", lambda args: (frames.append(args), Path(args[-1]).write_bytes(b"jpeg")))
    with Session(get_engine(settings)) as s:
        s.add_all([UploadJob(clip_id=clip_id, site="rumble", visibility="unlisted"), UploadJob(clip_id=clip_id)])
        s.commit()
    w = worker_module.Worker(settings)
    w.youtube = FakeYouTube()
    dest = get(settings, Destination, 1)
    dest.youtube_token = "refresh"
    with Session(get_engine(settings)) as s:
        s.add(dest)
        s.commit()

    assert w.upload_next() is True  # Rumble's, queued first
    [job] = uploaded
    assert (job.title, job.visibility, job.category) == ("A Moment", "unlisted", "Podcasts")
    assert frames[0][:2] == ["-ss", "13.000"] and not job.thumbnail.exists()  # a third of the way in, then tidied up
    clip = get(settings, ClipRecord, clip_id)
    assert (clip.rumble_url, clip.youtube_url, clip.status) == ("https://rumble.com/v6test1-a-moment.html", "", "uploaded")
    assert Path(clip.video_path).exists()  # YouTube's upload still needs it
    assert get(settings, UploadJob, 1).method == "browser"

    assert w.upload_next() is True  # YouTube's
    clip = get(settings, ClipRecord, clip_id)
    assert clip.youtube_url == "https://youtu.be/vid123" and clip.video_path == ""  # up everywhere: deleted


def test_a_failed_rumble_upload_before_submitting_tries_again_tomorrow(settings, clip_id, monkeypatch):
    def refuse(*args, **kwargs):
        raise RumbleUploadError("The upload browser isn't signed in to Rumble.", screenshot="/data/shot.png", retry=True)

    monkeypatch.setattr(worker_module.rumble_upload, "upload", refuse)
    monkeypatch.setattr(worker_module, "probe_duration", lambda path: 39.0)
    monkeypatch.setattr(worker_module.ffmpeg, "run", lambda args: Path(args[-1]).write_bytes(b"jpeg"))
    with Session(get_engine(settings)) as s:
        s.add(UploadJob(clip_id=clip_id, site="rumble"))
        s.commit()
    worker_module.Worker(settings).upload_next()
    job = get(settings, UploadJob, 1)
    assert (job.status, job.screenshot) == ("failed", "/data/shot.png") and job.retry_at is not None


@pytest.mark.parametrize("youtube, rumble, sites", [
    (True, True, ["rumble", "youtube"]), (False, True, ["rumble"]), (True, False, ["youtube"]),
])
def test_a_show_that_uploads_by_itself_goes_where_its_channels_uploads_go(settings, clip_id, youtube, rumble, sites):
    with Session(get_engine(settings)) as s:
        show = Show(name="Kino", auto_shorts="upload", auto_chapters="upload")
        s.add(show)
        s.commit()
        vod, dest = s.get(Vod, 1), s.get(Destination, 1)
        vod.show_id, dest.youtube, dest.rumble = show.id, youtube, rumble
        s.add_all([vod, dest])
        s.commit()
    worker_module.Worker(settings)._auto_upload(clip_id)
    with Session(get_engine(settings)) as s:
        assert sorted(j.site for j in s.exec(select(UploadJob))) == sites


def test_rumble_and_youtube_keep_their_own_turns(settings, clip_id):
    from datetime import timedelta

    from clipper.db import utcnow
    from clipper.schedule import upload_turns

    with Session(get_engine(settings)) as s:
        show = Show(name="Kino", upload_gap_hours=2)
        s.add(show)
        s.commit()
        vod = s.get(Vod, 1)
        vod.show_id = show.id
        earlier = ClipRecord(vod_id=1, key="hl02", kind="highlight", start=0, end=5, title="Earlier", status="uploaded",
                             destination_id=1)
        s.add_all([vod, earlier])
        s.commit()
        s.add(UploadJob(clip_id=earlier.id, status="done", started_at=utcnow() - timedelta(hours=1)))  # on YouTube
        youtube, rumble = UploadJob(clip_id=clip_id), UploadJob(clip_id=clip_id, site="rumble")
        s.add_all([youtube, rumble])
        s.commit()
        turns = upload_turns(s)
        assert turns[youtube.id].reason == "gap"  # an hour after the last on YouTube
        assert turns[rumble.id].reason == ""  # nothing on Rumble yet: it goes now


def test_choosing_rumble_in_the_app(settings, clip_id):
    client = TestClient(web_app.create_app(settings))
    assert client.get("/api/rumble").json()["licence"] == "Rumble Only"

    rumble = client.post(f"/api/clips/{clip_id}/uploads", json={"which": "video", "site": "rumble"})
    assert rumble.status_code == 201 and rumble.json()["site"] == "rumble"
    assert client.post(f"/api/clips/{clip_id}/uploads", json={"site": "rumble"}).status_code == 409  # already going
    assert client.post(f"/api/clips/{clip_id}/uploads", json={"site": "youtube"}).status_code == 201  # both

    dest = client.patch("/api/destinations/1", json={"name": "Kino Klips", "rumble": False}).json()
    assert (dest["youtube"], dest["rumble"], dest["rumble_category"]) == (True, False, "Podcasts")  # YouTube only now
    client.delete(f"/api/upload-jobs/{rumble.json()['id']}")
    res = client.post(f"/api/clips/{clip_id}/uploads", json={"site": "rumble"})
    assert res.status_code == 409 and "go to Rumble" in res.json()["detail"]

    dest = client.patch("/api/destinations/1", json={"name": "Kino Klips", "youtube": False, "rumble": True}).json()
    assert (dest["youtube"], dest["rumble"]) == (False, True)  # Rumble only
    res = client.patch("/api/destinations/1", json={"name": "Kino Klips", "rumble": False})
    assert res.status_code == 422 and "YouTube, Rumble or both" in res.json()["detail"]  # somewhere, at least
    assert client.patch("/api/destinations/1", json={"name": "Kino Klips", "rumble_category": "Cats"}).status_code == 422
