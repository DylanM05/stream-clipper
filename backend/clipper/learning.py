"""What a show's past clips teach the AI that picks the next stream's (segment.py): how its uploads did on YouTube
against the show's usual, how the channel owner changed or turned down the AI's picks (down to the words a moved
start or end took in or left out), and the moments the owner clipped themselves. With the owner's own notes about
what works, it goes into the AI's input for each new stream, and the Stats screen shows it.

The AI's picks are kept with each clip as first offered (ClipRecord.picked_*, was_extra); clips from before that get
them from their stream's segments.json, which has kept them all along, and from how their scores ranked (backfill).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import inf
from pathlib import Path
from statistics import median

from sqlmodel import Session, col, select

from . import compilation, cuts, titles
from .config import Settings
from .db import PICKED_KINDS, ClipRecord, Show, UploadJob, Vod
from .models import Transcript, Word
from .stats import compact
from .workspace import Workspace, read_json

log = logging.getLogger(__name__)

MIN_KIND = 4  # uploads of a kind with views before its usual (the median) means anything
MIN_BAND = 2  # uploads in a band of length or score before it's worth a mention
AROUND = timedelta(days=30)  # an upload is compared with others of its kind this close in time, so growth doesn't skew it
LATELY = 10  # the newest uploads of a kind that say what's usual lately
BEST, WORST, FEW = 6, 4, 3  # uploads shown as having done best and worst; and in the shorter lists
MIN_VIEWS_FOR_RATES = 100  # likes and comments per view mean little below this
LATEST = 100  # the newest picks looked at for changes and rejections
EXAMPLES = 6  # changed picks, and the owner's own clips, shown with their words
QUOTE_WORDS = 18  # longer quotes keep their start and end
MOVED_S = 0.5  # a start or end moved less than this wasn't changed
APPROVED = ("approved", "kept", "rendering", "rendered", "failed", "uploaded")  # kept: for its stream's compilation
PLURAL = {"clip": "Clips", "highlight": "Highlights", "Short": "Shorts"}
LENGTHS = {
    "clip": ((600, "under 10 min"), (1200, "10–20 min"), (2400, "20–40 min"), (inf, "over 40 min")),
    "highlight": ((30, "under 30 s"), (60, "30–60 s"), (120, "1–2 min"), (inf, "over 2 min")),
}
SCORES = ((0.9, "0.9 and up"), (0.8, "0.8 to 0.89"), (0.0, "under 0.8"))
HIGH_SCORE, LOW_SCORE = 0.85, 0.75  # a score this high that did badly, or this low that did well, was off


def track_record(s: Session, settings: Settings, show: Show) -> str:
    """The show's lessons for the AI, as sections of its input; empty when there's nothing to go on yet."""
    rows = s.exec(
        select(UploadJob, ClipRecord).join(ClipRecord).join(Vod, ClipRecord.vod_id == Vod.id)
        .where(Vod.show_id == show.id, UploadJob.status == "done", col(ClipRecord.kind).in_(PICKED_KINDS),
               col(UploadJob.views).is_not(None))
    ).all()
    transcripts = _Transcripts(settings)
    sections = [_how_they_did(rows, show), _changes(s, show, transcripts), _own_clips(s, show, transcripts)]
    if show.learning_notes.strip():
        sections.append(["# What the channel owner has noticed about what works (follow this over the numbers)",
                         show.learning_notes.strip()])
    return "\n\n".join("\n".join(lines) for lines in sections if lines)


# --- How uploads did -----------------------------------------------------------------


@dataclass
class _Rated:
    clip: ClipRecord
    job: UploadJob
    kind: str  # "clip", "highlight" or "Short"
    views: int  # a week (or a day) after going up
    ratio: float  # against the show's usual for its kind around then


def _how_they_did(rows: list[tuple[UploadJob, ClipRecord]], show: Show) -> list[str]:
    """Views a week after going up (or a day, where too few have that yet), each against the median of others of its
    kind from around then, since a Short and a 30-minute clip don't get the same views and a channel grows; then by
    length and by the AI's score, the best and worst, scores that were off, what people engaged with most for their
    views, and what kept getting views after its first day."""
    kinds: dict[str, list[tuple[UploadJob, ClipRecord]]] = {}
    for job, clip in rows:
        kinds.setdefault(_kind(clip, job.which), []).append((job, clip))
    rated: list[_Rated] = []
    usual: list[str] = []
    for kind, members in kinds.items():
        for field, when in (("views_week1", "a week"), ("views_day1", "a day")):
            have = sorted(((j, c) for j, c in members if getattr(j, field) is not None), key=lambda r: _went_up(r[0]))
            if len(have) < MIN_KIND:
                continue
            lately = median(getattr(j, field) for j, _ in have[-LATELY:])
            usual.append(f"- {PLURAL[kind]}: lately {compact(lately)} views {when} after going up ({len(have)} uploads so far)")
            for job, clip in have:
                base = _usual_around(job, [j for j, _ in have], field)
                if base > 0:
                    rated.append(_Rated(clip, job, kind, getattr(job, field), getattr(job, field) / base))
            break
    if not usual:
        return []

    lines = ["# How the show's past uploads did", "", "Their usual views, by kind:", *usual, *_staying_usual(kinds)]
    against = []
    for kind in ("clip", "highlight"):
        of_kind = [r for r in rated if (r.kind == "clip") == (kind == "clip")]
        if bands := _bands(of_kind, LENGTHS[kind], lambda r: _length(r.clip)):
            against.append(f"- {PLURAL[kind] if kind == 'clip' else 'Highlights and Shorts'} by length: {bands}")
    if bands := _bands([r for r in rated if not r.clip.manual], SCORES, lambda r: r.clip.score, upward=True):
        against.append(f"- By the score you gave them: {bands}")
    if against:
        lines += ["", "Views against usual (2.0× is twice what others of the kind got around then), with how many "
                      "uploads:", *against]

    ranked = sorted(rated, key=lambda r: r.ratio, reverse=True)
    picks = [r for r in ranked if not r.clip.manual]
    for heading, shown in (
        ("Did best:", [r for r in ranked if r.ratio >= 1.25][:BEST]),
        ("Did worst:", [r for r in reversed(ranked) if r.ratio <= 0.8][:WORST]),
        ("Scored high but did badly:", [r for r in reversed(picks) if r.clip.score >= HIGH_SCORE and r.ratio <= 0.6][:FEW]),
        ("Scored low but did well:", [r for r in picks if r.clip.score <= LOW_SCORE and r.ratio >= 1.5][:FEW]),
    ):
        if shown:
            lines += ["", heading, *(_rated_line(r, show) for r in shown)]
    return lines + _engaged(rated, show) + _stayed(kinds, show)


def _went_up(job: UploadJob) -> datetime:
    return job.finished_at or job.updated_at


def _usual_around(job: UploadJob, jobs: list[UploadJob], field: str) -> float:
    """The median of the other uploads of the kind that went up around the same time, or of all of them if too few."""
    others = [o for o in jobs if o is not job]
    near = [o for o in others if abs(_went_up(o) - _went_up(job)) <= AROUND]
    return median(getattr(o, field) for o in (near if len(near) >= MIN_KIND - 1 else others))


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


def _engaged(rated: list[_Rated], show: Show) -> list[str]:
    """Uploads people liked and commented on most for their views, against others of their kind: what viewers enjoyed,
    whether or not it got the most clicks."""
    found = []
    for kind in PLURAL:
        of_kind = [(r, _engagement(r.job)) for r in rated if r.kind == kind and _engagement(r.job) is not None]
        if len(of_kind) < MIN_KIND or not (usual := median(e for _, e in of_kind)):
            continue
        found += [(r, e / usual) for r, e in of_kind if e / usual >= 1.5]
    if not found:
        return []
    found.sort(key=lambda f: f[1], reverse=True)
    return ["", "Liked and commented on most for their views:", *(
        f"- \"{titles.own_part(show.title_template, r.clip.title)}\" ({r.kind}, {_duration(_length(r.clip))}): "
        f"{r.job.likes or 0} likes and {r.job.comments or 0} comments on {compact(r.job.views)} views, {ratio:.1f}× "
        f"the usual for its kind"
        for r, ratio in found[:FEW]
    )]


def _engagement(job: UploadJob) -> float | None:
    if not job.views or job.views < MIN_VIEWS_FOR_RATES or job.likes is None:
        return None
    return ((job.likes or 0) + (job.comments or 0)) / job.views


def _growth(job: UploadJob) -> float | None:
    """How many times its first day's views it had a week after going up."""
    return job.views_week1 / job.views_day1 if job.views_day1 and job.views_week1 is not None else None


