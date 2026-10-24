"""Settings, read from CLIPPER_* environment variables or a .env file."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLIPPER_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")

    # Download
    max_height: int = 1080
    # Keeps a long VOD download from using the whole connection (0 = no limit). Also changeable in the app.
    download_limit_mbps: float = 50

    # Disk space. A stream's big files (source, preview, audio, renders) are deleted this many days after
    # all its clips are uploaded or rejected (0 = right away); also changeable in the app. Each rendered clip
    # is deleted once the worker has uploaded it. New downloads wait while the data disk has less than
    # min_free_gb free.
    cleanup_after_days: float = 0
    min_free_gb: float = 40

    # Transcription (faster-whisper)
    whisper_model: str = "turbo"
    whisper_device: str = "cuda"
    whisper_compute_type: str = "float16"
    language: str | None = "en"  # None = auto-detect

    # Segmentation (Claude Code in headless mode, signed in with your subscription)
    claude_bin: str = "claude"
    llm_model: str = "opus"
    llm_effort: str | None = "high"
    llm_timeout_s: int = 3600
    highlights_per_hour: float = 3.0

    # Rendering
    video_encoder: str = "h264_nvenc"
    hwaccel: str | None = "cuda"
    caption_font: str = "DejaVu Sans"
    short_layout: Literal["blur", "crop"] = "blur"
    proxy_height: int = 480  # the small copy the phone app previews and trims with
    # The preview is made in the background once transcription is done, while Claude picks clips. With
    # preview_with_transcription it starts right after the download, alongside transcription: done sooner,
    # but the GPU and memory carry both at once. preview_on_gpu shrinks frames on the GPU when ffmpeg can.
    preview_with_transcription: bool = False
    preview_on_gpu: bool = True

    # Review app and worker
    app_password: str | None = None  # unset means no login, which is only for local testing
    secret_key: str | None = None  # signs login cookies; derived from the password when unset
    web_dir: Path | None = None  # the built phone app; defaults to frontend/dist in the repo
    worker_poll_s: float = 3.0
    monitor_interval_s: int = 600  # how often each watched channel is checked

    # Uploading through the YouTube API (youtube.py): a Google Cloud OAuth client of type "Web application".
    # Channels connected in the app upload this way; the others go through YouTube Studio.
    google_client_id: str = ""
    google_client_secret: str = ""
    youtube_category_id: str = "24"  # Entertainment; the API doesn't use the channel's upload defaults

    # Uploading through YouTube Studio in a signed-in browser (studio.py)
    studio_cdp_url: str = "http://127.0.0.1:9222"  # the browser's remote debugging address
    browser_view_url: str = ""  # where you see that browser to sign in, e.g. its noVNC page

    # Notifications (notify.py): an ntfy topic link like https://ntfy.sh/<topic> (with an access token if the
    # topic is protected) and/or a Discord channel's webhook link. app_url, the app's address, lets a
    # notification open the right page.
    ntfy_url: str = ""
    ntfy_token: str = ""
    discord_webhook_url: str = ""
    app_url: str = ""

    @property
    def studio_dir(self) -> Path:
        """Upload screenshots and the browser status the worker reports."""
        return self.data_dir / "studio"

    @property
    def browser_profile_dir(self) -> Path:
        """The desktop upload browser's profile (`clipper browser`), which keeps its YouTube sign-in."""
        return self.data_dir / "browser-profile"

    @field_validator("language", "llm_effort", "hwaccel", mode="before")
    @classmethod
    def _none_values(cls, v):
        # "none" as well as empty: PowerShell deletes a variable that is set to "".
        return None if isinstance(v, str) and v.strip().lower() in ("", "none", "off") else v

    @property
    def vods_dir(self) -> Path:
        return self.data_dir / "vods"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "clipper.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
