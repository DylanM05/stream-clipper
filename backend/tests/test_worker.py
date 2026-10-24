from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import line, transcript
from sqlmodel import Session, select

from clipper import worker as worker_module
from clipper.config import Settings
from clipper.db import ClipRecord, Destination, Show, Vod, get_engine, utcnow
from clipper.llm import UsageLimitError
from clipper.models import LLMChapter, LLMHighlight, Segmentation, Signals
from clipper.workspace import Workspace, write_json, write_text

TRANSCRIPT = transcript(*[line(i * 10.0, f"Sentence number {i}.") for i in range(100)], duration=1000)
SEGMENTATION = Segmentation(
    episode_title="Huck Blend Grift and the H3 Fallout",
    episode_summary="The hosts cover the week's drama.",
    chapters=[
        LLMChapter(start="00:00:00", end="00:01:00", category="pre_stream", title="Starting soon", summary="s", confidence=0.9),
        LLMChapter(start="00:01:00", end="00:08:00", category="content", title="Whole show", summary="s", confidence=0.9),
        LLMChapter(start="00:08:00", end="00:16:40", category="content", title="Second half", summary="s", confidence=0.9),
    ],
    highlights=[
        LLMHighlight(start="00:01:00", end="00:01:40", title="Vertical", description="d", why="w", score=0.8, vertical_ok=True),
        LLMHighlight(start="00:05:00", end="00:05:40", title="Landscape only", description="d", why="w", score=0.6, vertical_ok=False),
    ],
)


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path)


@pytest.fixture
def fake_pipeline(settings, monkeypatch):
    """Replace downloading, transcription and Claude with canned results."""
    calls = {"segment": 0, "segment_kwargs": None, "preview_room": None}

    def fetch(url, s, on_progress=None):
        ws = Workspace.open(s.vods_dir, "vod-1")
        source = ws.root / "source.mp4"
        source.write_bytes(b"")
        write_json(ws.meta_json, {
            "title": "Big stream", "channel": "Creator", "extractor_key": "TwitchVod", "duration": 1000,
            "upload_date": "20261023",
            "description": "LIVE NOW: the preshow &amp; more https://t.co/abc\nsecond line", "source_path": str(source),
        })
        write_text(ws.transcript_json, TRANSCRIPT.model_dump_json())
        on_progress and on_progress(1.0)
        return ws

    def segment_run(*args, **kwargs):
        calls["segment"] += 1
        calls["segment_kwargs"] = kwargs
        return SEGMENTATION

    def preview_title(text, room, settings_, notes=""):
        calls["preview_room"] = room
        return calls.get("preview", "")

    monkeypatch.setattr(worker_module.download, "fetch", fetch)
    monkeypatch.setattr(worker_module.render, "render_proxy", lambda *a, **k: None)
    monkeypatch.setattr(worker_module.audio, "extract", lambda ws: None)
    monkeypatch.setattr(worker_module.transcribe, "run", lambda *a, **k: TRANSCRIPT)
    monkeypatch.setattr(worker_module.signals, "run", lambda *a, **k: Signals())
    monkeypatch.setattr(worker_module.segment, "run", segment_run)
    monkeypatch.setattr(worker_module.titles, "preview_title", preview_title)
    return calls


def add_show(settings, **fields) -> int:
    with Session(get_engine(settings)) as s:
        show = Show(**fields)
        s.add(show)
        s.commit()
        return show.id


def test_shows_give_clips_their_title_pattern_and_the_stream_date(settings, fake_pipeline):
    show_id = add_show(settings, name="Kino Casino", match="twitch.tv/videos", title_template="KINO CASINO {date} - {title}")
    vod_id = add_vod(settings)
    worker_module.Worker(settings).process_vod(vod_id)

    assert get(settings, Vod, vod_id).show_id == show_id
    assert [c.title for c in clips(settings)] == [
        "KINO CASINO Oct 23, 2026 - Whole show",
        "KINO CASINO Oct 23, 2026 - Second half",
        "KINO CASINO Oct 23, 2026 - Vertical",
        "KINO CASINO Oct 23, 2026 - Landscape only",
    ]
    kwargs = fake_pipeline["segment_kwargs"]
    assert kwargs["title_prefixed"] is True and kwargs["title_room"] == 100 - len("KINO CASINO Oct 23, 2026 -") - 1


