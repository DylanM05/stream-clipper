"""Command line.

- `clipper serve` and `clipper worker` run the review app and the background worker.
- `clipper browser` opens the browser that uploads go through, on a desktop, for signing in.
- `clipper add <url>` queues a video for the worker.
- `clipper process <url-or-file>` runs the whole pipeline in the foreground, without the app.
- `clipper doctor` and `clipper llm-check` check the server setup and the AI chosen in the app.
- `clipper channel-check <url>` shows what the worker sees on a watched channel.
- `clipper rumble-upload-check` shows what Rumble's upload page asks for, in the upload browser.
- `clipper timing-check` finds, and with --fix puts right, transcripts timed early against their video.
- `clipper save-transcripts` saves the transcripts of past streams of the shows with Save transcripts on.
- `clipper breakdown` asks the AI for past streams' breakdowns, from their saved transcripts.
- `clipper deploy-wait` and `clipper deploy-done` let a deploy restart the worker between uploads (deploy/deploy.sh).
"""

import logging
import time
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Optional

import typer
from pydantic import BaseModel

from . import ffmpeg
from .config import get_settings
from .llm import LLMError, UsageLimitError
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
    notes: Annotated[Optional[str], typer.Option(help="Notes for the AI: names, running jokes, title style.")] = None,
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
        typer.secho(f"{e.note}{when} Run the same command again after that; finished steps are skipped.", fg="yellow", err=True)
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
    notes: Annotated[str, typer.Option(help="Notes for the AI: names, running jokes, title style.")] = "",
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
    """Check that the AI chosen in the app (Claude Code, Codex or OpenRouter) answers with structured output."""
    from sqlmodel import Session

    from . import ai
    from .db import get_engine

    settings = get_settings()
    with Session(get_engine(settings)) as s:
        choice = ai.chosen(s, settings)
    typer.echo(f"{ai.PROVIDERS[choice.provider]}, model {choice.model or 'its default'}")
    try:
        llm = ai.backend(settings, choice, effort="low", timeout_s=300)
        if hasattr(llm, "auth_status"):
            typer.echo(llm.auth_status())
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


@app.command("channel-check")
def channel_check(
    url: Annotated[str, typer.Argument(help="A channel link, as you'd add it under a show.")],
    links: Annotated[bool, typer.Option("--links", help="Rumble: also list every video link on the channel's page.")] = False,
) -> None:
    """Show what the worker sees on a channel: its newest videos, and whether each is live and how long it is."""
    from . import rumble
    from .monitor import ChannelUrlError, Monitor, list_rumble, parse_channel

    settings = get_settings()
    try:
        platform, channel_url = parse_channel(url)
    except ChannelUrlError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(f"{platform}: {channel_url}")
    skipped: list[tuple[str, str]] = []
    try:
        # Everything on a Rumble channel, not only what the worker hasn't settled yet.
        listed = (list_rumble(settings.studio_cdp_url, channel_url, skipped=skipped) if platform == "rumble"
                  else Monitor(settings)._list(platform, channel_url))
    except Exception as e:
        typer.secho(f"Couldn't read it: {e}", fg="red", err=True)
        raise typer.Exit(1)
    for item in listed:
        state = "LIVE" if item.live else to_timecode(item.duration) if item.duration else "length not known yet"
        typer.echo(f"  {state:>10}  {item.title[:80]}  {item.url}")
    if not listed:
        typer.echo("  (no videos)")
    if skipped:
        typer.echo("Skipped, as another channel's:")
        for title, owner in skipped:
            typer.echo(f"  {title[:80]}  (Rumble says: {owner})")
    if links and platform == "rumble":
        found = rumble.channel_videos(settings.studio_cdp_url, channel_url)
        typer.echo(f"Video links on the page in the upload browser ({len(found)}):")
        for link in found:
            typer.echo(f"  {link}")


@app.command("rumble-upload-check")
def rumble_upload_check(
    html: Annotated[bool, typer.Option("--html", help="Instead, show the form's two steps and any channel picker as "
                                                      "cut-down HTML.")] = False,
) -> None:
    """Show what Rumble's upload page asks for, as the upload browser (signed in to Rumble) sees it. Nothing is
    uploaded or changed; the page and a screenshot of it are saved under the data folder's rumble/."""
    from . import rumble

    settings = get_settings()
    save_dir = settings.data_dir / "rumble"
    try:
        page = rumble.upload_page(settings.studio_cdp_url, save_dir, sections=html)
    except rumble.RumbleError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1)
    if html:
        for name, part in page["sections"].items():
            typer.echo(f"=== {name} ===\n{part or '(not on the page)'}\n")
    else:
        for line in rumble.describe_form(page):
            typer.echo(line)
    typer.echo(f"\nSaved the page and a screenshot of it in {save_dir}")


