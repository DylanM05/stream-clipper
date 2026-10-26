"""Reading an account's video posts from the data X's profile page loads."""

import json

import pytest

from clipper.xposts import SIGNED_OUT, Post, XError, parse_posts, read_profile


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


def test_a_posts_time_comes_from_its_id():
    posted_ms = 1_792_773_700_123  # Oct 23 2026, 16:41:40.123 UTC
    post = Post(id=str((posted_ms - 1288834974657) << 22 | 0x2A5F1), url="", text="", duration=None)
    assert post.posted == posted_ms / 1000  # the low bits say which server, not when


def x_user(screen_name):
    """An author the way X sent it in Oct 2026: the name under "core", no "legacy"."""
    return {"user_results": {"result": {
        "__typename": "User", "rest_id": "17918281", "core": {"name": "🎰🎲🃏Warski🃏🎲🎰", "screen_name": screen_name},
        "pinned_items": {"tweet_ids_str": ["2113687311779282973"]}, "relationship_counts": {"followers": 54848},
    }}}


def x_post(rest_id, text, author="Andywarski", video_ms=None, **more):
    media = [{"type": "video", "media_key": "13_1", "video_info": {"aspect_ratio": [16, 9], "duration_millis": video_ms}}] if video_ms else []
    return {
        "__typename": "Tweet", "rest_id": rest_id, "core": x_user(author), "grok_analysis_button": True,
        "edit_control": {"edit_tweet_ids": [rest_id]}, "views": {"count": "18371", "state": "EnabledWithCount"},
        "legacy": {"full_text": text, "id_str": rest_id, "entities": {"media": media} if media else {},
                   **({"extended_entities": {"media": media}} if media else {}), "user_id_str": "17918281"},
        **more,
    }


def x_item(post, component="organic_tweets"):
    return {"content": {
        "__typename": "TimelineTimelineItem", "clientEventInfo": {"component": component, "element": "tweet"},
        "itemContent": {"__typename": "TimelineTweet", "itemType": "TimelineTweet", "tweet_results": {"result": post}},
    }, "entryId": f"tweet-{post['rest_id']}"}


def test_reads_the_user_originals_timeline_x_sends_since_oct_2026():
    """Trimmed from what x.com/Andywarski's profile loaded (UserOriginalsTimeline), Oct 25 2026."""
    quoted_photo = x_post("2113141074508075393", ".@Andywarski meowdy partner https://t.co/0DpVkoxfGr", author="LianaTaylor3")
    data = {"data": {"user": {"result": {"__typename": "User", "timeline": {"timeline": {"instructions": [
        {"type": "TimelineClearCache"},
        {"type": "TimelinePinEntry", "entry": x_item(x_post(
            "2113687311779282973",
            "LIVE TONIGHT ON LITERALLY THE MOST INSANE EP OF ♥️♠️♦️♣️KINO CASINO♣️♦️♠️♥️ AT 8:30PM EST - H3 CHEATS ON HILA "
            "&amp; PODCAST ENDS! RT TO SUPPORT: https://t.co/BbkqljfktU https://t.co/hN8Q2x4u8k",
            video_ms=1250416), component="pinned_tweets")},
        {"type": "TimelineAddEntries", "entries": [
            x_item(x_post("2113143074063036651", "LMFAO He is so mad you put that hat on him 🤣",
                          quoted_status_result={"result": quoted_photo})),
            x_item(x_post(
                "2112999526491439550",
                "LIVE TONIGHT ON AN INSANE ♥️♠️♦️♣️KINO CASINO♣️♦️♠️♥️ At 8 :30PM EST -  JER BLINDS HUCK TO GRIFT WORLD SERIES "
                "TICKETS! RETWEET TO SUPPORT: https://t.co/BbkqljeMEm https://t.co/f902xlzKVh",
                video_ms=1055433)),
            {"content": {"__typename": "TimelineTimelineModule", "header": {"text": "Who to follow"}, "items": [
                {"item": {"itemContent": {"__typename": "TimelineUser", "user_results": x_user("UnwellMiguel")["user_results"]}}},
            ]}, "entryId": "who-to-follow-1"},
        ]},
    ]}}}}}}
    posts = parse_posts(data, "andywarski")
    assert [(p.id, p.duration) for p in posts] == [("2113687311779282973", 1250.416), ("2112999526491439550", 1055.433)]
    assert posts[1].url == "https://x.com/Andywarski/status/2112999526491439550"
    assert posts[1].text == ("LIVE TONIGHT ON AN INSANE ♥️♠️♦️♣️KINO CASINO♣️♦️♠️♥️ At 8 :30PM EST - JER BLINDS HUCK TO GRIFT "
                             "WORLD SERIES TICKETS! RETWEET TO SUPPORT:")


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
