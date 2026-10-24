"""Cut the clips with ffmpeg on the GPU (NVENC): every clip in 16:9, and highlights also as 9:16 Shorts with captions."""

import logging
import re
import unicodedata
from collections.abc import Callable
from pathlib import Path

from .. import ffmpeg
from ..config import Settings
from ..models import Clip, Word
from ..workspace import Workspace, write_text
from .captions import build_ass

log = logging.getLogger(__name__)


def run(
    ws: Workspace, settings: Settings, clips: list[Clip], words: list[Word], *, shorts: bool = True, only: set[str] | None = None
) -> list[Path]:
    ws.clips_dir.mkdir(exist_ok=True)
    source = ws.source.resolve()
    outputs = []
    for clip in clips:
        if only and clip.id not in only:
            continue
        stem = f"{clip.id}-{slugify(clip.title)}"
        landscape = ws.clips_dir / f"{stem}.mp4"
        if not landscape.exists():
            log.info("Rendering %s (%.0f s)", landscape.name, clip.duration)
            render_landscape(source, clip, landscape, settings)
        outputs.append(landscape)
        if shorts and clip.kind == "highlight" and clip.vertical_ok:
            short = ws.clips_dir / f"{stem}-short.mp4"
            if not short.exists():
                log.info("Rendering %s (%.0f s)", short.name, clip.duration)
                render_short(source, clip, words, short, settings)
            outputs.append(short)
    return outputs


def render_landscape(
    source: Path, clip: Clip, out: Path, settings: Settings, on_progress: Callable[[float], None] | None = None
) -> None:
    tmp = out.with_name(out.stem + ".tmp.mp4")
    _encode(
        settings,
        lambda hwaccel: [
            *_input_args(source, clip, hwaccel),
            "-map", "0:v:0", "-map", "0:a:0?",
            *_video_args(settings), *_audio_args(clip),
            "-movflags", "+faststart", str(tmp),
        ],
        on_progress=on_progress,
        duration=clip.duration,
    )
    tmp.replace(out)


def render_short(
    source: Path,
    clip: Clip,
    words: list[Word],
    out: Path,
    settings: Settings,
    on_progress: Callable[[float], None] | None = None,
) -> None:
    captions = out.with_name(out.stem + ".ass")
    write_text(captions, build_ass(words, clip.start, clip.end, font=settings.caption_font))
    if settings.short_layout == "crop":
        video = "[0:v]crop=ih*9/16:ih,scale=1080:1920"
    else:  # the whole frame in the middle, over a blurred, zoomed copy of itself
        video = (
            "[0:v]split=2[bg][fg];"
            "[bg]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
            "boxblur=8:2,scale=1080:1920,eq=brightness=-0.06[bgb];"
            "[fg]scale=1080:-2[fgs];"
            "[bgb][fgs]overlay=(W-w)/2:(H-h)/2"
        )
    graph = f"{video},subtitles=filename={captions.name},setsar=1[v]"
    tmp = out.with_name(out.stem + ".tmp.mp4")
    # Runs inside the clips folder so the subtitle file can be named without ffmpeg's filter-path escaping.
    _encode(
        settings,
        lambda hwaccel: [
            *_input_args(source, clip, hwaccel),
            "-filter_complex", graph, "-map", "[v]", "-map", "0:a:0?",
            *_video_args(settings), *_audio_args(clip),
            "-movflags", "+faststart", tmp.name,
        ],
        cwd=out.parent,
        on_progress=on_progress,
        duration=clip.duration,
    )
    tmp.replace(out)


def render_proxy(ws: Workspace, settings: Settings, on_progress: Callable[[float], None] | None = None) -> Path:
    """A small copy of the whole video for previewing and trimming on a phone."""
    if ws.proxy_mp4.exists():
        return ws.proxy_mp4
    source = ws.source.resolve()
    tmp = ws.proxy_mp4.with_name("proxy.tmp.mp4")
    if "nvenc" in settings.video_encoder:
        codec = ["-c:v", settings.video_encoder, "-preset", "p4", "-rc", "vbr", "-cq", "30", "-b:v", "0"]
    else:
        codec = ["-c:v", settings.video_encoder, "-preset", "veryfast", "-crf", "30"]
    log.info("Making the preview copy")
    _encode(
        settings,
        lambda hwaccel: [
            *(["-hwaccel", hwaccel] if hwaccel else []), "-i", str(source),
            "-vf", f"scale=-2:{settings.proxy_height},fps=30", *codec, "-pix_fmt", "yuv420p",
            "-g", "60",  # a keyframe every 2 s keeps seeking snappy
            "-c:a", "aac", "-b:a", "96k", "-ac", "2", "-movflags", "+faststart", str(tmp),
        ],
        on_progress=on_progress,
        duration=ffmpeg.probe_duration(source),
    )
    tmp.replace(ws.proxy_mp4)
    return ws.proxy_mp4


def render_thumbnail(video: Path, t: float, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".tmp.jpg")
    ffmpeg.run(["-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", str(tmp)])
    tmp.replace(out)


def slugify(text: str, max_len: int = 50) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "clip"


def _encode(
    settings: Settings,
    build_args: Callable[[str | None], list[str]],
    cwd: Path | None = None,
    on_progress: Callable[[float], None] | None = None,
    duration: float | None = None,
) -> None:
    """Run ffmpeg with GPU decoding, falling back to CPU decoding if the GPU can't decode this video."""
    try:
        ffmpeg.run(build_args(settings.hwaccel), cwd=cwd, on_progress=on_progress, duration=duration)
    except ffmpeg.FFmpegError:
        if not settings.hwaccel:
            raise
        log.warning("Decoding with %s failed; retrying with CPU decoding", settings.hwaccel)
        ffmpeg.run(build_args(None), cwd=cwd, on_progress=on_progress, duration=duration)


def _input_args(source: Path, clip: Clip, hwaccel: str | None) -> list[str]:
    hw = ["-hwaccel", hwaccel] if hwaccel else []
    # -ss before -i seeks quickly, and stays frame-accurate because the clip is re-encoded.
    return [*hw, "-ss", f"{clip.start:.3f}", "-i", str(source), "-t", f"{clip.duration:.3f}"]


def _video_args(settings: Settings) -> list[str]:
    if "nvenc" in settings.video_encoder:
        return ["-c:v", settings.video_encoder, "-preset", "p5", "-tune", "hq",
                "-rc", "vbr", "-cq", "21", "-b:v", "0", "-pix_fmt", "yuv420p"]
    return ["-c:v", settings.video_encoder, "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]


def _audio_args(clip: Clip) -> list[str]:
    fade_out = max(0.0, clip.duration - 0.25)
    return ["-af", f"afade=t=in:d=0.15,afade=t=out:st={fade_out:.3f}:d=0.25",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
