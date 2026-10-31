"""Transcripts saved as files of their own, for use outside the app, for shows with Save transcripts on.

Each stream gets <data>/transcripts/<show>/<date>_<video>.json, with the stream's details, its lines and, for a show
with breakdown categories, what the AI found its time goes on, and a .txt beside it with the lines to read. The
.json is written last and whole, so something watching the folder for new ones never reads one half-written.
"""

import json
import re
from pathlib import Path

from . import titles
from .config import Settings
from .db import Show, Vod
from .models import Breakdown, LLMBreakdownPart, SegmentationWithBreakdown, Transcript
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
    write_text(path.with_suffix(".txt"), "".join(f"[{to_timecode(s.start)}] {s.text}\n" for s in transcript.segments))
    # Without spaces or line breaks: the word timings of a long stream run to megabytes.
    write_text(path, json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    return path


def parts(ws: Workspace) -> list[LLMBreakdownPart]:
    """The stream's breakdown as the AI gave it: the one asked for on its own if there is one, else the one that came
    with its clips. Empty if there's neither."""
    if ws.breakdown_json.exists():
        return Breakdown.model_validate_json(ws.breakdown_json.read_text(encoding="utf-8")).breakdown
    if ws.segmentation_json.exists():
        return SegmentationWithBreakdown.model_validate_json(ws.segmentation_json.read_text(encoding="utf-8")).breakdown
    return []


def breakdown(ws: Workspace, categories: dict[str, str], duration: float) -> list[dict]:
    """The AI's breakdown of the stream in seconds, each part under one of `categories` or Other. Empty for a stream
    whose clips were picked before the show had categories, until `clipper breakdown` is run for it."""
    names = {_key(name): name for name in categories}
    found = []
    for part in parts(ws):
        try:
            start, end = max(0.0, to_seconds(part.start)), min(duration, to_seconds(part.end))
        except ValueError:
            continue  # not a timecode: left out rather than guessed at
        if end > start:
            found.append({"start": start, "end": end, "category": names.get(_key(part.category), OTHER), "note": part.note})
    return found


def _key(name: str) -> str:
    """A category's name for matching: "Dead-air", "dead air" and "Dead_Air" are the same one."""
    return re.sub(r"[\s_-]+", " ", name).strip().lower()
