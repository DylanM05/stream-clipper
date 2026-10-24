import sqlite3
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from clipper.config import Settings
from clipper.db import ChannelVideo, ClipRecord, Vod, get_engine
from clipper.web import app as web_app

PRESHOW = {
    "name": "Kino Casino pre-show",
    "match": "x.com/Andywarski",
    "mode": "whole",
    "title_template": "♥️♠️♦️♣️KINO CASINO PRE SHOW♣️♦️♠️♥️ {date} - {title}",
}


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", web_dir=tmp_path / "web")


@pytest.fixture
def client(settings):
    return TestClient(web_app.create_app(settings))


def test_shows_set_the_mode_of_matching_links(client):
    assert client.post("/api/shows", json={**PRESHOW, "title_template": "no title here"}).status_code == 422
    show = client.post("/api/shows", json={**PRESHOW, "mode": "segment"}).json()
    assert show["channels"] == [] and show["video_count"] == 0

    auto = client.post("/api/vods", json={"url": "https://x.com/Andywarski/status/1"}).json()
    assert (auto["show_id"], auto["show_name"], auto["mode"]) == (show["id"], "Kino Casino pre-show", "segment")
    picked = client.post("/api/vods", json={"url": "https://x.com/Andywarski/status/2", "mode": "whole"}).json()
    assert picked["mode"] == "whole"
    other = client.post("/api/vods", json={"url": "https://x.com/someone/status/3"}).json()
    assert (other["show_id"], other["mode"]) == (None, "whole")

    updated = client.patch(f"/api/shows/{show['id']}", json={"name": " Pre-show ", "mode": "whole"}).json()
    assert (updated["name"], updated["mode"], updated["video_count"]) == ("Pre-show", "whole", 2)


def test_channels(client, settings):
    show = client.post("/api/shows", json={"name": "Kino Casino", "title_template": "KINO CASINO {date} - {title}"}).json()
    url = f"/api/shows/{show['id']}/channels"
    assert client.post(url, json={"url": "https://x.com/Andywarski"}).status_code == 422
    channel = client.post(url, json={"url": "kick.com/KinoCasino", "exclude": "rerun", "min_minutes": 60}).json()
    assert (channel["platform"], channel["url"], channel["min_minutes"]) == ("kick", "https://kick.com/kinocasino", 60)
    assert client.post(url, json={"url": "https://kick.com/kinocasino/videos"}).status_code == 409
    saved = client.get(f"/api/shows/{show['id']}").json()
    assert saved["channels"][0]["id"] == channel["id"]
    assert saved["match"] == "kick.com/kinocasino"  # the channel's links now match the show

    paused = client.patch(f"/api/channels/{channel['id']}", json={"enabled": False, "keywords": " kino , casino "}).json()
    assert paused["enabled"] is False and paused["keywords"] == "kino , casino"

    with Session(get_engine(settings)) as s:  # as if the watcher had listed an older video
        s.add(ChannelVideo(channel_id=channel["id"], video_id="v1", url="https://kick.com/kinocasino/videos/v1",
                           title="Old stream", duration=3600, status="older"))
        s.commit()
    [older] = client.get(f"/api/channels/{channel['id']}/videos").json()
    queued = client.post(f"/api/channel-videos/{older['id']}/process").json()
    assert queued["status"] == "queued"
    vod = client.get(f"/api/vods/{queued['vod_id']}").json()["vod"]
    assert (vod["show_id"], vod["mode"]) == (show["id"], "segment")

    assert client.delete(f"/api/shows/{show['id']}").status_code == 204
    assert client.get(f"/api/vods/{queued['vod_id']}").json()["vod"]["show_id"] is None
    assert client.get(f"/api/channels/{channel['id']}/videos").status_code == 404


