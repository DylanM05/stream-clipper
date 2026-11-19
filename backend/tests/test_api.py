import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from clipper import db as db_module
from clipper.config import Settings
from clipper.db import Channel, ChannelVideo, ClipRecord, Show, UploadJob, Vod, get_engine, utcnow
from clipper.web import app as web_app


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", web_dir=tmp_path / "web")


@pytest.fixture
def client(settings):
    return TestClient(web_app.create_app(settings))


def add_clip(settings, **fields) -> int:
    with Session(get_engine(settings)) as s:
        vod = Vod(url="https://www.twitch.tv/videos/1", title="Stream", duration=600, status="ready", workspace="twitchvod-1")
        s.add(vod)
        s.commit()
        s.refresh(vod)
        clip = ClipRecord(vod_id=vod.id, key="hl01", kind="highlight", start=10, end=70, title="Moment", **fields)
        s.add(clip)
        s.commit()
        s.refresh(clip)
        return clip.id


def set_status(settings, clip_id: int, status: str) -> None:
    with Session(get_engine(settings)) as s:
        clip = s.get(ClipRecord, clip_id)
        clip.status = status
        s.add(clip)
        s.commit()


def test_the_backfill_marks_streams_that_already_make_a_compilation(settings):
    with Session(get_engine(settings)) as s:
        due = Vod(url="https://kick.com/a/videos/1", compilation_due=True)
        made = Vod(url="https://kick.com/a/videos/2")
        plain = Vod(url="https://kick.com/a/videos/3")
        s.add_all([due, made, plain])
        s.commit()
        s.add(ClipRecord(vod_id=made.id, key="comp", kind="compilation", start=0, end=60, title="Best of"))
        s.commit()
        ids = due.id, made.id, plain.id
    db_module._engine.cache_clear()  # as when the app starts again
    with Session(get_engine(settings)) as s:
        assert [s.get(Vod, i).makes_compilation for i in ids] == [True, True, False]


def test_adding_videos_picks_the_mode_and_ignores_duplicates(client):
    x = client.post("/api/vods", json={"url": "https://x.com/someone/status/123"})
    assert x.status_code == 201
    assert x.json()["mode"] == "whole" and x.json()["status"] == "queued"
    assert client.post("/api/vods", json={"url": "https://x.com/someone/status/123"}).json()["id"] == x.json()["id"]

    twitch = client.post("/api/vods", json={"url": "https://www.twitch.tv/videos/9", "notes": "Co-host is Sam"})
    assert twitch.json()["mode"] == "segment" and twitch.json()["notes"] == "Co-host is Sam"
    assert client.post("/api/vods", json={"url": "not a link"}).status_code == 422
    assert [v["id"] for v in client.get("/api/vods").json()] == [twitch.json()["id"], x.json()["id"]]


def test_the_summary_says_whats_under_way_in_each_part_of_the_work(client, settings):
    with Session(get_engine(settings)) as s:
        s.add(Vod(url="https://www.twitch.tv/videos/7", title="Night Owls", status="downloading", progress=0.42))
        s.add(Vod(url="https://www.twitch.tv/videos/8", status="queued"))
        s.add(Vod(url="https://www.twitch.tv/videos/10", status="queued", workspace="twitchvod-10"))  # downloaded
        s.add(Vod(url="https://www.twitch.tv/videos/9", title="Overtime #212", status="segmenting", progress=0.9))
        s.commit()
    rendering = add_clip(settings, status="rendering")
    queued = add_clip(settings, status="approved")
    uploaded = add_clip(settings, status="rendered")
    with Session(get_engine(settings)) as s:
        s.add(UploadJob(clip_id=uploaded, status="uploading", progress=0.25))
        s.add(UploadJob(clip_id=uploaded, which="short", status="queued"))
        s.commit()

    downloads, processing, renders, uploads = client.get("/api/summary").json()["work"]
    assert downloads == {"stage": "downloads", "count": 2, "now": "Night Owls", "step": "downloading", "progress": 0.42}
    # The AI's step doesn't say how far along it is.
    assert processing == {"stage": "processing", "count": 2, "now": "Overtime #212", "step": "segmenting", "progress": None}
    assert (renders["count"], renders["now"], renders["step"]) == (2, "Moment", "rendering") and rendering and queued
    assert (uploads["count"], uploads["now"], uploads["progress"]) == (2, "Moment", 0.25)


