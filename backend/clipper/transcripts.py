"""Transcripts saved as files of their own, for use outside the app, for shows with Save transcripts on.

Each stream gets <data>/transcripts/<show>/<date>_<video>.json, with the stream's details, its lines and, for a show
with breakdown categories, what the AI found its time goes on, and a .txt beside it with the lines to read. The
.json is written last and whole, so something watching the folder for new ones never reads one half-written.
"""

from pathlib import Path

from . import titles
from .config import Settings
from .db import Show, Vod
from .models import SegmentationWithBreakdown, Transcript
from .timecode import to_seconds, to_timecode
from .workspace import Workspace, safe_id, write_json, write_text

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
        # Stretches with nobody talking have no lines, so the gaps between them are the stream's silences.
        "segments": [{"start": s.start, "end": s.end, "text": s.text} for s in transcript.segments],
    }
    if categories := show.breakdown_categories():
        data["categories"] = categories
        data["breakdown"] = breakdown(ws, categories, transcript.duration)
    write_text(path.with_suffix(".txt"), "".join(f"[{to_timecode(s.start)}] {s.text}\n" for s in transcript.segments))
    write_json(path, data)
    return path


def breakdown(ws: Workspace, categories: dict[str, str], duration: float) -> list[dict]:
    """The AI's breakdown of the stream in seconds, each part under one of `categories` or Other. Empty for a stream
    whose clips were picked before the show had categories."""
    if not ws.segmentation_json.exists():
        return []
    answer = SegmentationWithBreakdown.model_validate_json(ws.segmentation_json.read_text(encoding="utf-8"))
    names = {name.lower(): name for name in categories}
    parts = []
    for part in answer.breakdown:
        try:
            start, end = max(0.0, to_seconds(part.start)), min(duration, to_seconds(part.end))
        except ValueError:
            continue  # not a timecode: left out rather than guessed at
        if end > start:
            category = names.get(part.category.strip().lower(), OTHER)
            parts.append({"start": start, "end": end, "category": category, "note": part.note})
    return parts
