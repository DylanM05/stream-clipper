"""A deploy restarting the worker between uploads (deploy/deploy.sh, cli deploy-wait and deploy-done)."""

from datetime import timedelta

import pytest
from sqlmodel import Session
from typer.testing import CliRunner

from clipper import cli
from clipper import worker as worker_module
from clipper.config import Settings
from clipper.db import DEPLOY_HOLD, ClipRecord, UploadJob, Vod, deploy_held, get_app_setting, get_engine, utcnow


@pytest.fixture
def settings(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    return settings


def add_job(settings, **fields) -> int:
    with Session(get_engine(settings)) as s:
        vod = Vod(url="https://kick.com/kino/videos/1", status="ready")
        s.add(vod)
        s.commit()
        clip = ClipRecord(vod_id=vod.id, key="hl01", kind="highlight", start=0, end=10, title="t", status="rendered")
        s.add(clip)
        s.commit()
        job = UploadJob(clip_id=clip.id, **fields)
        s.add(job)
        s.commit()
        return job.id


def held(settings) -> bool:
    with Session(get_engine(settings)) as s:
        return deploy_held(s)


def test_a_deploy_waits_for_an_upload_through_the_browser_and_holds_new_ones(settings):
    add_job(settings, status="uploading", method="browser", site="rumble", progress=0.4)
    out = CliRunner().invoke(cli.app, ["deploy-wait", "--max-hours", "0.00001", "--every", "0.01"])
    assert out.exit_code == 1 and "rumble upload 1 at 40%" in out.output
    assert not held(settings)  # gave up: uploads go on as before

    with Session(get_engine(settings)) as s:
        job = s.get(UploadJob, 1)
        job.status = "done"
        s.add(job)
        s.commit()
    out = CliRunner().invoke(cli.app, ["deploy-wait"])
    assert out.exit_code == 0 and held(settings)
    assert CliRunner().invoke(cli.app, ["deploy-done"]).exit_code == 0 and not held(settings)


def test_uploads_through_the_api_dont_hold_a_deploy_up(settings):
    add_job(settings, status="uploading", method="api")  # carries on after the restart
    assert CliRunner().invoke(cli.app, ["deploy-wait"]).exit_code == 0


def test_the_worker_starts_no_upload_while_a_deploy_holds_them(settings, monkeypatch):
    add_job(settings, status="queued")
    assert CliRunner().invoke(cli.app, ["deploy-wait"]).exit_code == 0
    worker = worker_module.Worker(settings)
    monkeypatch.setattr(worker, "report_studio_status", lambda force=False: None)
    monkeypatch.setattr(worker, "run_upload", lambda job_id: pytest.fail("started an upload during a deploy"))
    assert worker.upload_next() is False


def test_a_hold_lapses_by_itself(settings):
    assert CliRunner().invoke(cli.app, ["deploy-wait"]).exit_code == 0
    with Session(get_engine(settings)) as s:
        until = get_app_setting(s, DEPLOY_HOLD)
        assert deploy_held(s) and not deploy_held(s, now=utcnow() + timedelta(minutes=16))
    assert until


def test_a_deploy_doesnt_wait_for_an_upload_that_stopped_moving_or_was_cancelled(settings):
    add_job(settings, status="uploading", method="browser", site="rumble", progress=0.01,
            started_at=utcnow() - timedelta(hours=3), progress_at=utcnow() - timedelta(hours=2, minutes=50))
    add_job(settings, status="uploading", method="browser", site="rumble", progress=0.5, cancel_requested=True,
            started_at=utcnow(), progress_at=utcnow())
    out = CliRunner().invoke(cli.app, ["deploy-wait", "--every", "0.01"])
    assert out.exit_code == 0, out.output
    assert "Not waiting for rumble upload 1: it hasn't moved from 1% since" in out.output
    assert "Not waiting for rumble upload 2: it was cancelled." in out.output
    assert "the worker can restart" in out.output
