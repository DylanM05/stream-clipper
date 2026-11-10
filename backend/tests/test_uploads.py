"""Uploads through YouTube Studio: the queue, the worker's handling, and the API. Studio itself is faked."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from clipper import worker as worker_module
from clipper.config import Settings
from clipper.db import ClipRecord, Destination, Show, UploadJob, Vod, get_engine
from clipper.studio import StudioError, read_status, resolve_channel_id, write_status
from clipper.web import app as web_app


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", web_dir=tmp_path / "web")


@pytest.fixture
def clip_id(settings):
    video = settings.data_dir / "clip.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    with Session(get_engine(settings)) as s:
        dest = Destination(name="Kino Clips", channel_id="UC1234567890123456789012")
        vod = Vod(url="https://kick.com/kino/videos/1", status="ready", duration=600)
        s.add_all([dest, vod])
        s.commit()
        clip = ClipRecord(vod_id=vod.id, key="hl01", kind="highlight", start=1, end=40, title="Huck Blend Is Back",
                          description="Jer returns.", status="rendered", video_path=str(video), destination_id=dest.id)
        s.add(clip)
        s.commit()
        return clip.id


class FakeStudio:
    def __init__(self, error: StudioError | None = None):
        self.error = error
        self.uploads = []

    def upload(self, job, on_progress=lambda _: None):
        self.uploads.append(job)
        on_progress(0.5)
        if self.error:
            raise self.error
        return "https://youtu.be/abc123"

    def status(self):
        return {"browser": True, "signed_in": True}


def get(settings, model, id_):
    with Session(get_engine(settings)) as s:
        return s.get(model, id_)


def test_api_queues_uploads_once_and_reports_the_browser(settings, clip_id):
    client = TestClient(web_app.create_app(settings))
    job = client.post(f"/api/clips/{clip_id}/uploads", json={"visibility": "unlisted"}).json()
    assert (job["status"], job["which"], job["visibility"]) == ("queued", "video", "unlisted")
    assert client.post(f"/api/clips/{clip_id}/uploads", json={}).status_code == 409  # already queued
    assert client.post(f"/api/clips/{clip_id}/uploads", json={"which": "short"}).status_code == 409  # no Short rendered
    assert [j["id"] for j in client.get(f"/api/clips/{clip_id}/uploads").json()] == [job["id"]]
    assert client.delete(f"/api/upload-jobs/{job['id']}").status_code == 204

    assert client.get("/api/studio").json()["browser"] is False
    write_status(settings.studio_dir / "status.json", {"browser": True, "signed_in": True})
    status = client.get("/api/studio").json()
    assert status["browser"] and status["signed_in"] and status["checked_at"]


def test_worker_uploads_to_the_clips_channel_and_records_the_link(settings, clip_id):
    w = worker_module.Worker(settings)
    w.studio = FakeStudio()
    with Session(get_engine(settings)) as s:
        s.add(UploadJob(clip_id=clip_id, visibility="private"))
        s.commit()

    assert w.upload_next() is True
    [upload] = w.studio.uploads
    assert (upload.title, upload.description, upload.visibility, upload.channel_id) == (
        "Huck Blend Is Back", "Jer returns.", "private", "UC1234567890123456789012",
    )
    job = get(settings, UploadJob, 1)
    assert (job.status, job.progress, job.youtube_url) == ("done", 1, "https://youtu.be/abc123")
    clip = get(settings, ClipRecord, clip_id)
    assert (clip.status, clip.youtube_url) == ("uploaded", "https://youtu.be/abc123")
    assert read_status(settings.studio_dir / "status.json")["signed_in"] is True
    assert w.upload_next() is False


def test_every_upload_has_the_shows_footer_under_its_description(settings, clip_id, monkeypatch):
    from clipper.youtube import Uploaded

    with Session(get_engine(settings)) as s:
        show = Show(name="Night Owls", description_footer="Full shows: https://example.com/nightowls")
        s.add(show)
        s.commit()
        show_id = show.id
        vod = s.get(Vod, 1)
        vod.show_id = show_id
        s.add_all([vod, UploadJob(clip_id=clip_id), UploadJob(clip_id=clip_id, site="rumble")])
        s.commit()
    expected = "Jer returns.\n\nFull shows: https://example.com/nightowls"

    w = worker_module.Worker(settings)
    w.studio = FakeStudio()
    assert w.upload_next() is True
    assert w.studio.uploads[0].description == expected  # through YouTube Studio

    rumble = []
    monkeypatch.setattr(worker_module.rumble_upload, "upload", lambda cdp, job, shots, on_progress=None: (
        rumble.append(job), "https://rumble.com/v6test1-a-moment.html")[1])
    monkeypatch.setattr(worker_module, "probe_duration", lambda path: 39.0)
    monkeypatch.setattr(worker_module.ffmpeg, "run", lambda args: Path(args[-1]).write_bytes(b"jpeg"))
    assert w.upload_next() is True
    assert rumble[0].description == expected  # to Rumble

    class FakeGoogle:
        videos = []

        def upload(self, refresh_token, video, on_progress=lambda _: None, session_url="", on_session=lambda _: None):
            self.videos.append(video)
            return Uploaded("vid123", "https://youtu.be/vid123", video.visibility)

    with Session(get_engine(settings)) as s:
        dest = s.get(Destination, 1)
        dest.youtube_token = "refresh"
        s.add_all([dest, UploadJob(clip_id=clip_id, which="video")])
        s.commit()
    w.youtube = FakeGoogle()
    assert w.upload_next() is True
    assert FakeGoogle.videos[0].description == expected  # through the YouTube API

    client = TestClient(web_app.create_app(settings))
    clip = client.get(f"/api/clips/{clip_id}").json()
    assert (clip["description"], clip["upload_description"], clip["description_footer"]) == (
        "Jer returns.", expected, "Full shows: https://example.com/nightowls",  # to copy, for uploading by hand
    )
    edited = client.patch(f"/api/shows/{show_id}", json={"description_footer": "  Subscribe!\n"}).json()
    assert edited["description_footer"] == "  Subscribe!\n"
    assert client.get(f"/api/clips/{clip_id}").json()["upload_description"] == "Jer returns.\n\nSubscribe!"


def test_a_videos_chapter_times_go_up_moved_on_by_its_intro(settings, clip_id):
    with Session(get_engine(settings)) as s:
        clip = s.get(ClipRecord, clip_id)
        clip.description, clip.intro_s = "Jer returns.\n\nChapters\n0:00 Start\n0:30 Huck Blend", 5.0
        s.add_all([clip, UploadJob(clip_id=clip_id)])
        s.commit()
    w = worker_module.Worker(settings)
    w.studio = FakeStudio()
    assert w.upload_next() is True
    moved = "Jer returns.\n\nChapters\n0:00 Start\n0:35 Huck Blend"
    assert w.studio.uploads[0].description == moved
    assert TestClient(web_app.create_app(settings)).get(f"/api/clips/{clip_id}").json()["upload_description"] == moved


def test_a_rendered_file_is_deleted_once_its_uploaded_and_the_upload_is_notified(settings, clip_id, monkeypatch):
    clips_dir = settings.vods_dir / "kickvod-1" / "clips"
    clips_dir.mkdir(parents=True)
    video, short = clips_dir / "hl01-huck.mp4", clips_dir / "hl01-huck-short.mp4"
    video.write_bytes(b"video")
    short.write_bytes(b"short")
    with Session(get_engine(settings)) as s:
        clip = s.get(ClipRecord, clip_id)
        clip.video_path, clip.short_path = str(video), str(short)
        s.get(Vod, clip.vod_id).workspace = "kickvod-1"
        s.add(UploadJob(clip_id=clip_id, which="short"))
        s.commit()
    sent = []
    monkeypatch.setattr(worker_module.notify, "send", lambda settings_, note: sent.append(note))
    w = worker_module.Worker(settings)
    w.studio = FakeStudio()
    w.upload_next()

    assert not short.exists() and video.exists()  # only what went up
    clip = get(settings, ClipRecord, clip_id)
    assert (clip.status, clip.short_path, clip.video_path) == ("uploaded", "", str(video))
    [note] = sent
    assert (note.title, note.message, note.link) == ("Uploaded: Huck Blend Is Back", "Short, public on Kino Clips.", "https://youtu.be/abc123")


def test_uploads_wait_while_paused(settings, clip_id):
    from clipper.db import set_app_setting

    with Session(get_engine(settings)) as s:
        s.add(UploadJob(clip_id=clip_id))
        set_app_setting(s, "paused_uploads", "1")
        s.commit()
    w = worker_module.Worker(settings)
    w.studio = FakeStudio()
    assert w.upload_next() is False and w.studio.uploads == []


def test_failed_uploads_keep_the_error_and_screenshot_and_can_be_retried(settings, clip_id):
    w = worker_module.Worker(settings)
    w.studio = FakeStudio(StudioError("The browser isn't signed in to YouTube.", screenshot="shot.png"))
    with Session(get_engine(settings)) as s:
        s.add(UploadJob(clip_id=clip_id))
        s.commit()
    w.upload_next()

    job = get(settings, UploadJob, 1)
    assert (job.status, job.error, job.screenshot) == ("failed", "The browser isn't signed in to YouTube.", "shot.png")
    assert get(settings, ClipRecord, clip_id).status == "rendered"

    client = TestClient(web_app.create_app(settings))
    assert client.post("/api/upload-jobs/1/retry").json()["status"] == "queued"


def test_an_upload_interrupted_by_a_restart_is_not_retried_automatically(settings, clip_id):
    with Session(get_engine(settings)) as s:
        s.add(UploadJob(clip_id=clip_id, status="uploading"))
        s.commit()
    worker_module.Worker(settings).recover()
    job = get(settings, UploadJob, 1)
    assert job.status == "failed" and "Check YouTube Studio" in job.error


def test_a_closed_upload_browser_gives_a_clear_error_or_is_reopened_on_a_desktop(tmp_path, monkeypatch):
    from clipper import studio as studio_module
    from clipper.studio import NOT_RUNNING, Studio, Upload

    job = Upload(file=tmp_path / "clip.mp4", title="t", description="", visibility="private", channel_id="")
    closed = "http://127.0.0.1:1"  # nothing listens there

    with pytest.raises(StudioError, match="isn't running"):
        Studio(closed, tmp_path).upload(job)

    launched = []

    def no_browser_installed(profile, port):
        launched.append((profile, port))
        raise FileNotFoundError("No Chrome, Chromium or Edge found")

    monkeypatch.setattr(studio_module, "launch_desktop_browser", no_browser_installed)
    with pytest.raises(StudioError) as err:
        Studio(closed, tmp_path, desktop_profile=tmp_path / "profile").upload(job)
    assert str(err.value) == NOT_RUNNING
    assert launched == [(tmp_path / "profile", 1)]


def test_channel_id_from_a_channel_link_needs_no_lookup():
    assert resolve_channel_id("https://www.youtube.com/channel/UCX6OQ3DkcsbYNE6H8uQQuVA/videos") == "UCX6OQ3DkcsbYNE6H8uQQuVA"
    assert resolve_channel_id("https://kick.com/someone") == ""
