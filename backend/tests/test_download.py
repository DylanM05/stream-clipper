from fastapi.testclient import TestClient

from clipper.config import Settings
from clipper.pipeline.download import speed_limit
from clipper.web import app as web_app


def test_speed_limit_is_the_total_not_per_piece():
    # 50 Mbit/s is 6.25 MB/s; yt-dlp applies it per parallel piece, so pieces go one at a time.
    assert speed_limit(50) == {"ratelimit": 6_250_000, "concurrent_fragment_downloads": 1}
    assert speed_limit(0) == {}


def test_the_limit_can_be_changed_in_the_app(tmp_path):
    client = TestClient(web_app.create_app(Settings(data_dir=tmp_path, web_dir=tmp_path / "web")))
    assert client.get("/api/settings").json()["download_limit_mbps"] == 50
    assert client.patch("/api/settings", json={"download_limit_mbps": 25}).json()["download_limit_mbps"] == 25
    assert client.get("/api/settings").json()["download_limit_mbps"] == 25
    assert client.patch("/api/settings", json={"download_limit_mbps": -1}).status_code == 422
