import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
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


def test_shows_keep_how_many_clips_to_offer_and_what_goes_ahead_without_review(client):
    show = client.post("/api/shows", json={**PRESHOW, "max_chapters": 3, "max_shorts": 8, "auto_episode": "upload",
                                           "auto_visibility": "unlisted"}).json()
    assert (show["max_chapters"], show["max_shorts"], show["auto_episode"], show["auto_chapters"], show["auto_visibility"]) == (
        3, 8, "upload", "review", "unlisted",
    )
    assert client.patch(f"/api/shows/{show['id']}", json={"auto_shorts": "sometimes"}).status_code == 422
    assert client.patch(f"/api/shows/{show['id']}", json={"max_shorts": -1}).status_code == 422
    assert client.patch(f"/api/shows/{show['id']}", json={"max_shorts": 12}).json()["max_shorts"] == 12
    assert show["auto_upload_score"] == 0  # off
    assert client.patch(f"/api/shows/{show['id']}", json={"auto_upload_score": 9}).status_code == 422  # it's 0-1
    assert client.patch(f"/api/shows/{show['id']}", json={"auto_upload_score": 0.9}).json()["auto_upload_score"] == 0.9


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_a_shorts_background_is_saved_at_the_shorts_size_and_the_video_placed_on_it(client, settings, tmp_path):
    show = client.post("/api/shows", json={"name": "Kino", "short_video_top": 0.32}).json()
    assert (show["short_background"], show["short_video_top"], show["short_video_width"]) == ("", 0.32, 1.0)

    art = tmp_path / "art.png"  # the size of an image as it came from the user, not quite 9:16
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=green:s=941x1700",
                    "-frames:v", "1", str(art)], check=True)
    show = client.put(f"/api/shows/{show['id']}/short-background", content=art.read_bytes()).json()
    assert show["short_background"].startswith(f"/media/shows/{show['id']}/short-background.png?v=")
    image = client.get(show["short_background"])
    assert image.headers["content-type"] == "image/png"
    saved = settings.short_background(show["id"])
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "json",
                                       str(saved)], capture_output=True, text=True).stdout)
    assert (probe["streams"][0]["width"], probe["streams"][0]["height"]) == (1080, 1920)

    assert client.put(f"/api/shows/{show['id']}/short-background", content=b"not an image").status_code == 422
    assert saved.exists()  # the one before stays

    show = client.patch(f"/api/shows/{show['id']}", json={"short_video_top": None, "short_video_width": 0.9}).json()
    assert (show["short_video_top"], show["short_video_width"]) == (None, 0.9)
    assert client.delete(f"/api/shows/{show['id']}/short-background").json()["short_background"] == ""
    assert not saved.exists()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_an_overlay_is_saved_and_a_frames_screen_reported(client, settings, tmp_path):
    show = client.post("/api/shows", json={"name": "Kino", "mode": "both"}).json()
    assert (show["overlay"], show["overlay_screen"], show["overlay_episodes"]) == ("", None, False)
    tv = tmp_path / "tv.png"  # a frame with a see-through screen, drawn by ffmpeg
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=gray:s=1920x1080,format=rgba",
                    "-vf", "drawbox=x=160:y=110:w=1600:h=900:color=black@0.0:replace=1:t=fill", "-frames:v", "1", str(tv)], check=True)

    show = client.put(f"/api/shows/{show['id']}/overlay", content=tv.read_bytes()).json()
    assert show["overlay"].startswith(f"/media/shows/{show['id']}/overlay.png?v=")
    assert show["overlay_screen"] == [160, 110, 1600, 900]
    assert client.get(show["overlay"]).headers["content-type"] == "image/png"
    assert client.patch(f"/api/shows/{show['id']}", json={"overlay_episodes": True}).json()["overlay_episodes"] is True
    moved = client.patch(f"/api/shows/{show['id']}", json={"overlay_x": 0.05, "overlay_y": 0.7, "overlay_scale": 0.6}).json()
    assert (moved["overlay_x"], moved["overlay_y"], moved["overlay_scale"]) == (0.05, 0.7, 0.6)
    assert client.patch(f"/api/shows/{show['id']}", json={"overlay_x": 1.5}).status_code == 422
    show = client.put(f"/api/shows/{show['id']}/overlay", content=tv.read_bytes()).json()
    assert (show["overlay_x"], show["overlay_y"], show["overlay_scale"]) == (None, None, 1.0)  # a new image starts as drawn
    assert show["overlay_box"] == [0, 0, 1920, 1080]

    jpg = tmp_path / "photo.jpg"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=gray:s=640x360",
                    "-frames:v", "1", str(jpg)], check=True)
    refused = client.put(f"/api/shows/{show['id']}/overlay", content=jpg.read_bytes())
    assert refused.status_code == 422 and "no transparent parts" in refused.json()["detail"]
    assert client.get(f"/api/shows/{show['id']}").json()["overlay_screen"] == [160, 110, 1600, 900]  # kept

    assert client.delete(f"/api/shows/{show['id']}/overlay").json()["overlay"] == ""
    assert not settings.show_overlay(show["id"]).exists()


