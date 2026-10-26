"""Uploads to YouTube by driving YouTube Studio in a real browser that's signed in to your account.

YouTube's Terms of Service only allow automated access through the official API, so this puts the
channels at some risk; it was chosen knowingly over waiting for API approval (docs/plan.md, Phase 3).

How it fits together:
- A normal Chrome/Chromium runs with a saved profile and a remote debugging port: the `browser`
  service in Docker (viewable through noVNC), or `clipper browser` on a desktop. You sign in to
  Google in it by hand once; Google refuses sign-ins it can tell are automated.
- The worker connects to that browser and fills in Studio's upload dialog, one upload at a time.
- Studio is a web page, not an API: when YouTube changes it, the selectors below need updating.
  A failed upload saves a screenshot of the page to show what changed. Studio must be in English.
"""

import json
import logging
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from yt_dlp import YoutubeDL

log = logging.getLogger(__name__)

VISIBILITY = {"public": "PUBLIC", "unlisted": "UNLISTED", "private": "PRIVATE"}
UPLOAD_TIMEOUT_S = 12 * 3600  # full episodes can be tens of gigabytes
SIGNED_IN_COOKIES = {"SAPISID", "__Secure-3PAPISID", "SID", "__Secure-1PSID"}
X_SIGNED_IN_COOKIES = {"auth_token"}  # the same browser reads X posts (xposts.py)


class StudioError(RuntimeError):
    def __init__(self, message: str, screenshot: str = "", link: str = "", retry: bool = False):
        super().__init__(message)
        self.screenshot = screenshot
        self.link = link  # set when the video already exists in Studio
        self.retry = retry  # it failed before the file reached Studio, so trying again later can't post it twice


@dataclass
class _Progress:
    attached: bool = False  # the file is in Studio: from here on, a failure leaves a draft behind
    link: str = ""


@dataclass
class Upload:
    file: Path
    title: str
    description: str
    visibility: str  # public, unlisted, private
    channel_id: str  # UC…; empty uploads to whichever channel the browser has selected


NOT_RUNNING = (
    "The upload browser isn't running. On the server, start the browser service (docker compose up -d); "
    "on a desktop, run `clipper browser` and leave it open."
)