def _staying_usual(kinds: dict[str, list[tuple[UploadJob, ClipRecord]]]) -> list[str]:
    lines = []
    for kind, members in kinds.items():
        growth = [g for j, _ in members if (g := _growth(j)) is not None]
        if len(growth) >= MIN_KIND:
            lines.append(f"- {PLURAL[kind]}: a week's views are usually {median(growth):.1f}× the first day's")
    return lines


def _stayed(kinds: dict[str, list[tuple[UploadJob, ClipRecord]]], show: Show) -> list[str]:
    """Uploads that kept getting views after their first day, far more than others of their kind: what people still
    find later, not just what lands the day it goes up."""
    found = []
    for kind, members in kinds.items():
        grown = [(j, c, g) for j, c in members if (g := _growth(j)) is not None]
        if len(grown) < MIN_KIND:
            continue
        usual = median(g for _, _, g in grown)
        found += [(j, c, kind, g, usual) for j, c, g in grown if g >= 1.5 * usual]
    if not found:
        return []
    found.sort(key=lambda f: f[3] / f[4], reverse=True)
    return ["", "Kept getting views after their first day:", *(
        f"- \"{titles.own_part(show.title_template, c.title)}\" ({kind}, {_duration(_length(c))}): {compact(j.views_week1)} "
        f"views in a week, {g:.1f}× its first day's (usually {usual:.1f}×)"
        for j, c, kind, g, usual in found[:FEW]
    )]


