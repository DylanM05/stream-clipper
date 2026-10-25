"""Uploading through the YouTube API, against an in-memory stand-in for Google and YouTube."""

import json
import re
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from clipper import worker as worker_module
from clipper.config import Settings
from clipper.db import ClipRecord, Destination, UploadJob, Vod, get_engine
from clipper.web import app as web_app
from clipper.youtube import (
    PLAYLIST_ITEMS_URL, PLAYLISTS_URL, TOKEN_URL, UPLOAD_URL, Google, Playlist, Uploaded, Video, YouTubeError,
    clean_description, sign_in_url,
)


class FakeYouTube:
    """Google's token endpoint and YouTube's resumable uploads, kept in memory."""

    def __init__(self, take_at_most: int | None = None, fail_puts: int = 0, privacy: str | None = None,
                 can_manage: bool = True):
        self.take_at_most = take_at_most  # bytes kept per PUT, like a connection that drops partway
        self.fail_puts = fail_puts  # PUTs answered with 503 first
        self.privacy = privacy  # what YouTube sets, if not what was asked for
        self.can_manage = can_manage  # signed in with the scope that manages playlists
        self.sessions: dict[str, dict] = {}
        self.ranges: list[str] = []
        self.playlists = [_playlist("PL1", "Pre-shows", 3), _playlist("PL2", "Full episodes", 12)]
        self.playlist_items: list[dict] = []

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def session(self, received: bytes = b"", total: int = 0, metadata: dict | None = None) -> str:
        url = f"https://upload.example/session/{len(self.sessions) + 1}"
        self.sessions[url] = {"data": bytearray(received), "total": total, "metadata": metadata or {}, "done": False}
        return url

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(TOKEN_URL):
            form = parse_qs(request.content.decode())
            if form.get("refresh_token") == ["revoked"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3599})
        assert request.headers["authorization"] == "Bearer access"
        if url.startswith((PLAYLIST_ITEMS_URL, PLAYLISTS_URL)):
            return self.handle_playlists(request, url)
        if url.startswith(UPLOAD_URL):
            metadata = json.loads(request.content)
            if metadata["snippet"]["title"] == "Over quota":
                return httpx.Response(403, json={"error": {"code": 403, "message": "quota", "errors": [{"reason": "quotaExceeded"}]}})
            location = self.session(total=int(request.headers["x-upload-content-length"]), metadata=metadata)
            return httpx.Response(200, headers={"Location": location})

        session = self.sessions.get(url)
        if session is None:
            return httpx.Response(404)
        if self.fail_puts:
            self.fail_puts -= 1
            return httpx.Response(503)
        content_range = request.headers["content-range"]
        self.ranges.append(content_range)
        if m := re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range):
            assert int(m[1]) == len(session["data"]), "chunks must carry on from what YouTube has"
            session["data"] += request.content[: self.take_at_most]
        if len(session["data"]) == session["total"]:
            session["done"] = True
            privacy = self.privacy or session["metadata"]["status"]["privacyStatus"]
            return httpx.Response(201, json={"id": "vid123", "status": {"privacyStatus": privacy}})
        headers = {"Range": f"bytes=0-{len(session['data']) - 1}"} if session["data"] else {}
        return httpx.Response(308, headers=headers)

    def handle_playlists(self, request: httpx.Request, url: str) -> httpx.Response:
        if request.method == "GET":  # one playlist per page, to exercise paging
            i = int(request.url.params.get("pageToken") or 0)
            body = {"items": self.playlists[i:i + 1]}
            if i + 1 < len(self.playlists):
                body["nextPageToken"] = str(i + 1)
            return httpx.Response(200, json=body)
        if not self.can_manage:
            return httpx.Response(403, json={"error": {"code": 403, "message": "Request had insufficient authentication scopes.",
                                                       "errors": [{"reason": "insufficientPermissions"}]}})
        body = json.loads(request.content)
        if url.startswith(PLAYLIST_ITEMS_URL):
            self.playlist_items.append(body["snippet"])
            return httpx.Response(200, json={"id": "item1", "snippet": body["snippet"]})
        playlist = _playlist(f"PL{len(self.playlists) + 1}", body["snippet"]["title"], 0, body["status"]["privacyStatus"])
        self.playlists.append(playlist)
        return httpx.Response(200, json=playlist)


