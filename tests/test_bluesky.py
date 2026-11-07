"""D39: new stories posted to Bluesky."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from digest import db
from digest.config import BlueskySettings, Config, Settings, load_config
from digest.publish import bluesky
from digest.publish.bluesky import Posted
from digest.publish.model import Name

from .conftest import CONFIG_DIR
from .fake_web import FakeWeb
from .test_publish import NOW, FakeDeployer, story

STOCKHOLM = Settings().zone
RULES = BlueskySettings(
    posts_per_day=3,
    min_minutes_between_posts=50,
    max_story_age_hours=6,
    common_hashtags=["News"],
    hashtags={"world": ["WorldNews"], "tech_science": ["Science", "Tech"]},
)
LIVE = Settings(
    site_url="https://ungated.example",
    bluesky_handle="ungated.example",
    bluesky_app_password=SecretStr("app-pass"),
)
SERVICE = "https://bsky.social/xrpc"


@pytest.fixture(scope="module")
def config() -> Config:
    return load_config(CONFIG_DIR)


def pick(stories: list[Any], posted: list[Posted] | None = None, **kw: Any) -> Any:
    args: dict[str, Any] = {"now": NOW, "live_since": NOW, "zone": STOCKHOLM} | kw
    return bluesky.pick(stories, posted or [], RULES, **args)


def test_enabled_needs_credentials_and_a_public_site() -> None:
    assert bluesky.enabled(LIVE)
    assert not bluesky.enabled(Settings(site_url="https://ungated.example"))
    assert not bluesky.enabled(LIVE.model_copy(update={"site_noindex": True}))
    assert not bluesky.enabled(LIVE.model_copy(update={"site_url": None}))


def test_picks_the_top_live_new_ai_story() -> None:
    top, other = story(1, score=5), story(2, score=1)
    assert pick([other, top]).id == 1
    # Not yet in a deployed build, too old, links-only, or already posted: never.
    assert pick([top], live_since=NOW - dt.timedelta(hours=2)) is None
    assert pick([story(3, hours_ago=7)]) is None
    assert pick([story(4, kind="links")]) is None
    done = [Posted(1, NOW - dt.timedelta(hours=3), "at://x/1")]
    assert pick([top, other], done).id == 2
    assert pick([top], live_since=None) is None


def test_spacing_and_daily_cap() -> None:
    s = story(9)
    recent = [Posted(1, NOW - dt.timedelta(minutes=20), "at://x/1")]
    assert pick([s], recent) is None
    # NOW is 14:00 in Stockholm: three posts already today hit the cap ...
    today = [Posted(i, NOW - dt.timedelta(hours=i), f"at://x/{i}") for i in (1, 2, 3)]
    assert pick([s], today) is None
    # ... but posts from yesterday don't count.
    earlier = [Posted(i, NOW - dt.timedelta(hours=12 + i), f"at://x/{i}") for i in (1, 2, 3)]
    assert pick([s], earlier).id == 9


def test_hashtags_follow_the_section() -> None:
    assert bluesky.hashtags(story(1, category="tech_science"), RULES) == ["News", "Science", "Tech"]
    assert bluesky.hashtags(story(1, category="sport"), RULES) == ["News"]


def placed(*names: str, category: str = "world") -> Any:
    return dataclasses.replace(
        story(1, category=category), names=tuple(Name(n, "place") for n in names)
    )


@pytest.mark.parametrize(
    ("name", "tag"),
    [
        ("Kyiv", "Ukraine"),  # a town tags its country
        ("Ukraine", "Ukraine"),
        ("Gaza", "Gaza"),  # a part that is its own, bigger tag
        ("West Bank", "WestBank"),
        ("United States", "USA"),
        ("London", "UK"),
        ("Türkiye", "Turkey"),
        ("New Zealand", "NewZealand"),
        ("Bosnia and Herzegovina", "Bosnia"),
        ("South Korea", "SouthKorea"),
        ("Georgia", None),  # as often the US state: no map, no tag
        ("Atlantis", None),
    ],
)
def test_place_tag_is_the_main_places_country(name: str, tag: str | None) -> None:
    assert bluesky.place_tag(placed(name)) == tag


def test_place_tag_follows_the_map_first_known_place_wins() -> None:
    assert bluesky.place_tag(placed("Atlantis", "Paris", "Germany")) == "France"
    assert bluesky.place_tag(story(1)) is None  # no names at all


def test_hashtags_add_the_country_once_and_can_be_turned_off() -> None:
    assert bluesky.hashtags(placed("Kyiv"), RULES) == ["News", "WorldNews", "Ukraine"]
    off = RULES.model_copy(update={"place_hashtag": False})
    assert bluesky.hashtags(placed("Kyiv"), off) == ["News", "WorldNews"]
    news = RULES.model_copy(update={"common_hashtags": ["News", "Ukraine"]})
    assert bluesky.hashtags(placed("Kyiv"), news) == ["News", "Ukraine", "WorldNews"]


def test_post_text_and_facets_use_utf8_byte_offsets() -> None:
    s = story(1, headline="Zürich café prices rise 5 €", sources=4)
    text, facets = bluesky.post_text(s, ["News", "WorldNews"])
    assert text == "Zürich café prices rise 5 €\n\nAI summary from 4 sources. #News #WorldNews"
    raw = text.encode()
    got = [raw[f["index"]["byteStart"] : f["index"]["byteEnd"]].decode() for f in facets]
    assert got == ["#News", "#WorldNews"]
    assert [f["features"][0]["tag"] for f in facets] == ["News", "WorldNews"]


def test_long_headline_is_cut_to_fit() -> None:
    s = story(1, headline="word " * 100, sources=1)
    text, facets = bluesky.post_text(s, ["News"])
    assert len(text) <= bluesky.MAX_TEXT
    assert text.endswith("…\n\nAI summary. #News") and len(facets) == 1


def test_description_keeps_whole_sentences() -> None:
    first = "A" * 200 + "."
    s = dataclasses.replace(story(1), summary=f"{first} {'B' * 150}. Third.")
    assert bluesky.description(s) == first
    s = dataclasses.replace(story(1), summary="C" * 400)
    assert len(bluesky.description(s)) == bluesky.MAX_DESCRIPTION


def fake_bluesky(
    web: FakeWeb, *, login: int = 200, earlier: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Routes for a working Bluesky and the site; returns the records that were posted."""
    records: list[dict[str, Any]] = []

    def create(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer jwt"
        records.append(json.loads(request.content))
        return httpx.Response(200, json={"uri": "at://did:plc:me/app.bsky.feed.post/1"})

    session = {"did": "did:plc:me", "accessJwt": "jwt"}
    web.route(f"{SERVICE}/com.atproto.server.createSession", httpx.Response(login, json=session))
    blob = {"blob": {"$type": "blob", "ref": {"$link": "bafy"}, "mimeType": "image/png"}}
    web.route(f"{SERVICE}/com.atproto.repo.uploadBlob", httpx.Response(200, json=blob))
    web.route(f"{SERVICE}/com.atproto.repo.createRecord", create)
    web.route(recent_url(), httpx.Response(200, json={"records": earlier or []}))
    return records


def recent_url() -> str:
    params = {"repo": "did:plc:me", "collection": "app.bsky.feed.post", "limit": 30}
    return str(httpx.URL(f"{SERVICE}/com.atproto.repo.listRecords", params=params))


def test_post_due_posts_once_with_a_link_card(tmp_path: Path, config: Config) -> None:
    path = tmp_path / "state.sqlite"
    db.migrate(path)
    engine = db.make_engine(path)
    web = FakeWeb()
    records = fake_bluesky(web)
    s = story(7, sources=3)
    png = httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})
    web.route("https://ungated.example" + s.share_path, png)

    n = bluesky.post_due(engine, LIVE, config, [s], NOW, live_since=NOW, client=web.client())
    assert n == 1 and len(records) == 1
    post = records[0]["record"]
    assert records[0]["repo"] == "did:plc:me"
    assert post["text"].startswith("Headline 7\n\nAI summary from 3 sources. #News")
    card = post["embed"]["external"]
    assert card["uri"] == "https://ungated.example/story/2026-10-24/story-7"
    assert card["title"] == "Headline 7" and card["thumb"]["ref"]["$link"] == "bafy"
    assert post["langs"] == ["en"]
    assert [p.story for p in bluesky.load_posted(engine)] == [7]

    # The same story is never posted twice, even once the spacing has passed.
    later = NOW + dt.timedelta(hours=2)
    again = bluesky.post_due(
        engine, LIVE, config, [s], later, live_since=later, client=web.client()
    )
    assert again == 0 and len(records) == 1


