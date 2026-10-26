from __future__ import annotations

import httpx
import pytest

from digest.config import Config
from digest.ingest import tdm
from digest.ingest.articles import fetch_article
from tests.fake_web import FakeWeb
from tests.test_fetcher import make_fetcher

ARTICLE = "https://news.example/2026/10/23/story"
PAGE = (
    b"<html><head><title>t</title></head><body><article><h1>Heading</h1>"
    + b"<p>"
    + b"Sentence number one of the article body. " * 40
    + b"</p></article></body></html>"
)


@pytest.fixture
def web() -> FakeWeb:
    w = FakeWeb()
    w.route("https://news.example/robots.txt", httpx.Response(404))
    w.route("https://news.example/.well-known/tdmrep.json", httpx.Response(404))
    w.route(ARTICLE, httpx.Response(200, content=PAGE, headers={"content-type": "text/html"}))
    return w


# --------------------------------------------------------------------------- pure checks


def test_tdmrep_first_matching_rule_wins() -> None:
    rules = tdm.parse_tdmrep(
        '[{"location": "/open/*", "tdm-reservation": 0}, {"location": "/*", "tdm-reservation": 1}]'
    )
    assert not tdm.reserved_by_tdmrep(rules, "/open/a")
    assert tdm.reserved_by_tdmrep(rules, "/news/a")
    assert not tdm.reserved_by_tdmrep([], "/news/a")


def test_tdmrep_rejects_garbage() -> None:
    with pytest.raises(tdm.TdmRepError):
        tdm.parse_tdmrep("{not json")
    with pytest.raises(tdm.TdmRepError):
        tdm.parse_tdmrep('{"location": "/"}')


def test_tdm_header_and_meta() -> None:
    assert tdm.reserved_by_headers({"TDM-Reservation": "1"})
    assert not tdm.reserved_by_headers({"tdm-reservation": "0"})
    assert tdm.reserved_by_html(b'<head><meta name="tdm-reservation" content="1"></head>')
    assert tdm.reserved_by_html(b"<head><META content='1' NAME='tdm-reservation'/></head>")
    assert not tdm.reserved_by_html(b'<head><meta name="tdm-reservation" content="0"></head>')
    assert not tdm.reserved_by_html(b'<head><meta name="description" content="1"></head>')


# --------------------------------------------------------------------------- article fetch (L4)


def test_article_text_is_extracted_and_trimmed(web: FakeWeb, ingest_config: Config) -> None:
    result = fetch_article(make_fetcher(web, ingest_config), ARTICLE, max_chars=300)
    assert result.outcome == "stored"
    assert result.text is not None
    assert 200 < len(result.text) <= 300
    assert "Sentence number one" in result.text


def test_site_wide_reservation_means_the_page_is_never_requested(
    web: FakeWeb, ingest_config: Config
) -> None:
    web.route(
        "https://news.example/.well-known/tdmrep.json",
        httpx.Response(200, json=[{"location": "/*", "tdm-reservation": 1}]),
    )
    result = fetch_article(make_fetcher(web, ingest_config), ARTICLE, 3000)
    assert result.outcome == "tdm_reserved"
    assert ARTICLE not in web.urls()


def test_unreadable_tdmrep_leaves_the_page_to_decide(web: FakeWeb, ingest_config: Config) -> None:
    web.route("https://news.example/.well-known/tdmrep.json", httpx.Response(500))
    fetch_article(make_fetcher(web, ingest_config), ARTICLE, 3000)
    assert ARTICLE in web.urls()  # D28: fetched, so the page header and meta decide


def test_soft_404_tdmrep_page_is_no_reservation(web: FakeWeb, ingest_config: Config) -> None:
    web.route(
        "https://news.example/.well-known/tdmrep.json",
        httpx.Response(200, text="<html>Not found</html>"),
    )
    assert fetch_article(make_fetcher(web, ingest_config), ARTICLE, 3000).outcome == "stored"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=PAGE, headers={"tdm-reservation": "1"}),
        httpx.Response(
            200,
            content=PAGE.replace(b"<title>", b'<meta name="tdm-reservation" content="1"><title>'),
        ),
    ],
)
def test_page_level_reservation_drops_the_page(
    web: FakeWeb, ingest_config: Config, response: httpx.Response
) -> None:
    web.route(ARTICLE, response)
    result = fetch_article(make_fetcher(web, ingest_config), ARTICLE, 3000)
    assert result.outcome == "tdm_reserved"
    assert result.text is None


def test_robots_disallowed_article_is_not_requested(web: FakeWeb, ingest_config: Config) -> None:
    web.route(
        "https://news.example/robots.txt",
        httpx.Response(200, text="User-agent: *\nDisallow: /2026/\n"),
    )
    result = fetch_article(make_fetcher(web, ingest_config), ARTICLE, 3000)
    assert result.outcome == "robots"
    assert ARTICLE not in web.urls()


def test_article_on_unregistered_host_is_never_requested(
    web: FakeWeb, ingest_config: Config
) -> None:
    result = fetch_article(make_fetcher(web, ingest_config), "https://cdn.example/a", 3000)
    assert result.outcome == "blocked"
    assert web.requests == []
