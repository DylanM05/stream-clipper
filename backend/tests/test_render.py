from clipper import ffmpeg
from clipper.config import Settings
from clipper.models import Clip
from clipper.pipeline import render

CLIP = Clip(id="hl01", kind="highlight", start=10, end=20, title="t", description="", score=0.5, llm_start="", llm_end="")


def test_none_turns_optional_settings_off():
    s = Settings(hwaccel="none", language="None", llm_effort="")
    assert s.hwaccel is None and s.language is None and s.llm_effort is None
    assert Settings(hwaccel="cuda").hwaccel == "cuda"


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