def test_failure_is_logged_and_retried_later(tmp_path: Path, config: Config) -> None:
    path = tmp_path / "state.sqlite"
    db.migrate(path)
    engine = db.make_engine(path)
    web = FakeWeb()
    records = fake_bluesky(web, login=401)
    n = bluesky.post_due(engine, LIVE, config, [story(1)], NOW, live_since=NOW, client=web.client())
    assert n == 0 and records == [] and bluesky.load_posted(engine) == []
    # Never the password in a request other than the login.
    assert all(
        b"app-pass" not in r.content for r in web.requests if "createSession" not in str(r.url)
    )


def test_missing_share_image_still_posts(tmp_path: Path, config: Config) -> None:
    path = tmp_path / "state.sqlite"
    db.migrate(path)
    engine = db.make_engine(path)
    web = FakeWeb()  # the share image is not routed: 599
    records = fake_bluesky(web)
    n = bluesky.post_due(engine, LIVE, config, [story(1)], NOW, live_since=NOW, client=web.client())
    assert n == 1 and "thumb" not in records[0]["record"]["embed"]["external"]


def test_a_story_already_on_bluesky_is_not_posted_again(tmp_path: Path, config: Config) -> None:
    """A run posted it but its state was never saved: Bluesky itself remembers."""
    path = tmp_path / "state.sqlite"
    db.migrate(path)
    engine = db.make_engine(path)
    web = FakeWeb()
    s = story(5)
    link = {
        "$type": "app.bsky.embed.external",
        "external": {"uri": "https://ungated.example" + s.url},
    }
    old = {"uri": "at://did:plc:me/app.bsky.feed.post/old", "value": {"embed": link}}
    records = fake_bluesky(web, earlier=[{"uri": "at://x/no-embed", "value": {}}, old])
    n = bluesky.post_due(engine, LIVE, config, [s], NOW, live_since=NOW, client=web.client())
    assert n == 0 and records == []
    assert bluesky.load_posted(engine) == [Posted(5, NOW, "at://did:plc:me/app.bsky.feed.post/old")]


