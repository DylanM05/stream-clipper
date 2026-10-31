"""What a show's past clips teach the AI that picks the next stream's (segment.py): how its uploads did on YouTube
against the show's usual, and how the channel owner changed or turned down the AI's picks, down to the words a moved
start or end took in or left out. With the owner's own notes about what works, it goes into the AI's input for each
new stream, and the Stats screen shows it.

The AI's picks are kept with each clip as first offered (ClipRecord.picked_*); clips from before that get them from
their stream's segments.json, which has kept them all along (backfill_picks).
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from math import inf
from pathlib import Path
from statistics import median

from sqlmodel import Session, col, select

from . import cuts, titles
from .config import Settings
from .db import ClipRecord, Show, UploadJob, Vod
from .models import Transcript, Word
from .stats import compact
from .workspace import Workspace, read_json

log = logging.getLogger(__name__)

MIN_KIND = 4  # uploads of a kind with views before its usual (the median) means anything
MIN_BAND = 2  # uploads in a band of length or score before it's worth a mention
BEST, WORST = 6, 4  # uploads shown as having done best and worst
LATEST = 100  # the newest picks looked at for changes and rejections
EXAMPLES = 6  # changed picks shown with their words
QUOTE_WORDS = 18  # longer quotes keep their start and end
MOVED_S = 0.5  # a start or end moved less than this wasn't changed
APPROVED = ("approved", "rendering", "rendered", "failed", "uploaded")
PLURAL = {"clip": "Clips", "highlight": "Highlights", "Short": "Shorts"}
LENGTHS = {
    "clip": ((600, "under 10 min"), (1200, "10–20 min"), (2400, "20–40 min"), (inf, "over 40 min")),
    "highlight": ((30, "under 30 s"), (60, "30–60 s"), (120, "1–2 min"), (inf, "over 2 min")),
}
SCORES = ((0.9, "0.9 and up"), (0.8, "0.8 to 0.89"), (0.0, "under 0.8"))


def track_record(s: Session, settings: Settings, show: Show) -> str:
    """The show's lessons for the AI, as sections of its input; empty when there's nothing to go on yet."""
    rows = s.exec(
        select(UploadJob, ClipRecord).join(ClipRecord).join(Vod, ClipRecord.vod_id == Vod.id)
        .where(Vod.show_id == show.id, UploadJob.status == "done", ClipRecord.kind != "whole",
               col(UploadJob.views).is_not(None))
    ).all()
    sections = [_how_they_did(rows, show), _changes(s, settings, show)]
    if show.learning_notes.strip():
        sections.append(["# What the channel owner has noticed about what works (follow this over the numbers)",
                         show.learning_notes.strip()])
    return "\n\n".join("\n".join(lines) for lines in sections if lines)


# --- How uploads did -----------------------------------------------------------------


@dataclass
class _Rated:
    clip: ClipRecord
    kind: str  # "clip", "highlight" or "Short"
    views: int
    ratio: float  # against the show's usual for its kind


def _how_they_did(rows: list[tuple[UploadJob, ClipRecord]], show: Show) -> list[str]:
    """Views a week after going up (or a day, where too few have that yet), each against the median of its kind, since
    a Short and a 30-minute clip don't get the same views; then by length, by the AI's score, and the best and worst."""
    kinds: dict[str, list[tuple[UploadJob, ClipRecord]]] = {}
    for job, clip in rows:
        kinds.setdefault(_kind(clip, job.which), []).append((job, clip))
    rated: list[_Rated] = []
    usual: list[str] = []
    for kind, members in kinds.items():
        for field, when in (("views_week1", "a week"), ("views_day1", "a day")):
            have = [(j, c) for j, c in members if getattr(j, field) is not None]
            if len(have) < MIN_KIND:
                continue
            mid = median(getattr(j, field) for j, _ in have)
            usual.append(f"- {PLURAL[kind]}: {compact(mid)} views {when} after going up ({len(have)} uploads)")
            if mid > 0:
                rated += [_Rated(c, kind, getattr(j, field), getattr(j, field) / mid) for j, c in have]
            break
    if not usual:
        return []

    lines = ["# How the show's past uploads did", "", "Their usual views, by kind:", *usual]
    against = []
    for kind in ("clip", "highlight"):
        of_kind = [r for r in rated if (r.kind == "clip") == (kind == "clip")]
        if bands := _bands(of_kind, LENGTHS[kind], lambda r: _length(r.clip)):
            against.append(f"- {PLURAL[kind] if kind == 'clip' else 'Highlights and Shorts'} by length: {bands}")
    if bands := _bands([r for r in rated if not r.clip.manual], SCORES, lambda r: r.clip.score, upward=True):
        against.append(f"- By the score you gave them: {bands}")
    if against:
        lines += ["", "Views against usual (2.0× is twice the usual), with how many uploads:", *against]

    ranked = sorted(rated, key=lambda r: r.ratio, reverse=True)
    best = [r for r in ranked if r.ratio >= 1.25][:BEST]
    worst = [r for r in reversed(ranked) if r.ratio <= 0.8][:WORST]
    for heading, picked in (("Did best:", best), ("Did worst:", worst)):
        if picked:
            lines += ["", heading, *(_rated_line(r, show) for r in picked)]
    return lines


def _bands(rated: list[_Rated], bands, value, upward: bool = False) -> str:
    """The median against-usual of each band with enough uploads, e.g. "under 10 min 0.6× (3), 10–20 min 1.4× (5)";
    empty unless at least two bands have them. Bands are (upper limit, label), or with `upward` (lower limit, label)."""
    found = []
    for i, (limit, label) in enumerate(bands):
        if upward:
            upper = bands[i - 1][0] if i else inf
            inside = [r.ratio for r in rated if limit <= value(r) < upper]
        else:
            lower = bands[i - 1][0] if i else 0
            inside = [r.ratio for r in rated if lower <= value(r) < limit]
        if len(inside) >= MIN_BAND:
            found.append(f"{label} {median(inside):.1f}× ({len(inside)})")
    return ", ".join(found) if len(found) >= 2 else ""


def _rated_line(r: _Rated, show: Show) -> str:
    made = "made by the channel owner" if r.clip.manual else f"scored {r.clip.score:.2f}"
    reason = _sentence(r.clip.why or r.clip.description)
    return (f"- \"{titles.own_part(show.title_template, r.clip.title)}\" ({r.kind}, {_duration(_length(r.clip))}, "
            f"{made}): {r.ratio:.1f}× usual, {compact(r.views)} views" + (f". {reason}" if reason else ""))


# --- How the owner changed the AI's picks ------------------------------------------------


def _changes(s: Session, settings: Settings, show: Show) -> list[str]:
    """How the owner moved the starts and ends of the AI's picks they approved, with the words that took in or left
    out where the transcript has them, what they cut out, how they retitled them, and which picks they turned down."""
    picks = s.exec(
        select(ClipRecord, Vod).join(Vod)
        .where(Vod.show_id == show.id, ClipRecord.kind != "whole", col(ClipRecord.manual).is_not(True),
               col(ClipRecord.reviewed_at).is_not(None))
        .order_by(col(ClipRecord.reviewed_at).desc()).limit(LATEST)
    ).all()
    approved = [(c, v) for c, v in picks if c.status in APPROVED and _looked_at(c)]
    rejected = [c for c, _ in picks if c.status == "rejected"]
    known = [(c, v) for c, v in approved if c.picked_start is not None and c.picked_end is not None]
    changed = [(c, v) for c, v in known if _moved(c.start, c.picked_start) or _moved(c.end, c.picked_end) or c.cuts]
    retitled = [
        (c.picked_title, own) for c, _ in approved
        if c.picked_title and _plain(own := titles.own_part(show.title_template, c.title)) != _plain(c.picked_title)
    ]

    lines: list[str] = []
    if len(known) >= 3:
        lines += ["", f"Of the last {len(known)} of your picks they approved, {_moves(known)}."]
    if changed:
        words: dict[int, list[Word]] = {}
        lines += ["", "Picks they changed, newest first:"]
        for clip, vod in changed[:EXAMPLES]:
            if vod.id not in words:
                words[vod.id] = _words(settings.vods_dir / vod.workspace) if vod.workspace else []
            lines.append(_changed_line(clip, words[vod.id], show))
    if retitled:
        lines += ["", "Titles they rewrote, yours and then theirs:",
                  *(f"- \"{yours}\" → \"{theirs}\"" for yours, theirs in retitled[:5])]
    if rejected:
        looked = len(rejected) + len(approved)
        lines += ["", f"They turned down {len(rejected)} of the last {looked} of your picks they looked at, for example:",
                  *(_rejected_line(c, show) for c in rejected[:5])]
    return ["# How the channel owner changed your earlier picks", *lines] if lines else []


def _looked_at(clip: ClipRecord) -> bool:
    """Approved by the owner, rather than going ahead by itself (a show can skip review), which happens as it's made."""
    return clip.reviewed_at is not None and clip.reviewed_at - clip.created_at > timedelta(minutes=1)


