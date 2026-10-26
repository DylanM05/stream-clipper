import io
from datetime import timedelta

import pytest
from sqlmodel import Session, select
from yt_dlp.networking.common import Response
from yt_dlp.networking.exceptions import HTTPError

from clipper import monitor
from clipper.config import Settings
from clipper.db import Channel, ChannelVideo, Show, Vod, get_engine, utcnow
from clipper.monitor import ChannelUrlError, Listed, Monitor, parse_channel


def test_parse_channel_links():
    assert parse_channel("kick.com/KinoCasino") == ("kick", "https://kick.com/kinocasino")
    assert parse_channel("https://www.twitch.tv/Shroud/videos?filter=archives") == ("twitch", "https://www.twitch.tv/shroud")
    assert parse_channel("https://m.youtube.com/@LinusTechTips/streams") == ("youtube", "https://www.youtube.com/@LinusTechTips")
    assert parse_channel("youtube.com/channel/UC123/videos") == ("youtube", "https://www.youtube.com/channel/UC123")
    assert parse_channel("https://x.com/Andywarski") == ("x", "https://x.com/andywarski")
    assert parse_channel("twitter.com/@Andywarski/status/2113687311779282973") == ("x", "https://x.com/andywarski")
    assert parse_channel("rumble.com/c/TheQuartering") == ("rumble", "https://rumble.com/c/TheQuartering")
    assert parse_channel("https://rumble.com/TheQuartering") == ("rumble", "https://rumble.com/c/TheQuartering")
    assert parse_channel("rumble.com/user/someone/videos") == ("rumble", "https://rumble.com/user/someone")
    assert parse_channel("rumble.com/vivafrei") == ("rumble", "https://rumble.com/c/vivafrei")  # starts with v, still a channel
    for not_an_account in ("https://x.com/i/status/1", "x.com/home", "x.com/someone.else", "rumble.com/v6abcd-a-stream.html"):
        with pytest.raises(ChannelUrlError):
            parse_channel(not_an_account)


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


def refusal(status: int) -> Response:
    """A yt-dlp response with `status`, for raising the HTTPError yt-dlp would."""
    return Response(io.BytesIO(b""), url="https://rumble.com/c/TheQuartering/livestreams", headers={}, status=status)


@pytest.fixture
def rumble(tmp_path, monkeypatch):
    """A show watching a Rumble channel, with yt-dlp replaced: the channel page lists links, and each video says
    whether it's live and how long it is."""
    settings = Settings(data_dir=tmp_path)
    with Session(get_engine(settings)) as s:
        show = Show(name="TheQuartering", mode="segment")
        s.add(show)
        s.commit()
        channel = Channel(show_id=show.id, url="https://rumble.com/c/TheQuartering", platform="rumble")
        s.add(channel)
        s.commit()
        channel_id = channel.id
    site = {"videos": {}, "read": [], "pages": [], "options": [], "refuse": None}

    class FakeYoutubeDL:
        def __init__(self, opts):
            site["options"].append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def urlopen(self, url):
            site["pages"].append(url)
            if site["refuse"]:
                raise HTTPError(refusal(site["refuse"]))
            # Like Rumble's page: other channels' live streams up top, then the channel's own videos, which can
            # include another channel's stream too (a raid, say). Each video is linked twice.
            grid = "".join(f'<a class="videostream__link link" href="/{v}-a-stream.html?e9s=src">' for v in site["videos"])
            return io.BytesIO(f"<html>{grid}{grid}</html>".encode())

        def extract_info(self, url, download=False):
            vid = monitor.RUMBLE_VIDEO.search(url)[1]
            site["read"].append(vid)
            return site["videos"][vid]

    monkeypatch.setattr(monitor, "YoutubeDL", FakeYoutubeDL)
    return settings, Monitor(settings), site, channel_id


OURS = "https://rumble.com/c/TheQuartering"


