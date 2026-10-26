"""Uploads through the YouTube Data API, with each upload channel signed in to Google once from the app.

How it fits together:
- Setup (README, "Automatic uploads"): a Google Cloud project with the YouTube Data API turned on, and
  an OAuth client of type "Web application" whose redirect URI is the app's /api/youtube/callback. Its ID
  and secret go in .env.
- Connecting a channel in the app sends you through Google's sign-in, where you pick the channel. The
  app keeps the refresh token that comes back (Destination.youtube_token) and the worker uploads with it.
- Uploads use YouTube's resumable protocol in chunks, so progress shows in the app and a dropped
  connection or a worker restart carries on from the bytes YouTube already has.
- An upload can then be added to one of the channel's playlists, picked (or created) in the app, and get
  its thumbnail (thumbnails.py).
- Afterwards the worker reads each upload's views (stats.py).

Google used to lock every upload from a project it hadn't audited to private. Its docs now say that no
longer happens. If it does anyway, `Uploaded.privacy` shows what YouTube actually set.
"""

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
PLAYLISTS_URL = "https://www.googleapis.com/youtube/v3/playlists"
PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMBNAIL_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
CALLBACK_PATH = "/api/youtube/callback"
# Upload videos; see which channel a sign-in is for and manage its playlists, which only the broad
# "youtube" scope allows. Channels connected before playlists existed only have youtube.readonly.
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube"

CHUNK_BYTES = 32 * 1024 * 1024  # chunks must be a multiple of 256 KiB; progress is reported after each
RETRYABLE = {500, 502, 503, 504}
MAX_RETRIES = 8
TOKEN_REFRESH_S = 45 * 60  # access tokens last an hour, and a full episode can take longer to upload
DESCRIPTION_MAX_BYTES = 5000
TITLE_MAX = 100

# What YouTube's error reasons mean for you.
REASONS = {
    "quotaExceeded": "The Google Cloud project's daily YouTube API quota is used up. It resets at midnight "
                     "Pacific time; retry after that.",
    "uploadLimitExceeded": "YouTube says this channel has uploaded as many videos as it can for now. Its limit "
                           "lifts within 24 hours of the earlier uploads; retry then.",
    "accessNotConfigured": "The YouTube Data API isn't turned on in the Google Cloud project. Enable it there, then retry.",
    "youtubeSignupRequired": "The Google account this channel was connected with has no YouTube channel. Connect it again.",
    "insufficientPermissions": "This channel was connected before the app could manage playlists. Tap Reconnect on it "
                               "under Shows → YouTube channels.",
    "playlistNotFound": "That playlist isn't on this channel (any more). Pick another one.",
}
SIGNED_OUT = ("This channel's YouTube sign-in has expired or was removed. Connect it again under "
              "Shows → YouTube channels, then retry.")
NO_CUSTOM_THUMBNAILS = ("YouTube doesn't let this channel use custom thumbnails yet. Verify the channel with a phone "
                        "number at youtube.com/verify, then tap Use on YouTube.")
_VIDEO_ID = re.compile(r"(?:youtu\.be/|/shorts/|[?&]v=|/video/)([\w-]{11})")


class YouTubeError(RuntimeError):
    def __init__(self, message: str, signed_out: bool = False):
        super().__init__(message)
        self.signed_out = signed_out  # the channel's sign-in no longer works: it needs connecting again


class _Retry(Exception):
    """A temporary failure (a 5xx or a dropped connection): wait, then carry on from what YouTube has."""

    def __init__(self, message: str, expired: bool = False):
        super().__init__(message)
        self.expired = expired  # the upload session is gone; start a new one


@dataclass
class Video:
    file: Path
    title: str
    description: str
    visibility: str  # public, unlisted, private
    category_id: str = "24"
    short: bool = False  # only changes the link: a vertical video under 3 minutes becomes a Short by itself


@dataclass
class Uploaded:
    video_id: str
    url: str
    privacy: str  # what YouTube set, which isn't always what was asked for


@dataclass
class Playlist:
    id: str  # PL…
    title: str
    privacy: str
    count: int  # videos in it


@dataclass
class Stats:
    views: int | None
    likes: int | None  # None when the channel hides them
    comments: int | None


def video_id(url: str) -> str:
    """The 11-character id in a video's link (youtu.be, watch, Shorts or Studio), or ""."""
    m = _VIDEO_ID.search(url or "")
    return m[1] if m else ""


def sign_in_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Google's sign-in page, where you pick the channel to connect."""
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",  # a refresh token, so the worker can upload without you
        "prompt": "select_account consent",  # always ask which account, and always return a refresh token
        "state": state,
    })


