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



def test_a_show_can_watch_a_folder_of_tv_episodes_inside_the_media_folder(tmp_path):
    media = tmp_path / "media"
    (media / "TV" / "Show").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    # None, not left out: the Docker image's CLIPPER_MEDIA_DIR would fill it in (the tests run there in CI).
    unset = TestClient(web_app.create_app(Settings(data_dir=tmp_path / "data", web_dir=tmp_path / "web", media_dir=None)))
    show = unset.post("/api/shows", json={"name": "Cartoon Shorts", "video_type": "episodes", "max_chapters": 0}).json()
    assert (show["video_type"], show["max_chapters"]) == ("episodes", 0)
    added = unset.post(f"/api/shows/{show['id']}/channels", json={"url": str(media / "TV" / "Show")})
    assert added.status_code == 422 and "CLIPPER_HOST_MEDIA_DIR" in added.json()["detail"]
    assert unset.get("/api/folders").status_code == 422

    client = TestClient(web_app.create_app(Settings(data_dir=tmp_path / "data", web_dir=tmp_path / "web", media_dir=media)))
    url = f"/api/shows/{show['id']}/channels"
    for outside in (str(tmp_path / "elsewhere"), f"{media}/TV/../../elsewhere", str(media / "TV" / "Missing")):
        assert client.post(url, json={"url": outside}).status_code == 422
    folder = str((media / "TV" / "Show").resolve())
    channel = client.post(url, json={"url": f"{media}/TV/Show/", "backlog": True, "backlog_per_day": 3}).json()
    assert (channel["platform"], channel["url"], channel["backlog"], channel["backlog_per_day"]) == ("folder", folder, True, 3)
    assert client.post(url, json={"url": folder}).status_code == 409
    assert client.get(f"/api/shows/{show['id']}").json()["match"] == folder  # a file added by its path is the show's

    other = client.post("/api/shows", json={"name": "Other", "video_type": "other"}).json()
    moved = client.patch(f"/api/channels/{channel['id']}", json={"show_id": other["id"], "backlog_paused": True}).json()
    assert moved["backlog_paused"] is True
    assert (client.get(f"/api/shows/{show['id']}").json()["match"], client.get(f"/api/shows/{other['id']}").json()["match"]) == ("", folder)
    assert client.patch(f"/api/shows/{show['id']}", json={"video_type": "movies"}).status_code == 422
    assert client.patch(f"/api/channels/{channel['id']}", json={"backlog_per_day": 0}).status_code == 422