# --- How the owner changed the AI's picks ------------------------------------------------


def _changes(s: Session, show: Show, transcripts: "_Transcripts") -> list[str]:
    """How the owner moved the starts and ends of the AI's picks they approved, with the words that took in or left
    out where the transcript has them, what they cut out (in the clip, or later in the stream's compilation), how they
    retitled them, which extras they approved anyway, where they disagreed about a highlight working as a Short, and
    which picks they turned down. A pick kept for the compilation without review counts once they cut part of it out
    of the compilation."""
    picks = s.exec(
        select(ClipRecord, Vod).join(Vod)
        .where(Vod.show_id == show.id, col(ClipRecord.kind).in_(PICKED_KINDS), col(ClipRecord.manual).is_not(True),
               col(ClipRecord.reviewed_at).is_not(None))
        .order_by(col(ClipRecord.reviewed_at).desc()).limit(LATEST)
    ).all()
    in_compilation = _compilation_cuts(s, show)
    approved = [(c, v) for c, v in picks if c.status in APPROVED and (_looked_at(c) or c.id in in_compilation)]
    rejected = [c for c, _ in picks if c.status == "rejected"]
    known = [(c, v) for c, v in approved if c.picked_start is not None and c.picked_end is not None]
    changed = [(c, v) for c, v in known
               if _moved(c.start, c.picked_start) or _moved(c.end, c.picked_end) or c.cuts or c.id in in_compilation]
    retitled = [
        (c.picked_title, own) for c, _ in approved
        if c.picked_title and _plain(own := titles.own_part(show.title_template, c.title)) != _plain(c.picked_title)
    ]
    extras = [c for c, _ in approved if c.was_extra]

    lines: list[str] = []
    if len(known) >= 3:
        lines += ["", f"Of the last {len(known)} of your picks they approved, {_moves(known, in_compilation)}."]
    if changed:
        lines += ["", "Picks they changed, newest first:"]
        lines += [_changed_line(clip, transcripts.words(vod), show, in_compilation.get(clip.id, []))
                  for clip, vod in changed[:EXAMPLES]]
    if retitled:
        lines += ["", "Titles they rewrote, yours and then theirs:",
                  *(f"- \"{yours}\" → \"{theirs}\"" for yours, theirs in retitled[:5])]
    if extras:
        lines += ["", f"Extras they approved anyway: {len(extras)} of your picks you'd scored below the show's best few, "
                      "for example:", *(_pick_line(c, show) for c in extras[:5])]
    lines += _shorts(approved, show)
    if rejected:
        looked = len(rejected) + len(approved)
        lines += ["", f"They turned down {len(rejected)} of the last {looked} of your picks they looked at, for example:",
                  *(_pick_line(c, show) for c in rejected[:5])]
    return ["# How the channel owner changed your earlier picks", *lines] if lines else []


def _looked_at(clip: ClipRecord) -> bool:
    """Approved by the owner, rather than going ahead by itself (a show can skip review), which happens as it's made."""
    return clip.reviewed_at is not None and clip.reviewed_at - clip.created_at > timedelta(minutes=1)


