import shutil
import time
from pathlib import Path

import pytest

from clipper import ffmpeg
from clipper.config import Settings
from clipper.models import Clip
from clipper.pipeline import render

CLIP = Clip(id="hl01", kind="highlight", start=10, end=20, title="t", description="", score=0.5, llm_start="", llm_end="")


def test_none_turns_optional_settings_off():
    s = Settings(hwaccel="none", language="None", llm_effort="")
    assert s.hwaccel is None and s.language is None and s.llm_effort is None
    assert Settings(hwaccel="cuda").hwaccel == "cuda"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_ffmpeg_stops_when_progress_reporting_raises(tmp_path):
    class Stop(Exception):
        pass

    def stop(_fraction):
        raise Stop

    started = time.monotonic()
    with pytest.raises(Stop):
        # 10 minutes of generated video, far longer than the test waits
        ffmpeg.run(["-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30", "-t", "600", "-c:v", "mpeg4",
                    str(tmp_path / "out.mp4")], on_progress=stop, duration=600)
    assert time.monotonic() - started < 20


def test_previews_are_made_on_the_gpu_and_fall_back_to_shrinking_on_the_cpu(monkeypatch, tmp_path):
    from clipper.workspace import Workspace, write_json

    ws = Workspace.open(tmp_path, "vod")
    write_json(ws.meta_json, {"source_path": str(ws.root / "source.mp4")})
    runs = []

    def fake_run(args, cwd=None, on_progress=None, duration=None):
        runs.append(args[args.index("-vf") + 1])
        if "scale_cuda" in args[args.index("-vf") + 1]:
            raise ffmpeg.FFmpegError("No such filter: 'scale_cuda'")
        Path(args[-1]).write_bytes(b"preview")

    monkeypatch.setattr(ffmpeg, "run", fake_run)
    monkeypatch.setattr(ffmpeg, "probe_duration", lambda path: 3600.0)
    monkeypatch.setattr(ffmpeg, "has_filter", lambda name: True)
    render.render_proxy(ws, Settings(hwaccel="cuda"))

    # 30 fps first, so only half of a 60 fps stream's frames are shrunk
    assert runs == ["fps=30,scale_cuda=-2:480:format=yuv420p", "fps=30,scale=-2:480"]
    assert ws.proxy_mp4.read_bytes() == b"preview"

    monkeypatch.setattr(ffmpeg, "has_filter", lambda name: False)
    assert not render.gpu_proxy(Settings(hwaccel="cuda"))


def test_falls_back_to_cpu_decoding_when_the_gpu_cant_decode(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, cwd=None, **progress):
        calls.append(args)
        if "-hwaccel" in args:
            raise ffmpeg.FFmpegError("No device available for decoder")
        (tmp_path / "out.tmp.mp4").write_bytes(b"")
        return ""

    monkeypatch.setattr(ffmpeg, "run", fake_run)
    render.render_landscape(tmp_path / "source.mp4", CLIP, tmp_path / "out.mp4", Settings(hwaccel="cuda"))

    assert len(calls) == 2
    assert calls[0][:2] == ["-hwaccel", "cuda"]
    assert "-hwaccel" not in calls[1]
    assert (tmp_path / "out.mp4").exists()


def test_a_compilation_renders_each_part_on_its_own_and_joins_them_without_re_encoding(monkeypatch, tmp_path):
    calls, progress = [], []

    def fake_run(args, cwd=None, on_progress=None, duration=None):
        calls.append(args)
        if "concat" in args:
            listing = args[args.index("-i") + 1]
            assert Path(listing).read_text().count("file '") == 2
        if on_progress:
            on_progress(1.0)
        Path(args[-1]).write_bytes(b"video")

    monkeypatch.setattr(ffmpeg, "run", fake_run)
    clip = CLIP.model_copy(update={"kind": "compilation", "start": 100, "end": 1030, "cuts": [(130, 1000)]})
    render.render_compilation(tmp_path / "source.mp4", clip, tmp_path / "comp.mp4", Settings(hwaccel=None),
                              on_progress=progress.append)

    seeks = [(c[c.index("-ss") + 1], c[c.index("-t") + 1]) for c in calls if "-ss" in c]
    assert seeks == [("100.000", "30.000"), ("1000.000", "30.000")]  # the 14 minutes between are never read
    assert calls[-1][calls[-1].index("-c") + 1] == "copy"
    assert progress == [0.5, 1.0]
    assert [p.name for p in tmp_path.iterdir()] == ["comp.mp4"]  # the parts are gone
