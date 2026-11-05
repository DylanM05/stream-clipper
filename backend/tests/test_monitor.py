import io
import json
import os
import re
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

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


def test_videos_keep_when_they_were_posted_even_ones_listed_before_dates_were_kept(setup):
    settings, mon, fake, (show_id, channel_id) = setup
    fake.videos = [video("old")]
    mon.check(channel_id)
    assert items(settings)["old"].published_at is None  # as listed by an older version

    fake.videos = [replace(video("new"), published=1787329245.0), replace(video("old"), published=1787000000.0)]
    mon.check(channel_id)
    found = items(settings)
    assert found["new"].published_at == datetime(2026, 8, 21, 16, 20, 45, tzinfo=timezone.utc)
    assert found["old"].published_at == datetime.fromtimestamp(1787000000, timezone.utc)


def test_when_a_listed_video_was_posted():
    assert monitor.published({"timestamp": 1787329245}) == 1787329245  # Kick, Rumble, and YouTube roughly
    # Twitch's list doesn't say, but its preview image is stored under the stream's start time.
    thumbnail = ("https://static-cdn.jtvnw.net/cf_vods/d2nvs31859zcd8/"
                 "4bd2ab6c3c1e4d2fbf4b_kino_casino_41234567890_1787329245//thumb/thumb0-320x180.jpg")
    assert monitor.published({"thumbnail": thumbnail}) == 1787329245
    assert monitor.published({"thumbnail": "https://vod-secure.twitch.tv/_404/404_processing_320x180.png"}) is None
    assert monitor.published({}) is None


def refusal(status: int) -> Response:
    """A yt-dlp response with `status`, for raising the HTTPError yt-dlp would."""
    return Response(io.BytesIO(b""), url="https://rumble.com/v6a-a-stream.html", headers={}, status=status)


@pytest.fixture
def rumble(tmp_path, monkeypatch):
    """A show watching a Rumble channel, with the upload browser and yt-dlp replaced. The browser lists the videos
    on the channel's page. Each one's page carries the featured video's player first, and describes its own (whose
    id isn't the page's, as on Rumble); that player says whose video it is, whether it's live and how long it is."""
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

    def channel_videos(cdp_url, channel_url):
        site["pages"].append(channel_url)
        return [f"https://rumble.com/{v}-a-stream.html" for v in site["videos"]]

    class FakeYoutubeDL:
        def __init__(self, opts):
            site["options"].append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def urlopen(self, url):  # a video's page
            if site["refuse"]:
                raise HTTPError(refusal(site["refuse"]))
            page_id = re.search(r"rumble\.com/(v[0-9a-z]+)-", url)[1]
            own = {"@type": "VideoObject", "url": url, "embedUrl": f"https://rumble.com/embed/p{page_id[1:]}/"}
            return io.BytesIO((
                '<iframe src="https://rumble.com/embed/u4.pFEATURED/?pub=4"></iframe>'  # whose video isn't listed
                f"<script type=application/ld+json>{json.dumps([own])}</script>"
            ).encode())

        def extract_info(self, url, download=False):
            player = re.fullmatch(r"https://rumble\.com/embed/p(\w+)/", url)
            assert player, f"yt-dlp must be given the video's own player, not {url}"
            site["read"].append(f"v{player[1]}")
            return site["videos"][f"v{player[1]}"]

    monkeypatch.setattr(monitor.rumble, "channel_videos", channel_videos)
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
        "v6old": {"title": "Last week", "duration": 9000, "live_status": "was_live", "channel_url": OURS,
                  "timestamp": 1787329245},
    }
    mon.check(channel_id)
    found = items(settings)
    assert set(found) == {"v6live", "v6old"}  # only the channel's own streams
    assert (found["v6live"].status, found["v6live"].reason) == ("waiting", "Still live")
    assert found["v6old"].status == "older"
    assert found["v6old"].published_at == datetime.fromtimestamp(1787329245, timezone.utc)

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
    # The channel's page once per check, and every request yt-dlp makes passes itself off as Chrome.
    assert site["pages"] == ["https://rumble.com/c/TheQuartering"] * 3
    assert site["options"] and all(str(o.get("impersonate")).startswith("chrome") for o in site["options"])