def test_both_mode_gives_a_full_episode_with_chapters_plus_clips_each_headed_for_its_channel(settings, fake_pipeline):
    with Session(get_engine(settings)) as s:
        episodes, clip_channel = Destination(name="Full episodes"), Destination(name="Clips")
        s.add_all([episodes, clip_channel])
        s.commit()
        dest_ids = episodes.id, clip_channel.id
    add_show(settings, name="Kino Casino", match="twitch.tv/videos", mode="both",
             title_template="KINO CASINO {date} - {title}", episode_template="KINO CASINO FULL EPISODE {date} - {title}",
             notes="Hosts: Warski and Jer.", clip_guidance="Follow Jer's segments.",
             episode_destination_id=dest_ids[0], clip_destination_id=dest_ids[1])
    worker_module.Worker(settings).process_vod(add_vod(settings))

    full, *rest = sorted(clips(settings), key=lambda c: (c.kind != "whole", c.key))
    assert full.kind == "whole" and full.end == 1000
    assert full.title == "KINO CASINO FULL EPISODE Oct 23, 2026 - Huck Blend Grift and the H3 Fallout"
    assert full.description == "The hosts cover the week's drama.\n\nChapters\n0:00 Starting soon\n1:00 Whole show\n8:00 Second half"
    assert full.destination_id == dest_ids[0]
    assert {c.destination_id for c in rest} == {dest_ids[1]} and len(rest) == 4
    kwargs = fake_pipeline["segment_kwargs"]
    assert (kwargs["context"], kwargs["guidance"]) == ("Hosts: Warski and Jer.", "Follow Jer's segments.")
    assert fake_pipeline["preview_room"] is None  # Claude's episode title came with the segmentation


def test_whole_videos_get_claudes_short_title_in_the_show_pattern(settings, fake_pipeline):
    add_show(settings, name="Pre-show", match="x.com/a", mode="whole",
             title_template="♥️♠️♦️♣️KINO CASINO PRE SHOW♣️♦️♠️♥️ {date} - {title}")
    fake_pipeline["preview"] = "Huck Blend Grift Unleashed"
    vod_id = add_vod(settings, url="https://x.com/a/status/1", mode="segment")  # mode_auto: the show decides
    worker_module.Worker(settings).process_vod(vod_id)

    [clip] = clips(settings)
    assert clip.kind == "whole"
    assert clip.title == "♥️♠️♦️♣️KINO CASINO PRE SHOW♣️♦️♠️♥️ Oct 23, 2026 - Huck Blend Grift Unleashed"
    assert fake_pipeline["preview_room"] < 60


def add_vod(settings, url="https://www.twitch.tv/videos/1", mode="segment") -> int:
    with Session(get_engine(settings)) as s:
        vod = Vod(url=url, mode=mode)
        s.add(vod)
        s.commit()
        return vod.id


def get(settings, model, id_):
    with Session(get_engine(settings)) as s:
        return s.get(model, id_)


def clips(settings) -> list[ClipRecord]:
    with Session(get_engine(settings)) as s:
        return list(s.exec(select(ClipRecord).order_by(ClipRecord.key)))


def test_segment_mode_adds_clips_once(settings, fake_pipeline):
    vod_id = add_vod(settings)
    w = worker_module.Worker(settings)
    assert w.process_next_vod() is True

    vod = get(settings, Vod, vod_id)
    assert (vod.status, vod.title, vod.platform, vod.duration) == ("ready", "Big stream", "TwitchVod", 1000)
    found = clips(settings)
    assert [(c.key, c.kind, c.make_short) for c in found] == [
        ("ch01", "chapter", False), ("ch02", "chapter", False), ("hl01", "highlight", True), ("hl02", "highlight", False),
    ]
    assert all(c.status == "proposed" for c in found)

    w.process_vod(vod_id)  # running again doesn't duplicate clips
    assert len(clips(settings)) == 4
    assert w.process_next_vod() is False


def test_whole_mode_posts_the_entire_video(settings, fake_pipeline):
    vod_id = add_vod(settings, url="https://x.com/a/status/1", mode="whole")
    worker_module.Worker(settings).process_vod(vod_id)

    [clip] = clips(settings)
    assert (clip.key, clip.kind, clip.start, clip.end) == ("full", "whole", 0, 1000)
    assert clip.title == "LIVE NOW: the preshow & more"
    assert fake_pipeline["segment"] == 0


