"""A show's intro joined onto the start of its videos without re-encoding them (pipeline/intro.py)."""

import shutil
import subprocess
from pathlib import Path

import pytest

from clipper import ffmpeg
from clipper.config import Settings
from clipper.pipeline import intro

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="needs ffmpeg")

STREAM = {
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "sample_aspect_ratio": "1:1",
         "r_frame_rate": "60/1", "avg_frame_rate": "60/1", "time_base": "1/90000"},
        {"index": 1, "codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2},
    ],
    "format": {"duration": "28800.0"},
}


@pytest.fixture
def fake_ffmpeg(tmp_path, monkeypatch):
    """ffprobe answering with `probe["video"]`, and ffmpeg writing its output and keeping the arguments."""
    state = {"video": STREAM, "intro_seconds": 4.5, "intro_sound": False, "runs": []}

    def run(args, cwd=None, on_progress=None, duration=None):
        state["runs"].append(args)
        Path(args[-1]).write_bytes(b"joined" if "concat" in args else b"intro")
        if "concat" in args:
            state["listing"] = Path(args[args.index("-i") + 1]).read_text()
        return ""

    monkeypatch.setattr(intro.ffmpeg, "probe", lambda path: state["video"])
    monkeypatch.setattr(intro.ffmpeg, "probe_duration", lambda path: state["intro_seconds"])
    monkeypatch.setattr(intro.ffmpeg, "has_audio", lambda path: state["intro_sound"])
    monkeypatch.setattr(intro.ffmpeg, "run", run)
    (tmp_path / "source.mp4").write_bytes(b"stream")
    (tmp_path / "intro.mp4").write_bytes(b"intro")
    return state


def test_the_intro_is_encoded_to_match_the_video_and_joined_on_as_it_is(tmp_path, fake_ffmpeg):
    out = tmp_path / "clips" / "full-with-intro.mp4"
    out.parent.mkdir()
    seconds = intro.prepend(tmp_path / "intro.mp4", tmp_path / "source.mp4", out, Settings(data_dir=tmp_path))
    assert seconds == 4.5 and out.read_bytes() == b"joined"
    assert sorted(p.name for p in out.parent.iterdir()) == ["full-with-intro.mp4"]  # the temporary files are gone

    match, join = fake_ffmpeg["runs"]
    graph = match[match.index("-filter_complex") + 1]
    assert "scale=1920:1080:force_original_aspect_ratio=decrease" in graph and "pad=1920:1080" in graph and "fps=60/1" in graph
    assert "anullsrc=r=48000:cl=stereo" in match and "[1:a]aresample=48000,apad[a]" in graph  # silence: the intro has none
    assert match[match.index("-map"):match.index("-map") + 4] == ["-map", "[v]", "-map", "[a]"]
    assert (match[match.index("-t") + 1], match[match.index("-c:v") + 1], match[match.index("-c:a") + 1]) == ("4.500", "h264_nvenc", "aac")
    assert match[match.index("-video_track_timescale") + 1] == "90000"

    assert join[:6] == ["-f", "concat", "-safe", "0", "-i", join[5]] and join[join.index("-c") + 1] == "copy"
    first, second = fake_ffmpeg["listing"].splitlines()
    assert first.endswith(".intro-matched.tmp.mp4'") and second == f"file '{(tmp_path / 'source.mp4').resolve()}'"


def test_the_intro_matches_an_odd_video_too(tmp_path, fake_ffmpeg):
    fake_ffmpeg["intro_sound"] = True
    fake_ffmpeg["video"] = {
        "streams": [  # a DVD's squeezed widescreen picture, with its sound first and in mono AC-3
            {"index": 0, "codec_type": "audio", "codec_name": "ac3", "sample_rate": "48000", "channels": 1},
            {"index": 1, "codec_type": "video", "codec_name": "h264", "width": 720, "height": 480, "sample_aspect_ratio": "32:27",
             "r_frame_rate": "0/0", "avg_frame_rate": "30000/1001", "time_base": "1/1000"},
        ],
        "format": {"duration": "1300"},
    }
    intro.prepend(tmp_path / "intro.mp4", tmp_path / "source.mp4", tmp_path / "out.mp4", Settings(data_dir=tmp_path, video_encoder="libx264"))
    match = fake_ffmpeg["runs"][0]
    graph = match[match.index("-filter_complex") + 1]
    assert "scale=854:480:force_original_aspect_ratio=decrease" in graph and "scale=720:480,setsar=32/27" in graph
    assert "fps=30000/1001" in graph and "[0:a]aresample=48000,apad[a]" in graph and "anullsrc" not in " ".join(match)
    assert match[match.index("-map"):match.index("-map") + 4] == ["-map", "[a]", "-map", "[v]"]  # in the video's order
    assert (match[match.index("-c:v") + 1], match[match.index("-c:a") + 1], match[match.index("-ac") + 1]) == ("libx264", "ac3", "1")
    assert match[match.index("-video_track_timescale") + 1] == "1000"