def test_rumble_streams_are_queued_once_their_replay_is_finished(rumble):
    settings, mon, site, channel_id = rumble
    site["videos"] = {
        "v6lofi": {"title": "lofi house radio", "duration": None, "live_status": "is_live",
                   "channel_url": "https://rumble.com/c/LofiGirl"},  # in the "live now" strip on every page
        "v6live": {"title": "Live now", "duration": None, "live_status": "is_live", "channel_url": OURS},
        "v6guest": {"title": "Someone else", "duration": None, "live_status": "is_live",
                    "channel_url": "https://rumble.com/c/SomeoneElse"},
        "v6old": {"title": "Last week", "duration": 9000, "live_status": "was_live", "channel_url": OURS},
    }
    mon.check(channel_id)
    found = items(settings)
    assert set(found) == {"v6live", "v6old"}  # only the channel's own streams
    assert (found["v6live"].status, found["v6live"].reason) == ("waiting", "Still live")
    assert found["v6old"].status == "older"

    site["read"].clear()
    site["videos"]["v6live"] = {"title": "Live now", "duration": 14400, "live_status": "was_live", "channel_url": OURS}
    mon.check(channel_id)
    assert items(settings)["v6live"].reason == "Making sure the stream has ended"
    assert site["read"] == ["v6live"]  # last week's is settled and the other channels' are known: none is read

    mon.check(channel_id)
    queued = items(settings)["v6live"]
    assert queued.status == "queued"
    with Session(get_engine(settings)) as s:
        assert s.get(Vod, queued.vod_id).url == "https://rumble.com/v6live-a-stream.html"
    # One page per check, the channel's own, and every request passes itself off as Chrome.
    assert site["pages"] == ["https://rumble.com/c/TheQuartering"] * 3
    assert site["options"] and all(str(o.get("impersonate")).startswith("chrome") for o in site["options"])


def test_channel_check_says_which_streams_it_skipped_and_whose_they_are(rumble):
    from typer.testing import CliRunner

    from clipper.cli import app

    _, _, site, _ = rumble
    site["videos"] = {
        "v6mine": {"title": "Q After Dark", "duration": 3600, "live_status": "was_live", "channel_url": OURS},
        "v6lofi": {"title": "lofi house radio", "duration": None, "live_status": "is_live",
                   "channel_url": "https://rumble.com/c/LofiGirl"},
    }
    out = CliRunner().invoke(app, ["channel-check", "rumble.com/c/TheQuartering", "--links"]).output
    assert "Q After Dark" in out.split("Skipped")[0]
    assert "lofi house radio  (Rumble says: https://rumble.com/c/LofiGirl)" in out
    assert "/v6mine-a-stream.html  class: videostream__link link" in out  # every link on the page, for working out why


def test_a_rumble_video_counts_only_if_rumble_says_its_the_channels():
    owner_is = monitor.rumble_owner_is
    assert owner_is({"channel_url": "https://rumble.com/c/thequartering"}, OURS)  # case doesn't matter
    assert not owner_is({"channel_url": "https://rumble.com/c/SomeoneElse", "channel": "TheQuartering"}, OURS)
    assert owner_is({"channel": "The Quartering"}, OURS)  # no link: by name
    assert not owner_is({}, OURS)  # says nothing: doesn't count


def test_rumble_turning_the_server_away_reads_plainly_on_the_channel(rumble):
    settings, mon, site, channel_id = rumble
    site["refuse"] = 429
    mon.check(channel_id)
    with Session(get_engine(settings)) as s:
        assert "429 Too Many Requests" in s.get(Channel, channel_id).last_error
        assert s.get(Channel, channel_id).last_error.startswith("Rumble says the server asked too often")


def test_only_rumble_gets_impersonation():
    from clipper.pipeline.download import site_options

    assert str(site_options("https://rumble.com/v6abcd-a-stream.html")["impersonate"]).startswith("chrome")
    assert site_options("https://kick.com/kinocasino/videos/abc") == {}
    assert site_options("https://notrumble.com/x") == {}


def post(id_, duration=1250.0):
    return Listed(id=id_, url=f"https://x.com/Andywarski/status/{id_}", title="LIVE TONIGHT ON KINO CASINO",
                  duration=duration, live=False, final=True)


