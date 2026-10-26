"""Thumbnails for landscape videos: Claude comes up with the idea and writes the image prompt from what's said
in the video, then an image model on OpenRouter draws it. Frames of the video go along as references, so the
people in it look like themselves.

Claude Code only writes text, so the drawing is done on OpenRouter (CLIPPER_OPENROUTER_API_KEY, with the
model in CLIPPER_THUMBNAIL_MODEL). Each picture costs a few cents there, on top of a small Claude call.

Shorts don't get one: YouTube doesn't show custom thumbnails in the Shorts feed.
"""

import base64
import logging
import tempfile
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

from . import ffmpeg
from .config import Settings
from .db import ClipRecord
from .llm import ClaudeCode
from .models import Transcript
from .timecode import to_timecode
from .workspace import Workspace

log = logging.getLogger(__name__)

API_URL = "https://openrouter.ai/api/v1/images"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
WIDTH, HEIGHT = 1280, 720  # YouTube's size for thumbnails, which may be up to 2 MB
FRAMES = 4  # from the video, for the image model to draw the people from
MAX_TRANSCRIPT_CHARS = 12_000
TIMEOUT_S = 300


class ThumbnailError(RuntimeError):
    pass


class Idea(BaseModel):
    people: list[str] = Field(description="Who the thumbnail shows, by name: only people seen or heard in the video")
    concept: str = Field(description="The joke or idea, in one sentence")
    text: str = Field(description="The words on the thumbnail (at most 4), or empty for none")
    prompt: str = Field(description="The complete prompt for the image model")


def make(settings: Settings, clip: ClipRecord, ws: Workspace, notes: str = "", http: httpx.Client | None = None) -> tuple[Path, Idea]:
    """Make the clip's thumbnail: a JPEG next to its renders, and Claude's idea for it."""
    if not settings.openrouter_api_key:
        raise ThumbnailError("Thumbnails need an OpenRouter key: set CLIPPER_OPENROUTER_API_KEY (see the README).")
    if not ws.meta_json.exists() or not ws.source.exists():
        raise ThumbnailError("The stream's video files were deleted, so there are no frames to draw the people from.")
    idea = write_idea(settings, clip, excerpt(ws, clip), notes)
    with tempfile.TemporaryDirectory(prefix="clipper-thumb-") as tmp:
        refs = frames(ws.source, clip.start, clip.end, Path(tmp))
        image = draw(settings, idea.prompt, refs, http)
        out = ws.clips_dir / f"{clip.key}-thumbnail.jpg"
        save(image, out, Path(tmp))
    return out, idea


def write_idea(settings: Settings, clip: ClipRecord, transcript: str, notes: str = "") -> Idea:
    kind = {"chapter": "chapter of a stream", "highlight": "short clip", "whole": "full episode"}[clip.kind]
    lines = ["# The video", f"Title: {clip.title}", f"What it is: {kind}, {to_timecode(clip.end - clip.start)} long"]
    if clip.description.strip():
        lines += ["", "# Its description", clip.description.strip()[:3000]]
    if notes.strip():
        lines += ["", "# About the show", notes.strip()]
    if clip.thumbnail_hint.strip():
        lines += ["", "# The channel owner's idea for this thumbnail (build on it)", clip.thumbnail_hint.strip()]
    if transcript:
        lines += ["", "# What's said in it", transcript]
    llm = ClaudeCode(bin=settings.claude_bin, model=settings.llm_model, effort="medium", timeout_s=600)
    return llm.structured(
        system_prompt=(PROMPTS_DIR / "thumbnail.md").read_text(encoding="utf-8"),
        task=f"Come up with this video's thumbnail and write the image prompt. {FRAMES} frames of the video go to "
             "the image model with it. The video's details follow.",
        stdin_text="\n".join(lines) + "\n",
        schema=Idea,
    )


def excerpt(ws: Workspace, clip: ClipRecord) -> str:
    """The clip's transcript lines, thinned out evenly if there are too many. None for a full episode: its
    description already sums it up."""
    if clip.kind == "whole" or not ws.transcript_json.exists():
        return ""
    transcript = Transcript.model_validate_json(ws.transcript_json.read_text(encoding="utf-8"))
    lines = [f"[{to_timecode(seg.start - clip.start)}] {seg.text.strip()}"
             for seg in transcript.segments if seg.text.strip() and clip.start <= seg.start < clip.end]
    total = sum(len(line) + 1 for line in lines)
    if total > MAX_TRANSCRIPT_CHARS:
        step = total / MAX_TRANSCRIPT_CHARS
        lines = [lines[int(i * step)] for i in range(int(len(lines) / step))]
    return "\n".join(lines)


def frames(source: Path, start: float, end: float, out_dir: Path, count: int = FRAMES) -> list[Path]:
    """Stills spread through the clip, staying clear of its first and last moments."""
    paths = []
    for i in range(count):
        t = start + (end - start) * (i + 0.5) / count
        path = out_dir / f"frame-{i}.jpg"
        ffmpeg.run(["-ss", f"{t:.3f}", "-i", str(source), "-frames:v", "1", "-vf", "scale=1024:-2", "-q:v", "3", str(path)])
        paths.append(path)
    return paths


def draw(settings: Settings, prompt: str, refs: list[Path], http: httpx.Client | None = None) -> bytes:
    """Have the image model draw the prompt, with the reference frames. Returns the image file's bytes."""
    body = {
        "model": settings.thumbnail_model,
        "prompt": prompt,
        "aspect_ratio": "16:9",
        "n": 1,
        "input_references": [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode()}}
            for p in refs
        ],
    }
    log.info("Drawing a thumbnail with %s", settings.thumbnail_model)
    client = http or httpx.Client(timeout=TIMEOUT_S)
    try:
        res = client.post(API_URL, json=body, headers={"Authorization": f"Bearer {settings.openrouter_api_key}"})
    except httpx.HTTPError as e:
        raise ThumbnailError(f"Couldn't reach OpenRouter ({e}). Try again.") from e
    try:
        answer = res.json()
    except ValueError:
        answer = {}
    if res.status_code >= 400:
        error = answer.get("error") if isinstance(answer, dict) else None
        message = error.get("message") if isinstance(error, dict) else res.reason_phrase
        raise ThumbnailError(f"OpenRouter refused it ({res.status_code}): {message}"[:500])
    images = [d for d in answer.get("data") or [] if isinstance(d, dict) and d.get("b64_json")]
    if not images:
        raise ThumbnailError("The image model sent no picture back; it may have turned the idea down. Try again, "
                             "or give it an idea of your own.")
    return base64.b64decode(images[0]["b64_json"])


def save(image: bytes, out: Path, tmp_dir: Path) -> None:
    """Fill YouTube's 1280x720 (cropping any edge that doesn't fit) as a JPEG well under its 2 MB limit."""
    drawn = tmp_dir / "drawn.img"
    drawn.write_bytes(image)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".tmp.jpg")
    ffmpeg.run([
        "-i", str(drawn), "-frames:v", "1",
        "-vf", f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT}",
        "-q:v", "3", str(tmp),
    ])
    tmp.replace(out)
