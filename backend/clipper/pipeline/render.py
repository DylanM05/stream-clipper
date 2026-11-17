"""Cut the clips with ffmpeg on the GPU (NVENC): every clip in 16:9, and highlights also as 9:16 Shorts with captions."""

import logging
import os
import re
import shutil
import unicodedata
import uuid
from collections.abc import Callable
from pathlib import Path

from .. import cuts as cuts_
from .. import ffmpeg
from ..config import Settings
from ..models import Box, Clip, Word
from ..overlay import Overlay, filter_graph
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
    source: Path,
    clip: Clip,
    out: Path,
    settings: Settings,
    on_progress: Callable[[float], None] | None = None,
    overlay: Overlay | None = None,
) -> None:
    """With the show's `overlay`, the video comes out 1920x1080 with it on (overlay.py). Parts cut out of the
    clip in the app are left out, and what's either side of each joined."""
    tmp = out.with_name(out.stem + ".tmp.mp4")
    overlay_input = ["-i", str(overlay.path.resolve())] if overlay else []
    if clip.cuts:
        audio = ffmpeg.has_audio(source)
        graph = _cut_graph(clip, audio)
        if overlay:
            graph += ";" + filter_graph(overlay, video="[vc]")
        maps = ["-map", "[v]" if overlay else "[vc]", *(["-map", "[a]"] if audio else [])]
        streams = [*overlay_input, "-filter_complex", graph, *maps, *_video_args(settings), *_audio_codec()]
    else:
        video = [*overlay_input, "-filter_complex", filter_graph(overlay), "-map", "[v]"] if overlay else ["-map", "0:v:0"]
        streams = [*video, "-map", "0:a:0?", *_video_args(settings), *_audio_args(clip)]
    _encode(
        settings,
        lambda hwaccel: [*_input_args(source, clip, hwaccel), *streams, "-movflags", "+faststart", str(tmp)],
        on_progress=on_progress,
        duration=clip.duration,
    )
    tmp.replace(out)


BLOCK_GAP_S = 10.0  # in render_compilation, a part cut out this long or longer splits the clip into blocks


