"""The app's editor: parts cut out of clips, clips made from any part of a stream, and what the timeline shows."""

import asyncio
import gzip
import json
import os
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from clipper import cuts, ffmpeg, stats
from clipper.config import Settings
from clipper.db import ClipRecord, Destination, Show, UploadJob, Vod, get_engine
from clipper.models import Clip, Word
from clipper.pipeline import audio, render
from clipper.web import app as web_app
from clipper.workspace import Workspace, write_json

from conftest import line, transcript

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="needs ffmpeg")


# --- Cuts --------------------------------------------------------------------------


def test_cuts_are_kept_inside_the_clip_sorted_and_merged():
    # The last two are slivers once inside the clip, too short to be meant.
    assert cuts.tidy(10, 70, [(40, 45), (20, 30), (28, 33), (69.99, 80), (50, 50.01)]) == (10, 70, [(20, 33), (40, 45)])
    # A cut that reaches an end trims the clip instead.
    assert cuts.tidy(10, 70, [(5, 15), (60, 70)]) == (15, 60, [])
    assert cuts.tidy(10, 70, [(30, 20)]) == (10, 70, [(20, 30)])  # given backwards


def test_the_parts_that_stay_and_how_long_the_clip_comes_out():
    assert cuts.kept(10, 70, [(20, 30), (40, 45)]) == [(10, 20), (30, 40), (45, 70)]
    assert cuts.kept_length(10, 70, [(20, 30), (40, 45)]) == 45
    assert cuts.kept(10, 70, []) == [(10, 70)]
    assert cuts.parse(cuts.dump([(20.1234, 30)])) == [(20.123, 30.0)]
    assert cuts.parse("") == [] and cuts.parse("not json") == [] and cuts.dump([]) == ""


def test_a_clip_with_cuts_is_shorter():
    clip = Clip(id="my01", kind="highlight", start=10, end=70, title="t", description="", score=0, llm_start="", llm_end="",
                cuts=[(20, 30)])
    assert clip.duration == 50


# --- Rendering --------------------------------------------------------------------------

CUT_CLIP = Clip(id="my01", kind="highlight", start=10, end=20, title="t", description="", score=0, llm_start="",
                llm_end="", cuts=[(12, 13), (15, 17)])


def test_captions_close_up_the_cuts():
    words = [Word(start=11, end=11.5, text="one"), Word(start=12.2, end=12.6, text="gone"),
             Word(start=12.8, end=13.4, text="two"), Word(start=17.5, end=18, text="three")]
    timed = [(round(w.start, 2), round(w.end, 2), w.text) for w in render.kept_words(words, CUT_CLIP)]
    # Kept: 10-12 (0-2 out), 13-15 (2-4 out), 17-20 (4-7 out). "two" ran into the first cut and is cut short.
    assert timed == [(1.0, 1.5, "one"), (2.0, 2.4, "two"), (4.5, 5.0, "three")]


def test_the_cut_graph_joins_the_parts_that_stay():
    graph = render._cut_graph(CUT_CLIP, audio=True)
    assert "[0:v]split=3[vp0][vp1][vp2]" in graph
    assert "[vp1]trim=start=3.000:end=5.000,setpts=PTS-STARTPTS[v1]" in graph
    assert "[ap2]atrim=start=7.000:end=10.000" in graph
    assert "[v0][a0][v1][a1][v2][a2]concat=n=3:v=1:a=1[vc][ac]" in graph
    assert graph.endswith("[ac]afade=t=in:d=0.15,afade=t=out:st=6.750:d=0.25[a]")  # 7 s out
    silent = render._cut_graph(CUT_CLIP, audio=False)
    assert "concat=n=3:v=1:a=0[vc]" in silent and "[0:a]" not in silent


def test_clips_without_cuts_render_as_before(monkeypatch, tmp_path):
    runs = []
    monkeypatch.setattr(ffmpeg, "run", lambda args, **_: runs.append(args) or Path(args[-1]).write_bytes(b"v"))
    plain = CUT_CLIP.model_copy(update={"cuts": []})
    render.render_landscape(Path("/src.mp4"), plain, tmp_path / "out.mp4", Settings(hwaccel="none", video_encoder="libx264"))
    args = runs[0]
    assert args[:6] == ["-ss", "10.000", "-t", "10.000", "-i", str(Path("/src.mp4"))]  # \src.mp4 on Windows
    assert "-filter_complex" not in args and args[args.index("-af") + 1].startswith("afade=t=in:d=0.15")