@app.command("timing-check")
def timing_check(
    vod_ids: Annotated[Optional[list[int]], typer.Argument(help="Video ids, as this prints them. Default: the 5 newest finished videos.")] = None,
    fix: Annotated[bool, typer.Option("--fix", help="Put their transcripts on the video's timing, move the clips not trimmed by hand to match, and render again the ones affected.")] = False,
) -> None:
    """Check that videos' transcripts, which time the Shorts' captions, line up with the video (see timing.py)."""
    from sqlmodel import Session, col, select

    from . import timing
    from .db import Vod, get_engine
    from .models import Transcript
    from .workspace import Workspace, read_json, write_text

    settings = get_settings()
    with Session(get_engine(settings)) as s:
        query = select(Vod).where(col(Vod.workspace).is_not(None), Vod.status == "ready").order_by(col(Vod.id).desc())
        vods = s.exec(query.where(col(Vod.id).in_(vod_ids)) if vod_ids else query.limit(5)).all()
        for vod in vods:
            typer.echo(f"{vod.id}  {vod.title[:80]}")
            ws = Workspace(settings.vods_dir / vod.workspace)
            if not ws.transcript_json.exists():
                typer.echo("    No transcript: it was posted whole.")
                continue
            if vod.cleaned_at or not ws.source.exists():
                typer.echo("    Its video file is deleted, so it can't be checked.")
                continue
            transcript = Transcript.model_validate_json(ws.transcript_json.read_text(encoding="utf-8"))
            found = timing.measure(ws.source)
            if not found.jumps:
                typer.echo("    Lines up with the video.")
                continue
            old = found.made_the_old_way(transcript)
            if old is None:
                typer.echo("    Its sound jumps, but whether its transcript allows for that can't be told; left as it is.")
                continue
            if not old:
                typer.echo("    Lines up with the video (its transcript allows for where its sound jumps).")
                continue
            for jump in found.jumps[:8]:
                typer.echo(f"    From {to_timecode(found.video_time(jump.at))}, words are timed "
                           f"{abs(jump.shift):.1f} s {'early' if jump.shift > 0 else 'late'}")
            if len(found.jumps) > 8:
                typer.echo(f"    …and it jumps {len(found.jumps) - 8} more times")
            if not fix:
                typer.echo(f"    Put it right with: clipper timing-check {vod.id} --fix")
                continue

            write_text(ws.transcript_json, found.fix(transcript).model_dump_json())
            ws.audio_wav.unlink(missing_ok=True)  # the old soundtrack; made again with the right timing if needed
            picked = {c["id"]: (c["start"], c["end"]) for c in read_json(ws.segments_json)} if ws.segments_json.exists() else {}
            clips = timing.fix_clips(s, vod.id, found, picked)
            typer.secho("    Fixed its transcript. Clips you trimmed by hand keep their cut points.", fg="green")
            for heading, listed in (
                ("Moved to where the AI meant them", clips.moved),
                ("Rendering again, with captions in time", clips.rendering),
                ("Their queued uploads were taken out of the queue; upload them again once they're rendered", clips.unqueued),
                ("Uploading right now, so left as they are", clips.uploading),
            ):
                if listed:
                    typer.echo(f"    {heading}:")
                    for clip in listed:
                        typer.echo(f"      {clip.key}  {clip.title[:70]}")
        if not vods:
            typer.echo("No finished videos" + (" with those ids." if vod_ids else "."))


@app.command("save-transcripts")
def save_transcripts() -> None:
    """Save the transcripts of past streams, of every show with Save transcripts on, as new ones are (see transcripts.py)."""
    from sqlmodel import Session, col, select

    from . import transcripts
    from .db import Show, Vod, get_engine
    from .models import Transcript
    from .workspace import Workspace, safe_id

    settings = get_settings()
    with Session(get_engine(settings)) as s:
        shows = s.exec(select(Show).where(col(Show.save_transcripts).is_(True)).order_by(col(Show.id))).all()
        for show in shows:
            saved = 0
            for vod in s.exec(select(Vod).where(Vod.show_id == show.id, col(Vod.workspace).is_not(None))):
                ws = Workspace(settings.vods_dir / vod.workspace)
                if ws.transcript_json.exists() and ws.meta_json.exists():  # posted whole, or not transcribed yet
                    transcript = Transcript.model_validate_json(ws.transcript_json.read_text(encoding="utf-8"))
                    transcripts.save(settings, show, vod, ws, transcript)
                    saved += 1
            typer.echo(f"{show.name}: {saved} transcript{'' if saved == 1 else 's'} in "
                       f"{settings.transcripts_dir / safe_id(show.name)}")
        if not shows:
            typer.echo("No show has Save transcripts on (under the show, on the Shows screen).")


