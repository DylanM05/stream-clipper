import pytest
from fastapi.testclient import TestClient

from clipper.config import Settings
from clipper.pipeline import download
from clipper.pipeline.download import SpeedLimit
from clipper.web import app as web_app


def test_the_speed_limit_is_the_total_of_all_the_pieces():
    clock, slept = [0.0], []
    limit = SpeedLimit(80, clock=lambda: clock[0], sleep=slept.append)  # 80 Mbit/s is 10 MB/s

    def report(at: float, done: int, filename: str = "source.f137.mp4") -> None:
        clock[0] = at
        limit({"status": "downloading", "filename": filename, "downloaded_bytes": done})

    report(0, 0)
    report(1, 15_000_000)  # 15 MB in a second from all the pieces together: half a second ahead
    assert slept == [0.5]
    report(10, 20_000_000)  # a slow patch, 8 seconds behind...
    report(10.1, 32_000_000)  # ...isn't made up in a burst over the limit
    assert slept == [0.5, pytest.approx(0.1)]
    report(11, 500_000, "source.f140.m4a")  # the audio counts afresh
    assert len(slept) == 2


def test_a_limited_download_still_fetches_several_pieces_at_once(tmp_path, monkeypatch):
    opened = []

    class FakeYoutubeDL:
        def __init__(self, opts):
            opened.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            return {"id": "1", "extractor_key": "TwitchVod", "title": "Stream"}

        def process_ie_result(self, info, download=True):
            raise RuntimeError("stop here")

    monkeypatch.setattr(download, "YoutubeDL", FakeYoutubeDL)
    with pytest.raises(RuntimeError, match="stop here"):
        download.fetch("https://www.twitch.tv/videos/1", Settings(data_dir=tmp_path), limit_mbps=50)
    opts = opened[-1]
    assert opts["concurrent_fragment_downloads"] == 8 and "ratelimit" not in opts
    assert any(isinstance(hook, SpeedLimit) for hook in opts["progress_hooks"])


def test_the_limit_can_be_changed_in_the_app(tmp_path):
    client = TestClient(web_app.create_app(Settings(data_dir=tmp_path, web_dir=tmp_path / "web")))
    assert client.get("/api/settings").json()["download_limit_mbps"] == 50
    assert client.patch("/api/settings", json={"download_limit_mbps": 25}).json()["download_limit_mbps"] == 25
    assert client.get("/api/settings").json()["download_limit_mbps"] == 25
    assert client.patch("/api/settings", json={"download_limit_mbps": -1}).status_code == 422


def test_full_speed_hours_are_set_in_the_app(tmp_path):
    client = TestClient(web_app.create_app(Settings(data_dir=tmp_path, web_dir=tmp_path / "web")))
    body = client.patch("/api/settings", json={"full_speed_from": 5, "full_speed_until": 11}).json()
    assert (body["full_speed_from"], body["full_speed_until"], body["timezone"]) == (5, 11, "America/New_York")
    assert client.patch("/api/settings", json={"full_speed_until": 25}).status_code == 422