def test_clips_with_cuts_take_the_whole_stretch_and_filter_the_cuts_out(monkeypatch, tmp_path):
    runs = []
    monkeypatch.setattr(ffmpeg, "run", lambda args, **_: runs.append(args) or Path(args[-1]).write_bytes(b"v"))
    monkeypatch.setattr(ffmpeg, "has_audio", lambda path: True)
    render.render_landscape(Path("/src.mp4"), CUT_CLIP, tmp_path / "out.mp4", Settings(hwaccel="none", video_encoder="libx264"))
    args = runs[0]
    assert args[args.index("-t") + 1] == "10.000"  # start to end; the filters take the cuts out
    assert args[args.index("-map") + 1] == "[vc]" and "[a]" in args and "-af" not in args


@needs_ffmpeg
def test_a_clip_with_cuts_comes_out_as_long_as_the_parts_that_stay(tmp_path):
    source = tmp_path / "source.mp4"
    ffmpeg.run(["-f", "lavfi", "-i", "testsrc=size=320x180:rate=30", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-t", "12", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source)])
    settings = Settings(hwaccel="none", video_encoder="libx264")
    out = tmp_path / "clip.mp4"
    render.render_landscape(source, CUT_CLIP.model_copy(update={"start": 1, "end": 11, "cuts": [(3, 4), (6, 8)]}), out, settings)
    streams = {s["codec_type"]: float(s["duration"]) for s in ffmpeg.probe(out)["streams"]}
    assert streams["video"] == pytest.approx(7, abs=0.1) and streams["audio"] == pytest.approx(7, abs=0.1)

    short = tmp_path / "clip-short.mp4"
    words = [Word(start=1.5 + i, end=2 + i, text=f"word{i}") for i in range(9)]
    render.render_short(source, CUT_CLIP.model_copy(update={"start": 1, "end": 11, "cuts": [(3, 4), (6, 8)]}), words, short, settings)
    probe = ffmpeg.probe(short)
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1080, 1920)
    assert float(probe["format"]["duration"]) == pytest.approx(7, abs=0.1)


def first_pixel(video: Path, x: int, y: int) -> tuple[int, ...]:
    """The colour of one pixel of a video's first frame."""
    rgb = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-frames:v", "1", "-vf", f"format=rgb24,crop=1:1:{x}:{y}",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return tuple(rgb[:3])


@needs_ffmpeg
@pytest.mark.parametrize("size, sar", [
    ("640x360", "1"),
    ("720x480", "32/27"),  # a DVD's widescreen picture, stored squeezed: cropped as it's shown, not as it's stored
    ("360x640", "1"),  # already narrower than a Short: all of its width stays
])
def test_a_cropped_short_is_filled_with_the_middle_of_the_picture(tmp_path, size, sar):
    source = tmp_path / "source.mp4"
    # Red down the sides, bright green in the middle third and a bit: a little wider than the middle 9:16 of 16:9.
    ffmpeg.run(["-f", "lavfi", "-i", f"color=red:s={size}:d=1:r=10", "-vf",
                f"drawbox=x=iw*0.33:y=0:w=iw*0.34:h=ih:color=lime:t=fill,setsar={sar}",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)])
    clip = CUT_CLIP.model_copy(update={"start": 0, "end": 1, "cuts": []})
    words = [Word(start=0.1, end=0.5, text="hello")]
    settings = Settings(hwaccel="none", video_encoder="libx264")

    cropped = tmp_path / "cropped.mp4"
    render.render_short(source, clip, words, cropped, settings, crop=True)
    video = next(s for s in ffmpeg.probe(cropped)["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1080, 1920)
    r, g, b = first_pixel(cropped, 20, 20)
    if size == "360x640":
        assert r > 200 and g < 60  # the red side is still there
    else:
        assert g > 200 and r < 60  # only the green middle

    on_background = tmp_path / "on-background.mp4"
    render.render_short(source, clip, words, on_background, settings, crop=False)
    r, g, b = first_pixel(on_background, 20, 20)
    assert not (g > 200 and r < 60)  # the dimmed blur behind the whole picture


def test_a_split_shorts_webcams_are_as_tall_as_their_shape_within_limits():
    cam = (0.75, 0, 0.25, 0.25)  # 16:9, like the stream
    assert render.webcam_grid([cam]) == (1, 1080, 608)
    assert render.webcam_grid([(0.1, 0.1, 0.2, 0.3)]) == (1, 1080, 912)  # taller than that
    assert render.webcam_grid([(0.1, 0.1, 0.1, 0.5)]) == (1, 1080, 960)  # at most half the Short
    assert render.webcam_grid([(0, 0, 1, 0.2)]) == (1, 1080, 480)  # at least a quarter
    assert render.webcam_grid([cam] * 2) == (2, 540, 480)  # side by side, cropped to fill a quarter of the Short
    assert render.webcam_grid([cam] * 3) == (3, 360, 480)
    assert render.webcam_grid([cam] * 4) == (2, 540, 304)  # two by two
    assert [render.webcams_height([cam] * n) for n in (1, 2, 3, 4)] == [608, 480, 480, 608]

    one = render.split_graph("[0:v]", [cam], None)
    assert "crop=trunc(iw*0.2500/2)*2:trunc(ih*0.2500/2)*2:trunc(iw*0.7500):trunc(ih*0.0000)" in one
    assert "scale=1080:608[c0]" in one and "scale=1080:1312[bottom]" in one and one.endswith("[c0][bottom]vstack")
    assert "[game]crop='min" in one  # all of the picture, without a part marked for the game
    assert "[game]crop=trunc(iw*0.5000" in render.split_graph("[0:v]", [cam], (0, 0, 0.5, 1))
    two = render.split_graph("[0:v]", [cam, (0, 0, 0.25, 0.25)], None)
    assert "split=3[cam0][cam1][game]" in two and "[c0][c1]hstack=inputs=2[row0]" in two and two.endswith("[row0][bottom]vstack")
    four = render.split_graph("[0:v]", [cam] * 4, None)
    assert "[c2][c3]hstack=inputs=2[row1]" in four and "[row0][row1]vstack=inputs=2[top]" in four and four.endswith("[top][bottom]vstack")


@needs_ffmpeg
def test_a_split_short_has_the_webcam_on_top_and_the_game_under_it(tmp_path):
    source = tmp_path / "source.mp4"
    # Blue, with a green webcam in the top-right corner and the game in red down the middle.
    ffmpeg.run(["-f", "lavfi", "-i", "color=blue:s=640x360:d=1:r=10", "-vf",
                "drawbox=x=480:y=0:w=160:h=90:color=lime:t=fill,drawbox=x=iw*0.3:y=0:w=iw*0.4:h=ih:color=red:t=fill",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)])
    clip = CUT_CLIP.model_copy(update={"start": 0, "end": 1, "cuts": []})
    out = tmp_path / "split.mp4"
    render.render_short(source, clip, [Word(start=0.1, end=0.5, text="hello")], out,
                        Settings(hwaccel="none", video_encoder="libx264"), split=([(0.75, 0, 0.25, 0.25)], None))
    video = next(s for s in ffmpeg.probe(out)["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1080, 1920)
    r, g, b = first_pixel(out, 540, 300)
    assert g > 200 and r < 60  # the webcam fills the top
    r, g, b = first_pixel(out, 540, 1700)
    assert r > 200 and g < 60  # the middle of the game under it

    # A second webcam, yellow in the top-left corner, goes on the right of the first.
    two = tmp_path / "two.mp4"
    ffmpeg.run(["-i", str(source), "-vf", "drawbox=x=0:y=0:w=160:h=90:color=yellow:t=fill", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", str(tmp_path / "two-cams.mp4")])
    render.render_short(tmp_path / "two-cams.mp4", clip, [Word(start=0.1, end=0.5, text="hello")], two,
                        Settings(hwaccel="none", video_encoder="libx264"),
                        split=([(0.75, 0, 0.25, 0.25), (0, 0, 0.25, 0.25)], None))
    r, g, b = first_pixel(two, 270, 240)
    assert g > 200 and r < 60  # the green webcam on the left
    r, g, b = first_pixel(two, 810, 240)
    assert r > 200 and g > 200 and b < 60  # the yellow one on the right
    r, g, b = first_pixel(two, 540, 1700)
    assert r > 200 and g < 60  # the game under them


@needs_ffmpeg
def test_the_overlay_goes_on_after_the_parts_are_joined(tmp_path):
    from clipper.overlay import Overlay

    source = tmp_path / "source.mp4"
    ffmpeg.run(["-f", "lavfi", "-i", "testsrc=size=320x180:rate=30", "-t", "6", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)])
    logo = tmp_path / "overlay.png"
    ffmpeg.run(["-f", "lavfi", "-i", "color=c=red@0.5:size=1920x1080,format=rgba", "-frames:v", "1", str(logo)])
    overlay = Overlay(path=logo, box=(0, 0, 200, 100), screen=None)
    out = tmp_path / "clip.mp4"
    clip = CUT_CLIP.model_copy(update={"start": 0, "end": 6, "cuts": [(2, 3)]})
    render.render_landscape(source, clip, out, Settings(hwaccel="none", video_encoder="libx264"), overlay=overlay)
    video = next(s for s in ffmpeg.probe(out)["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1920, 1080)
    assert float(video["duration"]) == pytest.approx(5, abs=0.1)
    assert not any(s["codec_type"] == "audio" for s in ffmpeg.probe(out)["streams"])  # the source had no sound


# --- Waveform -------------------------------------------------------------------------


def write_wav(path: Path, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(audio.SAMPLE_RATE)
        w.writeframes(samples.astype("<i2").tobytes())


def test_the_waveform_is_the_loudest_sound_of_each_part(tmp_path):
    ws = Workspace.open(tmp_path, "vod")
    sr = audio.SAMPLE_RATE
    samples = np.zeros(sr * 4, dtype=np.int16)
    samples[sr : 2 * sr] = 16384  # half volume in the second second
    samples[3 * sr + 10] = -32768  # one full-volume spike in the fourth
    samples[2 * sr + 5] = 3  # a whisper: rounded up, so it isn't silence
    write_wav(ws.audio_wav, samples)
    # From the overview, a byte per fiftieth of a second.
    assert audio.peaks(ws, 0, 4, 4) == pytest.approx([0.0, 0.5, 0.004, 1.0], abs=0.003)
    assert audio.peaks(ws, 3, 6, 3) == [1.0, 0.0, 0.0]  # past the end it's quiet
    # Zoomed right in, from the samples themselves.
    assert audio.peaks(ws, 0.95, 1.05, 100)[:5] == [0.0] * 5 and audio.peaks(ws, 0.95, 1.05, 100)[-5:] == [0.5] * 5
    # 3.0005 s is sample 8 of the fourth second, so the spike (sample 10) is in the second of these 2-sample parts.
    assert audio.peaks(ws, 3.0005, 3.001, 4) == [0.0, 1.0, 0.0, 0.0]


def test_the_waveforms_overview_is_made_once_kept_small_and_made_again_for_a_new_soundtrack(tmp_path):
    ws = Workspace.open(tmp_path, "vod")
    write_wav(ws.audio_wav, np.full(audio.SAMPLE_RATE * 60, 8192, dtype=np.int16))
    audio.peaks(ws, 0, 60, 10)
    assert ws.peaks_u8.stat().st_size == 60 * audio.OVERVIEW_RATE  # a byte a reading
    made = ws.peaks_u8.stat().st_mtime_ns
    audio.peaks(ws, 0, 30, 10)
    assert ws.peaks_u8.stat().st_mtime_ns == made  # kept
    os.utime(ws.audio_wav, ns=(made + 10**9, made + 10**9))  # extracted again (timing-check --fix does)
    write_wav(ws.audio_wav, np.full(audio.SAMPLE_RATE * 60, 32767, dtype=np.int16))
    os.utime(ws.audio_wav, ns=(made + 10**9, made + 10**9))
    assert audio.peaks(ws, 0, 60, 2) == [1.0, 1.0]
    ws.delete_media()
    assert not ws.peaks_u8.exists()


# --- API -----------------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", web_dir=tmp_path / "web", timezone="America/New_York")


@pytest.fixture
def client(settings):
    return TestClient(web_app.create_app(settings))


def add_vod(settings, show: Show | None = None, **fields) -> tuple[int, Workspace]:
    ws = Workspace.open(settings.vods_dir, "twitchvod-1")
    write_json(ws.meta_json, {"source_path": str(ws.root / "source.mp4"), "release_timestamp": 1792800000})  # Oct 23, 2026
    ws.transcript_json.write_text(transcript(
        line(10, "First line of the stream."), line(20, "Second line here."), line(40, "Third line, later on."),
    ).model_dump_json(), encoding="utf-8")
    with Session(get_engine(settings)) as s:
        if show:
            s.add(show)
            s.flush()
        vod = Vod(url="https://www.twitch.tv/videos/1", title="Stream", duration=600, status="ready",
                  workspace="twitchvod-1", show_id=show.id if show else None, **fields)
        s.add(vod)
        s.commit()
        return vod.id, ws


def add_clip(settings, vod_id: int, **fields) -> int:
    with Session(get_engine(settings)) as s:
        clip = ClipRecord(vod_id=vod_id, **{"key": "hl01", "kind": "highlight", "start": 10, "end": 70, "title": "Moment", **fields})
        s.add(clip)
        s.commit()
        return clip.id


def test_cutting_parts_out_of_a_clip(client, settings):
    vod_id, _ = add_vod(settings)
    clip_id = add_clip(settings, vod_id)
    edited = client.patch(f"/api/clips/{clip_id}", json={"cuts": [[40, 45], [20, 30], [65, 75]]}).json()
    assert edited["cuts"] == [[20, 30], [40, 45]] and edited["end"] == 65  # the last cut trimmed the end
    assert client.get(f"/api/clips/{clip_id}").json()["cuts"] == [[20, 30], [40, 45]]
    # Other changes leave the cuts be.
    assert client.patch(f"/api/clips/{clip_id}", json={"title": "New"}).json()["cuts"] == [[20, 30], [40, 45]]

    # A Short's 3-minute limit goes by what's left once the cuts are out.
    long = add_clip(settings, vod_id, key="hl02", start=0, end=200)
    assert client.patch(f"/api/clips/{long}", json={"make_short": True}).status_code == 422
    assert client.patch(f"/api/clips/{long}", json={"make_short": True, "cuts": [[50, 80]]}).json()["make_short"]
    # Not everything can go.
    assert client.patch(f"/api/clips/{clip_id}", json={"cuts": [[10.5, 64.8]]}).status_code == 422
    # Full episodes go up as they are.
    whole = add_clip(settings, vod_id, key="full", kind="whole", start=0, end=600)
    assert client.patch(f"/api/clips/{whole}", json={"cuts": [[50, 60]]}).status_code == 422

    with Session(get_engine(settings)) as s:
        clip = s.get(ClipRecord, clip_id)
        clip.status = "rendered"
        s.add(clip)
        s.commit()
    assert client.patch(f"/api/clips/{clip_id}", json={"cuts": [[20, 30], [40, 45]]}).json()["status"] == "rendered"
    assert client.patch(f"/api/clips/{clip_id}", json={"cuts": []}).json()["status"] == "approved"  # renders again


def test_making_a_clip_from_any_part_of_the_stream(client, settings):
    with Session(get_engine(settings)) as s:
        dest = Destination(name="Clips")
        s.add(dest)
        s.commit()
        dest_id = dest.id
    show = Show(name="Kino", title_template="KINO {date} - {title}", clip_destination_id=dest_id, clip_playlist_id="PL1")
    vod_id, _ = add_vod(settings, show)

    info = client.get(f"/api/vods/{vod_id}/new-clip").json()
    assert info == {"title_pattern": "KINO Oct 23, 2026 - {title}", "title_room": 80, "destination_id": dest_id, "playlist_id": "PL1"}

    body = {"kind": "highlight", "start": 100, "end": 160, "cuts": [[120, 130]], "title": "  KINO Oct 23, 2026 -  The bit ",
            "make_short": True, "destination_id": dest_id, "playlist_id": "PL1"}
    made = client.post(f"/api/vods/{vod_id}/clips", json=body)
    assert made.status_code == 201
    clip = made.json()
    assert clip["key"] == "my01" and clip["manual"] and clip["status"] == "proposed" and clip["title"] == "KINO Oct 23, 2026 - The bit"
    assert clip["cuts"] == [[120, 130]] and clip["make_short"] and clip["vertical_ok"] and clip["playlist_id"] == "PL1"
    assert client.get("/api/summary").json()["to_review"] == 1

    second = client.post(f"/api/vods/{vod_id}/clips", json={**body, "kind": "chapter", "approve": True}).json()
    assert second["key"] == "my02" and second["status"] == "approved" and not second["make_short"]  # Shorts are for highlights

    assert client.post(f"/api/vods/{vod_id}/clips", json={**body, "title": "  "}).status_code == 422
    assert client.post(f"/api/vods/{vod_id}/clips", json={**body, "end": 700}).status_code == 422
    with Session(get_engine(settings)) as s:
        vod = s.get(Vod, vod_id)
        vod.status = "transcribing"
        s.add(vod)
        s.commit()
    assert client.post(f"/api/vods/{vod_id}/clips", json=body).status_code == 409


def test_the_streams_transcript_whole_or_in_part_with_words(client, settings):
    vod_id, _ = add_vod(settings)
    lines = client.get(f"/api/vods/{vod_id}/transcript").json()
    assert [l["text"] for l in lines] == ["First line of the stream.", "Second line here.", "Third line, later on."]
    assert "words" not in lines[0]
    part = client.get(f"/api/vods/{vod_id}/transcript", params={"start": 15, "end": 30, "words": True}).json()
    assert [l["text"] for l in part] == ["Second line here."]
    assert part[0]["words"] == [[20, 20.3, "Second"], [20.35, 20.65, "line"], [20.7, 21.0, "here."]]


def test_a_long_transcript_comes_compressed(client, settings):
    vod_id, ws = add_vod(settings)
    ws.transcript_json.write_text(transcript(*(line(i * 5, f"Line number {i} of a long stream.") for i in range(200))).model_dump_json())
    raw = client.get(f"/api/vods/{vod_id}/transcript", headers={"Accept-Encoding": "gzip"})
    assert raw.headers["content-encoding"] == "gzip" and len(raw.json()) == 200  # the client unpacks it
    assert len(gzip.compress(json.dumps(raw.json()).encode())) < len(json.dumps(raw.json()))


def test_the_waveform_from_the_api(client, settings):
    vod_id, ws = add_vod(settings)
    assert client.get(f"/api/vods/{vod_id}/peaks", params={"start": 0, "end": 2, "count": 2}).status_code == 404
    write_wav(ws.audio_wav, np.full(audio.SAMPLE_RATE * 2, 8192, dtype=np.int16))
    waveform = client.get(f"/api/vods/{vod_id}/peaks", params={"start": 0, "end": 2, "count": 2}).json()
    assert waveform["start"] == 0 and waveform["end"] == 2 and waveform["peaks"] == pytest.approx([0.25, 0.25], abs=0.003)
    assert client.get(f"/api/vods/{vod_id}/peaks", params={"start": 2, "end": 1}).status_code == 422


def make_preview(path: Path, seconds: int) -> None:
    """A preview as the worker makes them: 30 frames a second, a keyframe every 2 s."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run(["-f", "lavfi", "-i", "testsrc=size=320x180:rate=30", "-t", str(seconds), "-c:v", "libx264", "-g", "60",
                "-pix_fmt", "yuv420p", str(path)])


@needs_ffmpeg
def test_stills_every_so_often_in_one_pass(tmp_path):
    preview = tmp_path / "proxy.mp4"
    make_preview(preview, 25)
    assert render.render_stills(preview, tmp_path / "frames", 10) == 3
    assert sorted(p.name for p in (tmp_path / "frames").glob("*.jpg")) == ["0.jpg", "100.jpg", "200.jpg"]
    # A minute's worth, zoomed in: every keyframe, named by its moment in tenths of a second.
    assert render.render_stills(preview, tmp_path / "close", 2, start=10, length=10) == 5
    assert sorted(int(p.stem) for p in (tmp_path / "close").glob("*.jpg")) == [100, 120, 140, 160, 180]
    assert not list((tmp_path / "close").glob(".stills-*"))  # tidied up


@needs_ffmpeg
def test_stills_for_the_timeline(client, settings, monkeypatch):
    vod_id, ws = add_vod(settings)
    assert client.get(f"/media/vods/{vod_id}/frame.jpg", params={"t": 1}).status_code == 404  # no preview yet
    make_preview(ws.proxy_mp4, 25)
    # Zoomed in: the minute around it in one run, on the 2 s grid of keyframes.
    frame = client.get(f"/media/vods/{vod_id}/frame.jpg", params={"t": 2.9})
    assert frame.status_code == 200 and frame.headers["content-type"] == "image/jpeg"
    assert {p.name for p in ws.frames_dir.glob("*.jpg")} >= {"20.jpg", "40.jpg", "120.jpg", "220.jpg"}
    # Zoomed out, before the worker's pass: just the one.
    (ws.frames_dir / "100.jpg").unlink()
    assert client.get(f"/media/vods/{vod_id}/frame.jpg", params={"t": 10}).status_code == 200
    # Once the worker's pass is done, a zoomed-out still that isn't there isn't looked for.
    ws.stills_done.write_text("3\n")
    monkeypatch.setattr(render, "render_frame", lambda *a: pytest.fail("ran ffmpeg for a still the pass would have made"))
    assert client.get(f"/media/vods/{vod_id}/frame.jpg", params={"t": 900}).status_code == 404
    ws.delete_media()
    assert not ws.frames_dir.exists()


@needs_ffmpeg
def test_a_clips_picture_in_the_lists_is_made_once(client, settings, monkeypatch):
    vod_id, ws = add_vod(settings)
    make_preview(ws.proxy_mp4, 25)
    clip_id = add_clip(settings, vod_id, start=4, end=20)
    picture = client.get(f"/media/clips/{clip_id}/thumb.jpg")
    assert picture.status_code == 200 and picture.headers["content-type"] == "image/jpeg"
    late = add_clip(settings, vod_id, key="hl02", start=40, end=60)  # after the end of the preview
    assert client.get(f"/media/clips/{late}/thumb.jpg").status_code == 404
    monkeypatch.setattr(render, "render_thumbnail", lambda *a: pytest.fail("made the same picture again"))
    assert client.get(f"/media/clips/{clip_id}/thumb.jpg").status_code == 200


def test_a_list_of_new_clips_has_their_pictures_made_one_at_a_time(settings, monkeypatch):
    vod_id, _ = add_vod(settings)
    ids = [add_clip(settings, vod_id, key=f"hl{i:02}", start=60 * i, end=60 * i + 30) for i in range(1, 9)]
    running, most, lock = [0], [0], threading.Lock()

    def make(video, t, out):
        with lock:
            running[0] += 1
            most[0] = max(most[0], running[0])
        time.sleep(0.05)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\xff\xd8\xff\xd9")
        with lock:
            running[0] -= 1

    monkeypatch.setattr(render, "render_thumbnail", make)

    async def ask_for_all():  # as a page of clips does, all at once
        transport = httpx.ASGITransport(app=web_app.create_app(settings))
        async with httpx.AsyncClient(transport=transport, base_url="http://app") as client:
            return await asyncio.gather(*(client.get(f"/media/clips/{i}/thumb.jpg") for i in ids))

    assert [r.status_code for r in asyncio.run(ask_for_all())] == [200] * len(ids)
    assert most[0] == 1


@needs_ffmpeg
def test_the_worker_makes_the_editors_overviews_once(settings):
    from clipper.worker import Worker

    vod_id, ws = add_vod(settings)
    make_preview(ws.proxy_mp4, 25)
    write_wav(ws.audio_wav, np.full(audio.SAMPLE_RATE * 25, 4096, dtype=np.int16))
    worker = Worker(settings)
    assert worker.prepare_next_for_editor() is True
    assert ws.peaks_u8.exists() and ws.stills_done.read_text().strip() == "3"
    assert (ws.frames_dir / "200.jpg").exists()
    assert worker.prepare_next_for_editor() is False  # done
    assert Worker(settings).prepare_next_for_editor() is False  # and a new worker sees it's done


def test_clips_made_in_the_app_stay_out_of_the_score_comparison():
    made = ClipRecord(vod_id=1, key="my01", kind="chapter", start=0, end=10, title="t", score=0, manual=True)
    picked = ClipRecord(vod_id=1, key="ch01", kind="chapter", start=0, end=10, title="t", score=0.1)
    bands = stats.score_bands([(UploadJob(clip_id=1, views=10), made), (UploadJob(clip_id=2, views=20), picked)])
    assert sum(b.count for b in bands) == 1
