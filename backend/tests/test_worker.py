import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from conftest import line, transcript
from sqlmodel import Session, select
from typer.testing import CliRunner

from clipper import cli
from clipper import worker as worker_module
from clipper.config import Settings
from clipper.db import (
    Channel, ChannelVideo, ClipRecord, Destination, Show, UploadJob, Vod, get_engine, set_app_setting, utcnow,
)
from clipper.llm import UsageLimitError
from clipper.models import LLMChapter, LLMHighlight, Segmentation, Signals
from clipper.workspace import Workspace, read_json, write_json, write_text

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

    def fetch(url, s, on_progress=None, limit_mbps=None):
        calls["limit_mbps"] = limit_mbps
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


def test_downloads_use_the_speed_limit_set_in_the_app(settings, fake_pipeline):
    from clipper.db import set_app_setting

    w = worker_module.Worker(settings)
    w.process_vod(add_vod(settings))
    assert fake_pipeline["limit_mbps"] == settings.download_limit_mbps == 50  # the default

    with Session(get_engine(settings)) as s:
        set_app_setting(s, "download_limit_mbps", "20")
        s.commit()
    w.process_vod(add_vod(settings, url="https://www.twitch.tv/videos/2"))
    assert fake_pipeline["limit_mbps"] == 20


def test_changing_the_speed_limit_mid_download_carries_on_at_the_new_speed(settings, fake_pipeline, monkeypatch):
    from clipper.db import set_app_setting

    finish = worker_module.download.fetch  # the fake from fake_pipeline
    limits = []

    def fetch(url, s, on_progress=None, limit_mbps=None):
        limits.append(limit_mbps)
        if len(limits) == 1:
            with Session(get_engine(settings)) as session:  # changed in the app partway through
                set_app_setting(session, "download_limit_mbps", "500")
                session.commit()
            on_progress(0.4)
            raise AssertionError("the download should have stopped to pick up the new limit")
        return finish(url, s, on_progress, limit_mbps)

    monkeypatch.setattr(worker_module.download, "fetch", fetch)
    vod_id = add_vod(settings)
    worker_module.Worker(settings).process_vod(vod_id)
    assert limits == [50, 500]
    assert get(settings, Vod, vod_id).status == "ready"


def test_whole_mode_posts_the_entire_video(settings, fake_pipeline):
    vod_id = add_vod(settings, url="https://x.com/a/status/1", mode="whole")
    worker_module.Worker(settings).process_vod(vod_id)

    [clip] = clips(settings)
    assert (clip.key, clip.kind, clip.start, clip.end) == ("full", "whole", 0, 1000)
    assert clip.title == "LIVE NOW: the preshow & more"
    assert fake_pipeline["segment"] == 0


def test_a_show_that_saves_transcripts_gets_each_streams_in_its_own_folder(settings, fake_pipeline):
    add_show(settings, name="Night Owls", match="twitch.tv/videos", save_transcripts=True)
    worker_module.Worker(settings).process_vod(add_vod(settings))

    folder = settings.transcripts_dir / "night-owls"
    saved = read_json(folder / "2026-10-23_vod-1.json")
    assert {k: saved[k] for k in ("id", "show", "title", "url", "channel", "platform", "date", "duration")} == {
        "id": "vod-1", "show": "Night Owls", "title": "Big stream", "url": "https://www.twitch.tv/videos/1",
        "channel": "Creator", "platform": "TwitchVod", "date": "2026-10-23", "duration": 1000,
    }
    line = TRANSCRIPT.segments[1]
    assert saved["segments"][1] == {"start": 10.0, "end": line.end, "text": "Sentence number 1.",
                                    "words": [[w.start, w.end] for w in line.words]}
    assert len(saved["segments"]) == 100
    assert (folder / "2026-10-23_vod-1.txt").read_text(encoding="utf-8").splitlines()[1] == "[00:00:10] Sentence number 1."


