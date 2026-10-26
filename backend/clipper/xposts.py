"""Reads an X account's newest video posts through the upload browser, signed in to X.

X only shows an account's posts to signed-in visitors, and its API costs money, so the worker opens the
profile in the browser that YouTube Studio uploads go through (studio.py). You sign that browser in to
X by hand once, through its live view. The posts are read from the data X's own page loads, not from
the page's layout, and found wherever they are in it, since X rearranges that data now and then.

X's terms don't allow automated access, even through your own sign-in. One profile looked at every few
minutes is unlikely to stand out, but X could ask the account to verify itself or lock it: sign in
with a spare account, never your main one.
"""

import html
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from playwright.sync_api import Page, sync_playwright

from .studio import NOT_RUNNING, X_SIGNED_IN_COOKIES, signed_in

# X's page loads its data from these, each named after what it fetches. A profile's posts came from
# "UserTweets"; by Oct 2026 they come from "UserOriginalsTimeline".
GRAPHQL = re.compile(r"/graphql/[^/?]+/(\w+)")
SIGNED_OUT = (
    "The upload browser isn't signed in to X. Open it, sign in at x.com with a spare X account (not your main "
    "one), then tap Check."
)
TOO_OFTEN = "X says the account in the upload browser is looking at profiles too often. It tries again on the next check."


class XError(RuntimeError):
    pass


@dataclass
class Post:
    id: str
    url: str
    text: str
    duration: float | None  # of its video, in seconds

    @property
    def posted(self) -> float:
        """When it was posted, as a unix time: X's post ids count milliseconds since late 2010 in their top bits."""
        return ((int(self.id) >> 22) + 1288834974657) / 1000


def video_posts(cdp_url: str, handle: str) -> list[Post]:
    """The account's newest posts that have a video, as the upload browser sees its profile."""
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp(cdp_url, timeout=15_000)
            except Exception as e:
                raise XError(NOT_RUNNING) from e
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            if not signed_in(context, "https://x.com", X_SIGNED_IN_COOKIES):
                raise XError(SIGNED_OUT)
            page = context.new_page()
            try:
                return parse_posts(read_profile(page, handle), handle)
            finally:
                page.close()
    except XError:
        raise
    except Exception as e:
        raise XError(f"Lost the upload browser ({e})".splitlines()[0][:500]) from e


def read_profile(page: Page, handle: str, wait_s: float = 45) -> list[dict]:
    """Open the profile and return the data X's page loaded with posts in it.

    Whichever request brings the posts counts, not one by name, since X renames them now and then. When none
    does, the error says what X did load, which shows what changed.
    """
    loaded = []
    page.on("response", lambda r: loaded.append(r) if GRAPHQL.search(r.url) else None)
    page.bring_to_front()  # in case X holds back a timeline in a tab that isn't showing
    page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded", timeout=60_000)
    deadline = time.monotonic() + wait_s
    found, checked = [], 0
    while True:
        for response in loaded[checked:]:
            checked += 1
            if response.ok and (data := _json(response)) is not None and any(_tweets(data)):
                found.append(data)
        if found:
            return found
        path = urlparse(page.url).path
        if path.startswith(("/login", "/i/flow/login")):
            raise XError(SIGNED_OUT)
        if path.startswith("/account/access"):
            raise XError("X wants the account in the upload browser to verify itself. Open the browser and do that, then tap Check.")
        if time.monotonic() > deadline:
            raise XError(_no_posts(handle, page.url, loaded))
        page.wait_for_timeout(500)


def _json(response) -> dict | list | None:
    try:
        return response.json()
    except Exception:  # not JSON, or its body is gone
        return None


def _no_posts(handle: str, url: str, loaded: list) -> str:
    """Why no posts came, from what X's page did load."""
    if any(r.status == 429 for r in loaded):
        return TOO_OFTEN
    if not loaded:
        return (f"X didn't load anything for @{handle} in the upload browser (it ended up at {url}). Open the browser "
                f"and look at x.com/{handle}.")
    names = ", ".join(dict.fromkeys(
        GRAPHQL.search(r.url)[1] + ("" if r.ok else f" ({r.status})") for r in loaded
    ))
    return (f"X loaded @{handle}'s profile but no posts came with it (it loaded {names}). Open the upload browser and "
            f"look at x.com/{handle}: does it show the posts there?")


def parse_posts(data: dict, handle: str) -> list[Post]:
    """The account's own posts with a video, in the order X lists them.

    Reposts are left out, and so are other accounts' posts: quoted ones (inside a post, so never looked
    at) and promoted ones (by someone else).
    """
    tweets = list(_tweets(data))
    if not tweets:
        raise XError(
            f"Found no posts at all in what X loaded for @{handle}. If the account has posts, X has changed how its "
            "page sends them and the watcher (xposts.py) needs updating."
        )
    posts, seen = [], set()
    for tweet in tweets:
        legacy = tweet["legacy"]
        if tweet["rest_id"] in seen or "retweeted_status_result" in legacy:
            continue
        author = _author(tweet)
        if author.lower() != handle.lower():
            continue
        media = (legacy.get("extended_entities") or legacy.get("entities") or {}).get("media") or []
        videos = [m for m in media if m.get("type") == "video"]
        if not videos:
            continue
        seen.add(tweet["rest_id"])
        millis = (videos[0].get("video_info") or {}).get("duration_millis")
        posts.append(Post(
            id=tweet["rest_id"],
            url=f"https://x.com/{author}/status/{tweet['rest_id']}",
            text=_text(tweet),
            duration=millis / 1000 if millis else None,
        ))
    return posts


def _tweets(node):
    """Every post in the response, outermost only: a quoted post sits inside the post quoting it."""
    if isinstance(node, dict):
        if node.get("rest_id") and isinstance(node.get("legacy"), dict) and "full_text" in node["legacy"]:
            yield node
            return
        for value in node.values():
            yield from _tweets(value)
    elif isinstance(node, list):
        for item in node:
            yield from _tweets(item)


def _author(tweet: dict) -> str:
    user = ((tweet.get("core") or {}).get("user_results") or {}).get("result") or {}
    return (user.get("core") or {}).get("screen_name") or (user.get("legacy") or {}).get("screen_name") or ""


def _text(tweet: dict) -> str:
    """The post's text on one line, without its t.co links. Long posts keep their full text elsewhere."""
    note = (((tweet.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}).get("text")
    text = html.unescape(note or tweet["legacy"]["full_text"])  # X sends "&amp;"
    return " ".join(re.sub(r"https://t\.co/\w+", "", text).split())