@app.command()
def breakdown(
    vod_ids: Annotated[Optional[list[int]], typer.Argument(help="Video ids, as this prints them. Default: every stream of the shows with breakdown categories that has none yet.")] = None,
    again: Annotated[bool, typer.Option("--again", help="Also do the streams that have one already, e.g. after changing the categories.")] = False,
    imported: Annotated[bool, typer.Option("--imported", help="Instead, the transcripts added to the shows' folders from elsewhere, e.g. an archive of past streams.")] = False,
) -> None:
    """Ask the AI what past streams' time went on, from their saved transcripts, and save it with them (see transcripts.py).

    Nothing is downloaded or transcribed again, and the streams' clips stay as they are. Each stream is one AI request
    over its whole transcript.
    """
    from sqlmodel import Session, col, select

    from . import transcripts
    from .db import Show, Vod, get_engine
    from .models import Transcript
    from .workspace import Workspace

    settings = get_settings()
    with Session(get_engine(settings)) as s:
        shows = {show.id: show for show in s.exec(select(Show))}
        query = select(Vod).where(col(Vod.workspace).is_not(None), col(Vod.show_id).is_not(None)).order_by(col(Vod.id))
        vods = s.exec(query.where(col(Vod.id).in_(vod_ids)) if vod_ids and not imported else query).all()
    if imported:
        _break_down_imported(settings, list(shows.values()), {vod.workspace for vod in vods}, again)
        return
    todo = []
    for vod in vods:
        show = shows[vod.show_id]
        ws = Workspace(settings.vods_dir / vod.workspace)
        if not show.breakdown_categories():
            why_not = "Its show has no breakdown categories, or doesn't save transcripts."
        elif not ws.transcript_json.exists():
            why_not = "No transcript: it was posted whole, or isn't transcribed yet."
        elif vod_ids or again or not transcripts.parts(ws):
            todo.append((vod, show, ws))
            continue
        else:
            continue  # has one already
        if vod_ids:  # asked for by its id, so say why not
            typer.echo(f"{vod.id}  {vod.title[:80]}\n    {why_not}")
    for i, (vod, show, ws) in enumerate(todo):
        typer.echo(f"{vod.id}  {vod.title[:80]}")
        transcript = Transcript.model_validate_json(ws.transcript_json.read_text(encoding="utf-8"))
        try:
            parts = segment.break_down(ws, settings, transcript, show.breakdown_categories(), context=show.notes)
        except UsageLimitError as e:
            when = f" It resets at {e.resets_at:%Y-%m-%d %H:%M %Z}." if e.resets_at else ""
            rest = " ".join(str(v.id) for v, _, _ in todo[i:])
            typer.secho(f"    {e.note}{when} Carry on after that with: clipper breakdown {rest}", fg="yellow", err=True)
            raise typer.Exit(2)
        except LLMError as e:
            typer.secho(f"    {e}", fg="red", err=True)
            continue
        transcripts.save(settings, show, vod, ws, transcript)
        typer.secho(f"    {len(parts)} parts, saved with its transcript", fg="green")
    if not todo:
        typer.echo("Nothing to break down." + ("" if vod_ids or again else " To do ones that have a breakdown again, add --again."))