def test_transcripts_are_only_saved_for_shows_that_ask_and_failing_to_doesnt_stop_the_stream(settings, fake_pipeline, monkeypatch):
    show_id = add_show(settings, name="Night Owls", match="twitch.tv/videos")
    vod_id = add_vod(settings)
    worker_module.Worker(settings).process_vod(vod_id)
    assert not settings.transcripts_dir.exists()

    with Session(get_engine(settings)) as s:
        s.get(Show, show_id).save_transcripts = True
        s.commit()
    monkeypatch.setattr(worker_module.transcripts, "save", lambda *a: 1 / 0)
    vod_id = add_vod(settings, url="https://www.twitch.tv/videos/2")
    worker_module.Worker(settings).process_vod(vod_id)
    assert get(settings, Vod, vod_id).status == "ready"


def test_save_transcripts_saves_past_streams_of_the_shows_with_it_on(settings, fake_pipeline, monkeypatch):
    show_id = add_show(settings, name="Night Owls", match="twitch.tv/videos")
    add_show(settings, name="Pre-show", match="x.com/a", save_transcripts=True)  # nothing transcribed
    worker_module.Worker(settings).process_vod(add_vod(settings))
    with Session(get_engine(settings)) as s:
        s.get(Show, show_id).save_transcripts = True
        s.commit()

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    out = CliRunner().invoke(cli.app, ["save-transcripts"])
    assert out.exit_code == 0, out.output
    assert "Night Owls: 1 transcript in" in out.output and "Pre-show: 0 transcripts in" in out.output
    assert read_json(settings.transcripts_dir / "night-owls" / "2026-10-23_vod-1.json")["title"] == "Big stream"


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


MEMBERS_ERROR = ("ERROR: [youtube] 1isrqE6jWmo: Join this channel to get access to members-only content like this "
                 "video, and other exclusive perks.")


def members_only(*args, **kwargs):
    raise worker_module.download.MembersOnly(MEMBERS_ERROR)


def test_a_watched_channels_members_only_video_is_skipped_without_a_word(settings, fake_pipeline, monkeypatch):
    sent = []
    monkeypatch.setattr(worker_module.notify, "send", lambda settings_, note: sent.append(note))
    monkeypatch.setattr(worker_module.download, "fetch", members_only)
    vod_id = add_vod(settings, url="https://www.youtube.com/watch?v=1isrqE6jWmo")
    with Session(get_engine(settings)) as s:
        show = Show(name="Show")
        s.add(show)
        s.commit()
        channel = Channel(show_id=show.id, url="https://www.youtube.com/@creator", platform="youtube")
        s.add(channel)
        s.commit()
        s.add(ChannelVideo(channel_id=channel.id, video_id="1isrqE6jWmo", url="https://www.youtube.com/watch?v=1isrqE6jWmo",
                           vod_id=vod_id, status="queued", backlog_at=utcnow()))
        s.commit()

    worker_module.Worker(settings).process_vod(vod_id)

    assert get(settings, Vod, vod_id) is None and sent == []
    with Session(get_engine(settings)) as s:
        row = s.exec(select(ChannelVideo)).one()
    assert (row.status, row.reason, row.vod_id, row.backlog_at) == ("skipped", "Members only", None, None)


def test_a_members_only_video_added_by_hand_fails_but_nothing_is_sent(settings, fake_pipeline, monkeypatch):
    sent = []
    monkeypatch.setattr(worker_module.notify, "send", lambda settings_, note: sent.append(note))
    monkeypatch.setattr(worker_module.download, "fetch", members_only)
    vod_id = add_vod(settings, url="https://www.youtube.com/watch?v=1isrqE6jWmo")
    worker_module.Worker(settings).process_vod(vod_id)
    vod = get(settings, Vod, vod_id)
    assert vod.status == "failed" and vod.error.startswith("Members only:") and sent == []