def test_review_flow(client, settings):
    clip_id = add_clip(settings)
    assert [c["id"] for c in client.get("/api/clips").json()] == [clip_id]
    assert client.get("/api/summary").json()["to_review"] == 1

    assert client.post(f"/api/clips/{clip_id}/approve").json()["status"] == "approved"
    with Session(get_engine(settings)) as s:
        assert s.get(ClipRecord, clip_id).approved_in_review  # what a show that uploads what you approve goes by
    assert client.get("/api/clips").json() == []
    assert [c["id"] for c in client.get("/api/clips", params={"status": ["approved", "rendered"]}).json()] == [clip_id]

    assert client.post(f"/api/clips/{clip_id}/restore").json()["status"] == "proposed"
    assert client.post(f"/api/clips/{clip_id}/reject").json()["status"] == "rejected"
    detail = client.get("/api/vods/1").json()
    assert detail["vod"]["counts"] == {"rejected": 1}
    assert detail["clips"][0]["vod_title"] == "Stream"


def test_editing_validates_and_renders_approved_clips_again(client, settings):
    clip_id = add_clip(settings)
    assert client.patch(f"/api/clips/{clip_id}", json={"start": 50, "end": 40}).status_code == 422
    assert client.patch(f"/api/clips/{clip_id}", json={"end": 700}).status_code == 422  # past the end of the video
    assert client.patch(f"/api/clips/{clip_id}", json={"start": 0, "end": 200, "make_short": True}).status_code == 422
    assert client.patch(f"/api/clips/{clip_id}", json={"title": "   "}).status_code == 422

    edited = client.patch(f"/api/clips/{clip_id}", json={"title": "  New   title ", "end": 72.5}).json()
    assert edited["title"] == "New title" and edited["end"] == 72.5 and edited["status"] == "proposed"

    set_status(settings, clip_id, "rendered")
    assert client.patch(f"/api/clips/{clip_id}", json={"end": 72.5}).json()["status"] == "rendered"  # no change
    assert client.patch(f"/api/clips/{clip_id}", json={"end": 75}).json()["status"] == "approved"


def test_login_is_required_when_a_password_is_set(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app.time, "sleep", lambda _: None)
    client = TestClient(web_app.create_app(Settings(data_dir=tmp_path, web_dir=tmp_path / "web", app_password="hunter2")))

    assert client.get("/api/vods").status_code == 401
    assert client.get("/media/clips/1/thumb.jpg").status_code == 401
    assert client.get("/api/session").json() == {"authenticated": False, "password_required": True}
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401

    assert client.post("/api/login", json={"password": "hunter2"}).status_code == 204
    assert client.get("/api/vods").status_code == 200
    assert client.get("/api/session").json()["authenticated"] is True

    client.cookies.clear()
    client.cookies.set("clipper_session", "9999999999.forged")
    assert client.get("/api/vods").status_code == 401


def rendered_clip(settings, **fields) -> tuple[int, Path]:
    clips_dir = settings.vods_dir / "twitchvod-1" / "clips"
    clips_dir.mkdir(parents=True)
    video = clips_dir / "hl01-moment.mp4"
    video.write_bytes(b"video")
    return add_clip(settings, status="rendered", video_path=str(video), **fields), video


def add_job(settings, clip_id: int, status: str = "queued") -> None:
    with Session(get_engine(settings)) as s:
        s.add(UploadJob(clip_id=clip_id, status=status))
        s.commit()


def test_a_clip_ready_to_upload_can_go_back_to_the_inbox_and_its_render_is_deleted(client, settings):
    clip_id, video = rendered_clip(settings)
    add_job(settings, clip_id)
    back = client.post(f"/api/clips/{clip_id}/restore").json()
    assert back["status"] == "proposed" and not back["has_video"] and not video.exists()
    assert client.get(f"/api/clips/{clip_id}/uploads").json() == []  # its queued upload is cancelled


def test_a_clip_ready_to_upload_can_be_deleted_unless_its_uploading_or_uploaded(client, settings):
    clip_id, video = rendered_clip(settings)
    add_job(settings, clip_id, "uploading")
    assert client.post(f"/api/clips/{clip_id}/reject").status_code == 409
    assert video.exists()

    with Session(get_engine(settings)) as s:
        for job in s.exec(select(UploadJob)):
            job.status = "done"
            s.add(job)
        s.commit()
    assert client.post(f"/api/clips/{clip_id}/reject").json()["status"] == "rejected"
    assert not video.exists()
    set_status(settings, clip_id, "uploaded")
    assert client.post(f"/api/clips/{clip_id}/reject").status_code == 409


