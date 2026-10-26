"""Thumbnails for landscape videos: Claude comes up with the idea and writes the image prompt from what's said
in the video, then an image model on OpenRouter draws it. Frames of the video go along as references, so the
people in it look like themselves.

Public figures the video talks about can be in it too. Claude names them, and a photo of each is looked up
(their Wikipedia picture, else the picture of the YouTube channel a search finds first) and sent along as
another reference, so the image model knows what they look like. Without a photo they're drawn from Claude's
description.

Claude Code only writes text, so the drawing is done on OpenRouter (CLIPPER_OPENROUTER_API_KEY, with the
model in CLIPPER_THUMBNAIL_MODEL). Each picture costs a few cents there, on top of a small Claude call.

Shorts don't get one: YouTube doesn't show custom thumbnails in the Shorts feed.
"""

import base64
import logging
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
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
WIKIPEDIA_URL = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "StreamClipper/1.0 (thumbnail reference photos)"  # Wikimedia asks every client to say what it is
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
WIDTH, HEIGHT = 1280, 720  # YouTube's size for thumbnails, which may be up to 2 MB
FRAMES = 4  # from the video, for the image model to draw the people from; fewer when photos go along too
MIN_FRAMES = 2
MAX_LOOKUPS = 3  # people from outside the video looked up per thumbnail
MAX_PHOTO_BYTES = 10_000_000
MAX_TRANSCRIPT_CHARS = 12_000
TIMEOUT_S = 300

# The picture of the YouTube channel a search finds first for a name, as (channel name, picture link), or None.
ChannelPicture = Callable[[str], tuple[str, str] | None]


class ThumbnailError(RuntimeError):
    pass


class Person(BaseModel):
    name: str = Field(description="As the video says it, e.g. a nickname or channel name")
    full_name: str = Field(description="Their real or best-known full name if you know it, else the same as name")
    in_video: bool = Field(description="True if they're on the stream (seen or heard), false if it only talks about them")
    looks: str = Field(description="What they look like, if you know: hair, facial hair, glasses, build. Empty if not")


class Idea(BaseModel):
    people: list[Person] = Field(description="Everyone the thumbnail shows")
    concept: str = Field(description="The joke or idea, in one sentence")
    text: str = Field(description="The words on the thumbnail (at most 4), or empty for none")
    prompt: str = Field(description="The complete prompt for the image model, naming each person")


@dataclass
class Photo:
    """A reference photo of someone the video talks about."""

    person: Person
    path: Path
    source: str  # where it came from, e.g. "Wikipedia"


def make(settings: Settings, clip: ClipRecord, ws: Workspace, notes: str = "", http: httpx.Client | None = None,
         channel_picture: ChannelPicture | None = None) -> tuple[Path, Idea, str]:
    """Make the clip's thumbnail: a JPEG next to its renders, Claude's idea for it, and who's in it and where
    their likeness came from."""
    if not settings.openrouter_api_key:
        raise ThumbnailError("Thumbnails need an OpenRouter key: set CLIPPER_OPENROUTER_API_KEY (see the README).")
    if not ws.meta_json.exists() or not ws.source.exists():
        raise ThumbnailError("The stream's video files were deleted, so there are no frames to draw the people from.")
    idea = write_idea(settings, clip, excerpt(ws, clip), notes)
    http = http or httpx.Client(timeout=TIMEOUT_S, follow_redirects=True)
    with tempfile.TemporaryDirectory(prefix="clipper-thumb-") as tmp:
        outside = [p for p in idea.people if not p.in_video][:MAX_LOOKUPS]
        photos = [photo for i, p in enumerate(outside) if (photo := find_photo(p, Path(tmp) / f"person-{i}.jpg", http, channel_picture))]
        refs = frames(ws.source, clip.start, clip.end, Path(tmp), count=max(MIN_FRAMES, FRAMES - len(photos)))
        prompt = idea.prompt + "\n\n" + reference_guide(idea, len(refs), photos)
        image = draw(settings, prompt, refs + [p.path for p in photos], http)
        out = ws.clips_dir / f"{clip.key}-thumbnail.jpg"
        save(image, out, Path(tmp))
    return out, idea, cast_list(idea, photos)