def test_videos_posted_on_x_are_queued_as_soon_as_they_are_seen(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    with Session(get_engine(settings)) as s:
        show = Show(name="Pre-show", mode="whole")
        s.add(show)
        s.commit()
        channel = Channel(show_id=show.id, url="https://x.com/andywarski", platform="x", min_minutes=5)
        s.add(channel)
        s.commit()
        show_id, channel_id = show.id, channel.id
    on_x = [post("1")]
    asked = []
    monkeypatch.setattr(monitor.xposts, "video_posts", lambda cdp_url, handle: asked.append(handle) or [
        monitor.xposts.Post(id=p.id, url=p.url, text=p.title, duration=p.duration) for p in on_x
    ])
    mon = Monitor(settings)

    mon.check(channel_id)  # what's already there is only listed
    assert asked == ["andywarski"]
    assert items(settings)["1"].status == "older"

    on_x = [post("3", duration=40), post("2"), post("1")]
    mon.check(channel_id)
    found = items(settings)
    assert found["2"].status == "queued"
    assert (found["3"].status, found["3"].reason) == ("skipped", "Shorter than 5 minutes")
    with Session(get_engine(settings)) as s:
        vod = s.get(Vod, found["2"].vod_id)
        assert (vod.url, vod.show_id, vod.mode) == ("https://x.com/Andywarski/status/2", show_id, "whole")


# Pre-show posts from x.com/Andywarski (Oct 2026), as the watcher reads them: links are left out.
PRESHOWS = [
    "LIVE TONIGHT ON LITERALLY THE MOST INSANE EP OF ♥️♠️♦️♣️KINO CASINO♣️♦️♠️♥️ AT 8:30PM EST - JER HUCK BLEND GRIFT "
    "UNLEASHED!H3 CHEATS ON HILA & PODCAST ENDS! ANGRY JOE IS SPIRALLING!  DSP SCAMS DENTS! THE REVENGE OF RTU! AND A TON "
    "MORE!RT TO SUPPORT:",
    "LIVE TONIGHT ON AN INSANE ♥️♠️♦️♣️KINO CASINO♣️♦️♠️♥️ At 8 :30PM EST -  JER BLINDS HUCK TO GRIFT WORLD SERIES "
    "TICKETS! DSP SEETHES AT CASINO & KICK! RTU GETS REVENGE! BLADE ER? AND A TON MORE!!! RETWEET TO SUPPORT:",
    "TONIGHT ON AN INSANE ♥️♠️♦️♣️KINO CASINO♣️♦️♠️♥️AT 8:30PM EST - QUARTERING WORLD SERIES SCAM & HEISENSPERG! DSP "
    "POVERTY FORCES HIM TO CAMEO!  RTU! SPOOKY SLOP! AND A TON MORE! RETWEET TO SUPPORT:",
    "LIVE TONIGHT ON ANOTHER ♥️♠️♦️♣️KINO CASINO♣️♦️♠️♥️AT 8:30PM EST - QUARTERING FELTED AS COSTCO EXPOSES DOG PILL "
    "LIES!!! DSP WINS EVO AND FELTS CRAIG! MR MORROW, FAILED GHOST HAUNTINGS AND MORE! RETWEET TO SUPPORT:",
    "TONIGHT ON A WILD ♥️♠️♦️♣️KINO CASINO♣️♦️♠️♥️AT 8:30PM EST - QUARTERING GRIFTS OFF DYING DOG HE MADE SICK!?!? DSP "
    "WAGE QUITS SUNSHINE! MR MORROW RETURNS! VICTOR FELTED! AND A TON MORE - RETWEET TO SUPPORT:",
]


@pytest.mark.parametrize("text, wanted", [
    *((text, True) for text in PRESHOWS),
    ("DSP's EVO win was the funniest thing on Kino Casino last night", False),  # a clip: no "tonight on"
    ("Streaming tonight on Kick, come hang out", False),  # no "kino casino"
])
def test_keywords_joined_with_plus_need_every_part(text, wanted):
    channel = Channel(show_id=1, url="https://x.com/andywarski", platform="x", keywords="tonight on + kino casino")
    reason = monitor.skip_reason(channel, Listed(id="1", url="", title=text, duration=1250, live=False, final=True))
    assert reason == ("" if wanted else "The post has none of the keywords")


def test_x_problems_read_as_they_are(setup):
    settings, mon, fake, (_, channel_id) = setup
    fake.error = monitor.xposts.XError(monitor.xposts.SIGNED_OUT)
    mon.check(channel_id)
    with Session(get_engine(settings)) as s:
        assert s.get(Channel, channel_id).last_error == monitor.xposts.SIGNED_OUT


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