def _playlist(id_: str, title: str, count: int, privacy: str = "public") -> dict:
    return {"id": id_, "snippet": {"title": title}, "status": {"privacyStatus": privacy}, "contentDetails": {"itemCount": count}}


def google(fake: FakeYouTube, waits: list | None = None) -> Google:
    return Google("client", "secret", http=fake.client(), chunk_bytes=4, sleep=(waits if waits is not None else []).append)


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"0123456789")
    return Video(file=path, title="Jer <finally> wins", description="It happened.", visibility="public")


def test_sign_in_asks_for_a_lasting_sign_in_and_which_account():
    query = parse_qs(urlparse(sign_in_url("client", "https://app.example/api/youtube/callback", "state1")).query)
    assert query["access_type"] == ["offline"] and query["state"] == ["state1"]
    assert "select_account" in query["prompt"][0] and "consent" in query["prompt"][0]
    scopes = query["scope"][0].split()
    assert "https://www.googleapis.com/auth/youtube.upload" in scopes
    assert "https://www.googleapis.com/auth/youtube" in scopes  # playlists


def test_playlists_are_listed_created_and_added_to():
    fake = FakeYouTube()
    g = google(fake)
    assert g.playlists("refresh") == [Playlist("PL1", "Pre-shows", "public", 3), Playlist("PL2", "Full episodes", "public", 12)]
    assert g.create_playlist("refresh", "Kino <Casino> clips", "unlisted") == Playlist("PL3", "Kino ‹Casino› clips", "unlisted", 0)
    g.add_to_playlist("refresh", "PL3", "vid123")
    assert fake.playlist_items == [{"playlistId": "PL3", "resourceId": {"kind": "youtube#video", "videoId": "vid123"}}]


def test_a_channel_connected_before_playlists_is_asked_to_reconnect():
    g = google(FakeYouTube(can_manage=False))
    assert len(g.playlists("refresh")) == 2  # reading them worked with the old sign-in
    with pytest.raises(YouTubeError, match="Tap Reconnect"):
        g.add_to_playlist("refresh", "PL1", "vid123")


def test_uploads_in_chunks_with_progress_and_returns_the_link(video):
    fake = FakeYouTube()
    progress, sessions = [], []
    result = google(fake).upload("refresh", video, on_progress=progress.append, on_session=sessions.append)

    assert result == Uploaded(video_id="vid123", url="https://youtu.be/vid123", privacy="public")
    [session] = fake.sessions.values()
    assert bytes(session["data"]) == b"0123456789"
    assert fake.ranges == ["bytes 0-3/10", "bytes 4-7/10", "bytes 8-9/10"]
    assert progress == [0.4, 0.8, 1.0]
    assert sessions == list(fake.sessions)
    snippet, status = session["metadata"]["snippet"], session["metadata"]["status"]
    assert snippet["title"] == "Jer ‹finally› wins"  # YouTube refuses < and >
    assert (status["privacyStatus"], status["selfDeclaredMadeForKids"]) == ("public", False)

    video.short = True
    assert google(FakeYouTube()).upload("refresh", video).url == "https://youtube.com/shorts/vid123"


def test_carries_on_from_what_youtube_received_after_a_failure(video):
    fake = FakeYouTube(take_at_most=3, fail_puts=1)
    waits = []
    google(fake, waits).upload("refresh", video)
    [session] = fake.sessions.values()
    assert bytes(session["data"]) == b"0123456789"
    assert waits == [2]  # waited once, after the 503
    assert fake.ranges[0] == "bytes */10"  # then asked how far it got before sending more


