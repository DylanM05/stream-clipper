"""Command line.

- `clipper serve` and `clipper worker` run the review app and the background worker.
- `clipper browser` opens the browser that uploads go through, on a desktop, for signing in.
- `clipper add <url>` queues a video for the worker.
- `clipper process <url-or-file>` runs the whole pipeline in the foreground, without the app.
- `clipper doctor` and `clipper llm-check` check the server setup and the Claude Code login.
"""

import json
import logging
from pathlib import Path
from typing import Annotated, Optional

import typer
from pydantic import BaseModel

from . import ffmpeg
from .config import get_settings
from .llm import ClaudeCode, LLMError, UsageLimitError
from .models import Clip
from .pipeline import audio, download, refine, render, segment, signals, transcribe
from .timecode import to_timecode
from .workspace import STEPS

app = typer.Typer(
    help="Turn long stream VODs into chapter and highlight clips.", no_args_is_help=True, add_completion=False
)


@app.callback()
def _setup(verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show debug logging.")] = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # One line per HTTP request while the Whisper model downloads is noise.
    for noisy in ("httpx", "httpx2", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@app.command()
def process(
    source: Annotated[str, typer.Argument(help="VOD or video URL (Twitch, YouTube, Kick, X...) or a local video file.")],
    notes: Annotated[Optional[str], typer.Option(help="Notes for Claude: names, running jokes, title style.")] = None,
    notes_file: Annotated[Optional[Path], typer.Option(exists=True, dir_okay=False, help="Read the notes from a file.")] = None,
    redo: Annotated[Optional[str], typer.Option(help=f"Re-run this step and every later one: {', '.join(STEPS)}.")] = None,
    render_clips: Annotated[bool, typer.Option("--render/--no-render", help="Render the clips with ffmpeg.")] = True,
    shorts: Annotated[bool, typer.Option("--shorts/--no-shorts", help="Also render highlights as vertical Shorts.")] = True,
    only: Annotated[Optional[list[str]], typer.Option(help="Only render these clip ids, e.g. --only hl03 --only ch02.")] = None,
) -> None:
    """Download, transcribe, segment and render one VOD. Steps that already finished are skipped."""
    if redo and redo not in STEPS:
        raise typer.BadParameter(f"choose one of: {', '.join(STEPS)}", param_hint="--redo")
    if notes_file:
        notes = notes_file.read_text(encoding="utf-8")
    settings = get_settings()

    ws = download.fetch(source, settings)
    if redo:
        ws.clear_from(redo)
    audio.extract(ws)
    transcript = transcribe.run(ws, settings)
    hints = signals.run(ws, settings)
    try:
        segmentation = segment.run(ws, settings, transcript, hints, context=notes or "")
    except UsageLimitError as e:
        when = f" It resets at {e.resets_at:%Y-%m-%d %H:%M %Z}." if e.resets_at else ""
        typer.secho(
            f"Your Claude plan's usage limit was reached.{when} Run the same command again after that; "
            "finished steps are skipped.",
            fg="yellow", err=True,
        )
        raise typer.Exit(2)
    except LLMError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1)

    clips = refine.run(ws, segmentation, transcript)
    _print_clips(clips)
    if not render_clips:
        typer.echo(f"\nClip list saved to {ws.segments_json}")
        return
    outputs = render.run(ws, settings, clips, transcript.words(), shorts=shorts, only=set(only or []) or None)
    typer.echo(f"\n{len(outputs)} files in {ws.clips_dir}")


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Address to listen on; 0.0.0.0 for all.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = 8080,
) -> None:
    """Run the review app (API and phone app)."""
    import uvicorn

    from .web.app import create_app

    uvicorn.run(create_app(get_settings()), host=host, port=port)


@app.command()
def worker() -> None:
    """Process queued videos and render approved clips, until stopped."""
    from .worker import Worker

    Worker(get_settings()).run()


