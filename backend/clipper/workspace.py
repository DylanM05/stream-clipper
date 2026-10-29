"""Per-VOD working folder. Every pipeline step writes its output here, so a re-run skips steps that already finished."""

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Pipeline steps after download, in order, with the files each one produces.
STEP_OUTPUTS = {
    "audio": ["audio.wav"],
    "transcribe": ["transcript.json", "transcript.partial.json"],
    "signals": ["signals.json"],
    "segment": ["llm/segmentation.json"],
    "refine": ["segments.json"],
    "render": ["clips"],
}
STEPS = list(STEP_OUTPUTS)


def safe_id(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-").lower()[:80] or "vod"


@dataclass(frozen=True)
class Workspace:
    root: Path

    @classmethod
    def open(cls, vods_dir: Path, vod_id: str) -> "Workspace":
        ws = cls(vods_dir / safe_id(vod_id))
        ws.root.mkdir(parents=True, exist_ok=True)
        return ws

    @property
    def meta_json(self) -> Path:
        return self.root / "meta.json"

    @property
    def audio_wav(self) -> Path:
        return self.root / "audio.wav"

    @property
    def transcript_json(self) -> Path:
        return self.root / "transcript.json"

    @property
    def transcript_partial_json(self) -> Path:
        """The windows transcribed so far, while transcription is under way (see transcribe.py)."""
        return self.root / "transcript.partial.json"

    @property
    def signals_json(self) -> Path:
        return self.root / "signals.json"

    @property
    def llm_dir(self) -> Path:
        return self.root / "llm"

    @property
    def segmentation_json(self) -> Path:
        return self.llm_dir / "segmentation.json"

    @property
    def segments_json(self) -> Path:
        return self.root / "segments.json"

    @property
    def clips_dir(self) -> Path:
        return self.root / "clips"

    @property
    def proxy_mp4(self) -> Path:
        return self.root / "proxy.mp4"

    @property
    def thumbs_dir(self) -> Path:
        return self.root / "thumbs"

    @property
    def frames_dir(self) -> Path:
        """Stills for the editor's timeline, made from the preview copy as they're asked for."""
        return self.thumbs_dir / "frames"

    def meta(self) -> dict:
        return read_json(self.meta_json)

    @property
    def source(self) -> Path:
        return Path(self.meta()["source_path"])

    def delete_media(self) -> int:
        """Delete the big files (source, preview, audio, renders, the editor's stills) and return how many bytes that freed. The
        transcript and Claude's picks stay, and so does a source that was added from elsewhere by its path."""
        targets = [self.proxy_mp4, self.audio_wav, self.clips_dir, self.frames_dir,
                   *self.root.glob("audio.tmp.*"), *self.root.glob("proxy.tmp.*")]
        if self.meta_json.exists() and self.source.resolve().parent == self.root.resolve():
            targets += self.root.glob("source.*")
        return sum(remove(path) for path in targets)

    def clear_from(self, step: str) -> None:
        """Delete the output of `step` and every later step, so they run again."""
        for later in STEPS[STEPS.index(step):]:
            for name in STEP_OUTPUTS[later]:
                path = self.root / name
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()


def disk_free_gb(path: Path) -> float:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free / 1e9


def remove(path: Path) -> int:
    """Delete a file or folder; returns how many bytes that freed."""
    if path.is_dir():
        size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        shutil.rmtree(path)
        return size
    if path.exists():
        size = path.stat().st_size
        path.unlink()
        return size
    return 0


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    write_text(path, json.dumps(data, indent=2, ensure_ascii=False))


def write_text(path: Path, text: str) -> None:
    """Write via a temp file, so an interrupted run never leaves a half-written output that looks finished."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
