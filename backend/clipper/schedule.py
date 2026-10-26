"""When queued uploads go.

A show can space its clips out (Show.upload_gap_hours), so a channel gets one every few hours instead of a
whole stream's worth at once. Turns are per channel, in queue order: a clip waits until the gap has passed
since the last clip upload to its channel started. Full episodes never wait and don't take a turn. "Upload
now" (UploadJob.rush) skips the wait, but still takes a turn, so the next clip waits a gap after it.

A video whose thumbnail is still being made (thumbnails.py) also waits for that, up to THUMBNAIL_WAIT, so
the thumbnail goes up with it.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlmodel import Session, col, select

from .db import ClipRecord, Show, UploadJob, Vod, utcnow

THUMBNAIL_WAIT = timedelta(minutes=30)


@dataclass
class Turn:
    at: datetime  # when it may start
    reason: str = ""  # why it waits: "gap" (the show's spacing) or "thumbnail"


def upload_turns(s: Session, now: datetime | None = None) -> dict[int, Turn]:
    """Each queued upload's turn by job id, in queue order."""
    now = now or utcnow()
    gaps = {show.id: show.upload_gap_hours for show in s.exec(select(Show))}
    last: dict[int | None, datetime] = {}  # the latest clip upload to each channel; failed ones don't count
    started = s.exec(
        select(UploadJob.started_at, ClipRecord.destination_id).join(ClipRecord)
        .where(col(UploadJob.started_at).is_not(None), col(UploadJob.status).in_(("uploading", "done")),
               ClipRecord.kind != "whole")
    )
    for at, dest in started:
        last[dest] = max(at, last.get(dest, at))

    turns: dict[int, Turn] = {}
    queued = s.exec(
        select(UploadJob, ClipRecord, Vod)
        .join(ClipRecord, UploadJob.clip_id == ClipRecord.id)
        .join(Vod, ClipRecord.vod_id == Vod.id)
        .where(UploadJob.status == "queued")
        .order_by(col(UploadJob.created_at), col(UploadJob.id))
    )
    for job, clip, vod in queued:
        turn = Turn(now)
        if not job.rush:
            gap = gaps.get(vod.show_id) or 0
            if clip.kind != "whole" and gap and clip.destination_id in last:
                due = last[clip.destination_id] + timedelta(hours=gap)
                if due > now:
                    turn = Turn(due, "gap")
            thumbnail_by = job.created_at + THUMBNAIL_WAIT
            if job.which == "video" and clip.thumbnail_status in ("queued", "making") and thumbnail_by > turn.at:
                turn = Turn(thumbnail_by, "thumbnail")
        if clip.kind != "whole":
            last[clip.destination_id] = max(turn.at, last.get(clip.destination_id, turn.at))
        turns[job.id] = turn
    return turns
