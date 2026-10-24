"""Thin wrappers around the ffmpeg and ffprobe command-line tools."""

import json
import subprocess
import threading
from collections.abc import Callable
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

    With `on_progress` and the output's expected `duration`, reports progress from 0 to 1 as it runs.
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


def _run_with_progress(cmd: list[str], cwd: Path | None, on_progress: Callable[[float], None], duration: float) -> str:
    stderr: list[str] = []
    with subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace"
    ) as proc:
        # Read the log on another thread so a full stderr pipe can't stall ffmpeg.
        reader = threading.Thread(target=lambda: stderr.append(proc.stderr.read()), daemon=True)
        reader.start()
        for line in proc.stdout:
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and value.isdigit():
                on_progress(min(1.0, int(value) / 1e6 / duration))
        proc.wait()
        reader.join()
    log = "".join(stderr)
    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg exited with {proc.returncode}:\n{_tail(log)}")
    return log


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])