def _moved(now: float, picked: float | None) -> bool:
    return picked is not None and abs(now - picked) >= MOVED_S


def _moves(known: list[tuple[ClipRecord, Vod]]) -> str:
    """e.g. "they moved the start of 4 (later in 3, by a median of 2.1 s; ...) and the end of 6 (...), and cut parts
    out of 1", or that they kept them all as they were."""
    moved = []
    for which, now, picked in (("start", "start", "picked_start"), ("end", "end", "picked_end")):
        shifts = [getattr(c, now) - getattr(c, picked) for c, _ in known if _moved(getattr(c, now), getattr(c, picked))]
        if shifts:
            ways = [f"{way} in {len(found)}, by a median of {_duration(median(abs(d) for d in found))}"
                    for way, found in (("later", [d for d in shifts if d > 0]), ("earlier", [d for d in shifts if d < 0]))
                    if found]
            moved.append(f"the {which} of {len(shifts)} ({'; '.join(ways)})")
    with_cuts = sum(1 for c, _ in known if c.cuts)
    said = [f"they moved {' and '.join(moved)}"] if moved else []
    if with_cuts:
        said.append(f"{'' if said else 'they '}cut parts out of {with_cuts}")
    return ", and ".join(said) or "they kept every start and end where you put them"


def _changed_line(clip: ClipRecord, words: list[Word], show: Show) -> str:
    parts = []
    shift = clip.start - clip.picked_start
    if abs(shift) >= MOVED_S:
        if shift < 0:
            parts.append(f"the start moved {_duration(-shift)} earlier, taking in {_said(words, clip.start, clip.picked_start)}")
        else:
            parts.append(f"the start moved {_duration(shift)} later, leaving out {_said(words, clip.picked_start, clip.start)}")
    shift = clip.end - clip.picked_end
    if abs(shift) >= MOVED_S:
        ended = _quote(_texts(words, -inf, clip.picked_end)[-8:], tail=True)
        yours = f"yours ended on \"…{ended}\", " if ended else ""
        if shift > 0:
            parts.append(f"the end moved {_duration(shift)} later: {yours}and they kept going through "
                         f"{_said(words, clip.picked_end, clip.end)}")
        else:
            parts.append(f"the end moved {_duration(-shift)} earlier, leaving out {_said(words, clip.end, clip.picked_end)}")
    for a, b in cuts.parse(clip.cuts):
        parts.append(f"they cut out {_duration(b - a)}: {_said(words, a, b)}")
    kind = "Clip" if clip.kind == "chapter" else "Highlight"
    return f"- {kind} \"{titles.own_part(show.title_template, clip.title)}\": " + "; ".join(parts) + "."


