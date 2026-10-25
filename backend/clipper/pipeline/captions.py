"""Word-by-word captions for Shorts, written as an ASS subtitle file for ffmpeg to burn in.

Words appear in short groups, and the word being spoken is highlighted.
"""

from collections.abc import Iterator

from ..models import Word

SENTENCE_END = (".", "?", "!", "…")
HIGHLIGHT = "&H00E5FF&"  # ASS colours are blue-green-red: this is a warm yellow
MAX_GAP_S = 0.8  # a pause this long starts a new group
LINGER_S = 0.3  # how long the last word of a group stays up before the next group

_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,2,2,60,60,{margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build_ass(
    words: list[Word], start: float, end: float, *, font: str, max_words: int = 3, max_chars: int = 18, margin: int = 520
) -> str:
    """Captions for the part of the stream from `start` to `end`, with times relative to the clip, centred
    across the Short with their bottom `margin` pixels above its bottom edge."""
    clip_words = [w for w in words if w.end > start and w.start < end and _clean(w.text)]
    groups = list(_groups(clip_words, max_words, max_chars))
    events = []
    for g, group in enumerate(groups):
        texts = [_clean(w.text).upper() for w in group]
        next_start = groups[g + 1][0].start if g + 1 < len(groups) else end
        for k, word in enumerate(group):
            t0 = max(0.0, word.start - start)
            t1 = (group[k + 1].start if k + 1 < len(group) else min(word.end + LINGER_S, next_start)) - start
            t1 = min(t1, end - start)
            if t1 <= t0:
                continue
            text = " ".join(f"{{\\c{HIGHLIGHT}}}{t}{{\\r}}" if i == k else t for i, t in enumerate(texts))
            events.append(f"Dialogue: 0,{_ass_time(t0)},{_ass_time(t1)},Caption,,0,0,0,,{text}")
    return _HEADER.format(font=font, margin=margin) + "\n".join(events) + "\n"


def _groups(words: list[Word], max_words: int, max_chars: int) -> Iterator[list[Word]]:
    group: list[Word] = []
    for w in words:
        if group and (
            len(group) >= max_words
            or len(" ".join(_clean(x.text) for x in [*group, w])) > max_chars
            or w.start - group[-1].end > MAX_GAP_S
        ):
            yield group
            group = []
        group.append(w)
        if w.text.endswith(SENTENCE_END):
            yield group
            group = []
    if group:
        yield group


def _clean(text: str) -> str:
    """Strip characters ASS treats as markup."""
    return text.strip().replace("\\", "").replace("{", "(").replace("}", ")")


def _ass_time(t: float) -> str:
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"