def clean_title(title: str) -> str:
    return _no_angle_brackets(" ".join(title.split()))[:TITLE_MAX] or "Untitled"


def clean_description(text: str) -> str:
    """Within YouTube's 5000-byte limit, cut at a line break."""
    text = _no_angle_brackets(text)
    data = text.encode()
    if len(data) <= DESCRIPTION_MAX_BYTES:
        return text
    cut = data[:DESCRIPTION_MAX_BYTES].decode("utf-8", "ignore")
    return cut.rsplit("\n", 1)[0] if "\n" in cut else cut


def _no_angle_brackets(text: str) -> str:
    return text.replace("<", "‹").replace(">", "›")  # YouTube refuses < and > in titles and descriptions


class Google:
    def __init__(self, client_id: str, client_secret: str, http: httpx.Client | None = None,
                 chunk_bytes: int = CHUNK_BYTES, sleep: Callable[[float], None] = time.sleep):
        self.client_id = client_id
        self.client_secret = client_secret
        self.http = http or httpx.Client(timeout=httpx.Timeout(120.0))
        self.chunk_bytes = chunk_bytes
        self.sleep = sleep

    # --- Connecting a channel ---

    def exchange_code(self, code: str, redirect_uri: str) -> tuple[str, str]:
        """(refresh token, access token) for the code Google's sign-in sent back."""
        try:
            tokens = self._token({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})
        except _Retry as e:
            raise YouTubeError(f"Couldn't reach Google ({e}). Try connecting again.") from e
        if not tokens.get("refresh_token"):
            raise YouTubeError("Google didn't give the app a lasting sign-in. Try connecting again.")
        return tokens["refresh_token"], tokens["access_token"]

    def channel(self, access_token: str) -> tuple[str, str]:
        """(UC… id, name) of the channel a sign-in is for."""
        try:
            res = self.http.get(CHANNELS_URL, params={"part": "snippet", "mine": "true"},
                                headers={"Authorization": f"Bearer {access_token}"})
        except httpx.HTTPError as e:
            raise YouTubeError(f"Couldn't reach YouTube ({e}). Try connecting again.") from e
        if res.status_code >= 400:
            raise _api_error(res)
        items = res.json().get("items") or []
        if not items:
            raise YouTubeError("That Google account has no YouTube channel. Connect again and pick the channel's account.")
        return items[0]["id"], items[0]["snippet"]["title"]

    def revoke(self, refresh_token: str) -> None:
        """Remove the app's access to a channel. Best effort: the token is forgotten either way."""
        try:
            self.http.post(REVOKE_URL, data={"token": refresh_token})
        except httpx.HTTPError as e:
            log.warning("Couldn't revoke a YouTube sign-in: %s", e)

    # --- Playlists ---

    def playlists(self, refresh_token: str) -> list[Playlist]:
        """The channel's playlists, in YouTube's order."""
        auth = _Auth(self, refresh_token)
        found: list[Playlist] = []
        page = ""
        while len(found) < 500:
            params = {"part": "snippet,status,contentDetails", "mine": "true", "maxResults": 50}
            body = self._call("GET", PLAYLISTS_URL, auth, params={**params, "pageToken": page} if page else params)
            found += [_playlist(item) for item in body.get("items") or []]
            if not (page := body.get("nextPageToken")):
                break
        return found

    def create_playlist(self, refresh_token: str, title: str, privacy: str) -> Playlist:
        body = self._call("POST", PLAYLISTS_URL, _Auth(self, refresh_token), params={"part": "snippet,status"},
                          json={"snippet": {"title": clean_title(title)}, "status": {"privacyStatus": privacy}})
        return _playlist(body)

    def add_to_playlist(self, refresh_token: str, playlist_id: str, video_id: str) -> None:
        resource = {"kind": "youtube#video", "videoId": video_id}
        self._call("POST", PLAYLIST_ITEMS_URL, _Auth(self, refresh_token), params={"part": "snippet"},
                   json={"snippet": {"playlistId": playlist_id, "resourceId": resource}})

    # --- After uploading ---

    def set_thumbnail(self, refresh_token: str, video_id: str, image: Path) -> None:
        """Use a JPEG as the video's thumbnail. Only channels YouTube has verified by phone may."""
        try:
            self._request("POST", THUMBNAIL_URL, _Auth(self, refresh_token), headers={"Content-Type": "image/jpeg"},
                          params={"videoId": video_id, "uploadType": "media"}, content=image.read_bytes())
        except _Retry as e:
            raise YouTubeError(f"Couldn't reach YouTube ({e}). Try again.") from e
        except YouTubeError as e:
            if "(403)" in str(e) and "thumbnail" in str(e).lower():
                raise YouTubeError(NO_CUSTOM_THUMBNAILS) from e
            raise

    def video_stats(self, video_ids: list[str], refresh_token: str = "", api_key: str = "") -> dict[str, Stats]:
        """View, like and comment counts by video id, read as the channel or with an API key. Videos YouTube
        doesn't return (deleted, or private and not the channel's own) are left out."""
        auth = _Auth(self, refresh_token) if refresh_token else None
        found: dict[str, Stats] = {}
        for i in range(0, len(video_ids), 50):
            params = {"part": "statistics", "id": ",".join(video_ids[i:i + 50]), "maxResults": 50}
            body = self._call("GET", VIDEOS_URL, auth, params=params) if auth else self._get(VIDEOS_URL, {**params, "key": api_key})
            for item in body.get("items") or []:
                counts = item.get("statistics") or {}
                found[item["id"]] = Stats(*(_count(counts.get(k)) for k in ("viewCount", "likeCount", "commentCount")))
        return found

    def channel_picture(self, query: str, refresh_token: str = "", api_key: str = "") -> tuple[str, str] | None:
        """(name, profile picture link) of the channel YouTube's search finds first, or None. A search costs 100
        units of the daily quota, a hundred times what reading view counts does."""
        params = {"part": "snippet", "type": "channel", "q": query, "maxResults": 1}
        body = (self._call("GET", SEARCH_URL, _Auth(self, refresh_token), params=params) if refresh_token
                else self._get(SEARCH_URL, {**params, "key": api_key}))
        items = body.get("items") or []
        if not items:
            return None
        snippet = items[0].get("snippet") or {}
        pictures = snippet.get("thumbnails") or {}
        url = (pictures.get("high") or pictures.get("medium") or pictures.get("default") or {}).get("url", "")
        return (snippet.get("channelTitle") or snippet.get("title") or "", url) if url else None

    def _get(self, url: str, params: dict) -> dict:
        """A request with an API key instead of a channel's sign-in."""
        try:
            res = self.http.get(url, params=params)
        except httpx.HTTPError as e:
            raise YouTubeError(f"Couldn't reach YouTube ({e}). Try again.") from e
        if res.status_code >= 400:
            raise _api_error(res)
        return _json(res)

    def _call(self, method: str, url: str, auth: "_Auth", **kwargs) -> dict:
        """A one-off request as the channel; a temporary failure is just an error to try again later."""
        try:
            return _json(self._request(method, url, auth, headers={}, **kwargs))
        except _Retry as e:
            raise YouTubeError(f"Couldn't reach YouTube ({e}). Try again.") from e

    # --- Uploading ---

    def upload(self, refresh_token: str, video: Video, on_progress: Callable[[float], None] = lambda _: None,
               session_url: str = "", on_session: Callable[[str], None] = lambda _: None) -> Uploaded:
        """Upload a video to the channel `refresh_token` belongs to.

        `session_url` carries on from an earlier attempt; `on_session` gets each new session to keep for that.
        """
        total = video.file.stat().st_size
        if not total:
            raise YouTubeError("The file is empty")
        auth = _Auth(self, refresh_token)
        retries = 0
        while True:
            try:
                if session_url:
                    state = self._received(session_url, total, auth)
                    if isinstance(state, dict):
                        return self._uploaded(state, video)  # it had finished
                    offset = state
                else:
                    session_url, offset = self._start(video, total, auth), 0
                    on_session(session_url)
                return self._uploaded(self._send(session_url, video.file, offset, total, auth, on_progress), video)
            except _Retry as e:
                if e.expired:
                    session_url = ""
                retries += 1
                if retries > MAX_RETRIES:
                    raise YouTubeError(f"YouTube kept failing ({e}). Retry later; it carries on where it stopped.") from e
                wait = min(2 ** retries, 120)
                log.warning("YouTube upload: %s; trying again in %ds", e, wait)
                self.sleep(wait)

    def _start(self, video: Video, total: int, auth: "_Auth") -> str:
        """Open a resumable upload session with the title, description and visibility; returns its URL."""
        body = {
            "snippet": {
                "title": clean_title(video.title),
                "description": clean_description(video.description),
                "categoryId": video.category_id,
            },
            "status": {"privacyStatus": video.visibility, "selfDeclaredMadeForKids": False},
        }
        res = self._request(
            "POST", UPLOAD_URL, auth,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={"X-Upload-Content-Length": str(total), "X-Upload-Content-Type": "video/mp4"},
            json=body,
        )
        return res.headers["location"]

    def _received(self, session_url: str, total: int, auth: "_Auth") -> int | dict:
        """How far an earlier attempt got: the next byte to send, or the video if it had finished."""
        res = self._request("PUT", session_url, auth, headers={"Content-Range": f"bytes */{total}"}, content=b"")
        return _next_byte(res) if res.status_code == 308 else res.json()

    def _send(self, session_url: str, file: Path, offset: int, total: int, auth: "_Auth",
              on_progress: Callable[[float], None]) -> dict:
        with file.open("rb") as f:
            while True:
                f.seek(offset)
                chunk = f.read(self.chunk_bytes)
                res = self._request(
                    "PUT", session_url, auth, content=chunk,
                    headers={"Content-Type": "video/mp4", "Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{total}"},
                )
                if res.status_code != 308:
                    on_progress(1.0)
                    return res.json()
                offset = _next_byte(res)  # what YouTube says it has, not what was sent
                on_progress(offset / total)

    def _request(self, method: str, url: str, auth: "_Auth", headers: dict, **kwargs) -> httpx.Response:
        """A request signed in as the channel, refreshing the sign-in once if Google says it ran out."""
        for attempt in range(2):
            try:
                res = self.http.request(method, url, headers={**headers, **auth.headers()}, **kwargs)
            except httpx.TransportError as e:
                raise _Retry(str(e) or type(e).__name__) from e
            if res.status_code == 401 and attempt == 0:
                auth.refresh()
                continue
            if res.status_code in RETRYABLE:
                raise _Retry(f"YouTube answered {res.status_code}")
            if method == "PUT" and res.status_code in (404, 410):
                raise _Retry("the upload session expired", expired=True)
            if res.status_code >= 400:
                raise _api_error(res)
            return res
        raise _api_error(res)

    def _token(self, fields: dict) -> dict:
        try:
            res = self.http.post(TOKEN_URL, data={"client_id": self.client_id, "client_secret": self.client_secret, **fields})
        except httpx.TransportError as e:
            raise _Retry(str(e) or type(e).__name__) from e
        if res.status_code in RETRYABLE:
            raise _Retry(f"Google answered {res.status_code}")
        body = _json(res)
        if res.status_code == 200:
            return body
        if body.get("error") == "invalid_grant":
            raise YouTubeError(SIGNED_OUT, signed_out=True)
        if body.get("error") in ("invalid_client", "unauthorized_client"):
            raise YouTubeError("Google rejected the app's client ID or secret. Check CLIPPER_GOOGLE_CLIENT_ID and "
                               "CLIPPER_GOOGLE_CLIENT_SECRET in .env.")
        raise YouTubeError(f"Google sign-in failed: {body.get('error_description') or body.get('error') or res.status_code}")

    @staticmethod
    def _uploaded(resource: dict, video: Video) -> Uploaded:
        video_id = resource["id"]
        return Uploaded(
            video_id=video_id,
            url=f"https://youtube.com/shorts/{video_id}" if video.short else f"https://youtu.be/{video_id}",
            privacy=(resource.get("status") or {}).get("privacyStatus") or video.visibility,
        )


