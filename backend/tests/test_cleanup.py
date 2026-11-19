"""Freeing disk space: finished streams lose their big files after a grace period; low space pauses downloads."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from clipper import worker as worker_module
from clipper.config import Settings
from clipper.db import ClipRecord, UploadJob, Vod, get_engine, utcnow
from clipper.web import app as web_app
from clipper.workspace import Workspace, write_json


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", web_dir=tmp_path / "web")


def finished_stream(settings, clip_statuses=("uploaded", "rejected"), days_ago=4, source_outside=None):
    """A processed stream with its files, its clips last touched `days_ago` days ago."""
    ws = Workspace.open(settings.vods_dir, "kickvod-1")
    source = source_outside or ws.root / "source.mp4"
    for path in (source, ws.proxy_mp4, ws.audio_wav, ws.clips_dir / "hl01-x.mp4"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 1000)
    write_json(ws.meta_json, {"source_path": str(source)})
    write_json(ws.transcript_json, {"kept": True})
    then = utcnow() - timedelta(days=days_ago)
    with Session(get_engine(settings)) as s:
        vod = Vod(url="https://kick.com/kino/videos/1", status="ready", workspace="kickvod-1", updated_at=then)
        s.add(vod)
        s.commit()
        for i, status in enumerate(clip_statuses):
            s.add(ClipRecord(vod_id=vod.id, key=f"hl{i}", kind="highlight", start=0, end=10, title="t",
                             status=status, updated_at=then))
        s.commit()
        return vod.id, ws, source


def get(settings, model, id_):
    with Session(get_engine(settings)) as s:
        return s.get(model, id_)


def test_a_finished_stream_loses_its_big_files_and_keeps_the_rest(settings):
    vod_id, ws, source = finished_stream(settings)
    w = worker_module.Worker(settings)
    assert w.clean_up_next() is True

    assert not source.exists() and not ws.proxy_mp4.exists() and not ws.audio_wav.exists() and not ws.clips_dir.exists()
    assert ws.transcript_json.exists() and ws.meta_json.exists()
    assert get(settings, Vod, vod_id).cleaned_at is not None
    assert w.clean_up_next() is False  # nothing left to do


def test_a_failed_upload_that_tries_again_tonight_keeps_the_streams_files(settings):
    finished_stream(settings, ("uploaded",), days_ago=0)  # the video went up; its Short failed and waits to retry
    with Session(get_engine(settings)) as s:
        s.add(UploadJob(clip_id=1, which="short", status="failed", retry_at=utcnow() + timedelta(hours=5)))
        s.commit()
    w = worker_module.Worker(settings)
    assert w.clean_up_next() is False
    with Session(get_engine(settings)) as s:
        s.get(UploadJob, 1).retry_at = None  # dismissed, or given up on
        s.commit()
    assert w.clean_up_next() is True


def test_by_default_a_stream_is_cleaned_up_as_soon_as_its_clips_are_done_and_extras_count_as_done(settings):
    finished_stream(settings, ("uploaded", "extra", "rejected"), days_ago=0)
    assert worker_module.Worker(settings).clean_up_next() is True


@pytest.mark.parametrize("statuses, days_ago", [
    (("uploaded", "proposed"), 30),  # a clip still waits for review
    (("uploaded", "rendered"), 30),  # one is rendered but not uploaded yet
    (("uploaded", "rejected"), 1),  # finished, but still in the grace period
])
def test_streams_that_arent_finished_or_due_are_left_alone(settings, statuses, days_ago):
    settings.cleanup_after_days = 3
    _, ws, source = finished_stream(settings, statuses, days_ago)
    assert worker_module.Worker(settings).clean_up_next() is False
    assert source.exists()


def test_a_pending_upload_or_a_file_from_elsewhere_is_never_deleted(settings, tmp_path):
    outside = tmp_path / "my videos" / "stream.mp4"
    vod_id, ws, source = finished_stream(settings, source_outside=outside)
    with Session(get_engine(settings)) as s:
        clip = s.exec(select(ClipRecord)).first()
        s.add(UploadJob(clip_id=clip.id, status="queued"))
        s.commit()
    w = worker_module.Worker(settings)
    assert w.clean_up_next() is False

    with Session(get_engine(settings)) as s:
        for job in s.exec(select(UploadJob)):
            job.status = "done"
            s.add(job)
        s.commit()
    assert w.clean_up_next() is True
    assert outside.exists()  # added by its path: not the app's to delete
    assert not ws.proxy_mp4.exists()


def test_cleanup_waits_as_long_as_set_in_the_app(settings):
    finished_stream(settings, days_ago=4)
    client = TestClient(web_app.create_app(settings))
    assert client.patch("/api/settings", json={"cleanup_after_days": 7}).json() == {
        "download_limit_mbps": 50, "full_speed_from": 0, "full_speed_until": 0, "cleanup_after_days": 7,
        "timezone": "America/New_York",
    }
    assert worker_module.Worker(settings).clean_up_next() is False
    client.patch("/api/settings", json={"cleanup_after_days": 0})
    assert worker_module.Worker(settings).clean_up_next() is True


def add_compilation(settings, vod_id, status):
    with Session(get_engine(settings)) as s:
        s.add(ClipRecord(vod_id=vod_id, key="compilation", kind="compilation", start=0, end=10, title="c",
                         status=status, updated_at=utcnow() - timedelta(days=4)))
        s.commit()


def test_a_stream_with_a_kept_clip_and_no_uploaded_compilation_is_not_cleaned(settings):
    vod_id, _, source = finished_stream(settings, ("kept", "uploaded"))
    w = worker_module.Worker(settings)
    assert w.clean_up_next() is False  # no compilation yet (one kept clip makes none)
    add_compilation(settings, vod_id, "rendered")
    assert w.clean_up_next() is False
    assert source.exists()


@pytest.mark.parametrize("status", ["uploaded", "rejected"])
def test_it_is_cleaned_once_its_compilation_is_uploaded_or_rejected(settings, status):
    vod_id, _, source = finished_stream(settings, ("kept", "kept", "rejected"))
    add_compilation(settings, vod_id, status)
    assert worker_module.Worker(settings).clean_up_next() is True
    assert not source.exists()
    with Session(get_engine(settings)) as s:  # the kept clips stay kept: they're in it, and the AI learns from them
        assert [c.status for c in s.exec(select(ClipRecord).where(ClipRecord.kind == "highlight"))] == [
            "kept", "kept", "rejected"]


@pytest.mark.parametrize("compilation, kept_becomes", [("uploaded", "kept"), ("rendered", "rejected"), (None, "rejected")])
def test_deleting_a_streams_files_leaves_kept_clips_only_once_its_compilation_is_uploaded(settings, compilation,
                                                                                         kept_becomes):
    vod_id, _, source = finished_stream(settings, ("kept", "uploaded"))
    if compilation:
        add_compilation(settings, vod_id, compilation)
    client = TestClient(web_app.create_app(settings))
    assert client.post(f"/api/vods/{vod_id}/delete-files").json()["cleaned"] is True
    with Session(get_engine(settings)) as s:
        statuses = {c.key: c.status for c in s.exec(select(ClipRecord))}
    assert statuses["hl0"] == kept_becomes and statuses["hl1"] == "uploaded"
    if compilation:
        assert statuses["compilation"] == ("uploaded" if compilation == "uploaded" else "rejected")


def test_downloads_wait_while_the_disk_is_nearly_full(settings, monkeypatch):
    monkeypatch.setattr(worker_module, "disk_free_gb", lambda path: 12.0)
    fetched = []
    monkeypatch.setattr(worker_module.download, "fetch", lambda *a, **k: fetched.append(a))
    with Session(get_engine(settings)) as s:
        vod = Vod(url="https://kick.com/kino/videos/2")
        s.add(vod)
        s.commit()
        vod_id = vod.id
    worker_module.Worker(settings).process_vod(vod_id)

    vod = get(settings, Vod, vod_id)
    assert vod.status == "waiting" and "Only 12 GB free" in vod.error and vod.retry_at is not None
    assert fetched == []


def test_storage_endpoint(settings):
    client = TestClient(web_app.create_app(settings))
    finished_stream(settings)
    info = client.get("/api/storage").json()
    assert info["free_gb"] > 0 and info["min_free_gb"] == 40
    assert info["videos_gb"] == 0.0  # 4 KB of test files