def make_compiling(settings, clip_id: int) -> None:
    """Make the clip's stream one that makes a compilation (Vod.makes_compilation)."""
    with Session(get_engine(settings)) as s:
        vod = s.get(Vod, s.get(ClipRecord, clip_id).vod_id)
        vod.makes_compilation = True
        s.add(vod)
        s.commit()


def kept_clip(settings, **fields) -> int:
    clip_id = add_clip(settings, **fields)
    make_compiling(settings, clip_id)
    return clip_id


def stored(settings, clip_id: int) -> tuple[ClipRecord, Vod]:
    with Session(get_engine(settings)) as s:
        clip = s.get(ClipRecord, clip_id)
        return clip, s.get(Vod, clip.vod_id)


def test_keep_sets_kept_and_marks_it_reviewed(client, settings):
    clip_id = kept_clip(settings)
    kept = client.post(f"/api/clips/{clip_id}/keep")
    assert kept.status_code == 200 and kept.json()["status"] == "kept"
    clip, vod = stored(settings, clip_id)
    assert clip.approved_in_review and clip.reviewed_at is not None
    assert vod.compilation_due


def test_keep_is_refused_outside_a_compiling_stream(client, settings):
    assert client.post(f"/api/clips/{add_clip(settings)}/keep").status_code == 422
    with Session(get_engine(settings)) as s:
        vod = Vod(url="https://www.twitch.tv/videos/2", status="ready", workspace="twitchvod-2", makes_compilation=True)
        s.add(vod)
        s.commit()
        whole = ClipRecord(vod_id=vod.id, key="comp", kind="compilation", start=0, end=60, title="Best of")
        s.add(whole)
        s.commit()
        whole_id = whole.id
    assert client.post(f"/api/clips/{whole_id}/keep").status_code == 422  # only a chapter or a highlight


def test_keep_is_refused_from_uploaded(client, settings):
    clip_id = kept_clip(settings, status="uploaded")
    refused = client.post(f"/api/clips/{clip_id}/keep")
    assert refused.status_code == 409
    assert refused.json()["detail"] == "It's on YouTube already. It stays in the compilation."


def test_keep_is_refused_after_the_files_are_deleted(client, settings):
    clip_id = kept_clip(settings)
    with Session(get_engine(settings)) as s:
        vod = s.get(Vod, s.get(ClipRecord, clip_id).vod_id)
        vod.cleaned_at = utcnow()
        s.add(vod)
        s.commit()
    assert client.post(f"/api/clips/{clip_id}/keep").status_code == 409
    assert stored(settings, clip_id)[0].status == "proposed"


def test_keep_from_approved_discards_the_render(client, settings):
    clip_id, video = rendered_clip(settings)
    make_compiling(settings, clip_id)
    add_job(settings, clip_id)
    kept = client.post(f"/api/clips/{clip_id}/keep").json()
    assert kept["status"] == "kept" and not kept["has_video"] and not video.exists()
    assert client.get(f"/api/clips/{clip_id}/uploads").json() == []  # its queued upload is cancelled


def test_keep_twice_changes_nothing(client, settings):
    clip_id = kept_clip(settings)
    client.post(f"/api/clips/{clip_id}/keep")
    first = stored(settings, clip_id)[0].reviewed_at
    again = client.post(f"/api/clips/{clip_id}/keep")
    assert again.status_code == 200 and again.json()["status"] == "kept"
    assert stored(settings, clip_id)[0].reviewed_at == first


def test_approve_from_kept_is_keep_and_post(client, settings):
    clip_id = kept_clip(settings, status="kept")
    assert client.post(f"/api/clips/{clip_id}/approve").json()["status"] == "approved"
    clip, vod = stored(settings, clip_id)
    assert clip.approved_in_review and vod.compilation_due


def test_editing_a_kept_clips_ends_leaves_it_kept(client, settings):
    clip_id = kept_clip(settings, status="kept")
    edited = client.patch(f"/api/clips/{clip_id}", json={"start": 12, "end": 60, "cuts": [[20, 25]], "make_short": True})
    assert edited.status_code == 200 and edited.json()["status"] == "kept"
    assert stored(settings, clip_id)[0].start == 12


