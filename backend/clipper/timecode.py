"""Convert between seconds and HH:MM:SS timecodes."""

import re

_TIMECODE = re.compile(r"^\s*(?:(\d+):)?(\d+):(\d{2}(?:\.\d+)?)\s*$")


def to_seconds(tc: str) -> float:
    """'1:02:03' -> 3723.0. Also accepts 'MM:SS' and fractional seconds."""
    m = _TIMECODE.match(tc)
    if not m:
        raise ValueError(f"not a timecode: {tc!r}")
    hours, minutes, seconds = m.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)


def to_timecode(seconds: float) -> str:
    """3723.9 -> '01:02:03' (rounds down to whole seconds)."""
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"
