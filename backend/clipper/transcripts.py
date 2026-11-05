"""Transcripts saved as files of their own, for use outside the app, for shows with Save transcripts on.

Each stream gets <data>/transcripts/<show>/<date>_<video>.json, with the stream's details, its lines and, for a show
with breakdown categories, what the AI found its time goes on, and a .txt beside it with the lines to read. The
.json is written last and whole, so something watching the folder for new ones never reads one half-written.

Files in the same form can also be added to a show's folder from elsewhere, such as an archive of the streamer's
past streams; `clipper breakdown --imported` breaks those down too (from_elsewhere, add_breakdown).
"""

import json
import re
from pathlib import Path

from . import titles
from .config import Settings
from .db import Show, Vod
from .models import Breakdown, LLMBreakdownPart, SegmentationWithBreakdown, Transcript, TranscriptSegment
from .timecode import to_seconds, to_timecode
from .workspace import Workspace, safe_id, write_text

OTHER = "Other"  # the breakdown's category for anything the show's don't cover


def save(settings: Settings, show: Show, vod: Vod, ws: Workspace, transcript: Transcript) -> Path:
    """Save the stream's transcript in the show's folder, replacing one saved before; returns the .json's path."""
    meta = ws.meta()
    day = titles.stream_date(meta, vod.created_at.date(), settings.timezone, settings.day_starts_at)
    path = settings.transcripts_dir / safe_id(show.name) / f"{day.isoformat()}_{ws.root.name}.json"
    data = {
        "id": ws.root.name,
        "show": show.name,
        "title": meta.get("title") or vod.title,
        "url": vod.url,
        "channel": meta.get("channel") or meta.get("uploader") or vod.channel,
        "platform": meta.get("extractor_key") or vod.platform,
        "date": day.isoformat(),  # the day the stream started where its creators are (see titles.stream_date)
        "duration": transcript.duration,
        "language": transcript.language,
        # Stretches with nobody talking have no lines, so the gaps between them are the stream's silences. A line can
        # hold a pause too, so each has its words' [start, end] as well.
        "segments": [{"start": s.start, "end": s.end, "text": s.text, "words": [[w.start, w.end] for w in s.words]}
                     for s in transcript.segments],
    }
    if categories := show.breakdown_categories():
        data["categories"] = categories
        data["breakdown"] = breakdown(ws, categories, transcript.duration)
        data["other_summary"] = answer(ws).other_summary  # what the parts under Other were mostly about
    write_text(path.with_suffix(".txt"), "".join(f"[{to_timecode(s.start)}] {s.text}\n" for s in transcript.segments))
    # Without spaces or line breaks: the word timings of a long stream run to megabytes.
    write_text(path, json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    return path


def answer(ws: Workspace) -> Breakdown:
    """The stream's breakdown as the AI gave it: the one asked for on its own if there is one, else the one that came
    with its clips. Empty if there's neither."""
    if ws.breakdown_json.exists():
        return Breakdown.model_validate_json(ws.breakdown_json.read_text(encoding="utf-8"))
    if ws.segmentation_json.exists():
        found = SegmentationWithBreakdown.model_validate_json(ws.segmentation_json.read_text(encoding="utf-8"))
        return Breakdown(breakdown=found.breakdown, other_summary=found.other_summary)
    return Breakdown(breakdown=[])


def parts(ws: Workspace) -> list[LLMBreakdownPart]:
    return answer(ws).breakdown


def breakdown(ws: Workspace, categories: dict[str, str], duration: float) -> list[dict]:
    """The AI's breakdown of the stream in seconds, each part under one of `categories` or Other. Empty for a stream
    whose clips were picked before the show had categories, until `clipper breakdown` is run for it."""
    return in_seconds(parts(ws), categories, duration)


def from_elsewhere(settings: Settings, show: Show, made_here: set[str]) -> list[Path]:
    """The transcripts in the show's folder that the app didn't make: ones whose name (<date>_<id>.json) isn't for
    one of `made_here`, the app's own streams' folders."""
    folder = settings.transcripts_dir / safe_id(show.name)
    return [path for path in sorted(folder.glob("*.json")) if path.stem.partition("_")[2] not in made_here]


def read(path: Path) -> tuple[dict, Transcript]:
    """A saved transcript: the file as it is, and its lines as a Transcript."""
    data = json.loads(path.read_text(encoding="utf-8"))
    segments = [TranscriptSegment(start=s["start"], end=s["end"], text=s["text"]) for s in data["segments"]]
    duration = float(data.get("duration") or (segments[-1].end if segments else 0))
    return data, Transcript(language=data.get("language"), duration=duration, segments=segments)


def meta(data: dict) -> dict:
    """A saved transcript's details as yt-dlp gives them for the app's own streams, for the AI's input."""
    return {"title": data.get("title"), "channel": data.get("channel"), "extractor_key": data.get("platform"),
            "upload_date": (data.get("date") or "").replace("-", "")}


def add_breakdown(path: Path, data: dict, show: Show, answer: Breakdown, duration: float) -> None:
    """Put the AI's breakdown into a saved transcript from elsewhere, under the show's categories."""
    categories = show.breakdown_categories()
    data.update(categories=categories, breakdown=in_seconds(answer.breakdown, categories, duration),
                other_summary=answer.other_summary)
    write_text(path, json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def clear(ws: Workspace) -> None:
    """Set aside one of the app's own streams' breakdown, so `clipper breakdown` does it again from scratch: an empty one
    asked for on its own stands in for it. The one that came with its clips stays in its segmentation.json."""
    write_text(ws.breakdown_json, Breakdown(breakdown=[]).model_dump_json(indent=2))


def clear_imported(path: Path, data: dict) -> None:
    """Take the AI's breakdown out of a saved transcript from elsewhere, so it's broken down again from scratch."""
    for key in ("categories", "breakdown", "other_summary"):
        data.pop(key, None)
    write_text(path, json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def in_seconds(found: list[LLMBreakdownPart], categories: dict[str, str], duration: float) -> list[dict]:
    """The AI's parts in seconds, each under one of `categories` or Other."""
    names = {_key(name): name for name in categories}
    out = []
    for part in found:
        try:
            start, end = max(0.0, to_seconds(part.start)), min(duration, to_seconds(part.end))
        except ValueError:
            continue  # not a timecode: left out rather than guessed at
        if end > start:
            out.append({"start": start, "end": end, "category": names.get(_key(part.category), OTHER), "note": part.note})
    return out


def _key(name: str) -> str:
    """A category's name for matching: "Dead-air", "dead air" and "Dead_Air" are the same one."""
    return re.sub(r"[\s_-]+", " ", name).strip().lower()
