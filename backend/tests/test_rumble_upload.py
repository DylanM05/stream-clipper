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


def test_a_file_chosen_before_the_page_was_ready_is_chosen_again(page, tmp_path, monkeypatch):
    monkeypatch.setattr(rumble_upload, "START_WAIT_S", 2)
    video, thumbnail = files(tmp_path)
    job = RumbleUpload(file=video, title="A Moment", description="", visibility="public", category="News",
                       thumbnail=thumbnail)
    # The page sets its upload up 3 seconds in: after the 2 the uploader gives it, before its second try.
    link = rumble_upload.fill_in(page, job, lambda _: None, [False], page_url=PAGE + "?late=3000")
    assert link.startswith("https://rumble.com/") and page.evaluate("window.sent.file") == "hl01-a-moment.mp4"


def test_an_upload_that_never_starts_says_what_the_page_had(page, tmp_path, monkeypatch):
    monkeypatch.setattr(rumble_upload, "START_WAIT_S", 1)
    video, thumbnail = files(tmp_path)
    job = RumbleUpload(file=video, title="A Moment", description="", visibility="public", category="News",
                       thumbnail=thumbnail)
    sent = [False]
    with pytest.raises(RumbleUploadError, match="didn't start uploading the file.*script error: upload server unavailable"):
        rumble_upload.fill_in(page, job, lambda _: None, sent, page_url=PAGE + "?broken=1")
    assert sent == [False]  # never submitted: safe to try again


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
    assert res.status_code == 409 and "to go to Rumble" in res.json()["detail"]

    dest = client.patch("/api/destinations/1", json={"name": "Kino Klips", "youtube": False, "rumble": True}).json()
    assert (dest["youtube"], dest["rumble"]) == (False, True)  # Rumble only
    res = client.patch("/api/destinations/1", json={"name": "Kino Klips", "rumble": False})
    assert res.status_code == 422 and "YouTube, Rumble or both" in res.json()["detail"]  # somewhere, at least
    assert client.patch("/api/destinations/1", json={"name": "Kino Klips", "rumble_category": "Cats"}).status_code == 422


class _NoBrowser:
    """Stands in for Playwright: records how the upload browser was connected to, then finds nothing there."""

    def __init__(self):
        self.calls = []
        self.chromium = self

    def connect_over_cdp(self, url, **options):
        self.calls.append(options)
        raise ConnectionRefusedError("nothing listening")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_uploads_hand_the_browser_the_file_by_its_path_so_big_files_go(tmp_path, monkeypatch):
    # Over a connection to a browser Playwright doesn't think is local, it sends the file's bytes, and refuses
    # anything over 50 MB: "Cannot transfer files larger than 50Mb to a browser not co-located with the server".
    from clipper import studio

    fake = _NoBrowser()
    monkeypatch.setattr(rumble_upload, "sync_playwright", lambda: fake)
    video, thumbnail = files(tmp_path)
    job = RumbleUpload(file=video, title="t", description="", visibility="public", category="News", thumbnail=thumbnail)
    with pytest.raises(RumbleUploadError):
        rumble_upload.upload("http://127.0.0.1:9222", job, tmp_path)
    monkeypatch.setattr(studio, "sync_playwright", lambda: fake)
    with pytest.raises(studio.StudioError):
        studio.Studio("http://127.0.0.1:9222", tmp_path).upload(studio.Upload(video, "t", "", "public", ""))
    assert fake.calls and all(call.get("is_local") is True for call in fake.calls)


class _Chooser:
    def __init__(self, size):
        self.size = size

    def evaluate(self, script):
        return self.size


def test_a_browser_that_cant_read_the_file_is_said_plainly(tmp_path):
    video, _ = files(tmp_path)  # 5 bytes
    rumble_upload._check_chosen(_Chooser(5), video)  # all of it
    rumble_upload._check_chosen(_Chooser(-1), video)  # the page took it and emptied the chooser: nothing to tell
    with pytest.raises(RumbleUploadError, match="couldn't read .*hl01-a-moment.mp4"):
        rumble_upload._check_chosen(_Chooser(0), video)