def test_past_rumble_streams_further_down_the_page_never_come_up_as_new(rumble):
    settings, mon, site, channel_id = rumble
    past = {f"v6p{n}": {"title": f"Past stream {n}", "duration": 9000, "live_status": "was_live", "channel_url": OURS}
            for n in range(8)}  # newest first, as on the Live tab
    site["videos"] = {"v6lofi": {"title": "lofi", "duration": None, "live_status": "is_live",
                                 "channel_url": "https://rumble.com/c/LofiGirl"}, **past}
    mon.check(channel_id)
    assert {k: v.status for k, v in items(settings).items()} == {f"v6p{n}": "older" for n in range(monitor.RUMBLE_RECENT)}

    for _ in range(3):
        site["read"].clear()
        mon.check(channel_id)
        assert site["read"] == []  # the newest few are settled, and the older ones stay unread
    assert set(items(settings)) == {f"v6p{n}" for n in range(monitor.RUMBLE_RECENT)}

    site["videos"] = {"v6new": {"title": "Tonight", "duration": None, "live_status": "is_live", "channel_url": OURS},
                      **site["videos"]}
    mon.check(channel_id)
    assert site["read"] == ["v6new"]
    assert items(settings)["v6new"].status == "waiting"


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
    assert "  https://rumble.com/v6lofi-a-stream.html" in out.split("Video links")[1]  # every link the browser found


def test_a_rumble_video_counts_only_if_rumble_says_its_the_channels():
    owner_is = monitor.rumble.owner_is
    assert owner_is({"channel_url": "https://rumble.com/c/thequartering"}, OURS)  # case doesn't matter
    assert not owner_is({"channel_url": "https://rumble.com/c/SomeoneElse", "channel": "TheQuartering"}, OURS)
    assert owner_is({"channel": "The Quartering"}, OURS)  # no link: by name
    assert not owner_is({}, OURS)  # says nothing: doesn't count


def test_rumble_turning_the_server_away_reads_plainly_on_the_channel(rumble):
    settings, mon, site, channel_id = rumble
    site["videos"] = {"v6mine": {"title": "Q After Dark", "duration": 3600, "live_status": "was_live", "channel_url": OURS}}
    site["refuse"] = 429
    mon.check(channel_id)
    with Session(get_engine(settings)) as s:
        assert "429 Too Many Requests" in s.get(Channel, channel_id).last_error
        assert s.get(Channel, channel_id).last_error.startswith("Rumble says the server asked too often")


def test_only_rumble_gets_impersonation():
    options = monitor.rumble.ytdlp_options
    assert str(options("https://rumble.com/v6abcd-a-stream.html")["impersonate"]).startswith("chrome")
    assert options("https://kick.com/kinocasino/videos/abc") == {}
    assert options("https://notrumble.com/x") == {}


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


def episode(folder, name, minutes_ago=60):
    """A video file in a watched folder, last changed `minutes_ago`."""
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video")
    when = utcnow().timestamp() - minutes_ago * 60
    os.utime(path, (when, when))
    return path


def test_a_folder_lists_its_video_files_in_season_and_episode_order(tmp_path):
    media = tmp_path / "media"
    show = media / "TV" / "Show"
    for name in ("Season 10/S10E01.mkv", "Season 2/S02E10.mkv", "Season 2/S02E09.MP4", "Season 1/S01E01.mkv"):
        episode(show, name)
    episode(show, "Season 2/S02E11.mkv", minutes_ago=1)  # still being copied in
    episode(show, "Season 1/S01E02.mkv.part")  # a partial download
    episode(show, "Season 1/S01E01.srt")
    episode(show, "Season 1/._S01E01.mkv")  # a Mac's copy of its details
    episode(show, ".Trash/Old.mkv")
    (show / "Season 1" / "Elsewhere.mkv").symlink_to(episode(tmp_path, "outside.mkv"))

    listed = monitor.list_folder(str(show), media)
    assert [(item.id, item.live) for item in listed] == [
        ("Season 1/S01E01.mkv", False), ("Season 2/S02E09.MP4", False), ("Season 2/S02E10.mkv", False),
        ("Season 2/S02E11.mkv", True), ("Season 10/S10E01.mkv", False),
    ]
    first = listed[0]
    assert (first.url, first.title, first.duration, first.final) == (str(show / "Season 1/S01E01.mkv"), "S01E01", None, True)


def test_a_watched_folder_must_be_inside_the_media_folder(tmp_path):
    media = tmp_path / "media"
    (media / "TV").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (media / "TV" / "Link").symlink_to(tmp_path / "elsewhere")
    assert monitor.parse_folder(f" {media}/TV/../TV ", media) == str((media / "TV").resolve())
    for outside in (str(tmp_path / "elsewhere"), str(media / "TV" / "Link"), str(media / "Missing"), f"{media}/../elsewhere"):
        with pytest.raises(ChannelUrlError, match="Use a folder inside"):
            monitor.parse_folder(outside, media)
    with pytest.raises(ChannelUrlError, match="CLIPPER_HOST_MEDIA_DIR"):
        monitor.parse_folder(str(media / "TV"), None)