def test_upload_channels_shows_and_the_upload_kit(client, settings, monkeypatch):
    # Looking up a channel id calls YouTube; pretend only /channel/ links have one.
    monkeypatch.setattr(web_app, "resolve_channel_id", lambda url: "UCabc-123" if "/channel/" in url else "")
    by_id = client.post("/api/destinations", json={"name": "Kino Clips", "url": "https://www.youtube.com/channel/UCabc-123"}).json()
    by_handle = client.post("/api/destinations", json={"name": "Kino Full Episodes", "url": "https://www.youtube.com/@kinofull"}).json()
    assert by_id["studio_upload_url"] == "https://studio.youtube.com/channel/UCabc-123/videos/upload?d=ud"
    assert by_handle["studio_upload_url"] == "https://www.youtube.com/upload"

    assert client.post("/api/shows", json={"name": "X", "episode_template": "no title"}).status_code == 422
    assert client.post("/api/shows", json={"name": "X", "clip_destination_id": 999}).status_code == 422
    show = client.post("/api/shows", json={
        "name": "Kino Casino", "mode": "both", "title_template": "KINO {date} - {title}", "episode_template": "",
        "notes": "Hosts: Warski and Jer.", "clip_guidance": "Follow Jer.",
        "clip_destination_id": by_id["id"], "episode_destination_id": by_handle["id"],
    }).json()
    assert (show["mode"], show["clip_guidance"], show["episode_destination_id"]) == ("both", "Follow Jer.", by_handle["id"])

    # A rendered clip headed for the clips channel.
    video = settings.data_dir / "rendered.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"x" * 1234)
    with Session(get_engine(settings)) as s:
        vod = Vod(url="https://kick.com/kino/videos/1", title="Stream", duration=600, status="ready", show_id=show["id"])
        s.add(vod)
        s.commit()
        clip = ClipRecord(vod_id=vod.id, key="hl01", kind="highlight", start=1, end=40, status="rendered",
                          title='KINO Oct 23, 2026 - Jer: "Huck Blend" Is Back?', video_path=str(video),
                          destination_id=by_id["id"])
        s.add(clip)
        s.commit()
        clip_id = clip.id

    out = client.get(f"/api/clips/{clip_id}").json()
    assert (out["destination_name"], out["video_size"], out["has_video"]) == ("Kino Clips", 1234, True)
    download = client.get(f"/media/clips/{clip_id}/video.mp4", params={"download": True})
    assert "KINO Oct 23, 2026 - Jer Huck Blend Is Back.mp4" in unquote(download.headers["content-disposition"])

    moved = client.patch(f"/api/clips/{clip_id}", json={"destination_id": by_handle["id"], "title": "New title"}).json()
    assert (moved["destination_name"], moved["status"]) == ("Kino Full Episodes", "rendered")  # no new render needed

    uploaded = client.post(f"/api/clips/{clip_id}/uploaded", json={"youtube_url": "https://youtu.be/abc"}).json()
    assert (uploaded["status"], uploaded["youtube_url"]) == ("uploaded", "https://youtu.be/abc")
    assert client.post(f"/api/clips/{clip_id}/not-uploaded").json()["status"] == "rendered"

    assert client.delete(f"/api/destinations/{by_handle['id']}").status_code == 204
    assert client.get(f"/api/clips/{clip_id}").json()["destination_id"] is None
    assert client.get(f"/api/shows/{show['id']}").json()["episode_destination_id"] is None


def test_older_databases_get_new_columns(tmp_path):
    db = tmp_path / "clipper.db"
    with sqlite3.connect(db) as conn:  # a database from before shows existed
        conn.execute(
            "CREATE TABLE vod (id INTEGER PRIMARY KEY, url VARCHAR NOT NULL, mode VARCHAR NOT NULL, notes VARCHAR NOT NULL,"
            " workspace VARCHAR, title VARCHAR NOT NULL, channel VARCHAR NOT NULL, platform VARCHAR NOT NULL,"
            " duration FLOAT NOT NULL, status VARCHAR NOT NULL, progress FLOAT NOT NULL, error VARCHAR NOT NULL,"
            " retry_at DATETIME, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
        )
        conn.execute(
            "INSERT INTO vod VALUES (1, 'https://x.com/a/status/1', 'whole', '', NULL, 'Old', '', '', 60, 'ready', 1, '',"
            " NULL, '2026-10-01 00:00:00', '2026-10-01 00:00:00')"
        )
    with Session(get_engine(Settings(data_dir=tmp_path))) as s:
        vod = s.get(Vod, 1)
        assert (vod.title, vod.show_id, vod.mode_auto, vod.upload_date) == ("Old", None, True, "")
