"""Uploads to Rumble by filling in its upload page in the upload browser, signed in to the Rumble account whose
channel the videos go to.

Rumble's upload API needs an access key from its business team (bd@rumble.com), so this goes the way YouTube
Studio uploads do (studio.py): through the page, chosen on Oct 26, 2026. It breaks when Rumble changes the page;
a failed upload saves a screenshot of it to show what changed. `clipper rumble-upload-check` shows what the page
asks for. As read that way on Oct 26, 2026:
- Step 1 (#form): the file (#Filedata), which starts uploading as soon as it's chosen; the title, description, a
  primary category (a list the page's script builds, whose choice lands in #category_primary), a thumbnail of
  your own (#customThumb) and visibility, as radio buttons shown through their labels. Upload (#submitForm)
  goes on to step 2.
- Step 2 (#form2): the licence, chosen by clicking its box, with the upload's progress in .top_percent; two
  checkboxes (no exclusive deal with anyone else, and Rumble's terms) shown through their labels; Submit.
- Step 3: the new video's link, in #direct.
The page has no channel picker: an account has the one channel there.
"""

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .rumble import UPLOAD_PAGE
from .studio import NOT_RUNNING, connect

log = logging.getLogger(__name__)

# Rumble's primary categories (Oct 2026); one is required.
CATEGORIES = ("Automotive", "Cooking", "Entertainment", "Finance & Crypto", "Gaming", "Health & Science", "HowTo",
              "Music", "News", "Podcasts", "Sports", "Technology", "Travel", "Viral", "Vlogs")
# "Rumble Only (non-exclusive, similar to YouTube)", which is also Rumble's default. The two "Video Management"
# licences make Rumble the exclusive agent for the video, which only the creators could agree to, and
# "Personal Use" isn't monetized or searchable.
LICENCE = "Rumble Only"
UPLOAD_TIMEOUT_S = 12 * 3600  # full episodes can be tens of gigabytes
SETTLE_S = 15  # for the page's scripts to finish loading
START_WAIT_S = 30  # for the upload to start once the file is chosen
STEP_WAIT_S = 30
LINK_WAIT_S = 300
SIGNED_OUT = ("The upload browser isn't signed in to Rumble. Open it, sign in at rumble.com with the account whose "
              "channel the videos go to, then retry.")


class RumbleUploadError(RuntimeError):
    def __init__(self, message: str, screenshot: str = "", retry: bool = False):
        super().__init__(message)
        self.screenshot = screenshot
        self.retry = retry  # it wasn't submitted, so trying again later can't post it twice


@dataclass
class RumbleUpload:
    file: Path
    title: str
    description: str
    visibility: str  # public, unlisted or private
    category: str  # one of CATEGORIES
    thumbnail: Path  # Rumble takes one of your own; the app always gives one, so the page never waits on its own


def upload(cdp_url: str, job: RumbleUpload, screenshots_dir: Path,
           on_progress: Callable[[float], None] = lambda _: None) -> str:
    """Upload through Rumble's upload page and return the video's link. Raises RumbleUploadError, with a
    screenshot of the page when it can."""
    try:
        with sync_playwright() as p:
            try:
                browser = connect(p, cdp_url, 15_000)
            except Exception as e:
                raise RumbleUploadError(NOT_RUNNING, retry=True) from e
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()
            sent = [False]
            try:
                link = fill_in(page, job, on_progress, sent)
            except Exception as e:
                message = str(e).splitlines()[0][:400]
                shot = _screenshot(page, screenshots_dir)
                page.close()
                if sent[0]:
                    raise RumbleUploadError(
                        f"{message}. It had been submitted, so it may be on Rumble already: look at the channel's videos "
                        "there before retrying, or it goes up twice.", shot) from e
                raise RumbleUploadError(message, shot, retry=True) from e
            page.close()
            return link
    except RumbleUploadError:
        raise
    except Exception as e:
        raise RumbleUploadError(f"Lost the upload browser ({e})".splitlines()[0][:500], retry=True) from e


