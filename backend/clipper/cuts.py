"""Parts taken out of the middle of a clip in the app's editor, and what they do to its times.

A cut is a (start, end) pair in the stream's seconds, inside the clip. The finished video jumps over it: what's
before and after it are joined.
"""

import json

MIN_CUT_S = 0.05  # anything shorter is a slip of the mouse
EDGE_S = 0.01  # a cut this close to an end of the clip trims the clip instead

Cut = tuple[float, float]


def parse(text: str) -> list[Cut]:
    """ClipRecord.cuts, stored as JSON [[start, end], ...]; "" for none."""
    if not text:
        return []
    try:
        return [(float(a), float(b)) for a, b in json.loads(text)]
    except (ValueError, TypeError):
        return []


def dump(cuts: list[Cut]) -> str:
    return json.dumps([[round(a, 3), round(b, 3)] for a, b in cuts]) if cuts else ""


def tidy(start: float, end: float, cuts: list[Cut]) -> tuple[float, float, list[Cut]]:
    """The clip's ends and cuts made consistent: cuts kept inside the clip, sorted, overlapping ones merged and
    tiny ones dropped; a cut that reaches an end of the clip trims the clip instead."""
    merged: list[list[float]] = []
    for a, b in sorted((min(a, b), max(a, b)) for a, b in cuts):
        a, b = max(a, start), min(b, end)
        if b - a < MIN_CUT_S:
            continue
        if merged and a <= merged[-1][1] + EDGE_S:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    if merged and merged[0][0] <= start + EDGE_S:
        start = merged.pop(0)[1]
    if merged and merged[-1][1] >= end - EDGE_S:
        end = merged.pop()[0]
    return start, end, [(a, b) for a, b in merged]


def kept(start: float, end: float, cuts: list[Cut]) -> list[Cut]:
    """The parts of the clip that stay, in the stream's seconds."""
    parts, at = [], start
    for a, b in cuts:
        if a > at:
            parts.append((at, a))
        at = max(at, b)
    if end > at:
        parts.append((at, end))
    return parts


def removed(start: float, end: float, cut_list: list[Cut],
            picked_start: float, picked_end: float, picked_cuts: list[Cut]) -> list[Cut]:
    """What the owner took out of a compilation since the app made it from its plan (picked_*): a later start, an
    earlier end, and cuts beyond the gaps the app cut. A new cut touching a gap was merged into it by `tidy`, so
    only the new part counts. Sorted and merged."""
    parts = [(picked_start, start), (end, picked_end)]
    for a, b in cut_list:
        at = a
        for ga, gb in picked_cuts:
            if gb <= at or ga >= b:
                continue
            parts.append((at, ga))
            at = max(at, gb)
        parts.append((at, b))
    merged: list[list[float]] = []
    for a, b in sorted(p for p in parts if p[1] > p[0]):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def stream_time(parts: list[Cut], t: float) -> float:
    """Where `t` seconds into the finished video is in the stream, for the `parts` that stay (kept)."""
    for a, b in parts:
        if t < b - a:
            return a + t
        t -= b - a
    return parts[-1][1] if parts else 0.0


def kept_length(start: float, end: float, cuts: list[Cut]) -> float:
    """How long the clip comes out."""
    return sum(b - a for a, b in kept(start, end, cuts))