def test_youtubes_members_only_refusals_are_told_apart_from_other_failures():
    from yt_dlp.utils import DownloadError

    assert worker_module.download.members_only(DownloadError(MEMBERS_ERROR))
    assert worker_module.download.members_only(DownloadError("This video is available to this channel's members on level: Fan"))
    assert not worker_module.download.members_only(DownloadError("ERROR: [youtube] abc: Video unavailable"))


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

    def landscape(source, clip, out: Path, settings, on_progress=None, overlay=None):
        out.write_bytes(b"video")
        rendered.append(out.name)

    def short(source, clip, words, out: Path, settings, on_progress=None, **layout):
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


def test_shorts_go_on_the_shows_background_where_the_show_puts_the_video(settings, fake_pipeline, fake_render, monkeypatch):
    show_id = add_show(settings, name="Kino", match="twitch.tv/videos", short_video_top=0.32, short_video_width=0.9)
    layouts = []
    monkeypatch.setattr(worker_module.render, "render_short",
                        lambda *args, on_progress=None, **layout: (args[3].write_bytes(b"short"), layouts.append(layout)))
    w = worker_module.Worker(settings)
    w.process_vod(add_vod(settings))
    approve(settings, "hl01")
    w.render_next_clip()
    assert layouts == [{"crop": False, "background": None, "video_top": 0.32, "video_width": 0.9, "split": None}]  # no image uploaded yet

    background = settings.short_background(show_id)
    background.parent.mkdir(parents=True)
    background.write_bytes(b"png")
    approve(settings, "hl01")
    w.render_next_clip()
    assert layouts[-1]["background"] == background

    with Session(get_engine(settings)) as s:  # cropped to fill instead, which leaves the image out
        show = s.get(Show, show_id)
        show.short_layout = "crop"
        s.add(show)
        s.commit()
    approve(settings, "hl01")
    w.render_next_clip()
    assert (layouts[-1]["crop"], layouts[-1]["background"]) == (True, None)

    with Session(get_engine(settings)) as s:  # the webcam on top instead, which leaves the image out too
        show = s.get(Show, show_id)
        show.short_layout, show.short_webcams = "split", "[[0.75, 0, 0.25, 0.25], [0, 0, 0.2, 0.2]]"
        show.short_game = "[0, 0.1, 0.7, 0.9]"
        s.add(show)
        s.commit()
    approve(settings, "hl01")
    w.render_next_clip()
    assert (layouts[-1]["crop"], layouts[-1]["background"]) == (False, None)
    assert layouts[-1]["split"] == ([(0.75, 0, 0.25, 0.25), (0, 0, 0.2, 0.2)], (0, 0.1, 0.7, 0.9))


def test_shorts_are_cropped_as_the_show_says_or_else_as_the_server_does():
    show = Show(name="Kino")
    assert not show.short_crop("blur", background=False)
    assert show.short_crop("crop", background=False)
    assert not show.short_crop("crop", background=True)  # the show's background image goes ahead of the server's crop
    show.short_layout = "crop"
    assert show.short_crop("blur", background=True)
    show.short_layout = "blur"
    assert not show.short_crop("crop", background=False)


def test_shorts_are_split_once_the_webcam_is_marked():
    show = Show(name="Night Owls", short_layout="split")
    assert show.short_split() is None  # not marked yet
    show.short_webcams = "[[0.75, 0, 0.25, 0.25]]"
    assert show.short_split() == ([(0.75, 0, 0.25, 0.25)], None)
    show.short_webcams, show.short_game = '[[0.75, 0, 0.25, 0.25], "not a box", [0, 0, 0.2, 0.2]]', "not a box either"
    assert show.short_split() == ([(0.75, 0, 0.25, 0.25), (0, 0, 0.2, 0.2)], None)
    show.short_layout = "crop"
    assert show.short_split() is None and show.short_crop("blur", background=False)