class Studio:
    def __init__(self, cdp_url: str, screenshots_dir: Path, desktop_profile: Path | None = None):
        """With `desktop_profile`, an upload opens the desktop browser itself if it isn't running."""
        self.cdp_url = cdp_url
        self.screenshots_dir = screenshots_dir
        self.desktop_profile = desktop_profile

    def status(self) -> dict:
        """Whether the browser is reachable and signed in to YouTube, and to X."""
        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(self.cdp_url, timeout=10_000)
                context = browser.contexts[0] if browser.contexts else None
                return {
                    "browser": True,
                    "signed_in": signed_in(context, "https://www.youtube.com", SIGNED_IN_COOKIES),
                    "x_signed_in": signed_in(context, "https://x.com", X_SIGNED_IN_COOKIES),
                }
        except Exception as e:
            log.debug("Browser not reachable at %s: %s", self.cdp_url, e)
            return {"browser": False, "signed_in": False, "x_signed_in": False}

    def upload(self, job: Upload, on_progress: Callable[[float], None] = lambda _: None) -> str:
        """Upload through Studio and return the video's link. Raises StudioError, with a screenshot when it can."""
        try:
            with sync_playwright() as p:
                browser = self._connect(p)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.new_page()
                progress = _Progress()
                try:
                    link = self._upload(page, job, on_progress, progress)
                except Exception as e:
                    message = str(e).splitlines()[0][:400]
                    shot = self._screenshot(page)
                    if not progress.attached:
                        page.close()
                        raise StudioError(message, shot, retry=True) from e
                    # The file is already in Studio as a private draft. Leave the dialog open so it can be
                    # finished by hand; retrying would upload a second copy.
                    raise StudioError(
                        f"{message}. The video is already in YouTube Studio as a private draft, left open in the "
                        "upload browser: finish it there (or delete it) rather than retrying.",
                        shot,
                        progress.link,
                    ) from e
                page.close()
                return link
        except StudioError:
            raise
        except Exception as e:
            raise StudioError(f"Lost the upload browser ({e})".splitlines()[0][:500]) from e

    def _connect(self, p):
        try:
            return p.chromium.connect_over_cdp(self.cdp_url, timeout=15_000)
        except Exception as e:
            if not self.desktop_profile or urlparse(self.cdp_url).hostname not in ("127.0.0.1", "localhost"):
                raise StudioError(NOT_RUNNING, retry=True) from e
        # On a desktop: open it (the YouTube sign-in is kept in its profile) and wait for it to listen.
        try:
            launch_desktop_browser(self.desktop_profile, urlparse(self.cdp_url).port or 9222)
        except FileNotFoundError as e:
            raise StudioError(NOT_RUNNING, retry=True) from e
        log.info("Opened the upload browser")
        deadline = time.monotonic() + 30
        while True:
            try:
                return p.chromium.connect_over_cdp(self.cdp_url, timeout=5_000)
            except Exception as e:
                if time.monotonic() > deadline:
                    raise StudioError(NOT_RUNNING, retry=True) from e
                time.sleep(1)

    def _upload(self, page: Page, job: Upload, on_progress: Callable[[float], None], progress: "_Progress") -> str:
        url = (
            f"https://studio.youtube.com/channel/{job.channel_id}/videos/upload?d=ud"
            if job.channel_id else "https://www.youtube.com/upload"
        )
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        self._wait_for_studio(page)

        log.info("Studio: choosing %s", job.file.name)
        page.locator('input[type="file"]').set_input_files(str(job.file), timeout=60_000)
        progress.attached = True

        title = page.locator("#title-textarea #textbox")
        title.wait_for(timeout=120_000)
        title.fill(job.title)
        page.locator("#description-textarea #textbox").fill(job.description)
        page.locator('tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]').click(timeout=30_000)

        for _ in range(3):  # Details -> Video elements -> Checks -> Visibility
            page.locator("#next-button").click(timeout=30_000)
            page.wait_for_timeout(1500)
        page.locator(f'tp-yt-paper-radio-button[name="{VISIBILITY[job.visibility]}"]').click(timeout=30_000)

        progress.link = self._video_link(page)
        self._wait_for_upload(page, on_progress)
        save = page.locator("#done-button").or_(page.get_by_role("button", name=re.compile(r"^(Save|Publish|Schedule)$")))
        save.first.click(timeout=30_000)
        page.wait_for_timeout(3000)  # let Studio save before the tab closes
        log.info("Studio: uploaded %s", progress.link or "(link not found)")
        return progress.link

    @staticmethod
    def _wait_for_studio(page: Page, wait_s: float = 30) -> None:
        """Wait to land in Studio. Google's account pages can come first, e.g. to refresh the sign-in after
        the browser restarts or to switch to another channel, and send the page on by themselves; one
        that stays is asking for a sign-in."""
        try:
            page.wait_for_url(
                lambda url: urlparse(url).hostname == "studio.youtube.com",
                wait_until="domcontentloaded",
                timeout=wait_s * 1000,
            )
        except PlaywrightTimeoutError:
            raise StudioError(
                "YouTube asked the upload browser to sign in before opening this channel's Studio. Open the "
                "upload browser, sign in to the Google account that manages the channel, then retry."
            ) from None

    @staticmethod
    def _video_link(page: Page, wait_s: float = 30) -> str:
        """The new video's link, shown in the upload dialog. "" if it can't be found (not worth failing over).

        Only looks inside the dialog: the channel's other videos are listed behind it.
        """
        dialog = page.locator("ytcp-uploads-dialog")
        scope = dialog if dialog.count() else page
        links = scope.locator('a[href*="youtu.be/"], a[href*="youtube.com/shorts/"], a[href*="youtube.com/watch?v="]')
        shown = scope.get_by_text(re.compile(r"https://(youtu\.be|(www\.)?youtube\.com/shorts)/[\w-]{6,}"))
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            for i in range(links.count()):
                href = links.nth(i).get_attribute("href") or ""
                if re.search(r"youtu\.be/[\w-]{6,}|/shorts/[\w-]{6,}|watch\?v=[\w-]{6,}", href):
                    return href
            if shown.count():
                return shown.first.inner_text().strip()
            page.wait_for_timeout(1000)
        return ""

    def _wait_for_upload(self, page: Page, on_progress: Callable[[float], None]) -> None:
        """Wait until the file has finished uploading; the tab has to stay open until then.
        While it uploads, the dialog's footer says "Uploading 45% … minutes left"."""
        uploading = page.get_by_text(re.compile(r"Uploading\s+\d+\s*%")).filter(visible=True)
        deadline = time.monotonic() + UPLOAD_TIMEOUT_S
        while time.monotonic() < deadline:
            if not uploading.count():
                on_progress(1.0)
                return
            if percent := re.search(r"(\d+)\s*%", uploading.first.inner_text()):
                on_progress(int(percent[1]) / 100)
            page.wait_for_timeout(5000)
        raise StudioError("The upload didn't finish within 12 hours")

    def _screenshot(self, page: Page) -> str:
        try:
            self.screenshots_dir.mkdir(parents=True, exist_ok=True)
            path = self.screenshots_dir / f"upload-failed-{int(time.time())}.png"
            page.screenshot(path=str(path), full_page=True, timeout=15_000)
            return str(path)
        except Exception:
            return ""


def resolve_channel_id(url: str) -> str:
    """The UC… id of a YouTube channel link, looking up @handles with yt-dlp. "" if it can't be found."""
    if m := re.search(r"/channel/(UC[\w-]{20,})", url):
        return m[1]
    if not re.search(r"youtube\.com/(@[\w.-]+|c/|user/)", url):
        return ""
    try:
        opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "playlistend": 1}
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url if "://" in url else f"https://{url}", download=False)
        return info.get("channel_id") or ""
    except Exception as e:
        log.warning("Couldn't look up the channel id for %s: %s", url, e)
        return ""


def signed_in(context, site: str, cookie_names: set[str]) -> bool:
    """Whether the browser has a sign-in cookie for the site."""
    return bool(context and {c["name"] for c in context.cookies(site)} & cookie_names)


def write_status(status_file: Path, status: dict) -> None:
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps({**status, "checked_at": time.time()}), encoding="utf-8")


def read_status(status_file: Path) -> dict:
    try:
        return json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"browser": False, "signed_in": False, "x_signed_in": False, "checked_at": None}


def launch_desktop_browser(profile_dir: Path, port: int = 9222) -> subprocess.Popen:
    """Open Chrome or Edge with its own profile and a debugging port, for signing in on a desktop."""
    candidates = [
        shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chromium-browser"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    exe = next((c for c in candidates if c and Path(c).exists()), None)
    if not exe:
        raise FileNotFoundError("No Chrome, Chromium or Edge found")
    profile_dir.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen([
        exe, f"--user-data-dir={profile_dir}", f"--remote-debugging-port={port}",
        "--no-first-run", "--no-default-browser-check", "https://studio.youtube.com",
    ])