def test_an_earlier_session_is_resumed_and_a_finished_one_is_not_sent_again(video):
    fake = FakeYouTube()
    half = fake.session(received=b"01234", total=10, metadata={"status": {"privacyStatus": "unlisted"}})
    result = google(fake).upload("refresh", video, session_url=half)
    assert bytes(fake.sessions[half]["data"]) == b"0123456789" and result.privacy == "unlisted"
    assert len(fake.sessions) == 1  # no new session

    fake.ranges.clear()
    assert google(fake).upload("refresh", video, session_url=half).video_id == "vid123"
    assert fake.ranges == ["bytes */10"]  # only asked; nothing uploaded twice


def test_an_expired_session_starts_over(video):
    fake = FakeYouTube()
    sessions = []
    google(fake).upload("refresh", video, session_url="https://upload.example/session/gone", on_session=sessions.append)
    assert sessions == ["https://upload.example/session/1"]
    assert bytes(fake.sessions[sessions[0]]["data"]) == b"0123456789"


def test_youtube_errors_say_what_to_do(video):
    with pytest.raises(YouTubeError) as err:
        google(FakeYouTube()).upload("revoked", video)
    assert err.value.signed_out and "Connect it again" in str(err.value)

    video.title = "Over quota"
    with pytest.raises(YouTubeError, match="quota is used up"):
        google(FakeYouTube()).upload("refresh", video)


def test_long_descriptions_are_cut_at_a_line_within_5000_bytes():
    text = "\n".join(f"{i:02d}:00 ♥️ chapter {i}" for i in range(400))
    cut = clean_description(text)
    assert len(cut.encode()) <= 5000 and text.startswith(cut) and text[len(cut)] == "\n"


# --- The worker ---


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", web_dir=tmp_path / "web", google_client_id="client",
                    google_client_secret="secret", app_password="pw")


@pytest.fixture
def clip_id(settings):
    video = settings.data_dir / "clip.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    with Session(get_engine(settings)) as s:
        dest = Destination(name="Kino Casino Klips", channel_id="UCklips", youtube_token="refresh", youtube_account="Klips",
                           youtube_error="Google sign-in was cancelled.")
        vod = Vod(url="https://kick.com/kino/videos/1", status="ready", duration=600)
        s.add_all([dest, vod])
        s.commit()
        clip = ClipRecord(vod_id=vod.id, key="hl01", kind="highlight", start=1, end=40, title="Huck Blend Is Back",
                          description="Jer returns.", status="rendered", video_path=str(video), destination_id=dest.id)
        s.add(clip)
        s.commit()
        return clip.id


class FakeGoogle:
    def __init__(self, privacy: str | None = None, error: YouTubeError | None = None, playlist_error: YouTubeError | None = None):
        self.privacy, self.error, self.playlist_error = privacy, error, playlist_error
        self.calls = []
        self.added = []

    def upload(self, refresh_token, video, on_progress=lambda _: None, session_url="", on_session=lambda _: None):
        self.calls.append((refresh_token, video, session_url))
        if self.error:
            raise self.error
        on_session("https://upload.example/session/1")
        on_progress(0.5)
        return Uploaded("vid123", "https://youtu.be/vid123", self.privacy or video.visibility)

    def add_to_playlist(self, refresh_token, playlist_id, video_id):
        if self.playlist_error:
            raise self.playlist_error
        self.added.append((refresh_token, playlist_id, video_id))


def get(settings, model, id_):
    with Session(get_engine(settings)) as s:
        return s.get(model, id_)


def test_worker_uploads_connected_channels_through_the_api(settings, clip_id):
    w = worker_module.Worker(settings)
    w.youtube = FakeGoogle()
    with Session(get_engine(settings)) as s:
        s.add(UploadJob(clip_id=clip_id, visibility="unlisted"))
        s.commit()
    assert w.upload_next() is True

    [(token, video, session_url)] = w.youtube.calls
    assert (token, video.title, video.visibility, video.category_id, session_url) == (
        "refresh", "Huck Blend Is Back", "unlisted", "24", "",
    )
    job = get(settings, UploadJob, 1)
    assert (job.method, job.status, job.youtube_url, job.error) == ("api", "done", "https://youtu.be/vid123", "")
    assert job.session_url  # kept, so a retry could carry on
    assert get(settings, ClipRecord, clip_id).status == "uploaded"
    assert get(settings, Destination, 1).youtube_error == ""  # the sign-in evidently works


