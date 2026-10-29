"""The app's editor: parts cut out of clips, clips made from any part of a stream, and what the timeline shows."""

import gzip
import json
import shutil
import wave
from pathlib import Path

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
    assert args[:6] == ["-ss", "10.000", "-t", "10.000", "-i", "/src.mp4"][:6]
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


def test_the_waveform_is_the_loudest_sample_of_each_part(tmp_path, monkeypatch):
    ws = Workspace.open(tmp_path, "vod")
    sr = audio.SAMPLE_RATE
    samples = np.zeros(sr * 4, dtype=np.int16)
    samples[sr : 2 * sr] = 16384  # half volume in the second second
    samples[3 * sr + 10] = -32768  # one full-volume spike in the fourth
    write_wav(ws.audio_wav, samples)
    assert audio.peaks(ws, 0, 4, 4) == [0.0, 0.5, 0.0, 1.0]
    assert audio.peaks(ws, 3, 6, 3) == [1.0, 0.0, 0.0]  # past the end it's quiet
    monkeypatch.setattr(audio, "FULL_READ_SAMPLES", 100)  # sampled, as for a long stretch
    assert audio.peaks(ws, 0, 4, 4)[:2] == [0.0, 0.5]


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
    assert client.get(f"/api/vods/{vod_id}/peaks", params={"start": 0, "end": 2, "count": 2}).json() == {
        "start": 0, "end": 2, "peaks": [0.25, 0.25]}
    assert client.get(f"/api/vods/{vod_id}/peaks", params={"start": 2, "end": 1}).status_code == 422


@needs_ffmpeg
def test_stills_for_the_timeline(client, settings):
    vod_id, ws = add_vod(settings)
    assert client.get(f"/media/vods/{vod_id}/frame.jpg", params={"t": 1}).status_code == 404  # no preview yet
    ffmpeg.run(["-f", "lavfi", "-i", "testsrc=size=320x180:rate=30", "-t", "5", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                str(ws.proxy_mp4)])
    frame = client.get(f"/media/vods/{vod_id}/frame.jpg", params={"t": 2.04})
    assert frame.status_code == 200 and frame.headers["content-type"] == "image/jpeg"
    assert (ws.frames_dir / "20.jpg").exists() and not list(ws.frames_dir.glob("*.tmp.*"))
    assert client.get(f"/media/vods/{vod_id}/frame.jpg", params={"t": 900}).status_code in (200, 404)  # never a 500
    ws.delete_media()
    assert not ws.frames_dir.exists()


def test_clips_made_in_the_app_stay_out_of_the_score_comparison():
    made = ClipRecord(vod_id=1, key="my01", kind="chapter", start=0, end=10, title="t", score=0, manual=True)
    picked = ClipRecord(vod_id=1, key="ch01", kind="chapter", start=0, end=10, title="t", score=0.1)
    bands = stats.score_bands([(UploadJob(clip_id=1, views=10), made), (UploadJob(clip_id=2, views=20), picked)])
    assert sum(b.count for b in bands) == 1
