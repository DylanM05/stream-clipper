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
from clipper.thumbnails import Idea, Person, ThumbnailError
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
        s.add(Show(name="Don", notes="Don wears a red cap.", auto_thumbnails=True,
                   thumbnail_guidance="Must have the words DON'S DEN"))
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


def test_claude_follows_the_shows_rules_for_its_thumbnails(settings, clip_id, monkeypatch):
    sent = {}

    def structured(self, system_prompt, task, stdin_text, schema):
        sent.update(system=system_prompt, input=stdin_text)
        return Idea(people=[], concept="c", text="KINO CASINO PRESHOW", prompt="p")

    monkeypatch.setattr(thumbnails.ClaudeCode, "structured", structured)
    thumbnails.write_idea(settings, get_clip(settings, clip_id), "", rules="Must have the words Kino Casino Preshow")
    assert ("# The show's rules for every thumbnail (follow them, over your defaults)\n"
            "Must have the words Kino Casino Preshow") in sent["input"]
    assert "## The show's rules" in sent["system"]  # how to fit them in

    thumbnails.write_idea(settings, get_clip(settings, clip_id), "")
    assert "rules" not in sent["input"]


def test_long_clips_are_thinned_out_evenly(settings, clip_id, monkeypatch):
    monkeypatch.setattr(thumbnails, "MAX_TRANSCRIPT_CHARS", 100)
    clip = get_clip(settings, clip_id)
    clip.start, clip.end = 0, 300
    lines = thumbnails.excerpt(Workspace(settings.vods_dir / "vod-1"), clip).splitlines()
    assert 3 <= len(lines) < 10 and lines[0] == "[00:00:00] Line 0."


def test_the_worker_makes_queued_thumbnails_and_keeps_claudes_idea(settings, clip_id, monkeypatch):
    given = []

    def make(settings_, clip, ws, notes="", channel_picture=None, idea=None, rules=""):
        assert notes == "Don wears a red cap." and clip.thumbnail_hint == "Don as a judge"
        assert rules == "Must have the words DON'S DEN"  # the show's
        assert channel_picture("Anyone") is None  # no API key or connected channel to search YouTube with
        given.append(idea)
        out = ws.clips_dir / f"{clip.key}-thumbnail.jpg"
        out.parent.mkdir(exist_ok=True)
        out.write_bytes(b"jpeg")
        idea = idea or Idea(people=[don()], concept="Don as a judge, gavel raised.", text="GUILTY", prompt="Don bangs a gavel.")
        return out, idea, "Don (from the video)"

    monkeypatch.setattr(worker_module.thumbnails, "make", make)
    queue(settings, clip_id, thumbnail_hint="Don as a judge")
    w = worker_module.Worker(settings)
    assert w.make_next_thumbnail() is True and w.make_next_thumbnail() is False

    clip = get_clip(settings, clip_id)
    assert clip.thumbnail_status == "done" and given == [None]  # Claude came up with it
    assert clip.thumbnail_idea == 'Don as a judge, gavel raised. Text: "GUILTY". People: Don (from the video).'
    assert clip.thumbnail_path.endswith("ch01-thumbnail.jpg")
    assert Idea.model_validate_json(clip.thumbnail_plan).prompt == "Don bangs a gavel."  # kept, to draw again

    # Drawn again from a changed prompt: the same plan and people, without asking Claude.
    queue(settings, clip_id, thumbnail_redraw="Don bangs a gavel, in a courtroom sketch style.")
    w.make_next_thumbnail()
    assert given[1].people == [don()] and given[1].prompt == "Don bangs a gavel, in a courtroom sketch style."
    clip = get_clip(settings, clip_id)
    assert Idea.model_validate_json(clip.thumbnail_plan).prompt == "Don bangs a gavel, in a courtroom sketch style."
    assert clip.thumbnail_redraw == ""


def queue(settings, clip_id, **fields) -> None:
    """Ask for a thumbnail, as the app does."""
    with Session(get_engine(settings)) as s:
        clip = s.get(ClipRecord, clip_id)
        clip.thumbnail_status = "queued"
        for k, v in fields.items():
            setattr(clip, k, v)
        s.commit()


def don() -> Person:
    return Person(name="Don", full_name="Don", in_video=True, looks="")


def talked_about(name: str, full_name: str = "", looks: str = "") -> Person:
    return Person(name=name, full_name=full_name or name, in_video=False, looks=looks)


class FakeWeb:
    """Wikipedia's search, the pictures it links to, and OpenRouter, kept in memory."""

    def __init__(self, articles: dict[str, str], png: bytes):
        self.articles = articles  # search term → title of the article it finds
        self.png = png  # every picture
        self.drawn: list[dict] = []

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(thumbnails.WIKIPEDIA_URL):
            assert request.headers["user-agent"].startswith("StreamClipper")
            title = self.articles.get(request.url.params["gsrsearch"])
            pages = [{"title": title, "thumbnail": {"source": f"https://upload.example/{title}.png"}}] if title else []
            return httpx.Response(200, json={"query": {"pages": pages}} if pages else {"batchcomplete": True})
        if url.startswith("https://upload.example/") or url.startswith("https://yt.example/"):
            return httpx.Response(200, content=self.png, headers={"content-type": "image/png"})
        self.drawn.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(self.png).decode()}]})