def set_playlist(settings, clip_id, playlist_id="PL1"):
    with Session(get_engine(settings)) as s:
        clip = s.get(ClipRecord, clip_id)
        clip.playlist_id = playlist_id
        s.add_all([clip, UploadJob(clip_id=clip_id)])
        s.commit()


def test_uploads_go_into_the_clips_playlist(settings, clip_id):
    set_playlist(settings, clip_id)
    w = worker_module.Worker(settings)
    w.youtube = FakeGoogle()
    w.upload_next()
    assert w.youtube.added == [("refresh", "PL1", "vid123")]
    assert get(settings, UploadJob, 1).error == ""


def test_shorts_never_go_into_a_playlist(settings, clip_id):
    short = settings.data_dir / "clip-short.mp4"
    short.write_bytes(b"short")
    with Session(get_engine(settings)) as s:
        clip = s.get(ClipRecord, clip_id)
        clip.playlist_id, clip.short_path = "PL1", str(short)
        s.add_all([clip, UploadJob(clip_id=clip_id, which="short")])
        s.commit()
    w = worker_module.Worker(settings)
    w.youtube = FakeGoogle()
    w.upload_next()
    [(_, video, _)] = w.youtube.calls
    assert video.short and w.youtube.added == []
    assert get(settings, UploadJob, 1).status == "done"


def test_a_playlist_that_cant_take_it_leaves_the_upload_done_with_a_note(settings, clip_id):
    set_playlist(settings, clip_id)
    w = worker_module.Worker(settings)
    w.youtube = FakeGoogle(playlist_error=YouTubeError("Tap Reconnect on it under Shows → YouTube channels."))
    w.upload_next()
    job = get(settings, UploadJob, 1)
    assert job.status == "done" and job.error == "It isn't in the playlist: Tap Reconnect on it under Shows → YouTube channels."


def test_a_video_youtube_kept_private_says_so(settings, clip_id):
    w = worker_module.Worker(settings)
    w.youtube = FakeGoogle(privacy="private")
    with Session(get_engine(settings)) as s:
        s.add(UploadJob(clip_id=clip_id, visibility="public"))
        s.commit()
    w.upload_next()
    job = get(settings, UploadJob, 1)
    assert job.status == "done" and "made this video private, not public" in job.error


def test_a_retry_carries_on_only_with_nothing_changed(settings, clip_id):
    w = worker_module.Worker(settings)
    w.youtube = FakeGoogle()
    with Session(get_engine(settings)) as s:
        s.add(UploadJob(clip_id=clip_id))
        s.commit()
    w.upload_next()  # keeps its session and what it sent
    assert w.youtube.calls[0][2] == ""

    _requeue(settings, session_url="https://upload.example/session/9")  # as if it had stopped partway
    w.upload_next()
    assert w.youtube.calls[1][2] == "https://upload.example/session/9"

    _requeue(settings)
    with Session(get_engine(settings)) as s:
        clip = s.get(ClipRecord, clip_id)
        clip.title = "A new title"
        s.add(clip)
        s.commit()
    w.upload_next()
    assert w.youtube.calls[2][2] == ""  # the title changed: a fresh upload


def _requeue(settings, **fields):
    with Session(get_engine(settings)) as s:
        job = s.get(UploadJob, 1)
        job.status = "queued"
        for k, v in fields.items():
            setattr(job, k, v)
        s.add(job)
        s.commit()