def _rejected_line(clip: ClipRecord, show: Show) -> str:
    reason = _sentence(clip.why or clip.description)
    kind = "clip" if clip.kind == "chapter" else "highlight"
    return (f"- \"{titles.own_part(show.title_template, clip.title)}\" ({kind}, {_duration(_length(clip))}, "
            f"scored {clip.score:.2f})" + (f". {reason}" if reason else ""))


# --- Picks kept from before ----------------------------------------------------------


def backfill_picks(s: Session, settings: Settings) -> int:
    """Fill in the AI's picks of clips from before they were kept with the clip, from their stream's segments.json.
    A clip whose start and end are both just shifted from it, by the same amount, is left out: that's the timing fix
    (timing.py) moving it, not the owner. Returns how many it filled in."""
    rows = s.exec(
        select(ClipRecord, Vod).join(Vod)
        .where(col(ClipRecord.picked_start).is_(None), ClipRecord.kind != "whole", col(ClipRecord.manual).is_not(True),
               col(Vod.workspace).is_not(None))
    ).all()
    picks: dict[int, dict[str, dict]] = {}
    filled = 0
    for clip, vod in rows:
        if vod.id not in picks:
            picks[vod.id] = _segments(Workspace(settings.vods_dir / vod.workspace).segments_json)
        pick = picks[vod.id].get(clip.key)
        if not pick:
            continue
        start_shift, end_shift = clip.start - pick["start"], clip.end - pick["end"]
        if not clip.cuts and abs(start_shift) > 0.05 and abs(start_shift - end_shift) < 0.05:
            continue
        clip.picked_start, clip.picked_end, clip.picked_title = pick["start"], pick["end"], pick.get("title", "")
        s.add(clip)
        filled += 1
    s.commit()
    return filled


