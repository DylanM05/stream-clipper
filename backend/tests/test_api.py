import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from clipper.config import Settings
from clipper.db import ClipRecord, Vod, get_engine
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


def test_adding_videos_picks_the_mode_and_ignores_duplicates(client):
    x = client.post("/api/vods", json={"url": "https://x.com/someone/status/123"})
    assert x.status_code == 201
    assert x.json()["mode"] == "whole" and x.json()["status"] == "queued"
    assert client.post("/api/vods", json={"url": "https://x.com/someone/status/123"}).json()["id"] == x.json()["id"]

    twitch = client.post("/api/vods", json={"url": "https://www.twitch.tv/videos/9", "notes": "Co-host is Sam"})
    assert twitch.json()["mode"] == "segment" and twitch.json()["notes"] == "Co-host is Sam"
    assert client.post("/api/vods", json={"url": "not a link"}).status_code == 422
    assert [v["id"] for v in client.get("/api/vods").json()] == [twitch.json()["id"], x.json()["id"]]


def test_review_flow(client, settings):
    clip_id = add_clip(settings)
    assert [c["id"] for c in client.get("/api/clips").json()] == [clip_id]
    assert client.get("/api/summary").json()["to_review"] == 1

    assert client.post(f"/api/clips/{clip_id}/approve").json()["status"] == "approved"
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
