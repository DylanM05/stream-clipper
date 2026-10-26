"""Thumbnails: Claude's idea, drawn by an image model on OpenRouter from frames of the video."""

import base64
import json
import re
import shutil

import httpx
import pytest
from conftest import line, transcript
from fastapi.testclient import TestClient
from sqlmodel import Session

from clipper import thumbnails
from clipper import worker as worker_module
from clipper.config import Settings
from clipper.db import ClipRecord, Show, Vod, get_engine
from clipper.thumbnails import Idea, ThumbnailError
from clipper.web import app as web_app
from clipper.workspace import Workspace, write_json, write_text


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, openrouter_api_key="or-key")


@pytest.fixture
def clip_id(settings):
    ws = Workspace.open(settings.vods_dir, "vod-1")
    source = ws.root / "source.mp4"
    source.write_bytes(b"")
    write_json(ws.meta_json, {"title": "Stream", "source_path": str(source)})
    write_text(ws.transcript_json, transcript(*[line(i * 10.0, f"Line {i}.") for i in range(30)], duration=300).model_dump_json())
    with Session(get_engine(settings)) as s:
        s.add(Show(name="Don", notes="Don wears a red cap.", auto_thumbnails=True))
        s.commit()
        s.add(Vod(url="https://kick.com/don/videos/1", show_id=1, workspace="vod-1", status="ready", duration=300))
        s.commit()
        clip = ClipRecord(vod_id=1, key="ch01", kind="chapter", start=100, end=160, title="Lawsuit Segment", status="approved")
        s.add(clip)
        s.commit()
        return clip.id


def get_clip(settings, clip_id) -> ClipRecord:
    with Session(get_engine(settings)) as s:
        return s.get(ClipRecord, clip_id)


def test_the_image_model_gets_the_prompt_and_the_frames(settings, tmp_path):
    sent = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append((request.headers["authorization"], json.loads(request.content)))
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(b"png").decode(), "media_type": "image/png"}]})

    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpg")
    image = thumbnails.draw(settings, "Don looks shocked", [frame], httpx.Client(transport=httpx.MockTransport(handle)))

    assert image == b"png"
    [(auth, body)] = sent
    assert auth == "Bearer or-key"
    assert (body["model"], body["prompt"], body["aspect_ratio"]) == ("google/gemini-2.5-flash-image", "Don looks shocked", "16:9")
    assert body["input_references"] == [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,anBn"}}]


@pytest.mark.parametrize("status, answer, message", [
    (400, {"error": {"message": "Prompt flagged", "code": 400}}, "OpenRouter refused it (400): Prompt flagged"),
    (200, {"data": []}, "sent no picture back"),
])
def test_a_refused_or_empty_answer_says_why(settings, status, answer, message):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, json=answer)))
    with pytest.raises(ThumbnailError, match=re.escape(message)):
        thumbnails.draw(settings, "prompt", [], client)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_frames_come_from_through_the_clip_and_the_picture_fills_youtubes_size(tmp_path):
    from clipper import ffmpeg

    video = tmp_path / "video.mp4"
    ffmpeg.run(["-f", "lavfi", "-i", "testsrc=size=640x360:rate=10", "-t", "4", "-pix_fmt", "yuv420p", str(video)])
    refs = thumbnails.frames(video, 1, 3, tmp_path, count=2)
    assert [p.name for p in refs] == ["frame-0.jpg", "frame-1.jpg"] and all(p.stat().st_size for p in refs)

    square = tmp_path / "square.png"
    ffmpeg.run(["-f", "lavfi", "-i", "color=red:size=1024x1024", "-frames:v", "1", str(square)])
    out = tmp_path / "clips" / "ch01-thumbnail.jpg"
    thumbnails.save(square.read_bytes(), out, tmp_path)
    stream = ffmpeg.probe(out)["streams"][0]
    assert (stream["width"], stream["height"]) == (1280, 720) and out.stat().st_size < 2_000_000


def test_claude_reads_the_clips_lines_and_full_episodes_go_by_their_description(settings, clip_id):
    ws = Workspace(settings.vods_dir / "vod-1")
    clip = get_clip(settings, clip_id)
    assert thumbnails.excerpt(ws, clip).splitlines()[:2] == ["[00:00:00] Line 10.", "[00:00:10] Line 11."]
    clip.kind = "whole"
    assert thumbnails.excerpt(ws, clip) == ""


