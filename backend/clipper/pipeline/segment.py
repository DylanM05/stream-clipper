"""Ask the AI to split the stream into chapters and pick highlights, in one call over the whole transcript.

One call instead of two halves how much of your plan (or OpenRouter credit) each VOD uses: the transcript is
most of the input, and it only has to be sent once. The app calls chapters "clips". For the same reason, a show's
breakdown of what the stream's time goes on (Show.breakdown) is asked for in the same call.
"""

import logging
from pathlib import Path

from ..config import Settings
from .. import ai
from ..models import Segmentation, SegmentationWithBreakdown, Signals, Transcript
from ..timecode import to_timecode
from ..workspace import Workspace, write_text

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def run(
    ws: Workspace,
    settings: Settings,
    transcript: Transcript,
    signals: Signals,
    context: str = "",
    guidance: str = "",
    title_room: int = 100,
    title_prefixed: bool = False,
    episode_room: int | None = None,
    track_record: str = "",
    breakdown: dict[str, str] | None = None,
) -> Segmentation:
    """Ask the AI for chapters, highlights and an episode title.

    `context` is about the show and `guidance` is what its chapters and highlights should be.
    `title_room` and `episode_room` are how long clip and episode titles may be; `title_prefixed`
    means the show name and date get added to them. `track_record` is what the show's past clips teach it
    (learning.py). `breakdown` is the categories, by name with what each covers, that it also sorts the stream's
    time into; the answer is then a SegmentationWithBreakdown.
    """
    if ws.segmentation_json.exists():
        return Segmentation.model_validate_json(ws.segmentation_json.read_text(encoding="utf-8"))

    max_highlights = max(3, round(transcript.duration / 3600 * settings.highlights_per_hour))
    title_rule = (
        f"Keep chapter and highlight titles within {title_room} characters and the episode title within "
        f"{episode_room or title_room}"
    )
    if title_prefixed:
        title_rule += ", and leave out the show name and date: they're added in front automatically"
    system_prompt = (PROMPTS_DIR / "segment.md").read_text(encoding="utf-8")
    task = f"Split this {to_timecode(transcript.duration)} stream into chapters and pick up to {max_highlights} highlights"
    schema = Segmentation
    if breakdown:
        system_prompt += (PROMPTS_DIR / "breakdown.md").read_text(encoding="utf-8")
        task += ", and break down what its time goes on"
        schema = SegmentationWithBreakdown
    llm = ai.model(settings, effort=settings.llm_effort, timeout_s=settings.llm_timeout_s)
    result = llm.structured(
        system_prompt=system_prompt,
        task=f"{task}. {title_rule}. The stream details, hints and transcript follow.",
        stdin_text=build_input(ws.meta(), transcript, signals, context, guidance, track_record, breakdown),
        schema=schema,
        raw_log=ws.llm_dir / "segmentation.response.json",
    )
    write_text(ws.segmentation_json, result.model_dump_json(indent=2))
    log.info("%s proposed %d chapters and %d highlights", llm.name, len(result.chapters), len(result.highlights))
    return result


def build_input(meta: dict, transcript: Transcript, signals: Signals, context: str = "", guidance: str = "",
                track_record: str = "", breakdown: dict[str, str] | None = None) -> str:
    tc = to_timecode
    lines = [
        "# Stream",
        f"Title: {meta.get('title') or 'unknown'}",
        f"Channel: {meta.get('channel') or meta.get('uploader') or 'unknown'}",
        f"Platform: {meta.get('extractor_key') or 'unknown'}",
    ]
    if date := meta.get("upload_date"):
        lines.append(f"Date: {date[:4]}-{date[4:6]}-{date[6:]}")
    lines.append(f"Length: {tc(transcript.duration)}")
    if description := (meta.get("description") or "").strip():
        lines += ["", "# Stream description", description[:1000]]
    if context.strip():
        lines += ["", "# About the show", context.strip()]
    if guidance.strip():
        # The app calls chapters "clips", so the show's guidance may too.
        lines += ["", '# What the chapters and highlights should be (chapters may be called "clips" here)', guidance.strip()]
    if breakdown:
        lines += ["", "# Breakdown categories"]
        lines += [f"- {name}: {covers}" if covers else f"- {name}" for name, covers in breakdown.items()]
    if track_record.strip():
        lines += ["", track_record.strip()]
    if signals.platform_chapters:
        lines += ["", "# Platform chapters (game or category changes)"]
        lines += [f"[{tc(c.start)}] {c.title}" for c in signals.platform_chapters]
    if signals.long_silences:
        lines += ["", "# Long silences (possible breaks)"]
        lines.append(", ".join(f"{tc(s.start)}-{tc(s.end)}" for s in signals.long_silences))
    if signals.loud_moments:
        lines += ["", "# Loud moments (shouting, laughter or hype; check the transcript)"]
        lines.append(", ".join(f"{tc(m.t)} (+{m.db_above:.0f} dB)" for m in signals.loud_moments))
    if signals.scene_cuts:
        lines += ["", "# Big visual changes (scene switches, BRB screens, game changes)"]
        lines.append(", ".join(tc(c.t) for c in signals.scene_cuts))
    lines += ["", "# Transcript", *transcript_lines(transcript)]
    return "\n".join(lines) + "\n"


def transcript_lines(transcript: Transcript) -> list[str]:
    return [f"[{to_timecode(s.start)}] {s.text}" for s in transcript.segments if s.text]