def test_the_shows_overlay_goes_on_clips_and_on_full_episodes_only_when_switched_on(settings, fake_pipeline, monkeypatch):
    show_id = add_show(settings, name="Kino", match="x.com/a", mode="both",
                       overlay_box="[1440, 40, 440, 140]", overlay_screen="")
    image = settings.show_overlay(show_id)
    image.parent.mkdir(parents=True)
    image.write_bytes(b"png")
    rendered = {}

    def landscape(source, clip, out, settings_, on_progress=None, overlay=None):
        out.write_bytes(b"video")
        rendered[clip.id] = overlay

    monkeypatch.setattr(worker_module.render, "render_landscape", landscape)
    w = worker_module.Worker(settings)
    w.process_vod(add_vod(settings, url="https://x.com/a/status/1"))
    full = approve(settings, "full")
    approve(settings, "ch01")
    while w.render_next_clip():
        pass
    assert set(rendered) == {"ch01"} and rendered["ch01"].box == (1440, 40, 440, 140)
    assert Path(get(settings, ClipRecord, full).video_path).name == "source.mp4"  # posted as it is

    with Session(get_engine(settings)) as s:
        show = s.get(Show, show_id)
        show.overlay_episodes = True
        s.add(show)
        s.commit()
    approve(settings, "full")
    w.render_next_clip()
    assert rendered["full"].box == (1440, 40, 440, 140)
    assert Path(get(settings, ClipRecord, full).video_path).parent.name == "clips"  # re-encoded with it


def test_whole_videos_are_not_re_encoded(settings, fake_pipeline, fake_render):
    w = worker_module.Worker(settings)
    w.process_vod(add_vod(settings, url="https://x.com/a/status/1", mode="whole"))
    clip_id = approve(settings, "full")
    w.render_next_clip()

    clip = get(settings, ClipRecord, clip_id)
    assert clip.status == "rendered" and Path(clip.video_path).name == "source.mp4"
    assert fake_render == []


def test_the_shows_intro_is_joined_on_as_the_show_has_it_for_each_kind(settings, fake_pipeline, fake_render, monkeypatch):
    show_id = add_show(settings, name="Night Owls", match="twitch.tv/videos", mode="both", intro_seconds=4.5)
    intro_file = settings.show_intro(show_id)
    intro_file.parent.mkdir(parents=True)
    intro_file.write_bytes(b"intro")
    joined = []

    def prepend(intro_path, video, out, s, max_length=None, on_progress=None):
        if video.name == "source.mp4" and "refuse" in joined:
            raise worker_module.intro.IntroError("The video is HEVC: an intro can only be joined on without re-encoding H.264.")
        joined.append((video.name, out.name, max_length))
        out.write_bytes(b"with the intro")
        return 4.5

    monkeypatch.setattr(worker_module.intro, "prepend", prepend)
    sent = collect_notes(monkeypatch)
    w = worker_module.Worker(settings)
    w.process_vod(add_vod(settings))
    clip_id = approve(settings, "hl01")
    w.render_next_clip()
    assert joined == [("hl01-vertical.mp4", "hl01-vertical.mp4", None)]  # the render itself; not its Short, by default
    clip = get(settings, ClipRecord, clip_id)
    assert (Path(clip.video_path).name, clip.intro_s) == ("hl01-vertical.mp4", 4.5)

    with Session(get_engine(settings)) as s:
        show = s.get(Show, show_id)
        show.intro_clips, show.intro_shorts = False, True
        s.add(show)
        s.commit()
    approve(settings, "hl01")
    w.render_next_clip()
    assert joined[1:] == [("hl01-vertical-short.mp4", "hl01-vertical-short.mp4", 180)]  # kept within a Short's 3 minutes
    assert get(settings, ClipRecord, clip_id).intro_s == 0

    full_id = approve(settings, "full")
    w.render_next_clip()
    episode = get(settings, ClipRecord, full_id)
    assert joined[-1][0] == "source.mp4" and Path(episode.video_path).parent.name == "clips"  # a copy, with the intro
    assert episode.intro_s == 4.5 and fake_render == ["hl01-vertical.mp4", "hl01-vertical-short.mp4"] * 2  # not re-encoded

    joined.append("refuse")
    with Session(get_engine(settings)) as s:
        full = s.get(ClipRecord, full_id)
        full.status = "approved"
        s.add(full)
        s.commit()
    w.render_next_clip()
    episode = get(settings, ClipRecord, full_id)
    assert (Path(episode.video_path).name, episode.intro_s, episode.status) == ("source.mp4", 0, "rendered")  # goes up without
    assert not list((Path(episode.video_path).parent / "clips").glob("full-*"))  # the earlier copy with the intro is gone
    assert sent[-1].title.startswith("No intro on:") and "HEVC" in sent[-1].message