@app.command()
def browser(port: Annotated[int, typer.Option(help="Remote debugging port the worker connects to.")] = 9222) -> None:
    """Open the browser that YouTube uploads go through, on a desktop. Sign in to YouTube in it once."""
    from .studio import launch_desktop_browser

    settings = get_settings()
    launch_desktop_browser(settings.browser_profile_dir, port)
    typer.echo(
        "Opened a browser with its own profile. Sign in to YouTube there; it's remembered. If it's closed, "
        f"the worker opens it again when an upload starts. The worker connects on port {port} (CLIPPER_STUDIO_CDP_URL)."
    )


@app.command()
def add(
    url: Annotated[str, typer.Argument(help="Video or VOD URL.")],
    whole: Annotated[Optional[bool], typer.Option("--whole/--segment", help="Post the whole video, or find clips. Default: the show's setting, else whole for X links.")] = None,
    notes: Annotated[str, typer.Option(help="Notes for Claude: names, running jokes, title style.")] = "",
) -> None:
    """Queue a video for the worker."""
    from sqlmodel import Session

    from .db import get_engine, queue_vod

    mode = None if whole is None else ("whole" if whole else "segment")
    with Session(get_engine(get_settings())) as s:
        vod = queue_vod(s, url, mode=mode, notes=notes)
        s.commit()
        typer.echo(f"Queued video {vod.id} ({vod.mode})")


class _Check(BaseModel):
    answer: int
    title: str


@app.command("llm-check")
def llm_check() -> None:
    """Check that Claude Code is installed, signed in, and returns structured output."""
    settings = get_settings()
    llm = ClaudeCode(bin=settings.claude_bin, model=settings.llm_model, effort="low", timeout_s=300)
    typer.echo(llm.auth_status())
    try:
        result = llm.structured(
            system_prompt="You answer briefly.",
            task="What is 17 + 25? Also invent a short, funny clip title about a streamer losing to a chicken.",
            stdin_text="",
            schema=_Check,
        )
    except LLMError as e:
        typer.secho(f"Failed: {e}", fg="red", err=True)
        raise typer.Exit(1)
    typer.secho(f"OK: answer={result.answer}, title={result.title!r}", fg="green")


@app.command()
def doctor() -> None:
    """Check the setup: GPU video encoding, GPU for Whisper, Claude Code login and the data folder."""
    settings = get_settings()
    failures = 0

    def report(name: str, passed: bool, detail: str = "") -> None:
        nonlocal failures
        failures += not passed
        typer.secho(f"{'OK  ' if passed else 'FAIL'}  {name}" + (f": {detail}" if detail else ""), fg="green" if passed else "red")

    try:
        ffmpeg.run(["-f", "lavfi", "-i", "color=black:s=256x256:d=1", "-c:v", settings.video_encoder, "-f", "null", "-"])
        report(f"ffmpeg can encode with {settings.video_encoder}", True)
    except (ffmpeg.FFmpegError, FileNotFoundError) as e:
        report(f"ffmpeg can encode with {settings.video_encoder}", False, str(e).splitlines()[-1])

    try:
        import ctranslate2

        devices = ctranslate2.get_cuda_device_count()
        report("GPU visible to Whisper", devices > 0 or settings.whisper_device != "cuda", f"{devices} CUDA device(s)")
    except ImportError:
        report("GPU visible to Whisper", False, "faster-whisper isn't installed (pip install '.[transcribe]')")

    status = ClaudeCode(bin=settings.claude_bin).auth_status()
    try:
        info = json.loads(status)
        report("Claude Code signed in", bool(info.get("loggedIn")), f"{info.get('authMethod')}, {info.get('subscriptionType')} plan")
    except json.JSONDecodeError:
        report("Claude Code signed in", False, status[:200])

    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.data_dir / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        report("data folder is writable", True, str(settings.data_dir.resolve()))
    except OSError as e:
        report("data folder is writable", False, str(e))

    if failures:
        raise typer.Exit(1)


def _print_clips(clips: list[Clip]) -> None:
    typer.echo("")
    for c in clips:
        short = "  [short]" if c.kind == "highlight" and c.vertical_ok else ""
        minutes, seconds = divmod(int(c.duration), 60)
        typer.echo(
            f"{c.id}  {to_timecode(c.start)}-{to_timecode(c.end)}  {minutes:>3}:{seconds:02d}  "
            f"{c.score:.2f}  {c.title}{short}"
        )