def test_usage_limit_waits_and_retries_later(settings, fake_pipeline, monkeypatch):
    resets = datetime.now(timezone.utc) + timedelta(hours=2)

    def limited(*args, **kwargs):
        raise UsageLimitError("usage limit reached", resets)

    monkeypatch.setattr(worker_module.segment, "run", limited)
    vod_id = add_vod(settings)
    w = worker_module.Worker(settings)
    w.process_vod(vod_id)

    vod = get(settings, Vod, vod_id)
    assert vod.status == "waiting"
    assert abs((vod.retry_at - resets).total_seconds()) < 1
    assert w.process_next_vod() is False  # not before the reset

    with Session(get_engine(settings)) as s:
        row = s.get(Vod, vod_id)
        row.retry_at = utcnow() - timedelta(seconds=1)
        s.add(row)
        s.commit()
    monkeypatch.setattr(worker_module.segment, "run", lambda *a, **k: SEGMENTATION)
    assert w.process_next_vod() is True
    assert get(settings, Vod, vod_id).status == "ready"


def test_failures_are_recorded(settings, fake_pipeline, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(worker_module.transcribe, "run", broken)
    vod_id = add_vod(settings)
    worker_module.Worker(settings).process_vod(vod_id)
    vod = get(settings, Vod, vod_id)
    assert vod.status == "failed" and vod.error == "RuntimeError: disk full"


@pytest.fixture
def fake_render(monkeypatch):
    rendered = []

    def landscape(source, clip, out: Path, settings):
        out.write_bytes(b"video")
        rendered.append(out.name)

    def short(source, clip, words, out: Path, settings):
        assert words, "Shorts need the transcript words for captions"
        out.write_bytes(b"short")
        rendered.append(out.name)

    monkeypatch.setattr(worker_module.render, "render_landscape", landscape)
    monkeypatch.setattr(worker_module.render, "render_short", short)
    return rendered


def approve(settings, key: str) -> int:
    with Session(get_engine(settings)) as s:
        clip = s.exec(select(ClipRecord).where(ClipRecord.key == key)).one()
        clip.status, clip.reviewed_at = "approved", utcnow()
        s.add(clip)
        s.commit()
        return clip.id


def test_approved_clips_are_rendered(settings, fake_pipeline, fake_render):
    w = worker_module.Worker(settings)
    w.process_vod(add_vod(settings))
    clip_id = approve(settings, "hl01")

    assert w.render_next_clip() is True
    clip = get(settings, ClipRecord, clip_id)
    assert clip.status == "rendered"
    assert Path(clip.video_path).name == "hl01-vertical.mp4"
    assert Path(clip.short_path).name == "hl01-vertical-short.mp4"
    assert w.render_next_clip() is False


def test_whole_videos_are_not_re_encoded(settings, fake_pipeline, fake_render):
    w = worker_module.Worker(settings)
    w.process_vod(add_vod(settings, url="https://x.com/a/status/1", mode="whole"))
    clip_id = approve(settings, "full")
    w.render_next_clip()

    clip = get(settings, ClipRecord, clip_id)
    assert clip.status == "rendered" and Path(clip.video_path).name == "source.mp4"
    assert fake_render == []


def test_a_clip_edited_during_its_render_is_rendered_again(settings, fake_pipeline, monkeypatch):
    w = worker_module.Worker(settings)
    w.process_vod(add_vod(settings))
    clip_id = approve(settings, "hl02")

    def landscape_while_user_edits(source, clip, out, settings_):
        out.write_bytes(b"video")
        with Session(get_engine(settings)) as s:  # the app re-approves after an edit
            row = s.get(ClipRecord, clip_id)
            row.status = "approved"
            s.add(row)
            s.commit()

    monkeypatch.setattr(worker_module.render, "render_landscape", landscape_while_user_edits)
    w.render_next_clip()
    assert get(settings, ClipRecord, clip_id).status == "approved"


def test_recover_requeues_interrupted_work(settings, fake_pipeline):
    vod_id = add_vod(settings)
    with Session(get_engine(settings)) as s:
        vod = s.get(Vod, vod_id)
        vod.status = "transcribing"
        s.add(vod)
        s.commit()
    worker_module.Worker(settings).recover()
    assert get(settings, Vod, vod_id).status == "queued"