def test_a_clip_edited_during_its_render_is_rendered_again(settings, fake_pipeline, monkeypatch):
    w = worker_module.Worker(settings)
    w.process_vod(add_vod(settings))
    clip_id = approve(settings, "hl02")

    def landscape_while_user_edits(source, clip, out, settings_, on_progress=None, overlay=None):
        out.write_bytes(b"video")
        with Session(get_engine(settings)) as s:  # the app re-approves after an edit
            row = s.get(ClipRecord, clip_id)
            row.status = "approved"
            s.add(row)
            s.commit()

    monkeypatch.setattr(worker_module.render, "render_landscape", landscape_while_user_edits)
    w.render_next_clip()
    assert get(settings, ClipRecord, clip_id).status == "approved"


def test_an_edit_during_a_render_stops_it_at_the_next_progress_report(settings, fake_pipeline, monkeypatch):
    w = worker_module.Worker(settings)
    w.process_vod(add_vod(settings))
    clip_id = approve(settings, "hl02")

    def landscape(source, clip, out, settings_, on_progress=None, overlay=None):
        approve(settings, "hl02")  # the app re-approves after an edit
        on_progress(0.5)
        raise AssertionError("the render should have stopped")

    monkeypatch.setattr(worker_module.render, "render_landscape", landscape)
    w.render_next_clip()
    assert get(settings, ClipRecord, clip_id).status == "approved"


# --- Best picks, and kinds a show takes without review ---------------------------------------


def scored(chapter_scores=(0.6, 0.8)) -> Segmentation:
    seg = SEGMENTATION.model_copy(deep=True)
    for ch, score in zip(seg.chapters[1:], chapter_scores):
        ch.score = score
    return seg


def test_a_show_offers_its_best_chapters_and_highlights_and_keeps_the_rest_as_extras(settings, fake_pipeline, monkeypatch):
    monkeypatch.setattr(worker_module.segment, "run", lambda *a, **k: scored())
    add_show(settings, name="Kino", match="twitch.tv/videos", max_chapters=1, max_shorts=1)
    sent = collect_notes(monkeypatch)
    worker_module.Worker(settings).process_vod(add_vod(settings))

    assert {c.title: c.status for c in clips(settings)} == {
        "Whole show": "extra",  # scored 0.6
        "Second half": "proposed",  # scored 0.8
        "Vertical": "proposed",  # 0.8
        "Landscape only": "extra",  # 0.6
    }
    assert [(n.title, n.message) for n in sent] == [("Ready: Big stream", "2 to review · 2 extras")]


def test_without_a_show_every_clip_is_offered(settings, fake_pipeline):
    worker_module.Worker(settings).process_vod(add_vod(settings))
    assert {c.status for c in clips(settings)} == {"proposed"}


