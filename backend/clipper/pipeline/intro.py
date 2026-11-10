"""A show's intro joined onto the start of a video without re-encoding the video.

Only the intro is encoded, to match the video it goes on (its size, frame rate, timescale and sound), and the two are
put one after the other as they are, with ffmpeg's concat demuxer. For a full episode that's seconds of work instead
of hours of re-encoding the whole stream, and the stream's own picture is untouched.

Only H.264 can be joined like this; it's what the streaming sites and the app's own renders use. The concat demuxer
turns each part's H.264 into Annex B with its own parameter sets in line (its auto_convert), so the intro's encoder
settings differing from the stream's doesn't trouble a decoder.
"""

import logging
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path

from .. import ffmpeg
from ..config import Settings

log = logging.getLogger(__name__)

MAX_INTRO_S = 30  # longer costs watch time; 3-5 s is plenty
FASTSTART_MAX_BYTES = 4_000_000_000  # bigger (a full episode) skips the second pass moving the index to the front
# Encoders for the sound, by the video's sound's codec, so the intro's sound matches it.
SOUND_ENCODERS = {
    "aac": ["aac", "-b:a", "192k"],
    "ac3": ["ac3"],
    "eac3": ["eac3"],
    "mp3": ["libmp3lame", "-b:a", "192k"],
    "opus": ["libopus", "-b:a", "160k"],
}


class IntroError(Exception):
    """The intro can't be joined onto this video without re-encoding it; the message says why."""


def prepend(
    intro: Path,
    video: Path,
    out: Path,
    settings: Settings,
    *,
    max_length: float | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> float:
    """`intro` and then `video`, as `out` (which can be `video` itself). Raises IntroError if it can't be joined
    without re-encoding the video, or the result would be longer than `max_length`. Returns the intro's length."""
    target = ffmpeg.probe(video)
    streams = target.get("streams", [])
    picture = next((s for s in streams if s.get("codec_type") == "video"
                    and not s.get("disposition", {}).get("attached_pic")), None)
    sound = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not picture:
        raise IntroError("The video has no picture to join the intro to.")
    if picture.get("codec_name") != "h264":
        raise IntroError(f"The video is {str(picture.get('codec_name', 'an unknown format')).upper()}: an intro can only "
                         "be joined on without re-encoding H.264.")
    if sound and sound.get("codec_name") not in SOUND_ENCODERS:
        raise IntroError(f"The video's sound is {sound.get('codec_name')}, which the intro can't be made to match.")
    layout = sorted((s for s in (picture, sound) if s), key=lambda s: s["index"])
    if [s["index"] for s in layout] != list(range(len(layout))):
        raise IntroError("The video's picture and sound aren't its first streams, which joining needs.")
    length = float(target["format"]["duration"])
    seconds = min(ffmpeg.probe_duration(intro), MAX_INTRO_S)
    if max_length and length + seconds > max_length:
        raise IntroError(f"With the intro it would be longer than {max_length:g} seconds.")

    matched = out.with_name(out.stem + ".intro-matched.tmp.mp4")
    listing = out.with_name(out.stem + ".intro.tmp.txt")
    joined = out.with_name(out.stem + ".intro.tmp.mp4")
    try:
        ffmpeg.run(match_args(intro, ffmpeg.has_audio(intro), seconds, picture, sound, layout, settings, matched))
        listing.write_text("".join(f"file '{_quoted(p.resolve())}'\n" for p in (matched, video)), encoding="utf-8")
        faststart = ["-movflags", "+faststart"] if video.stat().st_size < FASTSTART_MAX_BYTES else []
        ffmpeg.run(
            ["-f", "concat", "-safe", "0", "-i", str(listing), "-map", "0:v:0", *(["-map", "0:a:0"] if sound else []),
             "-c", "copy", *faststart, str(joined)],
            on_progress=on_progress, duration=length + seconds,
        )
        joined.replace(out)
    finally:
        for temporary in (matched, listing, joined):
            temporary.unlink(missing_ok=True)
    log.info("Joined the %.1f s intro onto %s", seconds, out.name)
    return seconds


def match_args(
    intro: Path, intro_sound: bool, seconds: float, picture: dict, sound: dict | None, layout: list[dict],
    settings: Settings, out: Path,
) -> list[str]:
    """ffmpeg's arguments to encode `seconds` of the intro the way the video it goes on is: the same size (fitted in,
    with black around it), pixel shape, frame rate and timescale, and the same kind of sound, or silence if the intro
    has none. Its streams are in the video's order, as the concat demuxer pairs them by number."""
    width, height = int(picture["width"]), int(picture["height"])
    pixel = _fraction(picture.get("sample_aspect_ratio"), Fraction(1))
    shown = max(2, round(width * pixel / 2) * 2)  # as wide as it's shown, for a video with non-square pixels
    rate = picture.get("r_frame_rate") if _fraction(picture.get("r_frame_rate"), Fraction(0)) else None
    rate = rate or picture.get("avg_frame_rate") or "30"
    timescale = _fraction(picture.get("time_base"), Fraction(1, 90000)).denominator
    graph = [
        f"[0:v]scale={shown}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad={shown}:{height}:(ow-iw)/2:(oh-ih)/2,scale={width}:{height},setsar={pixel.numerator}/{pixel.denominator},"
        f"fps={rate},format=yuv420p,tpad=stop_mode=clone:stop_duration=1[v]"
    ]
    inputs = ["-i", str(intro)]
    sound_args: list[str] = []
    if sound:
        rate_hz = int(sound.get("sample_rate") or 48000)
        channels = int(sound.get("channels") or 2)
        if not intro_sound:
            inputs += ["-f", "lavfi", "-i", f"anullsrc=r={rate_hz}:cl={'mono' if channels == 1 else 'stereo'}"]
        graph.append(f"[{0 if intro_sound else 1}:a]aresample={rate_hz},apad[a]")
        sound_args = ["-c:a", *SOUND_ENCODERS[sound["codec_name"]], "-ar", str(rate_hz), "-ac", str(channels)]
    if "nvenc" in settings.video_encoder:
        encode = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "19", "-b:v", "0"]
    else:
        encode = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
    maps = [arg for s in layout for arg in ("-map", "[v]" if s is picture else "[a]")]
    return [
        *inputs, "-filter_complex", ";".join(graph), *maps, "-t", f"{seconds:.3f}",
        *encode, "-bf", "0", *sound_args, "-video_track_timescale", str(timescale), "-movflags", "+faststart", str(out),
    ]


def _fraction(text: object, default: Fraction) -> Fraction:
    """"16/9", "1:1" or "1/90000" as a fraction; `default` for anything else, or zero."""
    try:
        value = Fraction(str(text).replace(":", "/"))
    except (ValueError, ZeroDivisionError):
        return default
    return value if value > 0 else default


def _quoted(path: Path) -> str:
    """A path for a concat list between single quotes, which can't hold one itself."""
    return str(path).replace("'", "'\\''")