def test_only_a_page_or_channel_with_their_name_counts():
    same = thumbnails._same_person
    # One person, three names: Jer, TheQuartering and Jeremy Hambly.
    assert same(talked_about("TheQuartering", "Jeremy Hambly"), "Jeremy Hambly")
    assert same(talked_about("TheQuartering", "Jeremy Hambly"), "The Quartering")
    assert same(talked_about("Jer", "Jeremy Hambly"), "Jeremy Hambly")
    assert not same(talked_about("Jer"), "Jeremy Renner")  # a nickname alone could be any Jeremy
    assert same(talked_about("Ethan Klein"), "Ethan Klein TV")
    assert not same(talked_about("Ethan Klein"), "H3 Podcast")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_people_the_video_talks_about_are_looked_up_and_pointed_out_to_the_image_model(settings, clip_id, monkeypatch, tmp_path):
    from clipper import ffmpeg

    ws = Workspace(settings.vods_dir / "vod-1")
    ffmpeg.run(["-f", "lavfi", "-i", "testsrc=size=320x180:rate=10", "-t", "4", "-pix_fmt", "yuv420p", str(ws.source)])
    picture = tmp_path / "picture.png"
    ffmpeg.run(["-f", "lavfi", "-i", "color=blue:size=64x64", "-frames:v", "1", str(picture)])
    web = FakeWeb({"Ethan Klein": "Ethan Klein"}, picture.read_bytes())
    idea = Idea(
        people=[don(), talked_about("Ethan Klein", looks="beard, glasses"), talked_about("TheQuartering", "Jeremy Hambly"),
                talked_about("Nobody Famous", looks="bald")],
        concept="c", text="", prompt="Don laughs at Ethan Klein while TheQuartering sulks.",
    )
    monkeypatch.setattr(thumbnails, "write_idea", lambda *a, **k: idea)
    searched = []

    def channel_picture(name):
        searched.append(name)
        return ("The Quartering", "https://yt.example/q.jpg") if name == "TheQuartering" else ("Somebody Else", "https://yt.example/x.jpg")

    clip = get_clip(settings, clip_id)
    clip.start, clip.end = 0, 4  # the test video is 4 seconds long
    path, _, cast = thumbnails.make(settings, clip, ws, http=web.client(), channel_picture=channel_picture)

    assert path.exists()
    assert searched == ["TheQuartering", "Nobody Famous"]  # Ethan Klein was on Wikipedia; nobody looks Don up
    assert cast == ("Don (from the video), Ethan Klein (Wikipedia photo), TheQuartering (YouTube channel photo), "
                    "Nobody Famous (from a description)")
    [body] = web.drawn
    assert len(body["input_references"]) == 4  # 2 frames, 2 photos
    assert body["prompt"].startswith("Don laughs at Ethan Klein while TheQuartering sulks.\n\nReference images 1 to 2 are frames")
    assert "Reference image 3 is a photo of Ethan Klein" in body["prompt"]
    assert "Reference image 4 is a photo of TheQuartering" in body["prompt"]
    assert "Nobody Famous looks like this: bald" in body["prompt"]
    assert "beard, glasses" not in body["prompt"]  # the photo says it better


def test_a_failed_thumbnail_says_why(settings, clip_id, monkeypatch):
    def refuse(*args, **kwargs):
        raise ThumbnailError("The image model sent no picture back")

    monkeypatch.setattr(worker_module.thumbnails, "make", refuse)
    worker_module.Worker(settings).make_thumbnail(clip_id)
    clip = get_clip(settings, clip_id)
    assert (clip.thumbnail_status, clip.thumbnail_error) == ("failed", "The image model sent no picture back")

    # One that has a thumbnail keeps it when another try doesn't work out.
    image = settings.data_dir / "thumb.jpg"
    image.write_bytes(b"jpeg")
    queue(settings, clip_id, thumbnail_path=str(image), thumbnail_redraw="Don, but bigger")
    worker_module.Worker(settings).make_next_thumbnail()
    clip = get_clip(settings, clip_id)
    assert (clip.thumbnail_status, clip.thumbnail_path, clip.thumbnail_redraw) == ("done", str(image), "")
    assert clip.thumbnail_error == "The image model sent no picture back"


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

    # The prompt it was drawn from shows, and it can be drawn again from that or a changed one.
    with Session(get_engine(settings)) as s:
        row = s.get(ClipRecord, clip_id)
        row.thumbnail_plan = Idea(people=[don()], concept="c", text="", prompt="Don bangs a gavel.").model_dump_json()
        s.commit()
    assert client.get(f"/api/clips/{clip_id}").json()["thumbnail_prompt"] == "Don bangs a gavel."
    clip = client.post(f"/api/clips/{clip_id}/thumbnail", json={"prompt": " Don bangs a gavel, smiling. "}).json()
    assert (clip["thumbnail_status"], clip["thumbnail_redrawing"], clip["thumbnail_hint"]) == ("queued", True, "Don as a judge")
    assert get_clip(settings, clip_id).thumbnail_redraw == "Don bangs a gavel, smiling."

    settings.openrouter_api_key = ""
    unset = TestClient(web_app.create_app(settings))
    assert unset.post(f"/api/clips/{clip_id}/thumbnail", json={}).status_code == 409
