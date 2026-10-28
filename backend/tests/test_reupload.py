"""Uploading a clip again, rendered afresh, when its upload went wrong on the site's side (a video YouTube won't play)."""

from pathlib import Path

import pytest
from conftest import line, transcript
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from clipper import worker as worker_module
from clipper.config import Settings
from clipper.db import ClipRecord, UploadJob, Vod, get_engine, utcnow
from clipper.web import app as web_app
from clipper.workspace import Workspace, write_json, write_text


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", web_dir=tmp_path / "web")


@pytest.fixture
def clip_id(settings):
    """A highlight uploaded by the app: its video to YouTube (unlisted) and Rumble, its Short to YouTube."""
    with Session(get_engine(settings)) as s:
        vod = Vod(url="https://kick.com/kino/videos/1", status="ready", workspace="vod-1", duration=600)
        s.add(vod)
        s.commit()
        clip = ClipRecord(vod_id=vod.id, key="hl01", kind="highlight", start=1, end=40, title="A Moment", status="uploaded",
                          make_short=True, youtube_url="https://youtu.be/broken12345", rumble_url="https://rumble.com/v6a-a.html")
        s.add(clip)
        s.commit()
        s.add_all([
            UploadJob(clip_id=clip.id, which="video", visibility="public", status="failed", error="quota"),
            UploadJob(clip_id=clip.id, which="video", visibility="unlisted", status="done", youtube_url="https://youtu.be/broken12345"),
            UploadJob(clip_id=clip.id, which="video", site="rumble", status="done", youtube_url="https://rumble.com/v6a-a.html"),
            UploadJob(clip_id=clip.id, which="short", status="done", youtube_url="https://youtube.com/shorts/short123456"),
        ])
        s.commit()
        return clip.id


def test_an_uploaded_clip_renders_again_then_goes_up_again_where_it_went(settings, clip_id, monkeypatch):
    client = TestClient(web_app.create_app(settings))
    clip = client.post(f"/api/clips/{clip_id}/reupload").json()
    assert (clip["status"], clip["reuploading"], clip["youtube_url"], clip["rumble_url"]) == ("approved", True, "", "")

    def landscape(source, clip, out: Path, settings, on_progress=None, overlay=None):
        out.write_bytes(b"video")

    monkeypatch.setattr(worker_module.render, "render_landscape", landscape)
    monkeypatch.setattr(worker_module.render, "render_short", lambda source, clip, words, out, *a, **k: out.write_bytes(b"short"))
    ws = Workspace.open(settings.vods_dir, "vod-1")
    write_json(ws.meta_json, {"source_path": str(ws.root / "source.mp4")})
    write_text(ws.transcript_json, transcript(line(1.0, "A moment.")).model_dump_json())
    w = worker_module.Worker(settings)
    assert w.render_next_clip() is True

    with Session(get_engine(settings)) as s:
        clip = s.get(ClipRecord, clip_id)
        queued = sorted((j.which, j.site, j.visibility) for j in s.exec(select(UploadJob).where(UploadJob.status == "queued")))
    assert clip.status == "rendered" and clip.reupload == ""
    assert queued == [("short", "youtube", "public"), ("video", "rumble", "public"), ("video", "youtube", "unlisted")]


def test_only_an_uploaded_clip_with_its_files_and_no_upload_going_can_go_up_again(settings, clip_id):
    client = TestClient(web_app.create_app(settings))
    with Session(get_engine(settings)) as s:
        s.add(UploadJob(clip_id=clip_id, which="short", site="rumble"))  # still to go
        s.commit()
    assert client.post(f"/api/clips/{clip_id}/reupload").status_code == 409

    with Session(get_engine(settings)) as s:
        for job in s.exec(select(UploadJob).where(UploadJob.status == "queued")):
            s.delete(job)
        vod = s.get(Vod, 1)
        vod.cleaned_at = utcnow()
        s.add(vod)
        s.commit()
    res = client.post(f"/api/clips/{clip_id}/reupload")
    assert res.status_code == 409 and "files were deleted" in res.json()["detail"]


def test_a_clip_marked_uploaded_by_hand_has_nothing_to_repeat(settings):
    with Session(get_engine(settings)) as s:
        s.add(Vod(url="https://kick.com/kino/videos/2", status="ready", workspace="vod-2"))
        s.commit()
        clip = ClipRecord(vod_id=1, key="hl02", kind="highlight", start=1, end=9, title="By hand", status="uploaded")
        s.add(clip)
        s.commit()
        clip_id = clip.id
    res = TestClient(web_app.create_app(settings)).post(f"/api/clips/{clip_id}/reupload")
    assert res.status_code == 409 and "Not uploaded yet" in res.json()["detail"]