def fill_in(page: Page, job: RumbleUpload, on_progress: Callable[[float], None], sent: list[bool],
            page_url: str = UPLOAD_PAGE) -> str:
    """Fill in the upload page and submit it, then return the new video's link. sent[0] turns True on submitting."""
    seen = _watch(page)
    page.goto(page_url, wait_until="load", timeout=60_000)
    if "login" in page.url.lower() or page.locator("input[type=password]").filter(visible=True).count():
        raise RumbleUploadError(SIGNED_OUT)
    _settle(page)

    log.info("Rumble: choosing %s", job.file.name)
    _choose_file(page, job.file, seen)
    page.locator("#title").fill(job.title)
    page.locator("#description").fill(job.description)
    _choose_category(page, job.category)
    page.locator("#customThumb").set_input_files(str(job.thumbnail), timeout=30_000)
    _tick(page, f"visibility_{job.visibility}")
    page.locator("#submitForm").click(timeout=30_000)
    try:
        page.locator("#form2 .licensingOptions").wait_for(state="visible", timeout=STEP_WAIT_S * 1000)
    except PlaywrightTimeoutError:
        raise RumbleUploadError(
            (_errors(page) or "Rumble didn't go on from the video's details to the licensing step") + _noticed(seen)
        ) from None

    licence = page.locator("#form2 .licensingOptions .wight-box").filter(has_text=LICENCE)
    licence.locator("a.greenLink").click(timeout=30_000)  # not the box's link to the terms
    if "active-wight-box" not in (licence.get_attribute("class") or ""):
        raise RumbleUploadError(f'Couldn\'t choose the "{LICENCE}" licence on Rumble\'s upload page')
    _tick(page, "crights")  # no exclusive agreement with anyone else
    _tick(page, "cterms")  # Rumble's terms
    _wait_for_upload(page, on_progress)

    page.locator("#submitForm2").click(timeout=30_000)
    sent[0] = True
    link = _new_link(page)
    log.info("Rumble: uploaded %s", link)
    return link


def _watch(page: Page) -> list[str]:
    """What goes wrong on the page as it's filled in, for an error to say: its script errors, and requests that
    failed or were refused (by address, without the query, which can hold tokens)."""
    seen: list[str] = []

    def where(url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.netloc}{parsed.path}"[:120]

    page.on("console", lambda m: seen.append(f"script error: {m.text[:200]}") if m.type == "error" else None)
    page.on("requestfailed", lambda r: seen.append(f"{r.method} {where(r.url)} failed ({r.failure})"))
    page.on("response", lambda r: seen.append(f"{r.request.method} {where(r.url)} answered {r.status}") if r.status >= 400 else None)
    return seen


def _noticed(seen: list[str]) -> str:
    return f". The page also had: {'; '.join(seen[-6:])}" if seen else ""


def _settle(page: Page) -> None:
    """Give the page's scripts time to set up after it loads: one that isn't ready misses a file chosen too soon."""
    try:
        page.wait_for_load_state("networkidle", timeout=SETTLE_S * 1000)
    except PlaywrightTimeoutError:
        pass  # pages that keep polling never go quiet
    page.wait_for_timeout(2000)


def _choose_file(page: Page, file: Path, seen: list[str]) -> None:
    """Choose the video and wait for Rumble to start uploading it, choosing it again once if it doesn't. On Oct 27,
    2026 the page made its thumbnails from a file chosen right after it loaded, but never started uploading it."""
    chooser = page.locator("#Filedata")
    for _ in range(2):
        chooser.set_input_files(str(file), timeout=60_000)
        _check_chosen(chooser, file)
        if _upload_started(page):
            return
        log.info("Rumble: the upload didn't start; choosing the file again")
        # The same file chosen again isn't a change, so the page wouldn't hear of it: empty the chooser first (from a
        # script, which tells the page nothing).
        chooser.evaluate("el => { el.value = ''; }")
    raise RumbleUploadError("Rumble didn't start uploading the file" + (_noticed(seen) or ", and the page showed no error"))


