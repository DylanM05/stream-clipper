"""Rumble: its videos' own players, looked up instead of the first player on a video's page."""

import io
import json

import pytest
from yt_dlp.networking.common import Response
from yt_dlp.networking.exceptions import HTTPError

from clipper import rumble
from clipper.config import Settings
from clipper.pipeline import download
from clipper.rumble import RumbleError


class FakeYdl:
    """Answers Rumble's oEmbed API with `answer`, or refuses with `status`."""

    def __init__(self, answer=None, status: int | None = None):
        self.answer, self.status, self.asked = answer, status, []

    def urlopen(self, url):
        self.asked.append(url)
        if self.status:
            raise HTTPError(Response(io.BytesIO(b""), url=url, headers={}, status=self.status))
        return io.BytesIO(json.dumps(self.answer).encode() if not isinstance(self.answer, bytes) else self.answer)


PAGE = "https://rumble.com/v7h973m-9-thugs-vs-1-chad-white-boy-protecting-his-girlfriend-xbox-has-insane-updat.html"


def test_a_videos_own_player_comes_from_rumbles_oembed_api():
    ydl = FakeYdl({"type": "video", "html": '<iframe src="https://rumble.com/embed/u2ld.v6zq9k1/?pub=4" width="640"></iframe>'})
    assert rumble.player_url(PAGE, ydl) == "https://rumble.com/embed/v6zq9k1/"
    assert ydl.asked == [f"{rumble.OEMBED_URL}?url=https%3A%2F%2Frumble.com%2Fv7h973m-9-thugs-vs-1-chad-white-boy-"
                         "protecting-his-girlfriend-xbox-has-insane-updat.html"]
    assert rumble.player_url("https://rumble.com/embed/v6zq9k1/", FakeYdl()) == "https://rumble.com/embed/v6zq9k1/"


@pytest.mark.parametrize("ydl, message", [
    (FakeYdl(status=429), "asked too often"),
    (FakeYdl(status=404), "oEmbed API answered 404"),
    (FakeYdl(b"<html>not json</html>"), "didn't answer with JSON"),
    (FakeYdl({"type": "video", "html": ""}), "gave no player"),
])
def test_rumble_not_saying_which_player_says_why(ydl, message):
    with pytest.raises(RumbleError, match=message):
        rumble.player_url(PAGE, ydl)


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
            return io.BytesIO(json.dumps({"html": '<iframe src="https://rumble.com/embed/v6zq9k1/"></iframe>'}).encode())

        def extract_info(self, url, download=False):
            asked.append(url)
            raise RuntimeError("stop here")

    monkeypatch.setattr(download, "YoutubeDL", FakeYoutubeDL)
    with pytest.raises(RuntimeError, match="stop here"):
        download.fetch(PAGE, Settings(data_dir=tmp_path))
    assert asked == ["https://rumble.com/embed/v6zq9k1/"]  # not the page, whose first player is another video's