def _break_down_imported(settings, shows, made_here: set[str], again: bool) -> None:
    """`breakdown --imported`: the transcripts in the shows' folders that came from elsewhere."""
    from . import transcripts

    todo = []
    for show in shows:
        if show.breakdown_categories():
            for path in transcripts.from_elsewhere(settings, show, made_here):
                if again or not transcripts.read(path)[0].get("breakdown"):
                    todo.append((show, path))
    for i, (show, path) in enumerate(todo, 1):
        data, transcript = transcripts.read(path)
        typer.echo(f"{i}/{len(todo)}  {data.get('date')}  {(data.get('title') or path.stem)[:80]}")
        try:
            answer = segment.ask_breakdown(settings, transcripts.meta(data), transcript, show.breakdown_categories(),
                                           context=show.notes)
        except UsageLimitError as e:
            when = f" It resets at {e.resets_at:%Y-%m-%d %H:%M %Z}." if e.resets_at else ""
            typer.secho(f"    {e.note}{when} Run `clipper breakdown --imported` again after that: it carries on with the "
                        f"{len(todo) - i + 1} left.", fg="yellow", err=True)
            raise typer.Exit(2)
        except LLMError as e:
            typer.secho(f"    {e}", fg="red", err=True)
            continue
        transcripts.add_breakdown(path, data, show, answer, transcript.duration)
        typer.secho(f"    {len(answer.breakdown)} parts", fg="green")
    if not todo:
        typer.echo("Nothing to break down: no transcripts from elsewhere without a breakdown, in the folders of the shows "
                   "with breakdown categories." + ("" if again else " To do them all again, add --again."))


HOLD_FOR = timedelta(minutes=15)  # how long a deploy's hold on new uploads lasts unless it's renewed or ended


@app.command("deploy-wait")
def deploy_wait(
    max_hours: Annotated[float, typer.Option(help="Give up waiting after this long.")] = 12,
    every: Annotated[float, typer.Option(help="Seconds between checks.")] = 30,
) -> None:
    """Before a deploy restarts the worker: hold new uploads, and wait for one going up through the upload browser
    (YouTube Studio or Rumble) to finish, since those can't carry on after a restart and would need checking by hand.
    Everything else picks up by itself: videos and renders start their step again, and uploads through the YouTube
    API carry on where they were. The hold lapses by itself 15 minutes after the last check, so a deploy that stops
    partway can't leave uploads held; deploy-done ends it."""
    from sqlmodel import Session, col, select

    from .db import DEPLOY_HOLD, UploadJob, get_engine, set_app_setting, utcnow

    engine = get_engine(get_settings())
    deadline = time.monotonic() + max_hours * 3600
    while True:
        with Session(engine) as s:
            set_app_setting(s, DEPLOY_HOLD, (utcnow() + HOLD_FOR).isoformat())
            s.commit()
            going = s.exec(select(UploadJob).where(
                UploadJob.status == "uploading", col(UploadJob.method).not_in(("api",)),
            )).all()
        if not going:
            typer.echo("No upload is going through the upload browser: the worker can restart. New uploads are held.")
            return
        if time.monotonic() > deadline:
            _end_hold(engine)
            typer.echo(f"Still uploading after {max_hours:g} hours; not restarting the worker under it.", err=True)
            raise typer.Exit(1)
        jobs = ", ".join(f"{j.site} upload {j.id} at {round(j.progress * 100)}%" for j in going)
        typer.echo(f"Waiting for {jobs} to finish (new uploads are held meanwhile)")
        time.sleep(every)


@app.command("deploy-done")
def deploy_done() -> None:
    """Let uploads start again after a deploy (see deploy-wait)."""
    from .db import get_engine

    _end_hold(get_engine(get_settings()))
    typer.echo("Uploads can start again.")


def _end_hold(engine) -> None:
    from sqlmodel import Session

    from .db import DEPLOY_HOLD, set_app_setting

    with Session(engine) as s:
        set_app_setting(s, DEPLOY_HOLD, "")
        s.commit()


@app.command()
def doctor() -> None:
    """Check the setup: GPU video encoding, GPU for Whisper, the AI's sign-in and the data folder."""
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
    if settings.hwaccel == "cuda":
        # Optional: without it, previews still work, shrinking each frame on the CPU.
        gpu = render.gpu_proxy(settings)
        typer.echo(f"{'OK  ' if gpu else 'INFO'}  previews made entirely on the GPU: "
                   + ("yes" if gpu else "no, this ffmpeg has no scale_cuda filter, so previews shrink on the CPU (slower)"))

    try:
        import ctranslate2

        devices = ctranslate2.get_cuda_device_count()
        report("GPU visible to Whisper", devices > 0 or settings.whisper_device != "cuda", f"{devices} CUDA device(s)")
    except ImportError:
        report("GPU visible to Whisper", False, "faster-whisper isn't installed (pip install '.[transcribe]')")

    from sqlmodel import Session

    from . import ai
    from .db import get_engine

    with Session(get_engine(settings)) as s:
        choice = ai.chosen(s, settings)
    problem = ai.problem(settings, choice)
    report(f"the AI ({ai.PROVIDERS[choice.provider]}, {choice.model or 'its default model'}) set up", not problem, problem or "ready")

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