@pytest.mark.parametrize("video, reason", [
    ({"streams": [{"index": 0, "codec_type": "video", "codec_name": "hevc"}], "format": {"duration": "60"}}, "HEVC"),
    ({"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"},
                  {"index": 1, "codec_type": "audio", "codec_name": "dts"}], "format": {"duration": "60"}}, "dts"),
    ({"streams": [{"index": 0, "codec_type": "subtitle", "codec_name": "ass"},
                  {"index": 1, "codec_type": "video", "codec_name": "h264"}], "format": {"duration": "60"}}, "first streams"),
    ({"streams": [{"index": 0, "codec_type": "audio", "codec_name": "aac"}], "format": {"duration": "60"}}, "no picture"),
])
def test_a_video_the_intro_cant_be_joined_onto_says_why(tmp_path, fake_ffmpeg, video, reason):
    fake_ffmpeg["video"] = video
    with pytest.raises(intro.IntroError, match=reason):
        intro.prepend(tmp_path / "intro.mp4", tmp_path / "source.mp4", tmp_path / "out.mp4", Settings(data_dir=tmp_path))
    assert fake_ffmpeg["runs"] == []


def test_a_short_isnt_made_too_long_by_its_intro(tmp_path, fake_ffmpeg):
    fake_ffmpeg["video"] = {**STREAM, "format": {"duration": "177"}}
    with pytest.raises(intro.IntroError, match="longer than 180 seconds"):
        intro.prepend(tmp_path / "intro.mp4", tmp_path / "source.mp4", tmp_path / "source.mp4", Settings(data_dir=tmp_path),
                      max_length=180)
    assert (tmp_path / "source.mp4").read_bytes() == b"stream"


def frame_hashes(video: Path) -> list[str]:
    """Each decoded frame of a video's picture, hashed."""
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-map", "0:v:0", "-f", "framemd5", "-"],
                         capture_output=True, text=True, check=True).stdout
    return [line.rsplit(",", 1)[1].strip() for line in out.splitlines() if line and not line.startswith("#")]


@needs_ffmpeg
def test_the_stream_comes_out_untouched_after_the_intro(tmp_path):
    source = tmp_path / "source.mp4"
    ffmpeg.run(["-f", "lavfi", "-i", "testsrc=size=320x180:rate=30", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-t", "6", "-c:v", "libx264", "-g", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source)])
    logo = tmp_path / "intro.mov"  # bigger, a different frame rate, and silent
    ffmpeg.run(["-f", "lavfi", "-i", "color=red:s=640x480:r=25", "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(logo)])
    out = tmp_path / "joined.mp4"

    seconds = intro.prepend(logo, source, out, Settings(hwaccel="none", video_encoder="libx264"))
    assert seconds == pytest.approx(2)
    probe = ffmpeg.probe(out)
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (320, 180)
    assert float(probe["format"]["duration"]) == pytest.approx(8, abs=0.15)
    assert any(s["codec_type"] == "audio" for s in probe["streams"])
    errors = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "null", "-"], capture_output=True, text=True).stderr
    assert errors == ""  # every frame decodes, the intro's and the stream's

    stream, joined = frame_hashes(source), frame_hashes(out)
    assert joined[-len(stream):] == stream  # the stream's picture is exactly as it was: not re-encoded
    assert len(joined) - len(stream) == pytest.approx(60, abs=1)  # 2 s of intro at the stream's 30 fps