class _Auth:
    """A channel's access token, refreshed before it runs out."""

    def __init__(self, google: Google, refresh_token: str):
        self.google = google
        self.refresh_token = refresh_token
        self.token = ""
        self.fetched = 0.0

    def headers(self) -> dict:
        if not self.token or time.monotonic() - self.fetched > TOKEN_REFRESH_S:
            self.refresh()
        return {"Authorization": f"Bearer {self.token}"}

    def refresh(self) -> None:
        self.token = self.google._token({"grant_type": "refresh_token", "refresh_token": self.refresh_token})["access_token"]
        self.fetched = time.monotonic()


def _playlist(resource: dict) -> Playlist:
    return Playlist(
        id=resource["id"],
        title=(resource.get("snippet") or {}).get("title") or "",
        privacy=(resource.get("status") or {}).get("privacyStatus") or "",
        count=(resource.get("contentDetails") or {}).get("itemCount") or 0,
    )


def _count(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _next_byte(res: httpx.Response) -> int:
    """From a 308's Range header ("bytes=0-1234"): the first byte YouTube doesn't have yet."""
    m = re.fullmatch(r"bytes=0-(\d+)", res.headers.get("range", "").strip())
    return int(m[1]) + 1 if m else 0


def _json(res: httpx.Response) -> dict:
    try:
        body = res.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _api_error(res: httpx.Response) -> YouTubeError:
    error = _json(res).get("error") or {}
    if not isinstance(error, dict):
        error = {"message": str(error)}
    for item in error.get("errors") or []:
        if message := REASONS.get(item.get("reason", "")):
            return YouTubeError(message)
    if res.status_code == 401:
        return YouTubeError(SIGNED_OUT, signed_out=True)
    message = error.get("message") or res.reason_phrase
    return YouTubeError(f"YouTube refused it ({res.status_code}): {message}"[:500])
