"""Rumble: its videos' own players, read from the page's description of its video, not the first player on it."""

import io
import json

import pytest
from yt_dlp.networking.common import Response
from yt_dlp.networking.exceptions import HTTPError

from clipper import rumble
from clipper.config import Settings
from clipper.pipeline import download
from clipper.rumble import RumbleError

PAGE = "https://rumble.com/v7h973m-9-thugs-vs-1-chad-white-boy-protecting-his-girlfriend-xbox-has-insane-updat.html"


def page(url: str, player: str, described: list[dict] | None = None) -> bytes:
    """A video's page the way Rumble serves it (Oct 2026): the channel's featured video's player comes first, then
    the page's own, and the page describes its own video in JSON-LD."""
    if described is None:
        described = [
            {"@context": "http://schema.org", "@type": "VideoObject", "name": "9 Thugs Vs 1 Chad",
             "embedUrl": f"https://rumble.com/embed/{player}/", "url": url},
            {"@context": "http://schema.org", "@type": "WebSite", "url": "https://rumble.com/"},
        ]
    return (
        '<html><div class="featured"><iframe src="https://rumble.com/embed/u4.v6feat01/?pub=4"></iframe></div>'
        f"<script type=application/ld+json>{json.dumps(described)}</script>"
        f'<script>Rumble("play", {{"video":"{player}","div":"vid_{player}"}});</script></html>'
    ).encode()


class FakeYdl:
    """Serves `html` for any page, or refuses with `status`."""

    def __init__(self, html: bytes = b"", status: int | None = None):
        self.html, self.status, self.asked = html, status, []

    def urlopen(self, url):
        self.asked.append(url)
        if self.status:
            raise HTTPError(Response(io.BytesIO(b""), url=url, headers={}, status=self.status))
        return io.BytesIO(self.html)


def test_a_videos_own_player_is_the_one_its_page_describes_not_the_first_on_it():
    ydl = FakeYdl(page(PAGE, "v6zq9k1"))
    assert rumble.player_url(PAGE, ydl) == "https://rumble.com/embed/v6zq9k1/"
    assert ydl.asked == [PAGE]
    assert rumble.player_url("https://rumble.com/embed/v6zq9k1/", FakeYdl()) == "https://rumble.com/embed/v6zq9k1/"


ANOTHER_VIDEO = [{"@type": "VideoObject", "url": "https://rumble.com/v7other-something-else.html",
                  "embedUrl": "https://rumble.com/embed/v6other/"}]


@pytest.mark.parametrize("ydl, message", [
    (FakeYdl(status=429), "asked too often"),
    (FakeYdl(status=403), "Cloudflare"),
    (FakeYdl(status=404), "answered 404"),
    (FakeYdl(page(PAGE, "v6zq9k1", described=ANOTHER_VIDEO)), "Couldn't tell which player"),  # never another's
    (FakeYdl(b"<html><iframe src='https://rumble.com/embed/v6zq9k1/'></iframe></html>"), "Couldn't tell which player"),
])
def test_rumble_not_saying_which_player_is_the_pages_own_says_why(ydl, message):
    with pytest.raises(RumbleError, match=message):
        rumble.player_url(PAGE, ydl)


def control(**fields) -> dict:
    """A form control the way upload_page's script reports it."""
    return {"tag": "input", "type": "", "id": "", "name": "", "label": "", "text": "", "value": "", "checked": False,
            "shown": True, "required": False, "accept": "", "options": [], **fields}


def test_the_upload_page_is_described_shown_first_then_what_a_later_step_shows():
    page = {
        "url": rumble.UPLOAD_PAGE, "title": "Upload Video", "headings": ["Upload Video", "Licensing"],
        "forms": [{"id": "upform", "action": "/upload.php", "method": "post"}],
        "controls": [
            control(type="file", id="Filedata", name="Filedata", accept="video/*", label="Choose a file", required=True),
            control(type="hidden", name="csrf"),
            control(tag="select", id="channelId", name="channelId", label="Channel",
                    options=[["0", "TheClips"], ["5", "Kino Klips"]]),
            control(type="radio", name="license", value="6", label="Rumble Only", shown=False, checked=True),
            control(tag="button", type="submit", id="submitForm", text="Upload", shown=False),
        ],
    }
    assert rumble.describe_form(page) == [
        f"Page: Upload Video ({rumble.UPLOAD_PAGE})",
        "Form: POST /upload.php  #upform",
        "Headings: Upload Video | Licensing",
        "Showing (2):",
        '  input[file]  #Filedata  name=Filedata  accept=video/*  required  "Choose a file"',
        '  select  #channelId  name=channelId  "Channel"  options: 0=TheClips; 5=Kino Klips',
        "Hidden for now (2):",
        '  input[radio]  name=license  value=6  checked  "Rumble Only"',
        '  button[submit]  #submitForm  "Upload"',
        "Hidden fields: csrf",
    ]


def test_a_signed_out_browser_is_pointed_out():
    page = {"url": "https://rumble.com/login.php", "title": "Login", "controls": [control(type="password", name="password")]}
    assert rumble.describe_form(page)[0].startswith("The upload browser isn't signed in to Rumble")


def test_video_links_drop_tracking_and_other_pages():
    hrefs = [
        "https://rumble.com/v7hdjt2-q-after-hours-ep.-41.html?e9s=src_v1_sa",
        "https://rumble.com/c/TheQuartering/videos",
        "https://rumble.com/v7h973m-9-thugs.html",
        "https://rumble.com/v7hdjt2-q-after-hours-ep.-41.html",  # again, without tracking
        "https://rumble.com/vivafrei",  # a channel, not a video
    ]
    assert rumble.video_links(hrefs) == [
        "https://rumble.com/v7hdjt2-q-after-hours-ep.-41.html", "https://rumble.com/v7h973m-9-thugs.html",
    ]


def test_a_rumble_download_goes_by_the_videos_own_player(tmp_path, monkeypatch):
    asked = []

    class FakeYoutubeDL:
        def __init__(self, opts):
            assert str(opts.get("impersonate")).startswith("chrome")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def urlopen(self, url):
            return io.BytesIO(page(url, "v6zq9k1"))

        def extract_info(self, url, download=False):
            asked.append(url)
            raise RuntimeError("stop here")

    monkeypatch.setattr(download, "YoutubeDL", FakeYoutubeDL)
    with pytest.raises(RuntimeError, match="stop here"):
        download.fetch(PAGE, Settings(data_dir=tmp_path))
    assert asked == ["https://rumble.com/embed/v6zq9k1/"]  # not the page, nor the featured video's player on it