def test_long_clips_are_thinned_out_evenly(settings, clip_id, monkeypatch):
    monkeypatch.setattr(thumbnails, "MAX_TRANSCRIPT_CHARS", 100)
    clip = get_clip(settings, clip_id)
    clip.start, clip.end = 0, 300
    lines = thumbnails.excerpt(Workspace(settings.vods_dir / "vod-1"), clip).splitlines()
    assert 3 <= len(lines) < 10 and lines[0] == "[00:00:00] Line 0."


def test_the_worker_makes_queued_thumbnails_and_keeps_claudes_idea(settings, clip_id, monkeypatch):
    def make(settings_, clip, ws, notes=""):
        assert notes == "Don wears a red cap." and clip.thumbnail_hint == "Don as a judge"
        out = ws.clips_dir / f"{clip.key}-thumbnail.jpg"
        out.parent.mkdir(exist_ok=True)
        out.write_bytes(b"jpeg")
        return out, Idea(people=["Don"], concept="Don as a judge, gavel raised.", text="GUILTY", prompt="...")

    monkeypatch.setattr(worker_module.thumbnails, "make", make)
    with Session(get_engine(settings)) as s:
        clip = s.get(ClipRecord, clip_id)
        clip.thumbnail_status, clip.thumbnail_hint = "queued", "Don as a judge"
        s.commit()
    w = worker_module.Worker(settings)
    assert w.make_next_thumbnail() is True and w.make_next_thumbnail() is False

    clip = get_clip(settings, clip_id)
    assert (clip.thumbnail_status, clip.thumbnail_idea) == ("done", 'Don as a judge, gavel raised. Text: "GUILTY"')
    assert clip.thumbnail_path.endswith("ch01-thumbnail.jpg")


def test_a_failed_thumbnail_says_why(settings, clip_id, monkeypatch):
    def refuse(*args, **kwargs):
        raise ThumbnailError("The image model sent no picture back")

    monkeypatch.setattr(worker_module.thumbnails, "make", refuse)
    worker_module.Worker(settings).make_thumbnail(clip_id)
    clip = get_clip(settings, clip_id)
    assert (clip.thumbnail_status, clip.thumbnail_error) == ("failed", "The image model sent no picture back")


def test_shows_that_make_thumbnails_queue_them_for_rendered_chapters(settings, clip_id, monkeypatch):
    monkeypatch.setattr(worker_module.render, "render_landscape", lambda source, clip, out, *a, **k: out.write_bytes(b"v"))
    w = worker_module.Worker(settings)
    w.render_next_clip()
    assert get_clip(settings, clip_id).thumbnail_status == "queued"

    settings.openrouter_api_key = ""  # without a key there are no thumbnails
    with Session(get_engine(settings)) as s:
        s.add(ClipRecord(vod_id=1, key="ch02", kind="chapter", start=0, end=60, title="t", status="approved"))
        s.commit()
    w.render_next_clip()
    with Session(get_engine(settings)) as s:
        assert s.get(ClipRecord, 2).thumbnail_status == ""


def test_asking_for_a_thumbnail_in_the_app(settings, clip_id):
    client = TestClient(web_app.create_app(settings))
    assert client.get("/api/thumbnails").json() == {"configured": True, "model": "google/gemini-2.5-flash-image"}

    clip = client.post(f"/api/clips/{clip_id}/thumbnail", json={"hint": " Don as a judge "}).json()
    assert (clip["thumbnail_status"], clip["thumbnail_hint"], clip["thumbnail_url"]) == ("queued", "Don as a judge", "")
    assert client.post(f"/api/clips/{clip_id}/thumbnail", json={}).status_code == 409  # already on it
    assert client.get(f"/media/clips/{clip_id}/thumbnail.jpg").status_code == 404

    image = settings.data_dir / "thumb.jpg"
    image.write_bytes(b"jpeg")
    with Session(get_engine(settings)) as s:
        row = s.get(ClipRecord, clip_id)
        row.thumbnail_status, row.thumbnail_path = "done", str(image)
        s.commit()
    clip = client.get(f"/api/clips/{clip_id}").json()
    assert clip["thumbnail_url"].startswith(f"/media/clips/{clip_id}/thumbnail.jpg?v=")
    assert client.get(f"/media/clips/{clip_id}/thumbnail.jpg").content == b"jpeg"
    assert client.post(f"/api/clips/{clip_id}/thumbnail/apply").status_code == 409  # not uploaded yet

    settings.openrouter_api_key = ""
    unset = TestClient(web_app.create_app(settings))
    assert unset.post(f"/api/clips/{clip_id}/thumbnail", json={}).status_code == 409