def _compilation_cuts(s: Session, show: Show) -> dict[int, list[cuts.Cut]]:
    """What the owner cut out of the show's compilations since the app made them, by the clip each part was in: a part
    is split across the kept parts of the stream's clips in the compilation, and where two overlap (a highlight inside
    a chapter) goes to the shorter. Parts in the gaps between clips were already out. Compilations made before they
    recorded their plan (picked_*) are skipped, as are slips under MOVED_S (a gap dragged a hair into a clip)."""
    found: dict[int, list[cuts.Cut]] = {}
    made = s.exec(
        select(ClipRecord).join(Vod)
        .where(Vod.show_id == show.id, ClipRecord.kind == "compilation", col(ClipRecord.picked_start).is_not(None))
    ).all()
    for comp in made:
        left = cuts.removed(comp.start, comp.end, cuts.parse(comp.cuts),
                            comp.picked_start, comp.picked_end or comp.end, cuts.parse(comp.picked_cuts))
        if not left:
            continue
        clips = s.exec(select(ClipRecord).where(ClipRecord.vod_id == comp.vod_id)).all()
        for clip in sorted(compilation.included(clips), key=_length):
            for ka, kb in cuts.kept(clip.start, clip.end, cuts.parse(clip.cuts)):
                rest = []
                for a, b in left:
                    if max(a, ka) < min(b, kb):
                        found.setdefault(clip.id, []).append((max(a, ka), min(b, kb)))
                    rest += [p for p in ((a, min(b, ka)), (max(a, kb), b)) if p[1] > p[0]]
                left = rest
    found = {i: sorted(p for p in parts if p[1] - p[0] >= MOVED_S) for i, parts in found.items()}
    return {i: parts for i, parts in found.items() if parts}


def _moved(now: float, picked: float | None) -> bool:
    return picked is not None and abs(now - picked) >= MOVED_S


def _moves(known: list[tuple[ClipRecord, Vod]], in_compilation: dict[int, list[cuts.Cut]]) -> str:
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
    with_cuts = sum(1 for c, _ in known if c.cuts or c.id in in_compilation)
    said = [f"they moved {' and '.join(moved)}"] if moved else []
    if with_cuts:
        said.append(f"{'' if said else 'they '}cut parts out of {with_cuts}")
    return ", and ".join(said) or "they kept every start and end where you put them"


def _changed_line(clip: ClipRecord, words: list[Word], show: Show, in_compilation: list[cuts.Cut]) -> str:
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
    for a, b in in_compilation:
        parts.append(f"in the compilation they cut out {_duration(b - a)}: {_said(words, a, b)}")
    kind = "Clip" if clip.kind == "chapter" else "Highlight"
    return f"- {kind} \"{titles.own_part(show.title_template, clip.title)}\": " + "; ".join(parts) + "."


def _pick_line(clip: ClipRecord, show: Show) -> str:
    reason = _sentence(clip.why or clip.description)
    kind = "clip" if clip.kind == "chapter" else "highlight"
    return (f"- \"{titles.own_part(show.title_template, clip.title)}\" ({kind}, {_duration(_length(clip))}, "
            f"scored {clip.score:.2f})" + (f". {reason}" if reason else ""))


def _shorts(approved: list[tuple[ClipRecord, Vod]], show: Show) -> list[str]:
    """Highlights the owner made a Short of against the AI's call that it doesn't work vertically, or the other way.
    Highlights only kept for the compilation are left out: whether they'd make a Short was never the question."""
    highlights = [c for c, _ in approved if c.kind == "highlight" and c.status != "kept"]
    made = [c for c in highlights if c.make_short and not c.vertical_ok]
    dropped = [c for c in highlights if not c.make_short and c.vertical_ok]
    said = []
    for clips, what in ((made, "made Shorts of {n} you'd said don't work vertically"),
                        (dropped, "turned the Short off for {n} you'd said work vertically")):
        if clips:
            some = ", ".join(f"\"{titles.own_part(show.title_template, c.title)}\"" for c in clips[:3])
            said.append(what.format(n=len(clips)) + f" (for example {some})")
    return ["", f"Of the highlights they approved, they {' and '.join(said)}."] if said else []


# --- Moments the owner clipped themselves --------------------------------------------------


