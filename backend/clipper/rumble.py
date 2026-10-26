"""Rumble channels and videos.

Rumble sits behind Cloudflare's bot check, and its pages have moved on from what yt-dlp expects (Oct 26, 2026):
- A channel's list of videos is built by JavaScript in the browser. The page a plain request gets holds one of
  the channel's videos at most, plus the "live now" strip of other channels' streams that every Rumble page has.
  So the list is read from the upload browser (studio.py, the same one that reads X), where the page runs as
  it does for you.
- A video's page carries the channel's newest (or featured) video's player before its own. yt-dlp takes the
  first one, and so does Rumble's oEmbed API, so both report, and download, the wrong video: the "9 Thugs" page
  came back as the "$1000 Viewer Event", and once a newer stream was up, the Viewer Event's page as that. Each
  video's own player is read from the page's description of its own video instead (player_url; the page's
  id, like v7h973m, isn't the player's), and yt-dlp is given that.
- The requests yt-dlp makes to Rumble pass themselves off as Chrome (with curl-cffi), which gets round the bot
  check. That was chosen knowingly on Oct 26, 2026, and stops working if Cloudflare catches on.
"""

import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright
from yt_dlp import YoutubeDL
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.networking.impersonate import ImpersonateTarget

from .studio import NOT_RUNNING

log = logging.getLogger(__name__)