def test_publish_posts_only_stories_in_the_deployed_build(
    tmp_path: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from digest.publish import deploy, indexnow

    monkeypatch.setattr(indexnow, "ping", lambda site, urls: 200)
    calls: list[dt.datetime | None] = []

    def fake_post(*args: Any, live_since: dt.datetime | None, **kw: Any) -> int:
        calls.append(live_since)
        return 1

    monkeypatch.setattr(bluesky, "post_due", fake_post)
    path = tmp_path / "state.sqlite"
    db.migrate(path)
    engine = db.make_engine(path)
    d = FakeDeployer()
    done = deploy.publish(engine, LIVE, config, NOW, deployer=d, out=tmp_path / "s")
    assert done["deployed"] == 1 and done["bluesky"] == 1 and calls == [NOW]
    # A run that doesn't deploy still posts what the last deploy made live.
    soon = NOW + dt.timedelta(minutes=10)
    done = deploy.publish(engine, LIVE, config, soon, deployer=d, out=tmp_path / "s")
    assert done["skipped"] == "too_soon" and calls == [NOW, NOW]
    # Without credentials nothing is posted.
    off = Settings(site_url="https://ungated.example")
    done = deploy.publish(engine, off, config, soon, deployer=d, out=tmp_path / "s")
    assert "bluesky" not in done and len(calls) == 2


def test_shipped_hashtags_name_real_sections(config: Config) -> None:
    ids = {c.id for c in config.categories}
    assert set(config.settings.bluesky.hashtags) <= ids
    assert set(config.settings.bluesky.hashtags) == ids  # every section has its own tags


def test_site_links_the_bluesky_account_only_when_set(tmp_path: Path, config: Config) -> None:
    from digest.publish.render import build_site

    profile = "https://bsky.app/profile/ungated.example"
    out = tmp_path / "on"
    build_site(out, [story(1)], settings=LIVE, config=config, now=NOW)
    home = (out / "index.html").read_text("utf-8")
    assert f'<a href="{profile}" rel="me">Follow on Bluesky</a>' in home
    assert "Follow along" in (out / "about.html").read_text("utf-8")
    page = (out / story(1).url.lstrip("/")).with_suffix(".html").read_text("utf-8")
    assert f'"sameAs": ["{profile}"]' in page or f'"sameAs":["{profile}"]' in page

    out = tmp_path / "off"
    build_site(out, [story(1)], settings=Settings(site_url=LIVE.site_url), config=config, now=NOW)
    assert "bsky.app" not in (out / "index.html").read_text("utf-8")
    assert "Follow along" not in (out / "about.html").read_text("utf-8")


def test_bluesky_url_drops_a_leading_at() -> None:
    assert Settings(bluesky_handle="@ungated.fyi").bluesky_url == (
        "https://bsky.app/profile/ungated.fyi"
    )
    assert Settings().bluesky_url is None
