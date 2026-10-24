"""Reading an account's video posts from the data X's profile page loads."""

import json

import pytest

from clipper.xposts import SIGNED_OUT, XError, parse_posts, read_profile


def tweet(id_, text, author="Andywarski", video_ms=None, **extra):
    """A post the way X's timeline data has it."""
    legacy = {"full_text": text, "id_str": id_, "created_at": "Fri Oct 23 16:41:40 +0000 2026", **extra}
    if video_ms:
        legacy["extended_entities"] = {"media": [{"type": "video", "video_info": {"duration_millis": video_ms}}]}
    return {
        "__typename": "Tweet",
        "rest_id": id_,
        "core": {"user_results": {"result": {"__typename": "User", "core": {"screen_name": author}}}},
        "legacy": legacy,
    }


def entry(result):
    return {"content": {"itemContent": {"tweet_results": {"result": result}}}}


def timeline(*entries, pinned=None):
    instructions = [{"type": "TimelineAddEntries", "entries": list(entries)}]
    if pinned:
        instructions.insert(0, {"type": "TimelinePinEntry", "entry": pinned})
    return {"data": {"user": {"result": {"timeline": {"timeline": {"instructions": instructions}}}}}}


PRESHOW_TEXT = "LIVE TONIGHT ON ♥️♠️♦️♣️KINO CASINO♣️♦️♠️♥️ AT 8:30PM EST - H3 &amp; MORE!\nRT TO SUPPORT: https://t.co/BbkqljfktU https://t.co/hN8Q2x4u8k"


def test_finds_the_accounts_own_video_posts():
    someone_elses = tweet("5", "their video", author="someone", video_ms=60_000)
    quote = tweet("4", "look at this")  # quotes a video, has none of its own
    quote["quoted_status_result"] = {"result": someone_elses}
    data = timeline(
        entry(tweet("1", PRESHOW_TEXT, video_ms=1_250_416)),
        entry(tweet("2", "just words")),
        entry(tweet("3", "RT", retweeted_status_result={"result": someone_elses})),  # a repost
        entry(quote),
        entry({"__typename": "TweetWithVisibilityResults", "tweet": tweet("6", "limited reach", video_ms=90_000)}),
        entry(tweet("7", "buy now", author="advertiser", video_ms=15_000)),  # promoted
        pinned=entry(tweet("6", "limited reach", video_ms=90_000)),  # also pinned: listed once
    )
    posts = parse_posts(data, "andywarski")
    assert [p.id for p in posts] == ["6", "1"]
    preshow = posts[1]
    assert preshow.url == "https://x.com/Andywarski/status/1"
    assert preshow.duration == 1250.416
    assert preshow.text == "LIVE TONIGHT ON ♥️♠️♦️♣️KINO CASINO♣️♦️♠️♥️ AT 8:30PM EST - H3 & MORE! RT TO SUPPORT:"


def test_long_posts_keep_their_full_text():
    post = tweet("1", "The start of a long post…", video_ms=1000)
    post["note_tweet"] = {"note_tweet_results": {"result": {"text": "The start of a long post, and the rest of it"}}}
    [found] = parse_posts(timeline(entry(post)), "Andywarski")
    assert found.text == "The start of a long post, and the rest of it"


def test_no_posts_at_all_means_x_changed_its_data():
    with pytest.raises(XError, match="changed how its page sends them"):
        parse_posts({"data": {"user": {"result": {"timeline_v3": {"instructions": []}}}}}, "Andywarski")


def test_author_name_in_older_layout():
    post = tweet("1", "pre-show", video_ms=1000)
    post["core"]["user_results"]["result"] = {"legacy": {"screen_name": "Andywarski"}}
    assert [p.id for p in parse_posts(timeline(entry(post)), "andywarski")] == ["1"]


# --- Against a stand-in x.com in a real browser (skipped where none is installed) ---


@pytest.fixture(scope="module")
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        for kwargs in ({}, {"channel": "msedge"}, {"channel": "chrome"}):
            try:
                browser = p.chromium.launch(**kwargs)
                break
            except Exception:
                continue
        else:
            pytest.skip("no browser available")
        yield browser
        browser.close()


def stand_in_x(browser, profile_html: str, timeline_status: int = 200, data=None):
    """A tab where x.com is served without the network: the profile page, and the data it fetches."""
    tab = browser.new_page()
    tab.route("https://x.com/**", lambda r: r.fulfill(content_type="text/html", body=profile_html))
    tab.route("https://x.com/i/api/graphql/**", lambda r: r.fulfill(
        status=timeline_status if "Tweets" in r.request.url else 200, content_type="application/json",
        body=json.dumps(timeline(entry(tweet("1", "pre-show", video_ms=1_250_416))) if data is None else data),
    ))
    return tab


def loads(*operations: str) -> str:
    """A profile page that fetches these GraphQL operations."""
    calls = "".join(f"fetch('/i/api/graphql/AbC123/{op}?variables=%7B%7D');" for op in operations)
    return f"<script>setTimeout(() => {{ {calls} }}, 300)</script>"


@pytest.mark.parametrize("operation", ["UserTweets", "UserOriginalsTimeline", "UserPosts"])  # whatever X calls it
def test_reads_the_posts_the_profile_page_loads(browser, operation):
    tab = stand_in_x(browser, loads(operation))
    posts = parse_posts(read_profile(tab, "Andywarski", wait_s=5), "Andywarski")
    assert [(p.id, p.duration) for p in posts] == [("1", 1250.416)]
    tab.close()


def test_data_without_posts_is_named_in_the_error(browser):
    tab = stand_in_x(browser, loads("UserByScreenName", "ProfileSpotlightsQuery"), data={"data": {"user": {}}})
    with pytest.raises(XError) as e:
        read_profile(tab, "Andywarski", wait_s=2)
    assert "no posts came with it (it loaded UserByScreenName, ProfileSpotlightsQuery)" in str(e.value)
    tab.close()


def test_being_sent_to_the_login_page_means_signed_out(browser):
    tab = stand_in_x(browser, "<script>if (!location.pathname.startsWith('/i/flow')) location.href = '/i/flow/login'</script>")
    with pytest.raises(XError) as e:
        read_profile(tab, "Andywarski", wait_s=5)
    assert str(e.value) == SIGNED_OUT
    tab.close()


def test_too_many_requests(browser):
    tab = stand_in_x(browser, loads("UserTweets"), timeline_status=429)
    with pytest.raises(XError, match="too often"):
        read_profile(tab, "Andywarski", wait_s=2)
    tab.close()


def test_a_profile_that_never_loads_anything(browser):
    tab = stand_in_x(browser, "<p>This account doesn't exist</p>")
    with pytest.raises(XError, match="didn't load anything for @Andywarski"):
        read_profile(tab, "Andywarski", wait_s=1)
    tab.close()