def test_each_show_says_how_its_shorts_are_laid_out(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", web_dir=tmp_path / "web", short_layout="crop")
    client = TestClient(web_app.create_app(settings))
    show = client.post("/api/shows", json={"name": "Kino"}).json()
    assert show["short_layout"] == "crop"  # as the server's CLIPPER_SHORT_LAYOUT is
    background = settings.short_background(show["id"])
    background.parent.mkdir(parents=True)
    background.write_bytes(b"png")
    assert client.get(f"/api/shows/{show['id']}").json()["short_layout"] == "blur"  # its own background goes ahead
    assert client.patch(f"/api/shows/{show['id']}", json={"short_layout": "crop"}).json()["short_layout"] == "crop"
    assert client.patch(f"/api/shows/{show['id']}", json={"short_layout": "zoom"}).status_code == 422

def test_choosing_a_folder_from_the_media_folder(tmp_path):
    media = tmp_path / "media"
    for name in ("TV/Show/Season 10/S10E01.mkv", "TV/Show/Season 2/S02E01.mkv", "TV/Show/Season 1/S01E02.mkv",
                 "TV/Show/Season 1/S01E01.mkv", "TV/Show/Pilot.mp4", "TV/Show/Pilot.srt", "TV/Show/.hidden/x.mkv"):
        (media / name).parent.mkdir(parents=True, exist_ok=True)
        (media / name).write_bytes(b"video")
    (media / "Movies").mkdir()
    client = TestClient(web_app.create_app(Settings(data_dir=tmp_path / "data", web_dir=tmp_path / "web", media_dir=media)))

    top = client.get("/api/folders").json()
    assert (top["path"], top["parent"], top["folders"], top["videos"]) == (str(media.resolve()), None, ["Movies", "TV"], 0)
    show = client.get("/api/folders", params={"path": f"{media}/TV/Show"}).json()
    assert (show["parent"], show["folders"], show["videos"]) == (str((media / "TV").resolve()), ["Season 1", "Season 2", "Season 10"], 1)
    assert client.get("/api/folders", params={"path": str(tmp_path)}).status_code == 422

    videos = client.get("/api/folders/videos", params={"path": f"{media}/TV/Show"}).json()
    assert videos == {"videos": 5, "first": ["Pilot.mp4", "Season 1/S01E01.mkv", "Season 1/S01E02.mkv",
                                             "Season 2/S02E01.mkv", "Season 10/S10E01.mkv"]}


def test_the_library_shows_how_far_each_backlog_is_and_can_skip_and_put_back(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", web_dir=tmp_path / "web", media_dir=tmp_path)
    client = TestClient(web_app.create_app(settings))
    show = client.post("/api/shows", json={"name": "Kino", "video_type": "streams"}).json()
    kick = client.post(f"/api/shows/{show['id']}/channels", json={"url": "kick.com/kino"}).json()
    assert kick["backlog"] is False and client.get("/api/library").json() == []

    with Session(get_engine(settings)) as s:  # as if the watcher had listed it, and gone through some of it
        s.add(done := Vod(url="https://kick.com/kino/videos/a", status="ready"))
        s.add(working := Vod(url="https://kick.com/kino/videos/b", status="transcribing", progress=0.4))
        s.flush()
        s.add(ClipRecord(vod_id=done.id, key="hl01", kind="highlight", start=0, end=30, title="A"))
        s.add_all([
            ChannelVideo(channel_id=kick["id"], video_id=f"v{n}", url=f"https://kick.com/kino/videos/{n}", title=f"Stream {n}",
                         published_at=datetime(2026, month, day, tzinfo=timezone.utc), status=status, vod_id=vod)
            for n, (month, day, status, vod) in enumerate([
                (9, 1, "queued", done.id), (9, 20, "queued", working.id), (10, 2, "older", None),
                (10, 5, "older", None), (10, 9, "skipped", None), (10, 30, "waiting", None),
            ])
        ])
        s.commit()

    # The show already watches it, so adding it to the Library puts it in, checked straight away.
    added = client.post(f"/api/shows/{show['id']}/channels", json={"url": "https://kick.com/kino", "backlog": True,
                                                                  "backlog_per_day": 1, "backlog_order": "first"})
    assert added.status_code == 201 and added.json()["id"] == kick["id"] and added.json()["backlog"] is True
    assert added.json()["last_checked_at"] is None

    [summary] = client.get("/api/library").json()
    assert {k: summary[k] for k in ("name", "show_name", "video_type", "total", "done", "working", "failed", "left",
                                    "skipped", "copying", "next", "today")} == {
        "name": "kino", "show_name": "Kino", "video_type": "streams", "total": 5, "done": 1, "working": 1, "failed": 0,
        "left": 2, "skipped": 1, "copying": 1, "next": "Stream 2", "today": 0,
    }
    detail = client.get(f"/api/channels/{kick['id']}/backlog").json()
    assert [(i["title"], i["group"], i["state"]) for i in detail["items"]] == [  # oldest first, by month
        ("Stream 0", "September 2026", "done"), ("Stream 1", "September 2026", "working"),
        ("Stream 2", "October 2026", "left"), ("Stream 3", "October 2026", "left"),
        ("Stream 4", "October 2026", "skipped"), ("Stream 5", "October 2026", "copying"),
    ]
    first, second = detail["items"][:2]
    assert (first["vod_status"], first["counts"], second["vod_progress"]) == ("ready", {"proposed": 1}, 0.4)

    ids = {i["title"]: i["id"] for i in detail["items"]}
    skip = f"/api/channels/{kick['id']}/backlog/skip"
    assert client.post(skip, json={"ids": [ids["Stream 2"], ids["Stream 0"]], "skip": True}).status_code == 204
    assert client.get("/api/library").json()[0]["next"] == "Stream 3"  # one already done isn't skipped
    assert client.post(skip, json={"ids": [ids["Stream 2"], ids["Stream 4"]], "skip": False}).status_code == 204
    detail = client.get(f"/api/channels/{kick['id']}/backlog").json()
    assert [i["state"] for i in detail["items"]] == ["done", "working", "left", "left", "left", "copying"]
    assert detail["items"][4]["reason"] == "Already on the channel when you added it"

    newest = client.patch(f"/api/channels/{kick['id']}", json={"backlog_order": "latest"}).json()
    assert newest["last_checked_at"] is None
    assert client.get("/api/library").json()[0]["next"] == "Stream 4"
    detail = client.get(f"/api/channels/{kick['id']}/backlog").json()
    assert [(i["title"], i["place"]) for i in detail["items"]][:3] == [("Stream 5", None), ("Stream 4", 1), ("Stream 3", 2)]

    # At random, it's still shown oldest first, with each one's place in the line.
    client.patch(f"/api/channels/{kick['id']}", json={"backlog_order": "random"})
    detail = client.get(f"/api/channels/{kick['id']}/backlog").json()
    assert [i["title"] for i in detail["items"]] == [f"Stream {n}" for n in range(6)]
    places = {i["title"]: i["place"] for i in detail["items"] if i["place"]}
    assert sorted(places.values()) == [1, 2, 3] and set(places) == {"Stream 2", "Stream 3", "Stream 4"}
    assert detail["next"] == next(title for title, place in places.items() if place == 1)
    assert client.patch(f"/api/channels/{kick['id']}", json={"backlog_order": "sideways"}).status_code == 422
    client.patch(f"/api/channels/{kick['id']}", json={"backlog": False})
    assert client.get("/api/library").json() == []


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