def _segments(path: Path) -> dict[str, dict]:
    try:
        return {c["id"]: c for c in read_json(path)} if path.exists() else {}
    except (OSError, ValueError, KeyError, TypeError):
        log.warning("Couldn't read the AI's picks from %s", path)
        return {}


# --- Small helpers -----------------------------------------------------------------------


def _kind(clip: ClipRecord, which: str) -> str:
    return "clip" if clip.kind == "chapter" else "Short" if which == "short" else "highlight"


def _length(clip: ClipRecord) -> float:
    return cuts.kept_length(clip.start, clip.end, cuts.parse(clip.cuts))


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f} s" if seconds < 10 else f"{seconds:.0f} s"
    minutes = round(seconds / 60)
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60} min"


def _sentence(text: str, limit: int = 160) -> str:
    """The first sentence, shortened if it's long."""
    first = " ".join(text.split()).split(". ")[0].rstrip(".")
    return first if len(first) <= limit else first[: limit - 1].rsplit(" ", 1)[0] + "…"


def _plain(title: str) -> str:
    return " ".join(title.lower().split())


def _words(root: Path) -> list[Word]:
    path = Workspace(root).transcript_json
    try:
        return Transcript.model_validate_json(path.read_text(encoding="utf-8")).words()
    except (OSError, ValueError):
        return []


def _texts(words: list[Word], a: float, b: float) -> list[str]:
    """The words said from `a` to `b`, by where their middle falls."""
    return [w.text.strip() for w in words if a <= (w.start + w.end) / 2 < b and w.text.strip()]


def _quote(texts: list[str], tail: bool = False) -> str:
    if len(texts) <= QUOTE_WORDS:
        return " ".join(texts)
    if tail:
        return " ".join(texts[-QUOTE_WORDS:])
    return " ".join(texts[: QUOTE_WORDS - 6]) + " … " + " ".join(texts[-5:])


def _said(words: list[Word], a: float, b: float) -> str:
    texts = _texts(words, a, b)
    return f"\"{_quote(texts)}\"" if texts else "a stretch with no speech" if words else "that stretch"