def test_a_signed_out_channel_is_flagged_and_restarts_resume_api_uploads(settings, clip_id):
    w = worker_module.Worker(settings)
    w.youtube = FakeGoogle(error=YouTubeError("This channel's YouTube sign-in has expired", signed_out=True))
    with Session(get_engine(settings)) as s:
        s.add_all([UploadJob(clip_id=clip_id), UploadJob(clip_id=clip_id, status="uploading", method="api"),
                   UploadJob(clip_id=clip_id, which="short", status="uploading", method="studio")])
        s.commit()
    w.upload_next()
    assert get(settings, UploadJob, 1).status == "failed"
    assert "expired" in get(settings, Destination, 1).youtube_error

    w.recover()
    assert get(settings, UploadJob, 2).status == "queued"  # carries on where it stopped
    assert get(settings, UploadJob, 3).status == "failed"  # Studio: can't tell whether it finished


def test_unconnected_channels_still_go_through_studio(settings, clip_id):
    from test_uploads import FakeStudio

    with Session(get_engine(settings)) as s:
        dest = s.get(Destination, 1)
        dest.youtube_token = ""
        s.add_all([dest, UploadJob(clip_id=clip_id)])
        s.commit()
    w = worker_module.Worker(settings)
    w.youtube, w.studio = FakeGoogle(), FakeStudio()
    w.upload_next()
    assert w.youtube.calls == [] and len(w.studio.uploads) == 1
    assert get(settings, UploadJob, 1).method == "studio"


# --- Connecting a channel in the app ---


class FakeGoogleSignIn:
    channel_id, channel_name = "UCklips", "Kino Casino Klips"
    revoked: list[str] = []

    def __init__(self, client_id, client_secret):
        self.client_id = client_id

    def exchange_code(self, code, redirect_uri):
        assert redirect_uri == "https://clipper.tailnet.ts.net/api/youtube/callback"
        return f"refresh-{code}", "access"

    def channel(self, access_token):
        return self.channel_id, self.channel_name

    def revoke(self, token):
        self.revoked.append(token)

    def playlists(self, refresh_token):
        return [Playlist("PL1", "Pre-shows", "public", 3)]

    def create_playlist(self, refresh_token, title, privacy):
        return Playlist("PL2", title, privacy, 0)


@pytest.fixture
def app_client(settings, monkeypatch):
    monkeypatch.setattr(web_app, "Google", FakeGoogleSignIn)
    FakeGoogleSignIn.revoked = []
    client = TestClient(web_app.create_app(settings))
    client.post("/api/login", json={"password": "pw"})
    with Session(get_engine(settings)) as s:
        s.add(Destination(name="Kino Casino Klips", channel_id="UCklips"))
        s.commit()
    return client


def _connect(client) -> str:
    """Start connecting channel 1 and return the state Google would send back."""
    res = client.post("/api/destinations/1/connect", json={"origin": "https://clipper.tailnet.ts.net"})
    query = parse_qs(urlparse(res.json()["url"]).query)
    assert query["redirect_uri"] == ["https://clipper.tailnet.ts.net/api/youtube/callback"]
    return query["state"][0]


def test_connecting_a_channel_stores_its_sign_in(settings, app_client):
    assert app_client.get("/api/youtube").json()["configured"] is True
    state = _connect(app_client)

    outside = TestClient(web_app.create_app(settings))  # e.g. the browser Google's sign-in ran in, without the login
    page = outside.get("/api/youtube/callback", params={"state": state, "code": "abc"})
    assert page.status_code == 200 and "Connected Kino Casino Klips" in page.text

    [dest] = app_client.get("/api/destinations").json()
    assert (dest["api_connected"], dest["api_account"], dest["api_error"]) == (True, "Kino Casino Klips", "")
    assert "refresh" not in json.dumps(dest)  # the token never leaves the server
    assert get(settings, Destination, 1).youtube_token == "refresh-abc"

    dest = app_client.post("/api/destinations/1/disconnect").json()
    assert dest["api_connected"] is False and FakeGoogleSignIn.revoked == ["refresh-abc"]


