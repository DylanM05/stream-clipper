"""How uploads do on YouTube: their views, likes and comments, read through the YouTube Data API.

The worker reads them every few hours for an upload's first month, and keeps its views a day and a week after
it went up, so videos of different ages can be compared: the AI's score against what viewers did with it
(score_bands), and what the show's past clips teach the AI when it picks the next stream's (learning.py). A Monday-morning
notification sums up the week (digest). Reading costs 1 unit of the daily API quota per 50 videos.

Uploads to channels connected to the API are read with their sign-in. The rest need CLIPPER_YOUTUBE_API_KEY,
or else any connected channel's sign-in, which can read public videos too.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

from .db import ClipRecord, UploadJob
from .youtube import Stats

REFRESH = timedelta(hours=6)
TRACK_FOR = timedelta(days=30)
DAY = timedelta(days=1)
WEEK = timedelta(days=7)
# The AI's score bands compared in the app; the full episode is always 1, so it's left out.
BANDS = ((0.9, "0.9 and up"), (0.8, "0.8 to 0.89"), (0.0, "Under 0.8"))


def age(job: UploadJob, now: datetime) -> timedelta:
    return now - (job.finished_at or job.updated_at)


def due(job: UploadJob, now: datetime) -> bool:
    """Whether an upload's numbers should be read now: a day or a week after it went up, and every few hours
    in its first month."""
    a = age(job, now)
    if (job.views_day1 is None and DAY <= a < 2 * DAY) or (job.views_week1 is None and WEEK <= a < WEEK + DAY):
        return True
    return a < TRACK_FOR and (job.stats_at is None or now - job.stats_at >= REFRESH)


def record(job: UploadJob, stats: Stats, now: datetime) -> None:
    """Keep the latest numbers, and the views at a day or a week old when it's that age. Uploads first read
    when they're older than that (e.g. from before this existed) never get those."""
    job.views, job.likes, job.comments, job.stats_at = stats.views, stats.likes, stats.comments, now
    a = age(job, now)
    if job.views_day1 is None and DAY <= a < 2 * DAY:
        job.views_day1 = stats.views
    if job.views_week1 is None and WEEK <= a < WEEK + DAY:
        job.views_week1 = stats.views


@dataclass
class Band:
    label: str
    count: int  # uploads with numbers
    day1: float | None  # median views a day after going up
    week1: float | None
    views: float | None  # median views so far


def score_bands(rows: list[tuple[UploadJob, ClipRecord]]) -> list[Band]:
    """Median views by the AI's score, for chapters and highlights with numbers. Clips made in the app have no
    score from the AI, so they're left out."""
    bands = []
    upper = 2.0
    for low, label in BANDS:
        jobs = [j for j, c in rows if c.kind != "whole" and not c.manual and low <= c.score < upper and j.views is not None]
        upper = low
        bands.append(Band(
            label, len(jobs),
            *(_median([getattr(j, field) for j in jobs]) for field in ("views_day1", "views_week1", "views")),
        ))
    return bands


def digest(rows: list[tuple[UploadJob, ClipRecord]]) -> str:
    """The week's uploads in a few lines: how many, their views so far, and the most viewed."""
    views = sum(j.views or 0 for j, _ in rows)
    lines = [f"{len(rows)} upload{'' if len(rows) == 1 else 's'} · {compact(views)} views so far"]
    top = sorted((r for r in rows if r[0].views), key=lambda r: r[0].views, reverse=True)[:5]
    if top:
        lines += ["", "Most viewed:"]
        lines += [f"{i}. {clip.title}: {compact(job.views)}" for i, (job, clip) in enumerate(top, 1)]
    return "\n".join(lines)


def compact(n: float) -> str:
    """1234 → 1.2K"""
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + suffix
    return str(int(n))


def _median(values: list[int | None]) -> float | None:
    present = [v for v in values if v is not None]
    return median(present) if present else None
