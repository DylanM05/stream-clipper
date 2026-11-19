"""A stream's approved clips joined into one video, for a show that makes compilations (Show.compilation).

It's one more clip of the stream, of kind "compilation": from the start of the first approved clip to the end of the
last, with everything between them cut out (cuts.py). So the app's editor, renders and uploads treat it as they do any
clip. It's rendered a part at a time (render.render_compilation), so the hours between the parts are never decoded.
"""

from dataclasses import dataclass

from . import cuts, titles
from .db import PICKED_KINDS, ClipRecord, Vod

KEY = "comp"
MIN_CLIPS = 2  # one clip on its own is already a video
# Kept for it, or approved (in review or without it), whether rendered yet or not.
INCLUDED = ("kept", "approved", "rendering", "rendered", "uploaded", "failed")


@dataclass
class Plan:
    start: float
    end: float
    cuts: list[cuts.Cut]
    picks: list[ClipRecord]  # the clips in it, in stream order
    description: str


def plan(clips: list[ClipRecord], template: str) -> Plan | None:
    """The compilation of a stream's approved chapters and highlights, in the order they happened, or None with fewer
    than MIN_CLIPS. Parts of them that overlap (a highlight inside a chapter) go in once, and parts cut out of them in
    the app stay out. Its description lists them with where each starts, as chapters on YouTube, titled without the
    show's `template`."""
    picks = included(clips)
    if len(picks) < MIN_CLIPS:
        return None
    merged = kept_parts(picks)
    start, end, gaps = cuts.tidy(merged[0][0], merged[-1][1], [(x[1], y[0]) for x, y in zip(merged, merged[1:])])
    return Plan(start, end, gaps, picks, describe(picks, template, cuts.kept(start, end, gaps)))


def describe(picks: list[ClipRecord], template: str, parts: list[cuts.Cut]) -> str:
    """The compilation's description: its `picks` with where each starts in the video made of `parts`."""
    starts = [
        (_video_time(parts, cuts.kept(c.start, c.end, cuts.parse(c.cuts))[0][0]), titles.own_part(template, c.title))
        for c in picks
    ]
    return titles.chapter_list(starts) or "\n".join(f"- {title}" for _, title in starts)


def included(clips: list[ClipRecord]) -> list[ClipRecord]:
    """The stream's chapters and highlights that go in its compilation, in stream order."""
    return sorted(
        (c for c in clips if c.kind in PICKED_KINDS and c.status in INCLUDED), key=lambda c: (c.start, c.end),
    )


def kept_parts(picks: list[ClipRecord]) -> list[cuts.Cut]:
    """The parts of the `picks` that stay, in stream order, with overlapping and touching ones joined."""
    merged: list[list[float]] = []
    for a, b in sorted(part for c in picks for part in cuts.kept(c.start, c.end, cuts.parse(c.cuts))):
        if merged and a <= merged[-1][1] + cuts.EDGE_S:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def is_changed(clip: ClipRecord, plan: Plan | None) -> bool:
    """Whether the stream's clips now make a different compilation (`plan`, None for none) than the one `clip` was
    made from (picked_*): its start, end or gaps differ. The owner's own edits to it don't count. A compilation made
    before it recorded that is never out of date."""
    if clip.picked_start is None:
        return False
    if plan is None:
        return True
    return (round(plan.start, 3), round(plan.end, 3), cuts.dump(plan.cuts)) != (
        round(clip.picked_start, 3), round(clip.picked_end or 0, 3), clip.picked_cuts)


def mark_due(vod: Vod, clips: list[ClipRecord]) -> None:
    """Have the stream's compilation made (again checked for) after one of its picks changed status, if it makes one
    and hasn't got one yet: so a stream that had too few kept clips gets one once there are enough."""
    if vod.makes_compilation and not any(c.kind == "compilation" for c in clips):
        vod.compilation_due = True


def done(clips: list[ClipRecord]) -> bool:
    """Whether the stream's compilation is uploaded or rejected, so its kept clips need nothing more (until then the
    stream's files stay, as for any clip still to be uploaded)."""
    return any(c.kind == "compilation" and c.status in ("uploaded", "rejected") for c in clips)


def _video_time(parts: list[cuts.Cut], t: float) -> float:
    """How far into the finished video `t` (in the stream's seconds, inside one of the `parts`) comes."""
    before = 0.0
    for a, b in parts:
        if t < b:
            return before + max(0.0, t - a)
        before += b - a
    return before