def test_a_watched_channel_can_move_to_its_own_show_with_its_link(client, settings):
    kino = client.post("/api/shows", json={"name": "Kino Casino", "mode": "both", "match": "kick.com/kinocasinogaming"}).json()
    x = client.post(f"/api/shows/{kino['id']}/channels", json={"url": "x.com/Andywarski", "keywords": "tonight on"}).json()
    with Session(get_engine(settings)) as s:
        s.add(ChannelVideo(channel_id=x["id"], video_id="1", url="https://x.com/Andywarski/status/1", status="older"))
        s.commit()
    preshow = client.post("/api/shows", json=PRESHOW).json()  # its links already have x.com/Andywarski

    moved = client.patch(f"/api/channels/{x['id']}", json={"show_id": preshow["id"]}).json()
    assert (moved["show_id"], moved["keywords"]) == (preshow["id"], "tonight on")
    assert client.get(f"/api/shows/{kino['id']}").json()["match"] == "kick.com/kinocasinogaming"
    assert client.get(f"/api/shows/{preshow['id']}").json()["match"] == "x.com/Andywarski"  # not added twice
    assert [v["url"] for v in client.get(f"/api/channels/{x['id']}/videos").json()] == [
        "https://x.com/Andywarski/status/1",  # still seen, so not picked up again
    ]

    # pasted links now go to the pre-show
    assert client.post("/api/vods", json={"url": "https://x.com/Andywarski/status/2"}).json()["show_id"] == preshow["id"]

    again = client.post(f"/api/shows/{kino['id']}/channels", json={"url": "x.com/Andywarski"}).json()
    assert client.patch(f"/api/channels/{again['id']}", json={"show_id": preshow["id"]}).status_code == 409
    assert client.patch(f"/api/channels/{again['id']}", json={"show_id": 999}).status_code == 404


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
    assert client.post(url, json={"url": "https://x.com/i/status/1"}).status_code == 422
    channel = client.post(url, json={"url": "kick.com/KinoCasino", "exclude": "rerun", "min_minutes": 60}).json()
    assert (channel["platform"], channel["url"], channel["min_minutes"]) == ("kick", "https://kick.com/kinocasino", 60)
    assert client.post(url, json={"url": "https://kick.com/kinocasino/videos"}).status_code == 409
    saved = client.get(f"/api/shows/{show['id']}").json()
    assert saved["channels"][0]["id"] == channel["id"]
    assert saved["match"] == "kick.com/kinocasino"  # the channel's links now match the show

    paused = client.patch(f"/api/channels/{channel['id']}", json={"enabled": False, "keywords": " kino , casino "}).json()
    assert paused["enabled"] is False and paused["keywords"] == "kino , casino"

    with Session(get_engine(settings)) as s:  # as if the watcher had listed older videos
        s.add_all([
            ChannelVideo(channel_id=channel["id"], video_id="v0", url="https://kick.com/kinocasino/videos/v0",
                         title="Older stream", status="older", published_at=datetime(2026, 9, 1, tzinfo=timezone.utc)),
            ChannelVideo(channel_id=channel["id"], video_id="v1", url="https://kick.com/kinocasino/videos/v1",
                         title="Old stream", duration=3600, status="older", published_at=datetime(2026, 10, 1, tzinfo=timezone.utc)),
        ])
        s.commit()
    older, oldest = client.get(f"/api/channels/{channel['id']}/videos").json()  # newest first, by when they were posted
    assert (older["title"], older["published_at"]) == ("Old stream", "2026-10-01T00:00:00+00:00")
    assert oldest["title"] == "Older stream"
    queued = client.post(f"/api/channel-videos/{older['id']}/process").json()
    assert queued["status"] == "queued"
    vod = client.get(f"/api/vods/{queued['vod_id']}").json()["vod"]
    assert (vod["show_id"], vod["mode"]) == (show["id"], "segment")

    assert client.delete(f"/api/shows/{show['id']}").status_code == 204
    assert client.get(f"/api/vods/{queued['vod_id']}").json()["vod"]["show_id"] is None
    assert client.get(f"/api/channels/{channel['id']}/videos").status_code == 404


def test_watching_an_x_account(client):
    show = client.post("/api/shows", json=PRESHOW).json()
    url = f"/api/shows/{show['id']}/channels"
    channel = client.post(url, json={"url": "https://x.com/Andywarski", "min_minutes": 5}).json()
    assert (channel["platform"], channel["url"]) == ("x", "https://x.com/andywarski")
    assert client.get(f"/api/shows/{show['id']}").json()["match"] == "x.com/Andywarski"  # it already matched
    assert client.get("/api/studio").json()["x_signed_in"] is False  # the worker hasn't seen the browser yet


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
    rules = client.patch(f"/api/shows/{show['id']}", json={"thumbnail_guidance": "Must have the words Kino Casino Preshow"})
    assert rules.json()["thumbnail_guidance"] == "Must have the words Kino Casino Preshow"

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
