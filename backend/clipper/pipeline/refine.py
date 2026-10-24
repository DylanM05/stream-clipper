"""Turn Claude's proposals into clips with clean cut points: snapped to word boundaries, never
mid-sentence, within length limits, and with no overlapping highlights."""

import logging
from bisect import bisect_left, bisect_right

from ..models import Clip, Segmentation, Transcript
from ..timecode import to_seconds
from ..workspace import Workspace, write_json

log = logging.getLogger(__name__)

SENTENCE_END = (".", "?", "!", "…")
PAUSE_S = 0.6  # a gap this long between words counts as a sentence break
SEARCH_S = 4.0  # how far a cut point may move to reach a sentence boundary
LEAD_IN_S = 0.4  # room before the first word
TAIL_S = 1.0  # room after the last word, so its trailing sound isn't clipped
MIN_TAIL_S = 0.35  # kept even when the next word follows immediately
MIN_CHAPTER_S = 60.0
MIN_HIGHLIGHT_S = 10.0
MAX_HIGHLIGHT_S = 180.0  # YouTube's Shorts limit
MAX_OVERLAP = 0.25  # of the shorter highlight; more than this and the lower-scored one is dropped
MAX_TITLE = 100


def run(ws: Workspace, segmentation: Segmentation, transcript: Transcript) -> list[Clip]:
    clips = refine(segmentation, transcript)
    write_json(ws.segments_json, [c.model_dump() for c in clips])
    return clips


def refine(seg: Segmentation, transcript: Transcript) -> list[Clip]:
    snap = Snapper(transcript)
    return _chapters(seg, snap, transcript.duration) + _highlights(seg, snap, transcript.duration)


class Snapper:
    """Moves Claude's HH:MM:SS cut points onto sentence boundaries using the word timestamps.

    Claude's timecodes name transcript lines, rounded down to the second: a start means "from the
    start of this line" and an end means "through the end of this line". A cut may move outward to
    take in the rest of a sentence, but never into a line Claude named. Casual speech often has no
    punctuation, and moving inward drops exactly the line Claude chose, such as the punchline.
    """

    def __init__(self, transcript: Transcript):
        self.lines = [s for s in transcript.segments if s.text]
        self.line_starts = [s.start for s in self.lines]
        self.words = transcript.words()
        self.word_starts = [w.start for w in self.words]
        self.word_ends = [w.end for w in self.words]

    def start(self, t: float) -> float:
        i = bisect_left(self.line_starts, t)
        if i < len(self.lines) and self.lines[i].start < t + 1:
            return self._sentence_start(self.lines[i].start, named_line=True)
        return self._sentence_start(t, named_line=False)

    def end(self, t: float) -> float:
        i = bisect_left(self.line_starts, t + 1) - 1
        if i >= 0 and self.lines[i].start >= t:
            return self._sentence_end(self.lines[i].end, named_line=True)
        return self._sentence_end(t, named_line=False)

    def _break_before(self, i: int) -> bool:
        w = self.words
        return i == 0 or w[i - 1].text.endswith(SENTENCE_END) or w[i].start - w[i - 1].end >= PAUSE_S

    def _break_after(self, i: int) -> bool:
        w = self.words
        return i == len(w) - 1 or w[i].text.endswith(SENTENCE_END) or w[i + 1].start - w[i].end >= PAUSE_S

    def _sentence_start(self, t: float, named_line: bool) -> float:
        """Start of the sentence around `t`, looking back first so the setup isn't cut off."""
        w = self.words
        i = bisect_left(self.word_starts, t - (0.05 if named_line else 0.5))
        if i >= len(w) or w[i].start - t > SEARCH_S:
            return t  # no speech nearby (music, gameplay): keep Claude's time
        j = i
        while j > 0 and not self._break_before(j) and w[i].start - w[j - 1].start <= SEARCH_S:
            j -= 1
        if not self._break_before(j):
            j = i
            if not named_line:
                k = i
                while k < len(w) - 1 and not self._break_before(k) and w[k + 1].start - w[i].start <= SEARCH_S:
                    k += 1
                if self._break_before(k):
                    j = k
        floor = w[j - 1].end if j > 0 else 0.0
        return max(floor, w[j].start - LEAD_IN_S)

    def _sentence_end(self, t: float, named_line: bool) -> float:
        """End of the sentence around `t`, looking forward first so the payoff isn't cut off."""
        w = self.words
        i = bisect_right(self.word_ends, t + (0.05 if named_line else 0.5)) - 1
        if i < 0 or t - w[i].end > SEARCH_S:
            return t
        j = i
        while j < len(w) - 1 and not self._break_after(j) and w[j + 1].end - w[i].end <= SEARCH_S:
            j += 1
        if not self._break_after(j):
            j = i
            if not named_line:
                k = i
                while k > 0 and not self._break_after(k) and w[i].end - w[k - 1].end <= SEARCH_S:
                    k -= 1
                if self._break_after(k):
                    j = k
        # Whisper's word end times tend to be early, so stop short of the next word but never
        # closer than MIN_TAIL_S to this one.
        next_word = w[j + 1].start if j + 1 < len(w) else w[j].end + TAIL_S
        return min(max(next_word, w[j].end + MIN_TAIL_S), w[j].end + TAIL_S)