def test_kinds_a_show_takes_without_review_are_rendered_and_uploaded_by_themselves(settings, fake_pipeline, fake_render):
    with Session(get_engine(settings)) as s:
        dest = Destination(name="Kino Clips")
        s.add(dest)
        s.commit()
        dest_id = dest.id
    add_show(settings, name="Kino", match="twitch.tv/videos", mode="both", auto_episode="upload", auto_chapters="render",
             auto_visibility="unlisted", episode_destination_id=dest_id, clip_destination_id=dest_id)
    w = worker_module.Worker(settings)
    w.process_vod(add_vod(settings))

    statuses = {c.kind: c.status for c in clips(settings)}
    assert statuses == {"whole": "approved", "chapter": "approved", "highlight": "proposed"}
    while w.render_next_clip():
        pass
    with Session(get_engine(settings)) as s:
        jobs = s.exec(select(UploadJob)).all()
        assert [(s.get(ClipRecord, j.clip_id).kind, j.which, j.visibility) for j in jobs] == [("whole", "video", "unlisted")]


def test_a_highlight_uploaded_without_review_goes_up_as_its_short(settings, fake_pipeline, fake_render):
    with Session(get_engine(settings)) as s:
        dest = Destination(name="Kino Clips")
        s.add(dest)
        s.commit()
        dest_id = dest.id
    add_show(settings, name="Kino", match="twitch.tv/videos", auto_shorts="upload", clip_destination_id=dest_id)
    w = worker_module.Worker(settings)
    w.process_vod(add_vod(settings))
    while w.render_next_clip():
        pass
    with Session(get_engine(settings)) as s:
        jobs = s.exec(select(UploadJob)).all()
        assert sorted((s.get(ClipRecord, j.clip_id).title, j.which) for j in jobs) == [
            ("Landscape only", "video"), ("Vertical", "short"),
        ]


def test_offered_clips_claude_scores_high_enough_upload_without_review(settings, fake_pipeline, fake_render, monkeypatch):
    monkeypatch.setattr(worker_module.segment, "run", lambda *a, **k: scored())
    with Session(get_engine(settings)) as s:
        dest = Destination(name="Kino Clips")
        s.add(dest)
        s.commit()
        dest_id = dest.id
    add_show(settings, name="Kino", match="twitch.tv/videos", max_chapters=2, max_shorts=1, auto_upload_score=0.75,
             clip_destination_id=dest_id)
    w = worker_module.Worker(settings)
    w.process_vod(add_vod(settings))

    assert {c.title: c.status for c in clips(settings)} == {
        "Whole show": "proposed",  # 0.6
        "Second half": "approved",  # 0.8
        "Vertical": "approved",  # 0.8
        "Landscape only": "extra",  # 0.6, and only one Short is offered
    }
    while w.render_next_clip():
        pass
    with Session(get_engine(settings)) as s:
        jobs = s.exec(select(UploadJob)).all()
        assert sorted((s.get(ClipRecord, j.clip_id).title, j.which) for j in jobs) == [
            ("Second half", "video"), ("Vertical", "short"),
        ]


# --- Pausing ----------------------------------------------------------------------------------


def set_paused(settings, vod_id=None, value=True, part=None):
    with Session(get_engine(settings)) as s:
        if part:
            set_app_setting(s, f"paused_{part}", "1" if value else "0")
        else:
            row = s.get(Vod, vod_id)
            row.paused = value
            s.add(row)
        s.commit()


def test_pausing_a_video_stops_its_download_and_resuming_carries_on(settings, fake_pipeline, monkeypatch):
    vod_id = add_vod(settings)
    finish = worker_module.download.fetch  # the fake from fake_pipeline

    def paused_midway(url, s, on_progress=None, limit_mbps=None):
        set_paused(settings, vod_id)  # tapped Pause in the app
        on_progress(0.3)
        raise AssertionError("the download should have stopped")

    monkeypatch.setattr(worker_module.download, "fetch", paused_midway)
    w = worker_module.Worker(settings)
    assert w.process_next_vod() is True
    vod = get(settings, Vod, vod_id)
    assert (vod.status, vod.paused, vod.workspace) == ("queued", True, None)
    assert w.process_next_vod() is False  # held

    set_paused(settings, vod_id, False)
    monkeypatch.setattr(worker_module.download, "fetch", finish)
    assert w.process_next_vod() is True
    assert get(settings, Vod, vod_id).status == "ready"