def _check_chosen(chooser, file: Path) -> None:
    """That the page has the whole file. The browser opens it by its path (see studio.connect), so one that can't see
    the worker's files gets an empty one, and the page would only say it never started uploading."""
    try:
        size = chooser.evaluate("el => el.files.length ? el.files[0].size : -1")
    except Exception:
        return  # the page took the file and moved on
    if size not in (-1, file.stat().st_size):
        raise RumbleUploadError(
            f"The upload browser couldn't read {file}: it has to see the worker's files at the same place (in Docker, "
            "the data folder at /data in both)")


def _upload_started(page: Page) -> bool:
    """Whether the first step shows the upload going: a percentage, or some of its progress bar. (The second
    step's percentage reads 0% from the start, so it's no sign.)"""
    deadline = time.monotonic() + START_WAIT_S
    while time.monotonic() < deadline:
        texts = page.locator("#form .num_percent").all_inner_texts()
        widths = page.locator("#form .green_percent").evaluate_all("bars => bars.map(b => b.style.width)")
        if any(re.search(r"\d+\s*%", t) for t in texts) or any(w not in ("", "0", "0%", "0px") for w in widths):
            return True
        page.wait_for_timeout(1000)
    return False


def _choose_category(page: Page, category: str) -> None:
    """Pick from the list the page's script builds: open it, then click the entry."""
    box = page.locator(".select-container").filter(has=page.locator("input[name=primary-category]"))
    box.locator("input[name=primary-category]").click(timeout=30_000)
    box.locator(f'.select-option[data-label="{category}"]').click(timeout=30_000)
    if page.locator("#category_primary").input_value() in ("", "0"):
        raise RumbleUploadError(f"Couldn't choose the category {category} on Rumble's upload page")


def _tick(page: Page, input_id: str) -> None:
    """Tick a checkbox or radio button. They're hidden and drawn by their labels, and the terms label holds a link
    that a click on it could follow, so the input is clicked by script, which runs the page's own handlers."""
    page.locator(f"#{input_id}").evaluate("el => { if (!el.checked) el.click(); }")


def _wait_for_upload(page: Page, on_progress: Callable[[float], None]) -> None:
    """Wait for the file to finish uploading; the tab has to stay open until then. The page shows a percentage."""
    shown = page.locator(".top_percent, .num_percent")
    bar = page.locator("#form2 .progress-wrap")
    seen = False
    deadline = time.monotonic() + UPLOAD_TIMEOUT_S
    while time.monotonic() < deadline:
        percents = [int(m[1]) for text in shown.all_inner_texts() if (m := re.search(r"(\d+)\s*%", text))]
        if percents:
            seen = True
            on_progress(min(max(percents), 100) / 100)
            if max(percents) >= 100:
                return
        elif seen and not bar.is_visible():  # the progress went away: it's done
            on_progress(1.0)
            return
        if error := _errors(page):
            raise RumbleUploadError(error)
        page.wait_for_timeout(3000)
    raise RumbleUploadError("The upload didn't finish within 12 hours")


def _new_link(page: Page) -> str:
    """The new video's link, shown once it's submitted."""
    direct = page.locator("#direct")
    deadline = time.monotonic() + LINK_WAIT_S
    while time.monotonic() < deadline:
        if direct.count() and (link := re.search(r"https://rumble\.com/\S+", direct.input_value())):
            return link[0]
        if error := _errors(page):
            raise RumbleUploadError(error)
        page.wait_for_timeout(2000)
    raise RumbleUploadError("Rumble didn't show the new video's link")


def _errors(page: Page) -> str:
    """What the page's error messages say, if any shows."""
    return " ".join(t.strip() for t in page.locator(".error").filter(visible=True).all_inner_texts() if t.strip())


def _screenshot(page: Page, screenshots_dir: Path) -> str:
    try:
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        path = screenshots_dir / f"rumble-upload-failed-{int(time.time())}.png"
        page.screenshot(path=str(path), full_page=True, timeout=15_000)
        return str(path)
    except Exception:
        return ""
