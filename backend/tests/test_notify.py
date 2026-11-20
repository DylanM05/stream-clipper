"""Notifications to ntfy and Discord; the services themselves are faked."""

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from clipper import notify
from clipper.config import Settings
from clipper.db import get_engine, set_app_setting
from clipper.notify import Note
from clipper.web import app as web_app


@pytest.fixture
def posts(monkeypatch):
    sent = []

    def post(url, json=None, headers=None, timeout=None):
        sent.append({"url": url, "json": json, "headers": headers or {}})
        if "broken" in url:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(notify.httpx, "post", post)
    return sent


def test_ntfy_gets_json_at_the_server_root_so_titles_can_have_emoji(posts):
    settings = Settings(ntfy_url="https://ntfy.example.com/sub/kino-clips", ntfy_token="tk_abc", app_url="https://app.ts.net/")
    assert notify.deliver(settings, Note("Ready: ♥️ PRE SHOW", "2 to review", link="videos/3")) == []
    [sent] = posts
    assert sent["url"] == "https://ntfy.example.com/sub/"
    assert sent["json"] == {
        "topic": "kino-clips", "title": "Ready: ♥️ PRE SHOW", "message": "2 to review", "priority": 3,
        "tags": ["clapper"], "click": "https://app.ts.net/#/videos/3",
    }
    assert sent["headers"] == {"Authorization": "Bearer tk_abc"}


def test_discord_gets_an_embed_and_a_youtube_link_to_show_the_video(posts):
    settings = Settings(discord_webhook_url="https://discord.com/api/webhooks/1/abc")
    notify.deliver(settings, Note("Uploaded: Huck Blend", "Video, public.", link="https://youtu.be/xyz", tone="good"))
    [sent] = posts
    assert sent["json"]["content"] == "https://youtu.be/xyz"
    assert sent["json"]["embeds"] == [
        {"title": "Uploaded: Huck Blend", "description": "Video, public.", "color": 0x10B981, "url": "https://youtu.be/xyz"},
    ]


def test_app_pages_only_get_a_link_when_the_apps_address_is_set(posts):
    notify.deliver(Settings(discord_webhook_url="https://discord.com/api/webhooks/1/abc"), Note("Failed", link="videos/3"))
    assert "url" not in posts[0]["json"]["embeds"][0] and "content" not in posts[0]["json"]


def test_a_failing_service_is_reported_without_stopping_the_other(posts):
    settings = Settings(ntfy_url="https://broken.example.com/topic", discord_webhook_url="https://discord.com/api/webhooks/1/abc")
    errors = notify.deliver(settings, Note("Hi"))
    assert errors == ["ntfy: connection refused"] and len(posts) == 2
    assert notify.deliver(Settings(ntfy_url="https://ntfy.sh"), Note("Hi")) == [
        "ntfy: CLIPPER_NTFY_URL needs the topic at the end, like https://ntfy.sh/your-topic",
    ]


def test_nothing_is_sent_when_neither_is_set_up(posts):
    notify.send(Settings(), Note("Hi"))
    assert posts == []


def test_the_apps_test_button(tmp_path, posts):
    client = TestClient(web_app.create_app(Settings(data_dir=tmp_path, web_dir=tmp_path / "web")))
    assert client.get("/api/notifications").json() == {"ntfy": False, "discord": False, "links": False}
    assert client.post("/api/notifications/test").status_code == 409

    settings = Settings(data_dir=tmp_path, web_dir=tmp_path / "web", ntfy_url="https://broken.example.com/topic")
    response = TestClient(web_app.create_app(settings)).post("/api/notifications/test")
    assert response.status_code == 502 and "connection refused" in response.json()["detail"]


def test_every_notification_has_its_kind():
    """Each Note( in the app names its kind, so a new one can't go out without a switch."""
    import ast
    import pathlib

    found = {}
    for path in pathlib.Path(notify.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Note":
                kind = next((k.value.value for k in node.keywords if k.arg == "kind"), None)
                title = node.args[0]
                start = title.values[0].value if isinstance(title, ast.JoinedStr) else title.value
                found[start.split(":")[0].strip()] = kind
    assert found == {
        "Waiting for disk space": "failure", "Ready": "review", "Waiting for the AI": "heads_up",
        "Failed": "failure", "Render failed": "failure", "No intro on": "heads_up", "Not uploaded": "failure",
        "Only one clip kept": "heads_up", "Compilation to review": "review", "Uploaded": "uploaded",
        "Upload failed": "failure", "Last week on YouTube": "digest", "Can't check": "failure", "New for": "new",
    }


def test_a_kind_switched_off_isnt_sent(tmp_path, posts, monkeypatch):
    class Now:  # runs the background send straight away
        def __init__(self, target, args, **_):
            self.run = lambda: target(*args)

        def start(self):
            self.run()

    monkeypatch.setattr(notify.threading, "Thread", Now)
    settings = Settings(data_dir=tmp_path, discord_webhook_url="https://discord.com/api/webhooks/1/abc")
    with Session(get_engine(settings)) as s:
        set_app_setting(s, "notify_new", "0")
        s.commit()
    notify.send(settings, Note("New for Kino", kind="new"))
    notify.send(settings, Note("Ready: Kino", kind="review"))
    assert [p["json"]["embeds"][0]["title"] for p in posts] == ["Ready: Kino"]
    assert notify.enabled(settings, "review") and not notify.enabled(settings, "new")