def _own_clips(s: Session, show: Show, transcripts: "_Transcripts") -> list[str]:
    """The clips the owner made in the app, newest first, with how each begins and ends, and whether it overlaps
    one of the AI's picks of the same kind: a moment the AI didn't pick is the clearest sign of what it misses."""
    mine = s.exec(
        select(ClipRecord, Vod).join(Vod)
        .where(Vod.show_id == show.id, col(ClipRecord.manual).is_(True))
        .order_by(col(ClipRecord.created_at).desc()).limit(EXAMPLES)
    ).all()
    if not mine:
        return []
    ai_picks = s.exec(select(ClipRecord).where(
        col(ClipRecord.vod_id).in_({v.id for _, v in mine}), col(ClipRecord.manual).is_not(True),
        col(ClipRecord.kind).in_(PICKED_KINDS),
    )).all()
    lines = ["# Moments the channel owner clipped themselves, newest first"]
    for clip, vod in mine:
        words = transcripts.words(vod)
        opening = _quote(_texts(words, clip.start, clip.end)[:12])
        closing = _quote(_texts(words, clip.start, clip.end)[-10:], tail=True)
        said = f": it begins \"{opening}…\" and ends \"…{closing}\"" if opening else ""
        overlap = max(
            ((min(clip.end, _pick_end(p)) - max(clip.start, _pick_start(p)), p) for p in ai_picks
             if p.vod_id == clip.vod_id and p.kind == clip.kind),
            key=lambda o: o[0], default=(0.0, None),
        )
        kind = "clip" if clip.kind == "chapter" else "highlight"
        whose = (f"overlapping your pick \"{titles.own_part(show.title_template, overlap[1].title)}\"" if overlap[0] >= 1
                 else f"no {kind} of yours covered it")
        lines.append(f"- \"{titles.own_part(show.title_template, clip.title)}\" ({kind}, {_duration(_length(clip))}, "
                     f"{whose}){said}")
    return lines


def _pick_start(clip: ClipRecord) -> float:
    return clip.picked_start if clip.picked_start is not None else clip.start


def _pick_end(clip: ClipRecord) -> float:
    return clip.picked_end if clip.picked_end is not None else clip.end


# --- Picks kept from before ----------------------------------------------------------


def backfill(s: Session, settings: Settings) -> int:
    """Fill in what clips from before they were kept don't have: the AI's picks, from their stream's segments.json, and
    whether each was offered as an extra, from how its score ranked against the show's best few (as best_picks does,
    with the show's numbers as they are now). A clip whose start and end are both just shifted from its pick, by the
    same amount, gets none: that's the timing fix (timing.py) moving it, not the owner. Returns how many it changed."""
    rows = s.exec(
        select(ClipRecord, Vod).join(Vod)
        .where(col(ClipRecord.picked_start).is_(None), col(ClipRecord.kind).in_(PICKED_KINDS), col(ClipRecord.manual).is_not(True),
               col(Vod.workspace).is_not(None))
    ).all()
    picks: dict[int, dict[str, dict]] = {}
    changed = 0
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
        changed += 1

    unranked = set(s.exec(select(ClipRecord.vod_id).where(
        col(ClipRecord.was_extra).is_(None), col(ClipRecord.kind).in_(PICKED_KINDS), col(ClipRecord.manual).is_not(True),
    )))
    for vod_id in unranked:
        vod = s.get(Vod, vod_id)
        show = s.get(Show, vod.show_id) if vod and vod.show_id else None
        clips = s.exec(select(ClipRecord).where(ClipRecord.vod_id == vod_id, col(ClipRecord.manual).is_not(True))).all()
        for kind, limit in (("chapter", show and show.max_chapters), ("highlight", show and show.max_shorts)):
            ranked = sorted((c for c in clips if c.kind == kind), key=lambda c: c.score, reverse=True)
            offered = {c.id for c in ranked[:limit]}
            for c in ranked:
                if c.was_extra is None:
                    c.was_extra = c.status == "extra" or c.id not in offered
                    s.add(c)
                    changed += 1
    s.commit()
    return changed


def _segments(path: Path) -> dict[str, dict]:
    try:
        return {c["id"]: c for c in read_json(path)} if path.exists() else {}
    except (OSError, ValueError, KeyError, TypeError):
        log.warning("Couldn't read the AI's picks from %s", path)
        return {}


# --- Small helpers -----------------------------------------------------------------------


class _Transcripts:
    """Each stream's transcript words, read once for a whole track record."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.found: dict[int, list[Word]] = {}

    def words(self, vod: Vod) -> list[Word]:
        if vod.id not in self.found:
            self.found[vod.id] = _words(self.settings.vods_dir / vod.workspace) if vod.workspace else []
        return self.found[vod.id]


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
