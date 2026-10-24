from datetime import timedelta

import pytest
from sqlmodel import Session, select

from clipper import monitor
from clipper.config import Settings
from clipper.db import Channel, ChannelVideo, Show, Vod, get_engine, utcnow
from clipper.monitor import ChannelUrlError, Listed, Monitor, parse_channel


def test_parse_channel_links():
    assert parse_channel("kick.com/KinoCasino") == ("kick", "https://kick.com/kinocasino")
    assert parse_channel("https://www.twitch.tv/Shroud/videos?filter=archives") == ("twitch", "https://www.twitch.tv/shroud")
    assert parse_channel("https://m.youtube.com/@LinusTechTips/streams") == ("youtube", "https://www.youtube.com/@LinusTechTips")
    assert parse_channel("youtube.com/channel/UC123/videos") == ("youtube", "https://www.youtube.com/channel/UC123")
    with pytest.raises(ChannelUrlError):
        parse_channel("https://x.com/Andywarski")


def test_shows_match_by_link_or_channel_name():
    from clipper.db import find_show

    kick = Show(id=1, name="Kino", match="kick.com/kinocasino")
    twitch = Show(id=2, name="Other", match="twitch.tv/someone\nx.com/Andywarski")
    shows = [kick, twitch]
    assert find_show(shows, "https://kick.com/kinocasino/videos/abc") is kick
    assert find_show(shows, "https://www.twitch.tv/videos/123", names=["SomeOne"]) is twitch  # by channel name
    assert find_show(shows, "https://x.com/i/status/1", names=["@andywarski"]) is twitch
    assert find_show(shows, "https://www.twitch.tv/videos/123", names=["nobody"]) is None


def test_live_detection_for_each_platform():
    assert monitor.is_live({"is_live": True})  # Kick
    assert monitor.is_live({"live_status": "is_upcoming"})  # YouTube
    assert monitor.is_live({"thumbnail": "https://static-cdn.jtvnw.net/x/404_processing_320x180.png"})  # Twitch
    assert not monitor.is_live({"live_status": "was_live", "thumbnail": "https://i.ytimg.com/vi/x/hq720.jpg"})


class FakeChannel:
    """Stands in for yt-dlp: returns whatever the test says is on the channel right now."""

    def __init__(self):
        self.videos: list[Listed] = []
        self.error: Exception | None = None

    def __call__(self, platform, url):
        if self.error:
            raise self.error
        return list(self.videos)


def video(id_, title="Stream", duration=3600.0, live=False):
    return Listed(id=id_, url=f"https://kick.com/kino/videos/{id_}", title=title, duration=duration, live=live)


@pytest.fixture
def setup(tmp_path):
    settings = Settings(data_dir=tmp_path, monitor_interval_s=600)
    with Session(get_engine(settings)) as s:
        show = Show(name="Kino Casino", mode="segment", title_template="KINO CASINO {date} - {title}")
        s.add(show)
        s.commit()
        channel = Channel(show_id=show.id, url="https://kick.com/kino", platform="kick", exclude="rerun", min_minutes=30)
        s.add(channel)
        s.commit()
        ids = show.id, channel.id
    fake = FakeChannel()
    return settings, Monitor(settings, lister=fake), fake, ids


def items(settings) -> dict[str, ChannelVideo]:
    with Session(get_engine(settings)) as s:
        return {i.video_id: i for i in s.exec(select(ChannelVideo))}


def test_new_videos_are_queued_once_finished_and_filters_apply(setup):
    settings, mon, fake, (show_id, channel_id) = setup

    fake.videos = [video("live-now", duration=0, live=True), video("old")]
    mon.check(channel_id)  # first check: what's already there is only listed...
    assert items(settings)["old"].status == "older"
    assert items(settings)["live-now"].status == "waiting"  # ...but a stream that's live now still counts

    fake.videos = [video("new", duration=None, live=True), video("rerun", "Rerun of last week"), video("short", duration=600), video("old")]
    mon.check(channel_id)
    found = items(settings)
    assert (found["new"].status, found["new"].reason) == ("waiting", "Still live")
    assert found["rerun"].status == "waiting"  # filters are applied once it's confirmed finished

    fake.videos = [video("new", duration=7200), video("rerun", "Rerun of last week"), video("short", duration=600)]
    mon.check(channel_id)
    found = items(settings)
    assert (found["new"].status, found["new"].reason) == ("waiting", "Making sure the stream has ended")
    assert (found["rerun"].status, found["rerun"].reason) == ("skipped", 'The title has "rerun"')
    assert (found["short"].status, found["short"].reason) == ("skipped", "Shorter than 30 minutes")

    mon.check(channel_id)  # same length twice: finished
    new = items(settings)["new"]
    assert new.status == "queued"
    with Session(get_engine(settings)) as s:
        vod = s.get(Vod, new.vod_id)
        assert (vod.url, vod.show_id, vod.mode, vod.mode_auto) == ("https://kick.com/kino/videos/new", show_id, "segment", True)
        assert len(s.exec(select(Vod)).all()) == 1


def test_errors_are_shown_on_the_channel_and_checks_wait_for_the_interval(setup):
    settings, mon, fake, (_, channel_id) = setup
    fake.error = RuntimeError("Cloudflare said no")
    assert mon.check_next() is True
    with Session(get_engine(settings)) as s:
        ch = s.get(Channel, channel_id)
        assert ch.last_error == "RuntimeError: Cloudflare said no" and ch.last_checked_at is not None
    assert mon.check_next() is False  # checked just now: not due

    with Session(get_engine(settings)) as s:
        ch = s.get(Channel, channel_id)
        ch.last_checked_at = utcnow() - timedelta(seconds=601)
        s.add(ch)
        s.commit()
    fake.error = None
    assert mon.check_next() is True
    with Session(get_engine(settings)) as s:
        assert s.get(Channel, channel_id).last_error == ""