@pytest.fixture
def folder(tmp_path):
    media = tmp_path / "media"
    path = media / "TV" / "Show"
    path.mkdir(parents=True)
    settings = Settings(data_dir=tmp_path / "data", media_dir=media)
    with Session(get_engine(settings)) as s:
        show = Show(name="Cartoon Shorts", video_type="episodes")
        s.add(show)
        s.commit()
        channel = Channel(show_id=show.id, url=str(path.resolve()), platform="folder", backlog=True, backlog_per_day=2)
        s.add(channel)
        s.commit()
        channel_id = channel.id
    return settings, Monitor(settings), path, channel_id


def a_day_later(settings):
    """As if what was queued was queued a day ago."""
    with Session(get_engine(settings)) as s:
        for row in s.exec(select(ChannelVideo).where(ChannelVideo.status == "queued")):
            row.updated_at -= timedelta(days=1)
            row.backlog_at = row.backlog_at and row.backlog_at - timedelta(days=1)
            s.add(row)
        s.commit()


def statuses(settings) -> dict[str, str]:
    return {key.rsplit("/", 1)[-1]: row.status for key, row in items(settings).items()}


def change(settings, channel_id, **fields):
    with Session(get_engine(settings)) as s:
        channel = s.get(Channel, channel_id)
        for k, v in fields.items():
            setattr(channel, k, v)
        s.add(channel)
        s.commit()


def test_a_folders_older_episodes_go_a_few_a_day_in_order_and_new_ones_never_wait(folder):
    settings, mon, path, channel_id = folder
    for n in (3, 1, 2, 4, 5, 6):
        episode(path, f"Season 1/S01E0{n}.mkv")
    episode(path, "Season 2/S02E01.mkv", minutes_ago=1)  # being copied in when the folder is added: new

    mon.check(channel_id)  # the first two older episodes start right away
    assert statuses(settings) == {"S01E01.mkv": "queued", "S01E02.mkv": "queued", "S01E03.mkv": "older",
                                  "S01E04.mkv": "older", "S01E05.mkv": "older", "S01E06.mkv": "older",
                                  "S02E01.mkv": "waiting"}
    found = items(settings)
    assert found["Season 1/S01E03.mkv"].reason == "Already in the folder when you added it"
    assert found["Season 2/S02E01.mkv"].reason == "Still being copied in"
    with Session(get_engine(settings)) as s:
        vods = s.exec(select(Vod).order_by(Vod.id)).all()
        assert [v.url for v in vods] == [str(path / "Season 1/S01E01.mkv"), str(path / "Season 1/S01E02.mkv")]
        assert {v.show_id for v in vods} == {s.get(Channel, channel_id).show_id}

    mon.check(channel_id)  # nothing more today
    assert statuses(settings)["S01E03.mkv"] == "older"

    episode(path, "Season 2/S02E01.mkv")  # finished copying
    mon.check(channel_id)
    assert statuses(settings)["S02E01.mkv"] == "queued"  # new ones don't wait for the backlog, nor count towards it

    a_day_later(settings)
    mon.check(channel_id)
    assert (statuses(settings)["S01E03.mkv"], statuses(settings)["S01E04.mkv"]) == ("queued", "queued")

    a_day_later(settings)
    (path / "Season 1/S01E05.mkv").unlink()  # gone: left for later
    episode(path, "Season 1/S01E06.mkv", minutes_ago=1)  # being copied in again: left for later
    mon.check(channel_id)
    assert (statuses(settings)["S01E05.mkv"], statuses(settings)["S01E06.mkv"]) == ("older", "older")

    change(settings, channel_id, backlog_paused=True)
    episode(path, "Season 1/S01E05.mkv")
    episode(path, "Season 1/S01E06.mkv")
    mon.check(channel_id)
    assert (statuses(settings)["S01E05.mkv"], statuses(settings)["S01E06.mkv"]) == ("older", "older")

    change(settings, channel_id, backlog_paused=False, backlog_order="latest", backlog_per_day=1)
    mon.check(channel_id)  # from the last episode
    assert (statuses(settings)["S01E05.mkv"], statuses(settings)["S01E06.mkv"]) == ("older", "queued")


def test_a_backlog_can_go_at_random_the_same_way_on_every_check(folder):
    settings, mon, path, channel_id = folder
    names = [f"Season {s}/S0{s}E0{e}.mkv" for s in (1, 2, 3) for e in range(1, 6)]
    for name in names:
        episode(path, name)
    change(settings, channel_id, backlog_order="random", backlog_per_day=3)
    mon.check(channel_id)
    with Session(get_engine(settings)) as s:
        channel = s.get(Channel, channel_id)
        rows = s.exec(select(ChannelVideo).where(ChannelVideo.channel_id == channel_id)).all()
        shuffled = [row.video_id for row in monitor.backlog_order(channel, rows)]
    assert sorted(shuffled) == sorted(names) and shuffled != names  # all of them, not in order
    taken = sorted(key for key, row in items(settings).items() if row.status == "queued")
    assert taken == sorted(shuffled[:3])

    a_day_later(settings)
    mon.check(channel_id)  # the next three in the same shuffle
    assert sorted(key for key, row in items(settings).items() if row.status == "queued") == sorted(shuffled[:6])