def test_pausing_processing_lets_downloads_carry_on_and_pausing_downloads_lets_processing_carry_on(settings, fake_pipeline):
    w = worker_module.Worker(settings)
    set_paused(settings, part="processing")
    vod_id = add_vod(settings)
    assert w.process_next_vod() is True  # downloads it, then stops
    vod = get(settings, Vod, vod_id)
    assert (vod.status, vod.workspace) == ("queued", "vod-1")
    assert w.process_next_vod() is False

    set_paused(settings, part="processing", value=False)
    set_paused(settings, part="downloads")
    add_vod(settings, url="https://www.twitch.tv/videos/2")  # not downloaded: waits
    assert w.process_next_vod() is True
    assert get(settings, Vod, vod_id).status == "ready"
    assert w.process_next_vod() is False


def test_pausing_renders_stops_the_running_one_and_holds_the_rest(settings, fake_pipeline, monkeypatch):
    w = worker_module.Worker(settings)
    w.process_vod(add_vod(settings))
    clip_id = approve(settings, "hl02")

    def landscape(source, clip, out, settings_, on_progress=None, overlay=None):
        out.with_name(out.stem + ".tmp.mp4").write_bytes(b"half")
        set_paused(settings, part="renders")
        on_progress(0.5)
        raise AssertionError("the render should have stopped")

    monkeypatch.setattr(worker_module.render, "render_landscape", landscape)
    assert w.render_next_clip() is True
    assert get(settings, ClipRecord, clip_id).status == "approved"
    assert not list((settings.vods_dir / "vod-1" / "clips").glob("*.tmp.*"))
    assert w.render_next_clip() is False


def test_a_paused_clip_waits_while_others_render(settings, fake_pipeline, fake_render):
    w = worker_module.Worker(settings)
    w.process_vod(add_vod(settings))
    held, other = approve(settings, "hl01"), approve(settings, "hl02")
    with Session(get_engine(settings)) as s:
        row = s.get(ClipRecord, held)
        row.paused = True
        s.add(row)
        s.commit()
    assert w.render_next_clip() is True
    assert w.render_next_clip() is False
    assert (get(settings, ClipRecord, held).status, get(settings, ClipRecord, other).status) == ("approved", "rendered")


@pytest.mark.parametrize("with_transcription", [False, True])
def test_the_preview_is_made_alongside_claudes_step_or_also_transcription(settings, fake_pipeline, monkeypatch,
                                                                          with_transcription):
    settings.preview_with_transcription = with_transcription
    transcribing, transcribed, picking = threading.Event(), threading.Event(), threading.Event()
    seen = {}

    def proxy(ws, settings_, on_progress=None):
        seen["started_after_transcription"] = transcribed.is_set()
        assert (transcribing if with_transcription else picking).wait(5), "the preview should run alongside"

    def transcribe_run(*args, **kwargs):
        transcribing.set()
        time.sleep(0.2)  # a preview started now would overlap
        transcribed.set()
        return TRANSCRIPT

    def segment_run(*args, **kwargs):
        picking.set()
        return SEGMENTATION

    monkeypatch.setattr(worker_module.render, "render_proxy", proxy)
    monkeypatch.setattr(worker_module.transcribe, "run", transcribe_run)
    monkeypatch.setattr(worker_module.segment, "run", segment_run)
    vod_id = add_vod(settings)
    worker_module.Worker(settings).process_vod(vod_id)
    assert get(settings, Vod, vod_id).status == "ready"
    assert seen["started_after_transcription"] is not with_transcription