def test_picking_the_wrong_channel_or_a_forged_state_connects_nothing(settings, app_client, monkeypatch):
    monkeypatch.setattr(FakeGoogleSignIn, "channel_name", "Kino Casino")
    monkeypatch.setattr(FakeGoogleSignIn, "channel_id", "UCmain")
    page = app_client.get("/api/youtube/callback", params={"state": _connect(app_client), "code": "abc"})
    assert page.status_code == 400 and "You picked “Kino Casino”" in page.text
    dest = get(settings, Destination, 1)
    assert dest.youtube_token == "" and "You picked" in dest.youtube_error

    lan = app_client.post("/api/destinations/1/connect", json={"origin": "http://192.168.50.166:8976"})
    assert lan.status_code == 409 and "192.168.50.166:8976" in lan.json()["detail"]  # Google refuses private IPs

    forged = app_client.get("/api/youtube/callback", params={"state": "e30.forged", "code": "abc"})
    assert forged.status_code == 400 and "expired" in forged.text

    cancelled = app_client.get("/api/youtube/callback", params={"state": _connect(app_client), "error": "access_denied"})
    assert "cancelled" in cancelled.text


def test_picking_playlists_and_keeping_them_with_their_channel(settings, app_client):
    assert app_client.get("/api/destinations/1/playlists").status_code == 409  # not connected yet
    with Session(get_engine(settings)) as s:
        dest = s.get(Destination, 1)
        dest.youtube_token = "refresh"
        s.add_all([dest, Destination(name="Full episodes")])
        s.commit()
    assert app_client.get("/api/destinations/1/playlists").json() == [
        {"id": "PL1", "title": "Pre-shows", "privacy": "public", "count": 3},
    ]
    created = app_client.post("/api/destinations/1/playlists", json={"title": " Kino clips ", "privacy": "unlisted"}).json()
    assert created == {"id": "PL2", "title": "Kino clips", "privacy": "unlisted", "count": 0}

    show = app_client.post("/api/shows", json={"name": "Kino", "clip_destination_id": 1, "clip_playlist_id": "PL2",
                                               "episode_destination_id": 1, "episode_playlist_id": "PL1"}).json()
    show = app_client.patch(f"/api/shows/{show['id']}", json={"episode_destination_id": 2}).json()
    assert (show["clip_playlist_id"], show["episode_playlist_id"]) == ("PL2", "")  # the episode playlist was on channel 1

    with Session(get_engine(settings)) as s:
        vod = Vod(url="https://kick.com/kino/videos/1", status="ready", duration=600)
        s.add(vod)
        s.commit()
        clip = ClipRecord(vod_id=vod.id, key="hl01", kind="highlight", start=1, end=40, title="t", destination_id=1,
                          playlist_id="PL2")
        s.add(clip)
        s.commit()
        clip_id = clip.id
    assert app_client.patch(f"/api/clips/{clip_id}", json={"title": "New"}).json()["playlist_id"] == "PL2"
    assert app_client.patch(f"/api/clips/{clip_id}", json={"destination_id": 2}).json()["playlist_id"] == ""


def test_the_privacy_policy_google_links_to_needs_no_login(settings):
    settings.web_dir.mkdir(parents=True)
    (settings.web_dir / "index.html").write_text("<div id=root></div>")
    (settings.web_dir / "privacy.html").write_text("<h1>Privacy policy</h1>")
    client = TestClient(web_app.create_app(settings))  # not signed in
    assert "Privacy policy" in client.get("/privacy.html").text
    assert client.get("/api/destinations").status_code == 401


def test_connecting_needs_the_google_client_set_up(tmp_path):
    client = TestClient(web_app.create_app(Settings(data_dir=tmp_path / "data", web_dir=tmp_path / "web")))
    assert client.get("/api/youtube").json()["configured"] is False
    client.post("/api/destinations", json={"name": "Klips"})
    res = client.post("/api/destinations/1/connect", json={"origin": "https://clipper.tailnet.ts.net"})
    assert res.status_code == 409 and "isn't set up" in res.json()["detail"]
