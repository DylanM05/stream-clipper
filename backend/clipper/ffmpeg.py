"""Thin wrappers around the ffmpeg and ffprobe command-line tools."""

import json
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterator
from functools import lru_cache
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


def run(
    args: list[str],
    cwd: Path | None = None,
    on_progress: Callable[[float], None] | None = None,
    duration: float | None = None,
) -> str:
    """Run ffmpeg and return its log output (stderr). Raises FFmpegError on failure.

    With `on_progress` and the output's expected `duration`, reports progress from 0 to 1 as it runs. If
    `on_progress` raises (e.g. the work was paused), ffmpeg is stopped and the exception passes on.
    """
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-y"]
    if on_progress and duration:
        return _run_with_progress([*cmd, "-progress", "pipe:1", *args], cwd, on_progress, duration)
    proc = subprocess.run([*cmd, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg exited with {proc.returncode}:\n{_tail(proc.stderr)}")
    return proc.stderr


def probe(path: Path) -> dict:
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe could not read {path}:\n{_tail(proc.stderr)}")
    return json.loads(proc.stdout)


def probe_duration(path: Path) -> float:
    return float(probe(path)["format"]["duration"])


def has_audio(path: Path) -> bool:
    return any(s.get("codec_type") == "audio" for s in probe(path).get("streams", []))


def audio_frames(path: Path) -> Iterator[tuple[float | None, int]]:
    """The timestamp (None if it has none) and number of samples of each frame of the first audio stream.
    It decodes all of it: a minute or two for an 8-hour stream."""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "frame=pts_time,nb_samples",
           "-of", "csv=p=0", str(path)]
    # The log goes to a file: a damaged stream can log more than a pipe holds while the frames are read.
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as log:
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log, text=True, encoding="utf-8", errors="replace") as proc:
            for line in proc.stdout:
                pts, samples = (line.strip().split(",") + ["", ""])[:2]
                if samples.isdigit():
                    try:
                        yield float(pts), int(samples)
                    except ValueError:  # N/A
                        yield None, int(samples)
        if proc.returncode != 0:
            log.seek(0)
            raise FFmpegError(f"ffprobe could not read the sound in {path}:\n{_tail(log.read())}")


@lru_cache
def has_filter(name: str) -> bool:
    """Whether this ffmpeg build has a filter. GPU filters such as scale_cuda depend on how it was built."""
    try:
        proc = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(line.split()[1:2] == [name] for line in proc.stdout.splitlines())


def _run_with_progress(cmd: list[str], cwd: Path | None, on_progress: Callable[[float], None], duration: float) -> str:
    stderr: list[str] = []
    with subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace"
    ) as proc:
        # Read the log on another thread so a full stderr pipe can't stall ffmpeg.
        reader = threading.Thread(target=lambda: stderr.append(proc.stderr.read()), daemon=True)
        reader.start()
        try:
            for line in proc.stdout:
                key, _, value = line.strip().partition("=")
                if key == "out_time_us" and value.isdigit():
                    on_progress(min(1.0, int(value) / 1e6 / duration))
        except BaseException:
            proc.kill()
            raise
        proc.wait()
        reader.join()
    log = "".join(stderr)
    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg exited with {proc.returncode}:\n{_tail(log)}")
    return log


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])