def test_clips_are_ready_before_the_preview_is(settings, fake_pipeline, monkeypatch):
    sent = collect_notes(monkeypatch)
    seen = {}

    def proxy(ws, settings_, on_progress=None):
        deadline = time.monotonic() + 5
        while not clips(settings) and time.monotonic() < deadline:  # the slow preview finishes last
            time.sleep(0.05)
        seen["clips_while_previewing"] = len(clips(settings))

    monkeypatch.setattr(worker_module.render, "render_proxy", proxy)
    vod_id = add_vod(settings)
    worker_module.Worker(settings).process_vod(vod_id)

    assert seen["clips_while_previewing"] == 4
    assert get(settings, Vod, vod_id).status == "ready"
    [note] = sent
    assert note.message == "4 to review\nThe preview to watch them in the app is still being made."


@pytest.mark.parametrize("hour, limit", [(4, 50), (5, 0), (10.5, 0), (11, 50), (23, 50)])
def test_downloads_go_at_full_speed_in_the_hours_set(settings, hour, limit):
    with Session(get_engine(settings)) as s:
        set_app_setting(s, "full_speed_from", "5")
        set_app_setting(s, "full_speed_until", "11")
        s.commit()
    tz = ZoneInfo(settings.timezone)
    now = datetime(2026, 10, 26, int(hour), int(hour % 1 * 60), tzinfo=tz)
    assert worker_module.Worker(settings).download_limit(now) == limit


def test_full_speed_hours_can_run_past_midnight():
    assert [worker_module.in_hours(h, 22, 6) for h in (21, 22, 2, 6)] == [False, True, True, False]
    assert not any(worker_module.in_hours(h, 3, 3) for h in (0, 3, 12))  # the same hour twice is off


def test_a_failed_preview_fails_the_video_once_the_rest_is_done(settings, fake_pipeline, monkeypatch):
    def proxy(ws, settings_, on_progress=None):
        raise worker_module.render.ffmpeg.FFmpegError("ffmpeg exited with 1")

    monkeypatch.setattr(worker_module.render, "render_proxy", proxy)
    vod_id = add_vod(settings)
    worker_module.Worker(settings).process_vod(vod_id)
    vod = get(settings, Vod, vod_id)
    assert (vod.status, vod.error) == ("failed", "FFmpegError: ffmpeg exited with 1")
    assert fake_pipeline["segment"] == 1  # Claude's picks are saved, so a retry only redoes the preview


def test_pausing_stops_the_preview(settings, fake_pipeline, monkeypatch):
    vod_id = add_vod(settings, url="https://x.com/a/status/1", mode="whole")

    def proxy(ws, settings_, on_progress=None):
        set_paused(settings, vod_id)
        on_progress(0.4)
        raise AssertionError("the preview should have stopped")

    monkeypatch.setattr(worker_module.render, "render_proxy", proxy)
    worker_module.Worker(settings).process_vod(vod_id)
    vod = get(settings, Vod, vod_id)
    assert (vod.status, vod.paused) == ("queued", True)


def collect_notes(monkeypatch) -> list:
    sent = []
    monkeypatch.setattr(worker_module.notify, "send", lambda settings, note: sent.append(note))
    return sent


def test_failures_are_notified(settings, fake_pipeline, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    sent = collect_notes(monkeypatch)
    monkeypatch.setattr(worker_module.transcribe, "run", broken)
    vod_id = add_vod(settings)
    worker_module.Worker(settings).process_vod(vod_id)
    [note] = sent
    assert (note.title, note.message, note.link, note.tone) == ("Failed: Big stream", "RuntimeError: disk full", f"videos/{vod_id}", "bad")


def test_recover_requeues_interrupted_work(settings, fake_pipeline):
    vod_id = add_vod(settings)
    with Session(get_engine(settings)) as s:
        vod = s.get(Vod, vod_id)
        vod.status = "transcribing"
        s.add(vod)
        s.commit()
    worker_module.Worker(settings).recover()
    assert get(settings, Vod, vod_id).status == "queued"