def test_restore_and_reject_work_from_kept(client, settings):
    clip_id = kept_clip(settings, status="kept")
    assert client.post(f"/api/clips/{clip_id}/restore").json()["status"] == "proposed"
    assert stored(settings, clip_id)[1].compilation_due
    set_status(settings, clip_id, "kept")
    assert client.post(f"/api/clips/{clip_id}/reject").json()["status"] == "rejected"


def test_a_stream_with_a_compilation_already_isnt_made_due_again(client, settings):
    clip_id = kept_clip(settings)
    with Session(get_engine(settings)) as s:
        s.add(ClipRecord(vod_id=s.get(ClipRecord, clip_id).vod_id, key="comp", kind="compilation", start=0, end=60,
                         title="Best of"))
        s.commit()
    client.post(f"/api/clips/{clip_id}/keep")
    assert not stored(settings, clip_id)[1].compilation_due


def test_make_clip_with_keep_creates_it_kept(client, settings):
    clip_id = kept_clip(settings)
    vod_id = stored(settings, clip_id)[1].id
    made = client.post(f"/api/vods/{vod_id}/clips", json={"kind": "chapter", "start": 100, "end": 200, "title": "Mine",
                                                           "keep": True})
    assert made.status_code == 201 and made.json()["status"] == "kept"
    clip, vod = stored(settings, made.json()["id"])
    assert clip.approved_in_review and clip.reviewed_at is not None and vod.compilation_due
    plain = stored(settings, add_clip(settings))[1].id
    body = {"kind": "chapter", "start": 100, "end": 200, "title": "Mine", "keep": True}
    assert client.post(f"/api/vods/{plain}/clips", json=body).status_code == 422


def test_a_kept_clip_is_not_listed_as_ready_to_upload_or_counted(client, settings):
    kept_clip(settings, status="kept")
    assert client.get("/api/clips", params={"status": "rendered"}).json() == []
    assert client.get("/api/summary").json()["to_upload"] == 0


def test_pausing_parts_of_the_work_and_single_videos_and_clips(client, settings):
    assert client.get("/api/pauses").json() == {"downloads": False, "processing": False, "renders": False, "uploads": False}
    assert client.patch("/api/pauses", json={"renders": True, "uploads": True}).json()["renders"] is True
    assert client.get("/api/pauses").json() == {"downloads": False, "processing": False, "renders": True, "uploads": True}

    vod = client.post("/api/vods", json={"url": "https://www.twitch.tv/videos/9"}).json()
    assert (vod["paused"], vod["downloaded"]) == (False, False)
    assert client.post(f"/api/vods/{vod['id']}/pause").json()["paused"] is True
    assert client.post(f"/api/vods/{vod['id']}/resume").json()["paused"] is False

    clip_id = add_clip(settings)
    assert client.post(f"/api/clips/{clip_id}/pause").status_code == 409  # not approved yet
    client.post(f"/api/clips/{clip_id}/approve")
    assert client.post(f"/api/clips/{clip_id}/pause").json()["paused"] is True
    assert client.post(f"/api/clips/{clip_id}/resume").json()["paused"] is False


def test_deleting_a_streams_files_rejects_the_clips_that_arent_uploaded(client, settings):
    clip_id, video = rendered_clip(settings)
    root = settings.vods_dir / "twitchvod-1"
    (root / "source.mp4").write_bytes(b"stream")
    (root / "proxy.mp4").write_bytes(b"preview")
    (root / "meta.json").write_text(json.dumps({"source_path": str(root / "source.mp4")}))

    vod = client.post("/api/vods/1/delete-files").json()
    assert vod["cleaned"] is True
    assert not any(p.exists() for p in (root / "source.mp4", root / "proxy.mp4", video))
    assert client.get(f"/api/clips/{clip_id}").json()["status"] == "rejected"
    assert client.post(f"/api/clips/{clip_id}/approve").status_code == 409  # nothing left to cut it from


