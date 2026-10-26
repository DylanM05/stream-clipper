"""Fetch a VOD with yt-dlp (or register a local file) and record its metadata in meta.json."""

import hashlib
import logging
from collections.abc import Callable
from pathlib import Path

from yt_dlp import YoutubeDL

from .. import rumble
from ..config import Settings
from ..ffmpeg import probe_duration
from ..workspace import Workspace, write_json

log = logging.getLogger(__name__)

_META_FIELDS = (
    "id", "title", "uploader", "uploader_id", "channel", "extractor_key", "webpage_url",
    "duration", "upload_date", "timestamp", "release_timestamp", "chapters", "description",
)


def fetch(
    source: str,
    settings: Settings,
    on_progress: Callable[[float], None] | None = None,
    limit_mbps: float | None = None,
) -> Workspace:
    """`limit_mbps` caps the download speed (0 = none); without it, the CLIPPER_DOWNLOAD_LIMIT_MBPS setting."""
    path = Path(source)
    if path.is_file():
        return _register_local(path, settings)
    return _download(source, settings, on_progress, settings.download_limit_mbps if limit_mbps is None else limit_mbps)


def _register_local(path: Path, settings: Settings) -> Workspace:
    path = path.resolve()
    digest = hashlib.sha1(f"{path}|{path.stat().st_size}".encode()).hexdigest()[:12]
    ws = Workspace.open(settings.vods_dir, f"local-{digest}")
    if not ws.meta_json.exists():
        write_json(ws.meta_json, {
            "id": digest,
            "title": path.stem,
            "extractor_key": "Local",
            "duration": probe_duration(path),
            "chapters": [],
            "source_path": str(path),
        })
    return ws


def _download(url: str, settings: Settings, on_progress: Callable[[float], None] | None, limit_mbps: float) -> Workspace:
    with YoutubeDL({"quiet": True, "no_warnings": True, **rumble.ytdlp_options(url)}) as ydl:
        # A Rumble video page holds another video's player first, which yt-dlp would take: go by its own (rumble.py).
        info = ydl.extract_info(rumble.player_url(url, ydl) if rumble.is_rumble(url) else url, download=False)
    if info.get("_type") == "playlist":
        raise ValueError("That URL is a playlist or channel; give the URL of a single video or VOD.")
    if info.get("is_live"):
        raise ValueError("That stream is still live. Process it once the VOD is available.")

    ws = Workspace.open(settings.vods_dir, f"{info['extractor_key']}-{info['id']}")
    if ws.meta_json.exists() and ws.source.exists():
        log.info("Already downloaded: %s", ws.source)
        return ws

    h = settings.max_height
    opts = {
        "format": f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": str(ws.root / "source.%(ext)s"),
        "concurrent_fragment_downloads": 8,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [_ProgressLog(on_progress)],
        **speed_limit(limit_mbps),
        **rumble.ytdlp_options(url),
    }
    log.info("Downloading %r (%s)%s", info.get("title"), info.get("webpage_url") or url,
             f" at up to {limit_mbps:g} Mbit/s" if limit_mbps else "")
    with YoutubeDL(opts) as ydl:
        info = ydl.process_ie_result(info, download=True)

    downloads = info.get("requested_downloads") or []
    source = Path(downloads[0]["filepath"]) if downloads else ws.root / "source.mp4"
    meta = {k: info.get(k) for k in _META_FIELDS}
    meta["description"] = (meta["description"] or "")[:2000]
    meta["chapters"] = meta["chapters"] or []
    meta["source_path"] = str(source)
    write_json(ws.meta_json, meta)
    log.info("Downloaded to %s", source)
    return ws


def speed_limit(mbps: float) -> dict:
    """yt-dlp options capping the total download speed. Its limit applies to each piece of a stream
    downloaded in parallel, so with a limit the pieces are fetched one at a time."""
    if not mbps or mbps <= 0:
        return {}
    return {"ratelimit": int(mbps * 1_000_000 / 8), "concurrent_fragment_downloads": 1}


class _ProgressLog:
    """yt-dlp progress hook that logs every 10% instead of redrawing a progress bar."""

    def __init__(self, on_progress: Callable[[float], None] | None = None) -> None:
        self._next: dict[str, float] = {}
        self._on_progress = on_progress

    def __call__(self, d: dict) -> None:
        name = d.get("filename", "")
        if d.get("status") == "finished":
            log.info("Finished downloading %s", Path(name).name)
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if d.get("status") != "downloading" or not total:
            return
        frac = d.get("downloaded_bytes", 0) / total
        if self._on_progress:
            self._on_progress(min(1.0, frac))
        if frac >= self._next.get(name, 0.1):
            log.info("Downloading %s: %d%%", Path(name).name, frac * 100)
            self._next[name] = (int(frac * 10) + 1) / 10