@pytest.mark.parametrize("kind, clip_sites, episode_sites, sites", [
    ("highlight", "rumble", "", ["rumble"]),  # the show's choice over its channel's (YouTube only here)
    ("highlight", "both", "", ["rumble", "youtube"]),
    ("highlight", "", "rumble", ["youtube"]),  # not chosen for clips: the channel's
    ("whole", "", "rumble", ["rumble"]),  # full episodes have their own
])
def test_a_show_picks_where_its_uploads_go_after_the_channel(settings, clip_id, kind, clip_sites, episode_sites, sites):
    with Session(get_engine(settings)) as s:
        show = Show(name="Kino", auto_shorts="upload", auto_episode="upload", clip_sites=clip_sites, episode_sites=episode_sites)
        s.add(show)
        s.commit()
        vod, dest, clip = s.get(Vod, 1), s.get(Destination, 1), s.get(ClipRecord, clip_id)
        vod.show_id, dest.rumble, clip.kind = show.id, False, kind
        s.add_all([vod, dest, clip])
        s.commit()
    worker_module.Worker(settings)._auto_upload(clip_id)
    with Session(get_engine(settings)) as s:
        assert sorted(j.site for j in s.exec(select(UploadJob))) == sites
    # The clip's Upload page starts with the same choice, and lets it go to Rumble.
    client = TestClient(web_app.create_app(settings))
    assert client.get(f"/api/clips/{clip_id}").json()["upload_to"] == ("both" if len(sites) == 2 else sites[0])
    if "rumble" in sites:
        for job_id in range(1, len(sites) + 1):  # the automatic ones, so one by hand isn't a second of the same
            client.delete(f"/api/upload-jobs/{job_id}")
        assert client.post(f"/api/clips/{clip_id}/uploads", json={"site": "rumble"}).status_code == 201


def test_a_shows_choice_of_sites_is_saved_and_checked(settings):
    client = TestClient(web_app.create_app(settings))
    show = client.post("/api/shows", json={"name": "Kino", "clip_sites": "both"}).json()
    assert (show["clip_sites"], show["episode_sites"]) == ("both", "")
    assert client.patch(f"/api/shows/{show['id']}", json={"episode_sites": "rumble"}).json()["episode_sites"] == "rumble"
    assert client.patch(f"/api/shows/{show['id']}", json={"clip_sites": ""}).json()["clip_sites"] == ""  # the channel's again
    assert client.patch(f"/api/shows/{show['id']}", json={"clip_sites": "vimeo"}).status_code == 422


def test_an_upload_stuck_at_one_percentage_gives_up_after_a_while(monkeypatch):
    clock = [0.0]

    class Shown:
        def all_inner_texts(self):
            return ["1%"]

    class Page:
        def locator(self, selector):
            return Shown()

        def wait_for_timeout(self, ms):
            clock[0] += ms / 1000

    monkeypatch.setattr(rumble_upload.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(rumble_upload, "_errors", lambda page: "")
    seen = []
    with pytest.raises(RumbleUploadError, match="stopped at 1% and didn't move for 20 minutes"):
        rumble_upload._wait_for_upload(Page(), seen.append)
    assert 20 * 60 < clock[0] < 21 * 60 and seen[0] == 0.01


def test_an_upload_cancelled_in_the_app_while_going_up_is_stopped_and_taken_out(settings, clip_id, monkeypatch):
    client = TestClient(web_app.create_app(settings))

    def fake_upload(cdp_url, job, screenshots_dir, on_progress=lambda _: None):
        on_progress(0.01)
        assert client.delete("/api/upload-jobs/1").status_code == 204  # cancelled meanwhile
        assert get(settings, UploadJob, 1).cancel_requested
        try:
            on_progress(1.0)  # its next progress check stops it...
        except Exception as e:
            raise RumbleUploadError(str(e), retry=True) from e  # ...which the page's upload turns into its own error
        raise AssertionError("it should have stopped")

    monkeypatch.setattr(worker_module.rumble_upload, "upload", fake_upload)
    monkeypatch.setattr(worker_module, "probe_duration", lambda path: 39.0)
    monkeypatch.setattr(worker_module.ffmpeg, "run", lambda args: Path(args[-1]).write_bytes(b"jpeg"))
    with Session(get_engine(settings)) as s:
        s.add(UploadJob(clip_id=clip_id, site="rumble"))
        s.commit()
    assert worker_module.Worker(settings).upload_next() is True
    assert get(settings, UploadJob, 1) is None  # out of the queue, not failed
    assert get(settings, ClipRecord, clip_id).status == "rendered"