def _chapters(seg: Segmentation, snap: Snapper, duration: float) -> list[Clip]:
    parsed = []
    for ch in seg.chapters:
        try:
            parsed.append((to_seconds(ch.start), to_seconds(ch.end), ch))
        except ValueError:
            log.warning("Skipping chapter %r: bad timecodes %r-%r", ch.title, ch.start, ch.end)
    parsed.sort(key=lambda p: p[0])
    bounds = [(snap.start(s), snap.end(e)) for s, e, _ in parsed]

    clips: list[Clip] = []
    for idx, ((_, _, ch), (s, e)) in enumerate(zip(parsed, bounds)):
        if idx + 1 < len(bounds):
            e = min(e, bounds[idx + 1][0])  # a chapter may not run into the next one
        s, e = max(0.0, s), min(duration, e)
        if ch.category != "content":
            continue
        if e - s < MIN_CHAPTER_S:
            log.info("Skipping chapter %r: only %.0f s long", ch.title, e - s)
            continue
        clips.append(Clip(
            id=f"ch{len(clips) + 1:02d}", kind="chapter", start=round(s, 2), end=round(e, 2),
            # Answers from before chapters were scored only have the confidence in the boundaries.
            title=clean_title(ch.title), description=ch.summary.strip(), score=_unit(ch.score or ch.confidence),
            llm_start=ch.start, llm_end=ch.end,
        ))
    return clips


def _highlights(seg: Segmentation, snap: Snapper, duration: float) -> list[Clip]:
    candidates = []
    for hl in seg.highlights:
        try:
            s, e = snap.start(to_seconds(hl.start)), snap.end(to_seconds(hl.end))
        except ValueError:
            log.warning("Skipping highlight %r: bad timecodes %r-%r", hl.title, hl.start, hl.end)
            continue
        s, e = max(0.0, s), min(duration, e)
        if e - s > MAX_HIGHLIGHT_S:
            # Leave room for the snap to move forward to a sentence end, then enforce the hard limit.
            e = min(snap.end(s + MAX_HIGHLIGHT_S - SEARCH_S - TAIL_S), s + MAX_HIGHLIGHT_S)
        if e - s < MIN_HIGHLIGHT_S:
            log.info("Skipping highlight %r: only %.0f s long", hl.title, e - s)
            continue
        candidates.append((s, e, hl))

    kept = []
    for s, e, hl in sorted(candidates, key=lambda c: c[2].score, reverse=True):
        if all(_overlap(s, e, ks, ke) <= MAX_OVERLAP * min(e - s, ke - ks) for ks, ke, _ in kept):
            kept.append((s, e, hl))
        else:
            log.info("Dropping highlight %r: it overlaps a higher-scored one", hl.title)
    kept.sort(key=lambda c: c[0])

    return [
        Clip(
            id=f"hl{n:02d}", kind="highlight", start=round(s, 2), end=round(e, 2),
            title=clean_title(hl.title), description=hl.description.strip(), score=_unit(hl.score),
            why=hl.why.strip(), vertical_ok=hl.vertical_ok, llm_start=hl.start, llm_end=hl.end,
        )
        for n, (s, e, hl) in enumerate(kept, 1)
    ]


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def clean_title(text: str) -> str:
    """Whitespace collapsed and cut at a word boundary to YouTube's 100-character limit."""
    title = " ".join(text.split())
    if len(title) <= MAX_TITLE:
        return title
    return title[: MAX_TITLE - 1].rsplit(" ", 1)[0] + "…"


def _unit(x: float) -> float:
    return min(1.0, max(0.0, x))