def reference_guide(idea: Idea, frame_count: int, photos: list[Photo]) -> str:
    """Tells the image model which reference image shows whom, since the prompt only names them."""
    lines = [f"Reference images 1 to {frame_count} are frames from the video: the people on the stream should look like "
             "the people in them."]
    for i, photo in enumerate(photos, frame_count + 1):
        lines.append(f"Reference image {i} is a photo of {photo.person.name}: draw them so they're recognisable as that "
                     "person, in the thumbnail's style.")
    pictured = {p.person.name for p in photos}
    for person in idea.people:
        if not person.in_video and person.name not in pictured and person.looks.strip():
            lines.append(f"{person.name} looks like this: {person.looks.strip()}")
    return " ".join(lines)


def cast_list(idea: Idea, photos: list[Photo]) -> str:
    """E.g. "Don (from the video), Ethan Klein (Wikipedia photo), Hasan (from a description)"."""
    sources = {p.person.name: f"{p.source} photo" for p in photos}
    return ", ".join(
        f"{p.name} ({'from the video' if p.in_video else sources.get(p.name, 'from a description')})" for p in idea.people
    )


def find_photo(person: Person, out: Path, http: httpx.Client, channel_picture: ChannelPicture | None = None) -> Photo | None:
    """A photo of someone the video talks about: their Wikipedia picture, else their YouTube channel's picture.
    Only a page or channel whose name matches theirs counts, so a search that finds someone else is ignored."""
    for source, find in (("Wikipedia", lambda: _wikipedia_picture(person, http)), ("YouTube channel", lambda: _channel(person, channel_picture))):
        try:
            url = find()
            if url and _download_image(url, out, http):
                log.info("Found a %s photo of %s", source, person.name)
                return Photo(person, out, source)
        except (httpx.HTTPError, ffmpeg.FFmpegError, ValueError, KeyError) as e:
            log.info("No %s photo of %s: %s", source, person.name, e)
    return None


def _wikipedia_picture(person: Person, http: httpx.Client) -> str:
    """The lead picture of the Wikipedia article a search for them finds first, if it's their article."""
    res = http.get(WIKIPEDIA_URL, headers={"User-Agent": USER_AGENT}, params={
        "action": "query", "format": "json", "formatversion": 2, "generator": "search", "gsrsearch": person.full_name,
        "gsrlimit": 1, "prop": "pageimages", "piprop": "thumbnail", "pithumbsize": 800,
    })
    res.raise_for_status()
    pages = (res.json().get("query") or {}).get("pages") or []
    if not pages or not _same_person(person, pages[0].get("title", "")):
        return ""
    return (pages[0].get("thumbnail") or {}).get("source", "")


def _channel(person: Person, channel_picture: ChannelPicture | None) -> str:
    found = channel_picture(person.name) if channel_picture else None
    return found[1] if found and _same_person(person, found[0]) else ""


def _same_person(person: Person, title: str) -> bool:
    """Whether a page or channel name is theirs: "The Quartering" for TheQuartering, "Ethan Klein TV" for Ethan Klein.
    Short names must match exactly, so "Jer" doesn't find "Jeremy"."""
    found = _squash(title)

    def matches(name: str) -> bool:
        return bool(name and found) and (name == found or (min(len(name), len(found)) >= 6 and (name in found or found in name)))

    return matches(_squash(person.name)) or matches(_squash(person.full_name))


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _download_image(url: str, out: Path, http: httpx.Client) -> bool:
    """Fetch a picture and turn it into a JPEG at most 1024 wide; ffmpeg refuses anything that isn't an image."""
    res = http.get(url, headers={"User-Agent": USER_AGENT})
    res.raise_for_status()
    if not res.headers.get("content-type", "").startswith("image/") or len(res.content) > MAX_PHOTO_BYTES:
        return False
    raw = out.with_suffix(".download")
    raw.write_bytes(res.content)
    ffmpeg.run(["-i", str(raw), "-frames:v", "1", "-vf", "scale='min(1024,iw)':-2", "-q:v", "3", str(out)])
    return out.exists()


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
        raise ThumbnailError("The image model sent no picture back; it may have turned the idea down (some won't draw "
                             "well-known people). Try again, give it an idea of your own, or try another "
                             "CLIPPER_THUMBNAIL_MODEL.")
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