def test_a_video_can_be_deleted_with_its_clips_and_files_and_its_channel_doesnt_queue_it_again(client, settings):
    clip_id, video = rendered_clip(settings)
    add_job(settings, clip_id)
    with Session(get_engine(settings)) as s:
        show = Show(name="TheQuartering")
        s.add(show)
        s.commit()
        channel = Channel(show_id=show.id, url="https://rumble.com/c/TheQuartering", platform="rumble")
        s.add(channel)
        s.commit()
        s.add(ChannelVideo(channel_id=channel.id, video_id="v6a", url="https://www.twitch.tv/videos/1", status="queued", vod_id=1))
        vod = s.get(Vod, 1)
        vod.status = "downloading"
        s.add(vod)
        s.commit()
    assert client.delete("/api/vods/1").status_code == 409  # pause it first

    with Session(get_engine(settings)) as s:
        vod = s.get(Vod, 1)
        vod.status = "queued"
        s.add(vod)
        s.commit()
    assert client.delete("/api/vods/1").status_code == 204
    assert client.get("/api/vods").json() == []
    assert not (settings.vods_dir / "twitchvod-1").exists()
    with Session(get_engine(settings)) as s:
        assert not s.exec(select(ClipRecord)).all() and not s.exec(select(UploadJob)).all()
        seen = s.exec(select(ChannelVideo)).one()
        assert (seen.status, seen.vod_id) == ("skipped", None)  # the monitor leaves it alone from now on


def test_a_video_with_clips_on_youtube_stays(client, settings):
    clip_id = add_clip(settings, status="uploaded")
    assert client.delete("/api/vods/1").status_code == 409
    assert client.get(f"/api/clips/{clip_id}").status_code == 200


def test_clips_say_where_their_upload_stands(client, settings):
    clip_id, _ = rendered_clip(settings)

    def state():
        clip = client.get(f"/api/clips/{clip_id}").json()
        return clip["upload_state"], clip["next_upload"]

    assert state() == ("", None)  # nothing asked for yet
    with Session(get_engine(settings)) as s:
        show = Show(name="Spaced out", upload_gap_hours=2)
        s.add(show)
        s.commit()
        vod = s.get(Vod, 1)
        vod.show_id = show.id
        earlier = ClipRecord(vod_id=1, key="hl02", kind="highlight", start=0, end=5, title="Earlier", status="uploaded")
        s.add_all([vod, earlier])
        s.commit()
        s.add(UploadJob(clip_id=earlier.id, status="done", started_at=utcnow() - timedelta(hours=1)))
        job = UploadJob(clip_id=clip_id)
        s.add(job)
        s.commit()
        job_id = job.id
    upload, at = state()
    assert upload == "scheduled" and abs(datetime.fromisoformat(at) - (utcnow() + timedelta(hours=1))) < timedelta(minutes=1)

    client.post(f"/api/upload-jobs/{job_id}/now")
    assert state() == ("queued", None)  # its turn has come: it goes once the upload running is done

    def set_job(**fields):
        with Session(get_engine(settings)) as s:
            job = s.get(UploadJob, job_id)
            for k, v in fields.items():
                setattr(job, k, v)
            s.add(job)
            s.commit()

    set_job(status="uploading")
    assert state() == ("uploading", None)
    set_job(status="failed", retry_at=utcnow() - timedelta(minutes=1), error="YouTube says this channel is at its limit. More.")
    # Its retry time has come, but the show's 2-hour spacing from the upload an hour ago puts it an hour on.
    upload, at = state()
    assert upload == "retrying" and abs(datetime.fromisoformat(at) - (utcnow() + timedelta(hours=1))) < timedelta(minutes=1)
    assert client.get(f"/api/clips/{clip_id}").json()["upload_error"] == "YouTube says this channel is at its limit. More."
    set_job(retry_at=None)
    assert state() == ("failed", None)
    add_job(settings, clip_id)  # started again as a new upload: the latest counts, and it waits its turn
    assert state()[0] == "scheduled"


def test_serves_the_phone_app_with_fallback_to_index(tmp_path):
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<html>app</html>")
    (web / "assets" / "app.js").write_text("console.log(1)")
    (web / "manifest.webmanifest").write_text("{}")
    client = TestClient(web_app.create_app(Settings(data_dir=tmp_path / "data", web_dir=web)))

    assert client.get("/").text == "<html>app</html>"
    assert client.get("/videos/3").text == "<html>app</html>"
    assert client.get("/assets/app.js").text == "console.log(1)"
    assert client.get("/manifest.webmanifest").headers["content-type"].startswith("application/manifest+json")
    assert client.get("/api/nothing-here").status_code == 404