def render_compilation(
    source: Path,
    clip: Clip,
    out: Path,
    settings: Settings,
    on_progress: Callable[[float], None] | None = None,
    overlay: Overlay | None = None,
) -> None:
    """A clip whose parts are far apart in the stream, such as a compilation (compilation.py): rendered in blocks, each
    as render_landscape does, so the long stretches between them are never decoded, then joined without re-encoding.
    Each block's sound fades in and out, so the joins don't click. Shorter cuts (a pause taken out) stay inside their
    block and are joined as in any clip."""
    blocks: list[Clip] = []
    for a, b in cuts_.kept(clip.start, clip.end, clip.cuts):
        if blocks and a - blocks[-1].end < BLOCK_GAP_S:
            last = blocks[-1]
            blocks[-1] = last.model_copy(update={"end": b, "cuts": [*last.cuts, (last.end, a)]})
        else:
            blocks.append(clip.model_copy(update={"start": a, "end": b, "cuts": []}))
    files = [out.with_name(f"{out.stem}.tmp.part{i:03d}.mp4") for i in range(len(blocks))]
    listing = out.with_name(out.stem + ".tmp.parts.txt")
    joined = out.with_name(out.stem + ".tmp.mp4")
    total = max(clip.duration, 0.001)
    done = 0.0
    try:
        for block, file in zip(blocks, files):
            report = None
            if on_progress:
                def report(f: float, before: float = done, length: float = block.duration) -> None:
                    on_progress((before + f * length) / total)
            render_landscape(source, block, file, settings, on_progress=report, overlay=overlay)
            done += block.duration
        listing.write_text("".join(f"file '{_quoted(f.resolve())}'\n" for f in files), encoding="utf-8")
        ffmpeg.run(["-f", "concat", "-safe", "0", "-i", str(listing), "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                    "-movflags", "+faststart", str(joined)])
        joined.replace(out)
    finally:
        for temporary in (*files, listing, joined):
            temporary.unlink(missing_ok=True)


def _quoted(path: Path) -> str:
    """A path for a concat list between single quotes, which can't hold one itself."""
    return str(path).replace("'", "'\\''")


SHORT_W, SHORT_H = 1080, 1920
MAX_SHORT_S = 180  # YouTube's limit for Shorts
CAPTION_GAP = 136  # from the bottom of the video to the bottom of the captions under it


def render_short(
    source: Path,
    clip: Clip,
    words: list[Word],
    out: Path,
    settings: Settings,
    on_progress: Callable[[float], None] | None = None,
    background: Path | None = None,
    video_top: float | None = None,
    video_width: float = 1.0,
    crop: bool | None = None,
    split: tuple[list[Box], Box | None] | None = None,
) -> None:
    """The whole frame over a blurred copy of itself, or over the show's `background` image. `video_top` and
    `video_width` place the video on it as fractions of the Short (None centres it); the captions go just
    under it. `crop` instead fills the Short with the middle of the frame, with the captions in its lower third;
    without it, CLIPPER_SHORT_LAYOUT=crop does, for a Short without a background image. `split` (the webcams' parts
    of the picture, and the game's or None for all of it) instead puts the webcams across the top (webcam_grid) and
    the game under them, with the captions just below where they meet."""
    if split:
        crop, background = False, None
    elif crop is None:
        crop = settings.short_layout == "crop" and not background
    if crop:
        background = None
    width = max(2, round(SHORT_W * video_width / 2) * 2)
    height = round(width * 9 / 16)  # streams are 16:9
    top = round((SHORT_H - height) / 2 if video_top is None else video_top * SHORT_H)
    captions = out.with_name(out.stem + ".ass")
    if split:
        margin = SHORT_H - webcams_height(split[0]) - SPLIT_CAPTION_DROP
    else:
        margin = 520 if crop else max(60, SHORT_H - top - height - CAPTION_GAP)  # 520 under a centred video
    if clip.cuts:  # the captions follow the video as it comes out, with the cuts closed up
        ass = build_ass(kept_words(words, clip), 0, clip.duration, font=settings.caption_font, margin=margin)
    else:
        ass = build_ass(words, clip.start, clip.end, font=settings.caption_font, margin=margin)
    write_text(captions, ass)
    audio = ffmpeg.has_audio(source) if clip.cuts else False
    joined = _cut_graph(clip, audio) + ";" if clip.cuts else ""
    # Square pixels first: a DVD's widescreen picture is stored squeezed (720x480) and would come out squashed.
    picture = f"{'[vc]' if clip.cuts else '[0:v]'}scale='trunc(iw*sar/2)*2':ih,setsar=1"
    if split:
        video = split_graph(picture, *split)
    elif crop:  # the middle 9:16 of it, or of a picture that's narrower than that already, the middle of its height
        video = f"{picture},crop='min(iw,trunc(ih*9/16/2)*2)':'min(ih,trunc(iw*16/9/2)*2)',scale=1080:1920"
    else:
        blurred = (
            "[bg]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
            # Muted and darkened, so a bright patch of the stream (a green screen, say) doesn't glare.
            "boxblur=8:2,scale=1080:1920,eq=brightness=-0.15:saturation=0.4"
        )
        # The image goes over the blur (which shows through any transparent parts) and stays for every frame.
        back = f"{blurred}[blur];[1:v]scale=1080:1920,setsar=1[image];[blur][image]overlay=0:0[back]" if background else f"{blurred}[back]"
        y = "(H-h)/2" if video_top is None else str(top)
        video = f"{picture},split=2[bg][fg];{back};[fg]scale={width}:-2[front];[back][front]overlay=(W-w)/2:{y}"
    graph = f"{joined}{video},subtitles=filename={captions.name},setsar=1[v]"
    if clip.cuts:
        sound_map, sound = (["-map", "[a]"], _audio_codec()) if audio else ([], [])
    else:
        sound_map, sound = ["-map", "0:a:0?"], _audio_args(clip)
    tmp = out.with_name(out.stem + ".tmp.mp4")
    # Runs inside the clips folder so the subtitle file can be named without ffmpeg's filter-path escaping.
    _encode(
        settings,
        lambda hwaccel: [
            *_input_args(source, clip, hwaccel),
            *(["-i", str(background.resolve())] if background else []),
            "-filter_complex", graph, "-map", "[v]", *sound_map,
            *_video_args(settings), *sound,
            "-movflags", "+faststart", tmp.name,
        ],
        cwd=out.parent,
        on_progress=on_progress,
        duration=clip.duration,
    )
    tmp.replace(out)


SPLIT_CAPTION_DROP = 330  # a split Short's captions end this far below where the webcams meet the game
MAX_WEBCAMS = 4


def webcam_grid(webcams: list[Box]) -> tuple[int, int, int]:
    """How a split Short's webcams sit across its top, as (columns, each one's width, each one's height): side by side,
    or two by two for four. Together they're as tall as their shapes make them at that width (from a 16:9 picture),
    between a quarter and half of the Short; each is cropped to fill its place."""
    columns = 2 if len(webcams) == 4 else len(webcams)
    rows = -(-len(webcams) // columns)
    width = SHORT_W // columns
    tall = rows * sum(width * (h * 9) / (w * 16) for _, _, w, h in webcams) / len(webcams)
    return columns, width, round(min(SHORT_H // 2, max(SHORT_H // 4, tall)) / rows / 2) * 2


def webcams_height(webcams: list[Box]) -> int:
    """How tall the webcams are together, across the top of a split Short."""
    columns, _, height = webcam_grid(webcams)
    return height * -(-len(webcams) // columns)


def split_graph(picture: str, webcams: list[Box], game: Box | None) -> str:
    """Filters from `picture` to the webcams (webcam_grid) stacked on the game, each cut from its part of the picture
    and cropped to fill its place without being stretched."""
    columns, width, height = webcam_grid(webcams)
    n = len(webcams)
    chains = [f"{picture},split={n + 1}" + "".join(f"[cam{i}]" for i in range(n)) + "[game]"]
    chains += [f"[cam{i}]{_crop_to(box)},{_fill(width, height)}[c{i}]" for i, box in enumerate(webcams)]
    rows = []
    for r, first in enumerate(range(0, n, columns)):
        row = "".join(f"[c{i}]" for i in range(first, min(first + columns, n)))
        if row.count("[") > 1:
            chains.append(f"{row}hstack=inputs={row.count('[')}[row{r}]")
            row = f"[row{r}]"
        rows.append(row)
    top = rows[0]
    if len(rows) > 1:
        chains.append(f"{''.join(rows)}vstack=inputs={len(rows)}[top]")
        top = "[top]"
    chains.append(f"[game]{_crop_to(game) + ',' if game else ''}{_fill(SHORT_W, SHORT_H - webcams_height(webcams))}[bottom]")
    return ";".join(chains) + f";{top}[bottom]vstack"


def _crop_to(box: Box) -> str:
    """Crops the picture to `box`, given as fractions of it."""
    x, y, w, h = box
    return f"crop=trunc(iw*{w:.4f}/2)*2:trunc(ih*{h:.4f}/2)*2:trunc(iw*{x:.4f}):trunc(ih*{y:.4f})"


def _fill(width: int, height: int) -> str:
    """The middle of the picture at the shape of `width` x `height`, scaled to it."""
    return f"crop='min(iw,trunc(ih*{width}/{height}/2)*2)':'min(ih,trunc(iw*{height}/{width}/2)*2)',scale={width}:{height}"


def render_proxy(ws: Workspace, settings: Settings, on_progress: Callable[[float], None] | None = None) -> Path:
    """A small copy of the whole video for previewing and trimming on a phone.

    With an NVIDIA GPU and an ffmpeg that has scale_cuda, every frame is decoded, shrunk and encoded on the
    GPU without passing through the CPU, which is several times faster for a long stream. Otherwise the GPU
    decodes and encodes and the CPU shrinks.
    """
    if ws.proxy_mp4.exists():
        return ws.proxy_mp4
    source = ws.source.resolve()
    tmp = ws.proxy_mp4.with_name("proxy.tmp.mp4")
    if "nvenc" in settings.video_encoder:
        codec = ["-c:v", settings.video_encoder, "-preset", "p4", "-rc", "vbr", "-cq", "30", "-b:v", "0"]
    else:
        codec = ["-c:v", settings.video_encoder, "-preset", "veryfast", "-crf", "30"]
    rest = [
        "-g", "60",  # a keyframe every 2 s keeps seeking snappy
        "-c:a", "aac", "-b:a", "96k", "-ac", "2", "-movflags", "+faststart", str(tmp),
    ]
    duration = ffmpeg.probe_duration(source)
    # Down to 30 fps first, so a 60 fps stream has only half its frames shrunk.
    if gpu_proxy(settings):
        log.info("Making the preview copy on the GPU")
        try:
            ffmpeg.run([
                "-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-i", str(source),
                "-vf", f"fps=30,scale_cuda=-2:{settings.proxy_height}:format=yuv420p", *codec, *rest,
            ], on_progress=on_progress, duration=duration)
            tmp.replace(ws.proxy_mp4)
            return ws.proxy_mp4
        except ffmpeg.FFmpegError as e:
            log.warning("Making the preview on the GPU failed; shrinking on the CPU instead: %s", str(e).splitlines()[-1])
    else:
        log.info("Making the preview copy")
    _encode(
        settings,
        lambda hwaccel: [
            *(["-hwaccel", hwaccel] if hwaccel else []), "-i", str(source),
            "-vf", f"fps=30,scale=-2:{settings.proxy_height}", *codec, "-pix_fmt", "yuv420p", *rest,
        ],
        on_progress=on_progress,
        duration=duration,
    )
    tmp.replace(ws.proxy_mp4)
    return ws.proxy_mp4


def gpu_proxy(settings: Settings) -> bool:
    """Whether previews are made entirely on the GPU."""
    return (settings.preview_on_gpu and settings.hwaccel == "cuda" and "nvenc" in settings.video_encoder
            and ffmpeg.has_filter("scale_cuda"))


def render_thumbnail(video: Path, t: float, out: Path) -> None:
    """A clip's picture for the app's lists. One thread, so it doesn't take the server's CPU from a render."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".tmp.jpg")
    ffmpeg.run(["-threads", "1", "-ss", f"{t:.3f}", "-i", str(video), "-an", "-sn", "-frames:v", "1",
                "-vf", "scale=480:-2", "-q:v", "4", str(tmp)])
    if not tmp.exists():  # past the end of the video: ffmpeg finds no frame, and says so only in its log
        raise ffmpeg.FFmpegError(f"No frame at {t:.1f} s")
    tmp.replace(out)


STILL_EVERY_S = 10  # the pass over a whole stream makes a still this often, for the editor's zoomed-out timeline
STILL_BLOCK_S = 60  # zoomed in, stills are made a minute's worth (every 2 s) at a time
STILL_HEIGHT = 72


def render_stills(video: Path, out_dir: Path, every: float, start: float = 0, length: float | None = None,
                  height: int = STILL_HEIGHT) -> int:
    """Stills of `video` every `every` seconds, from `start` for `length` seconds or to the end, in one pass that
    decodes only keyframes: each the first keyframe in its `every` seconds (the preview has one at least every 2 s).
    One pass, because every run of ffmpeg first reads the video's whole index, which for an 8-hour preview takes
    about 180 MB and a moment of CPU. Named by their moment in tenths of a second, as the editor's timeline asks for
    them. Returns how many it made."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / f".stills-{uuid.uuid4().hex}"
    tmp.mkdir()
    try:
        span = ["-ss", f"{start:.3f}", *(["-t", f"{length:.3f}"] if length else [])]
        # -copyts keeps the stream's own times, which select and showinfo then see; showinfo logs each still's time,
        # in the order they're written.
        log_text = ffmpeg.run([
            "-threads", "1", "-skip_frame", "nokey", "-copyts", *span, "-i", str(video), "-an", "-sn",
            "-vf", f"select='lt(mod(t,{every:g}),2)',showinfo,scale=-2:{height}", "-fps_mode", "passthrough",
            "-q:v", "6", str(tmp / "%06d.jpg"),
        ])
        made: set[int] = set()
        for n, when in re.findall(r"\bn:\s*(\d+)\s+pts:\s*-?\d+\s+pts_time:(-?[\d.]+)", log_text):
            moment = int(float(when) // every * every * 10)
            image = tmp / f"{int(n) + 1:06d}.jpg"
            if moment not in made and image.exists():
                os.replace(image, out_dir / f"{moment}.jpg")
                made.add(moment)
        return len(made)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def render_frame(video: Path, t: float, out: Path, height: int = STILL_HEIGHT) -> None:
    """A small still for the editor's timeline, from the keyframe at or before `t` (the preview has one every 2 s),
    which needs no decoding up to `t` and is close enough for a strip of stills. One thread, so a few at once don't
    take the server's CPU. The editor asks for several at once, so each is written under a name of its own until
    it's done."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.stem}.{uuid.uuid4().hex}.tmp.jpg")
    try:
        ffmpeg.run(["-threads", "1", "-noaccurate_seek", "-ss", f"{t:.3f}", "-i", str(video), "-an", "-sn", "-frames:v", "1",
                    "-vf", f"scale=-2:{height}", "-q:v", "6", str(tmp)])
        if not tmp.exists():  # past the end of the video: ffmpeg finds no frame, and says so only in its log
            raise ffmpeg.FFmpegError(f"No frame at {t:.1f} s")
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)


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
    # -ss before -i seeks quickly, and stays frame-accurate because the clip is re-encoded. -t is before -i
    # too, so it limits this input even when another (a Short's background) follows. It's the whole stretch from
    # start to end, cuts and all: they're taken out by the filters.
    return [*hw, "-ss", f"{clip.start:.3f}", "-t", f"{clip.end - clip.start:.3f}", "-i", str(source)]


def _video_args(settings: Settings) -> list[str]:
    if "nvenc" in settings.video_encoder:
        return ["-c:v", settings.video_encoder, "-preset", "p5", "-tune", "hq",
                "-rc", "vbr", "-cq", "21", "-b:v", "0", "-pix_fmt", "yuv420p"]
    return ["-c:v", settings.video_encoder, "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]


def _audio_args(clip: Clip) -> list[str]:
    return ["-af", _fades(clip), *_audio_codec()]


def _fades(clip: Clip) -> str:
    """The sound fades in and out at the ends of the clip, so it doesn't start or stop with a click."""
    fade_out = max(0.0, clip.duration - 0.25)
    return f"afade=t=in:d=0.15,afade=t=out:st={fade_out:.3f}:d=0.25"


def _audio_codec() -> list[str]:
    return ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]


JOIN_FADE_S = 0.01  # each side of a join fades this quickly, so the sound doesn't click where it jumps


def _cut_graph(clip: Clip, audio: bool) -> str:
    """Filters that take the parts of the clip that stay and join them: the picture to [vc], and with `audio`
    the sound, faded at the clip's ends, to [a]. Times count from the clip's start, where the input begins."""
    parts = [(a - clip.start, b - clip.start) for a, b in cuts_.kept(clip.start, clip.end, clip.cuts)]
    n = len(parts)
    chains = ["[0:v]split=" + str(n) + "".join(f"[vp{i}]" for i in range(n))]
    chains += [f"[vp{i}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]" for i, (a, b) in enumerate(parts)]
    if audio:
        chains.append("[0:a]asplit=" + str(n) + "".join(f"[ap{i}]" for i in range(n)))
        chains += [
            f"[ap{i}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=in:d={JOIN_FADE_S},afade=t=out:st={max(0.0, b - a - JOIN_FADE_S):.3f}:d={JOIN_FADE_S}[a{i}]"
            for i, (a, b) in enumerate(parts)
        ]
    inputs = "".join(f"[v{i}]" + (f"[a{i}]" if audio else "") for i in range(n))
    chains.append(f"{inputs}concat=n={n}:v=1:a={int(audio)}[vc]" + ("[ac]" if audio else ""))
    if audio:
        chains.append(f"[ac]{_fades(clip)}[a]")
    return ";".join(chains)


def kept_words(words: list[Word], clip: Clip) -> list[Word]:
    """The clip's words, timed as it comes out: from 0 at its start, with the cuts closed up. A word that runs
    into a cut is cut short with it."""
    found, at = [], 0.0
    for a, b in cuts_.kept(clip.start, clip.end, clip.cuts):
        for w in words:
            if w.end > a and w.start < b:
                found.append(Word(start=max(w.start, a) - a + at, end=min(w.end, b) - a + at, text=w.text))
        at += b - a
    return found