def test_a_channels_whole_history_is_listed_once_for_its_backlog_and_worked_through_oldest_first(setup):
    settings, _, fake, (show_id, channel_id) = setup
    listings = []

    def history(platform, url):
        listings.append(url)
        return [replace(video(f"v{n}"), published=1787000000.0 + n * 86400) for n in (5, 4, 3, 2, 1)]

    mon = Monitor(settings, lister=fake, history=history)
    fake.videos = [replace(video("v5"), published=1787000000.0 + 5 * 86400)]
    mon.check(channel_id)  # watched as before: only its newest are seen, and none are processed
    assert statuses(settings) == {"v5": "older"} and listings == []

    change(settings, channel_id, backlog=True, backlog_per_day=1)
    fake.videos = [video("v6", duration=None, live=True), *fake.videos]  # a stream going on meanwhile
    mon.check(channel_id)  # the rest are older, not new, and the oldest goes first
    assert statuses(settings) == {"v5": "older", "v6": "waiting", "v4": "older", "v3": "older", "v2": "older", "v1": "queued"}
    assert items(settings)["v1"].published_at == datetime.fromtimestamp(1787086400, timezone.utc)

    fake.videos = [video("v6", duration=7200), *fake.videos[1:]]
    mon.check(channel_id)
    mon.check(channel_id)  # the stream has ended: new, so it doesn't wait for the backlog
    assert (statuses(settings)["v6"], statuses(settings)["v2"]) == ("queued", "older")
    assert listings == ["https://kick.com/kino"]  # only once

    a_day_later(settings)
    mon.check(channel_id)
    assert statuses(settings)["v2"] == "queued"


def test_channels_that_cant_list_their_history_work_through_what_they_list(setup):
    settings, _, fake, (show_id, channel_id) = setup
    mon = Monitor(settings, lister=fake, history=lambda platform, url: pytest.fail("Rumble can't list its history"))
    change(settings, channel_id, platform="rumble", backlog=True, backlog_per_day=5)
    fake.videos = [video("v2"), video("v1")]
    mon.check(channel_id)
    assert statuses(settings) == {"v2": "queued", "v1": "queued"}


def test_folders_watched_before_the_library_are_in_it_and_channels_arent(tmp_path):
    with sqlite3.connect(tmp_path / "clipper.db") as conn:  # as the version before the Library left it
        conn.execute("CREATE TABLE show (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL)")
        conn.execute("CREATE TABLE channel (id INTEGER PRIMARY KEY, show_id INTEGER NOT NULL, url VARCHAR NOT NULL, "
                     "platform VARCHAR NOT NULL, older_per_day INTEGER NOT NULL DEFAULT 2)")
        conn.execute("INSERT INTO show (id, name) VALUES (1, 'Kino')")
        conn.execute("INSERT INTO channel VALUES (1, 1, 'https://kick.com/kino', 'kick', 2), (2, 1, '/media/TV', 'folder', 3)")
    with Session(get_engine(Settings(data_dir=tmp_path))) as s:
        assert [(c.platform, c.backlog, c.backlog_per_day, c.backlog_order) for c in s.exec(select(Channel).order_by(Channel.id))] == [
            ("kick", False, 2, "first"), ("folder", True, 3, "first"),
        ]


def test_a_backlog_that_went_newest_first_still_does(tmp_path):
    with sqlite3.connect(tmp_path / "clipper.db") as conn:  # as the first version of the Library left it
        conn.execute("CREATE TABLE channel (id INTEGER PRIMARY KEY, show_id INTEGER NOT NULL, url VARCHAR NOT NULL, "
                     "platform VARCHAR NOT NULL, backlog BOOLEAN, backlog_newest_first BOOLEAN)")
        conn.execute("INSERT INTO channel VALUES (1, 1, '/media/A', 'folder', 1, 1), (2, 1, '/media/B', 'folder', 1, 0)")
    with Session(get_engine(Settings(data_dir=tmp_path))) as s:
        assert [c.backlog_order for c in s.exec(select(Channel).order_by(Channel.id))] == ["latest", "first"]


def test_a_watched_folder_that_goes_missing_is_reported(folder):
    settings, mon, path, channel_id = folder
    path.rmdir()
    mon.check(channel_id)
    with Session(get_engine(settings)) as s:
        assert "Use a folder inside" in s.get(Channel, channel_id).last_error