VIDEO_PAGE = re.compile(r"rumble\.com/(v[0-9a-z]+)[-.]", re.IGNORECASE)  # rumble.com/v6abcd-the-title.html
PLAYER = re.compile(r"rumble\.com/embed/(?:[0-9a-z]+\.)?([0-9a-z]+)", re.IGNORECASE)  # rumble.com/embed/v5xyz/
# Rumble leaves the quotes off its attributes where it can: <script type=application/ld+json>
JSON_LD = re.compile(r"<script[^>]*type=[\"']?application/ld\+json[\"']?[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)
GRID_WAIT_S = 30  # for the channel's page to show its videos in the browser


class RumbleError(RuntimeError):
    """Rumble, or the upload browser, didn't give what was needed; the message says why in plain words."""


def is_rumble(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "rumble.com" or host.endswith(".rumble.com")


def ytdlp_options(url: str) -> dict:
    """What yt-dlp needs to get anything from Rumble: requests that look like Chrome's. Nothing for other sites."""
    return {"impersonate": ImpersonateTarget.from_str("chrome")} if is_rumble(url) else {}


def channel_videos(cdp_url: str, channel_url: str) -> list[str]:
    """Links to the videos on the channel's Live tab, as the upload browser shows it, in page order. The "live now"
    strip of other channels' streams is among them: tell those apart by who Rumble says made each (owner_is)."""
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp(cdp_url, timeout=15_000)
            except Exception as e:
                raise RumbleError(NOT_RUNNING) from e
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()
            try:
                page.goto(f"{channel_url}/livestreams", wait_until="load", timeout=60_000)
                links = _wait_for_videos(page)
                if not links:
                    raise RumbleError(
                        f"The channel's page showed no videos in the upload browser (its title: {page.title()!r}). Open "
                        f"the upload browser and look at {channel_url}/livestreams; Rumble may want it to prove it's human."
                    )
                return links
            finally:
                page.close()
    except RumbleError:
        raise
    except Exception as e:
        raise RumbleError(f"Lost the upload browser ({e})".splitlines()[0][:500]) from e


def _wait_for_videos(page) -> list[str]:
    """The page's video links once the list has filled in: when no new ones turn up for two seconds."""
    deadline = time.monotonic() + GRID_WAIT_S
    links: list[str] = []
    steady_since = time.monotonic()
    while time.monotonic() < deadline:
        hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
        now = video_links(hrefs)
        if len(now) != len(links):
            links, steady_since = now, time.monotonic()
        elif links and time.monotonic() - steady_since >= 2:
            break
        page.wait_for_timeout(500)
    return links


def video_links(hrefs: list[str]) -> list[str]:
    """Rumble video pages among links, without their tracking (?e9s=…), in order, once each."""
    found = []
    for href in hrefs:
        path = urlparse(href).path
        if VIDEO_PAGE.search(f"rumble.com{path}") and path.endswith(".html"):
            found.append(f"https://rumble.com{path}")
    return list(dict.fromkeys(found))


def player_url(video_url: str, ydl: YoutubeDL) -> str:
    """The video's own player (rumble.com/embed/…), which yt-dlp reads correctly. A player link is returned as it is.

    The page describes its own video for search engines (a JSON-LD VideoObject) with the page's link and its
    player's. Only a description whose link is this page counts, so another video's player on the page can't be
    taken for it. If there's none, it says so rather than guess.
    """
    if PLAYER.search(video_url):
        return video_url
    page = VIDEO_PAGE.search(video_url)
    if not page:
        raise RumbleError(f"{video_url} isn't a Rumble video's page.")
    try:
        html = ydl.urlopen(video_url).read().decode("utf-8", "replace")
    except HTTPError as e:
        raise RumbleError(refusal(e.status) if e.status in (403, 429) else
                          f"Rumble answered {e.status} for {video_url}.") from e
    for item in _described(html):
        own = VIDEO_PAGE.search(str(item.get("url") or ""))
        player = PLAYER.search(str(item.get("embedUrl") or ""))
        if item.get("@type") == "VideoObject" and own and own[1].lower() == page[1].lower() and player:
            return f"https://rumble.com/embed/{player[1]}/"
    raise RumbleError(f"Couldn't tell which player on {video_url} is its own: the page doesn't describe its video "
                      "the way Rumble's pages did (Oct 2026), so rumble.py needs updating.")


def _described(html: str) -> list[dict]:
    """Everything the page describes in its JSON-LD blocks, flattened."""
    found: list[dict] = []

    def walk(node) -> None:
        if isinstance(node, list):
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            found.append(node)
            walk(node.get("@graph"))

    for block in JSON_LD.findall(html):
        try:
            walk(json.loads(block))
        except ValueError:
            continue  # a broken block; the others may still say
    return found


UPLOAD_PAGE = "https://rumble.com/upload.php"

# What each form control on a page is: its kind, label, whether it shows, a choice's value and a list's options.
# What's typed into fields and hidden fields' values aren't read: they can hold the session's tokens.
_FORM_JS = r"""() => {
  const text = s => (s || "").replace(/\s+/g, " ").trim();
  const shown = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const labelOf = el => {
    const label = el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (label) return text(label.innerText);
    const wrap = el.closest("label");
    if (wrap) return text(wrap.innerText);
    return text(el.getAttribute("aria-label") || el.getAttribute("placeholder") || el.getAttribute("title"));
  };
  const controls = [...document.querySelectorAll(
    "input, select, textarea, button, [role=button], [role=radio], [role=checkbox], [contenteditable=true]"
  )].map(el => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || el.getAttribute("role") || "").toLowerCase();
    const choice = type === "radio" || type === "checkbox";
    const button = tag === "button" || type === "button" || type === "submit";
    return {
      tag, type, id: el.id || "", name: el.getAttribute("name") || "",
      label: labelOf(el).slice(0, 150),
      text: button ? text(el.innerText || el.value).slice(0, 80) : "",
      value: choice ? (el.value || "").slice(0, 80) : "",
      checked: !!el.checked, shown: shown(el), required: !!el.required,
      accept: el.getAttribute("accept") || "",
      options: tag === "select" ? [...el.options].map(o => [o.value, text(o.text)]) : [],
    };
  });
  const forms = [...document.forms].map(f => ({
    id: f.id || "", action: f.getAttribute("action") || "", method: (f.getAttribute("method") || "get").toLowerCase(),
  }));
  const headings = [...document.querySelectorAll("h1, h2, h3, h4, legend")].map(h => text(h.innerText)).filter(Boolean);
  return {url: location.href, title: document.title, forms, headings: headings.slice(0, 60), controls};
}"""


# The parts of the upload page that aren't plain form controls, as cut-down HTML: elements with the attributes
# that identify them, and their text. The licence choices are links, the terms boxes are drawn over hidden
# checkboxes, and the categories and any channel picker are built by the page's scripts.
_SECTIONS_JS = r"""() => {
  const KEEP = /^(id|class|name|type|value|for|href|role|data-[\w-]+|aria-[\w-]+|placeholder|title|checked|selected)$/;
  const SKIP = new Set(["script", "style", "svg", "noscript", "link", "meta", "iframe", "img"]);
  const brief = node => {
    if (node.nodeType === 3) return node.textContent.replace(/\s+/g, " ").trim().slice(0, 120);
    if (node.nodeType !== 1 || SKIP.has(node.tagName.toLowerCase())) return "";
    if (node.matches("input[type=hidden]")) return "";
    const tag = node.tagName.toLowerCase();
    const attrs = [...node.attributes].filter(a => KEEP.test(a.name))
      .map(a => a.value === "" ? a.name : `${a.name}="${a.value.slice(0, 80)}"`).join(" ");
    const inner = [...node.childNodes].map(brief).filter(Boolean).join("");
    return `<${tag}${attrs ? " " + attrs : ""}>${inner}</${tag}>`;
  };
  const form = document.getElementById("form"), form2 = document.getElementById("form2");
  const parts = {
    "Step 1: video info, categories, visibility (#form)": form ? brief(form) : "",
    "Step 2: licensing and terms (#form2)": form2 ? brief(form2) : "",
  };
  // A channel picker outside those two, if there's one (inside them, it's shown above).
  const main = document.querySelector("main") || document.body;
  const channel = [...main.querySelectorAll("*")].filter(e =>
    !e.closest("#form, #form2") && /channel/i.test([e.id, e.getAttribute("class"), e.getAttribute("name")].join(" ")));
  parts["Channel picker elsewhere"] = channel.slice(0, 5).map(e => brief(e.parentElement || e)).join("\n");
  return parts;
}"""


def upload_page(cdp_url: str, save_dir: Path, sections: bool = False) -> dict:
    """What Rumble's upload page asks for, as the upload browser shows it: its forms and their controls (see
    describe_form), and with `sections`, the parts that aren't plain controls as cut-down HTML. The page and a
    screenshot of it are saved in `save_dir`, for building the uploader against."""
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp(cdp_url, timeout=15_000)
            except Exception as e:
                raise RumbleError(NOT_RUNNING) from e
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()
            try:
                page.goto(UPLOAD_PAGE, wait_until="load", timeout=60_000)
                page.wait_for_timeout(5000)  # for its scripts to build the form
                save_dir.mkdir(parents=True, exist_ok=True)
                (save_dir / "upload-page.html").write_text(page.content(), encoding="utf-8")
                page.screenshot(path=str(save_dir / "upload-page.png"), full_page=True, timeout=15_000)
                found = page.evaluate(_FORM_JS)
                if sections:
                    found["sections"] = page.evaluate(_SECTIONS_JS)
                return found
            finally:
                page.close()
    except RumbleError:
        raise
    except Exception as e:
        raise RumbleError(f"Lost the upload browser ({e})".splitlines()[0][:500]) from e


def describe_form(page: dict) -> list[str]:
    """upload_page's findings as lines of text, in page order: what shows, then what's hidden for now (later steps
    of the form often are)."""
    controls = page.get("controls") or []
    lines = []
    if "login" in (page.get("url") or "").lower() or any(c["type"] == "password" and c["shown"] for c in controls):
        lines.append("The upload browser isn't signed in to Rumble: open it, sign in at rumble.com, then run this again.")
    lines.append(f"Page: {page.get('title', '')} ({page.get('url', '')})")
    for form in page.get("forms") or []:
        lines.append(f"Form: {form['method'].upper()} {form['action'] or '(this page)'}" + (f"  #{form['id']}" if form["id"] else ""))
    if page.get("headings"):
        lines.append("Headings: " + " | ".join(page["headings"]))
    for shown in (True, False):
        group = [c for c in controls if c["shown"] == shown and c["type"] != "hidden"]
        lines.append(f"{'Showing' if shown else 'Hidden for now'} ({len(group)}):")
        lines += ["  " + _control(c) for c in group]
    hidden_fields = [c["name"] or c["id"] for c in controls if c["type"] == "hidden"]
    if hidden_fields:
        lines.append("Hidden fields: " + ", ".join(filter(None, hidden_fields)))
    return lines


def _control(c: dict) -> str:
    kind = c["tag"] + (f"[{c['type']}]" if c["type"] and c["type"] != c["tag"] else "")
    parts = [kind]
    parts += [f"#{c['id']}"] if c["id"] else []
    parts += [f"name={c['name']}"] if c["name"] else []
    parts += [f"value={c['value']}"] if c["value"] else []
    parts += [f"accept={c['accept']}"] if c["accept"] else []
    parts += ["checked"] if c["checked"] else []
    parts += ["required"] if c["required"] else []
    words = c["text"] or c["label"]
    parts += [f'"{words}"'] if words else []
    if c["options"]:
        shown = [f"{value}={label}" for value, label in c["options"][:150]]
        more = len(c["options"]) - len(shown)
        parts.append("options: " + "; ".join(shown) + (f"; and {more} more" if more > 0 else ""))
    return "  ".join(parts)


def owner_is(video: dict, channel_url: str) -> bool:
    """Whether Rumble says the video belongs to the channel: by its channel link (/c/Name or /user/Name), else by
    its channel's name. A video that says neither doesn't count, rather than risk another channel's stream."""
    want = channel_url.rstrip("/").rsplit("/", 1)[-1].lower()
    owner = (video.get("channel_url") or video.get("uploader_url") or "").rstrip("/")
    if owner:
        return owner.rsplit("/", 1)[-1].lower() == want
    names = {re.sub(r"\W", "", (video.get(k) or "").lower()) for k in ("channel", "uploader")}
    return re.sub(r"\W", "", want) in names


def refusal(status: int) -> str:
    if status == 429:
        return ("Rumble says the server asked too often (429 Too Many Requests). It usually clears up within the hour; "
                "the channel is tried again at each check.")
    if status == 403:
        return ("Rumble's Cloudflare check turned the server away (403 Forbidden). A newer yt-dlp (rebuild the image) "
                "may get through again.")
    return f"Rumble answered {status}."
