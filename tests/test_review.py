from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from digest.cli import app
from digest.config import Config
from digest.ingest.fetcher import Fetcher
from digest.ingest.guard import RequestGuard
from digest.ingest.review import (
    MemoryPolicyCache,
    merge_results,
    render_markdown,
    run_review,
    suggest,
)
from tests.fake_web import FakeWeb, item, rss

T0 = dt.datetime(2026, 10, 23, 9, 0, tzinfo=dt.UTC)
DATE = "Fri, 23 Oct 2026 08:00:00 GMT"
CANARY = "CANARYREVIEW77"


@pytest.fixture
def web() -> FakeWeb:
    w = FakeWeb()
    w.route("https://news.example/robots.txt", httpx.Response(404))
    w.route("https://news.example/.well-known/tdmrep.json", httpx.Response(404))
    w.route(
        "https://news.example/feed.xml",
        httpx.Response(
            200,
            content=rss(
                item(f"Title {CANARY}", f"https://news.example/{CANARY}", desc=CANARY, date=DATE)
            ),
        ),
    )
    w.route(
        f"https://news.example/{CANARY}",
        httpx.Response(200, content=b'<head><meta name="tdm-reservation" content="1"></head>'),
    )
    w.route(
        "https://radio.example/robots.txt",
        httpx.Response(200, text="User-agent: *\nDisallow: /\n"),
    )
    w.route("https://dormant.example/robots.txt", httpx.Response(503))
    return w


def review(web: FakeWeb, config: Config) -> dict[str, dict[str, object]]:
    fetcher = Fetcher(
        RequestGuard.for_sources(config, config.sources),  # disabled sources too
        MemoryPolicyCache(),
        config.settings.fetch,
        user_agent="DigestBot/1.0 (+https://digest.example/bot)",
        robots_token="DigestBot",
        client=web.client(),
        sleep=lambda _s: None,
    )
    return run_review(fetcher, config, now=lambda: T0)


def test_review_reports_verdicts_without_content(web: FakeWeb, ingest_config: Config) -> None:
    results = review(web, ingest_config)
    news, radio, dormant = results["news"], results["radio"], results["dormant"]
    [feed] = news["feeds"]  # type: ignore[misc]
    assert feed["status"] == 200 and feed["entries"] == 1 and feed["newest_age_hours"] == 1.0
    assert news["sample_article"]["page_tdm"] == "reserved"  # type: ignore[index]
    assert radio["feeds"][0]["robots"] == "disallowed"  # type: ignore[index]
    assert dormant["feeds"][0]["robots"] == "unavailable"  # type: ignore[index]
    assert CANARY.lower() not in json.dumps(results).lower()
    assert web.unrouted() == []


def test_suggestions(web: FakeWeb, ingest_config: Config) -> None:
    results = review(web, ingest_config)
    news, radio = ingest_config.source("news"), ingest_config.source("radio")
    assert suggest(news, results["news"]) == ("facts_only", "no: TDM reserved")
    assert suggest(radio, results["radio"])[0].startswith("do not enable: robots")
    assert suggest(news, None) == ("not checked", "not checked")


def test_merge_keeps_the_other_environment(web: FakeWeb, ingest_config: Config) -> None:
    results = review(web, ingest_config)
    data = merge_results({}, "github", results, T0)
    data = merge_results(data, "local", {"news": results["news"]}, T0)
    assert set(data["environments"]) == {"github", "local"}
    md = render_markdown(ingest_config, data)
    assert "| `news` News Example | yes | facts_only | ok (1) | ok (1) |" in md
    assert "robots disallowed" in md
    assert CANARY.lower() not in md.lower()


# --------------------------------------------------------------------------- CLI


runner = CliRunner()


def test_run_needs_once() -> None:
    assert runner.invoke(app, ["run"]).exit_code == 2


def test_run_once_cli(
    monkeypatch: pytest.MonkeyPatch, ingest_config_dir: Path, tmp_path: Path
) -> None:
    sources = ingest_config_dir / "sources.yaml"
    sources.write_text(
        sources.read_text(encoding="utf-8").replace("enabled: true", "enabled: false"),
        encoding="utf-8",
    )
    for k, v in {
        "CONFIG_DIR": str(ingest_config_dir),
        "STATE_STORE": "local_file",
        "STATE_PATH": str(tmp_path / "w" / "s.sqlite"),
        "STATUS_FILE": str(tmp_path / "status.json"),
        "BOT_CONTACT_URL": "https://digest.example/bot",
    }.items():
        monkeypatch.setenv(k, v)
    result = runner.invoke(app, ["run", "--once"])
    assert result.exit_code == 0, result.output
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["ok"] is True and status["counts"] == {"skipped": 1}
    assert not (tmp_path / "w").exists()  # no sources enabled: state untouched


def test_user_agent_needs_a_contact_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from digest.config import ConfigError, Settings

    s = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    with pytest.raises(ConfigError):
        _ = s.user_agent
    s = Settings(_env_file=None, site_url="https://x.pages.dev/")  # pyright: ignore[reportCallIssue]
    assert s.user_agent == "DigestBot/1.0 (+https://x.pages.dev/bot)"
    assert Settings(_env_file=None, product_name="Morgon Kollen").bot_name == "MorgonKollenBot"  # pyright: ignore[reportCallIssue]
