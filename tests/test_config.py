from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from digest.config import ConfigError, Settings, load_config
from tests.conftest import CONFIG_DIR


def _source(**overrides: Any) -> dict[str, Any]:
    src: dict[str, Any] = {
        "id": "example-news",
        "name": "Example News",
        "homepage": "https://news.example.org/",
        "feeds": [{"url": "https://news.example.org/feed.xml", "kind": "rss"}],
        "lang": "en",
        "country": "GB",
        "category_hints": ["world"],
        "usage_policy": "facts_only",
    }
    src.update(overrides)
    return src


def _write_sources(config_dir: Path, sources: list[dict[str, Any]]) -> None:
    (config_dir / "sources.yaml").write_text(yaml.safe_dump(sources), encoding="utf-8")


def test_shipped_config_is_valid() -> None:
    config = load_config(CONFIG_DIR)
    assert {c.id for c in config.categories} == {
        "world",
        "economy",
        "tech_science",
        "climate",
        "sport",
    }
    assert all(s.terms_checked_on for s in config.enabled_sources)  # reviewed before enabling
    assert 0 < config.settings.cluster.join_threshold < 1  # tuned on labels


def test_blocklist_matches_subdomains_only() -> None:
    bl = load_config(CONFIG_DIR).blocklist
    assert bl.is_blocked("dn.se")
    assert bl.is_blocked("www.DN.se.")
    assert not bl.is_blocked("notdn.se")
    assert not bl.is_blocked("svt.se")


def test_valid_source_loads(config_dir: Path) -> None:
    _write_sources(config_dir, [_source()])
    assert load_config(config_dir).source("example-news").usage_policy == "facts_only"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"feeds": [{"url": "https://www.dn.se/rss", "kind": "rss"}]}, "blocklisted"),
        ({"homepage": "https://cdn.svd.se/"}, "blocklisted"),
        ({"category_hints": ["gossip"]}, "unknown category"),
        ({"enabled": True}, "terms_checked_on"),
        ({"usage_policy": "link_only", "fetch_article_pages": True}, "link_only"),
        ({"template": "smhi_warning"}, "data_api"),
        ({"usage_policy": "public_domain"}, "usage_policy"),
        ({"surprise": 1}, "surprise"),
    ],
)
def test_invalid_sources_are_rejected(
    config_dir: Path, overrides: dict[str, Any], message: str
) -> None:
    _write_sources(config_dir, [_source(**overrides)])
    with pytest.raises(ConfigError, match=message):
        load_config(config_dir)


def test_duplicate_source_ids_are_rejected(config_dir: Path) -> None:
    other_feed = {"feeds": [{"url": "https://news.example.org/other.xml", "kind": "rss"}]}
    _write_sources(config_dir, [_source(), _source(**other_feed)])
    with pytest.raises(ConfigError, match="duplicate source ids"):
        load_config(config_dir)


def test_enabled_source_with_review_date(config_dir: Path) -> None:
    _write_sources(config_dir, [_source(enabled=True, terms_checked_on="2026-10-23")])
    assert [s.id for s in load_config(config_dir).enabled_sources] == ["example-news"]


def test_missing_and_malformed_files(config_dir: Path) -> None:
    (config_dir / "settings.yaml").write_text("fetch: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(config_dir)
    (config_dir / "settings.yaml").unlink()
    with pytest.raises(ConfigError, match="missing config file"):
        load_config(config_dir)


def test_training_providers_can_be_excluded(config_dir: Path) -> None:
    path = config_dir / "llm_providers.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for p in data["providers"]:
        p["enabled"] = True
    data["allow_training_providers"] = True
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert "openrouter" in {p.id for p in load_config(config_dir).llm.eligible()}

    data["allow_training_providers"] = False
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    eligible = {p.id for p in load_config(config_dir).llm.eligible()}
    assert "openrouter" not in eligible  # trains_on_inputs: varies
    assert "cloudflare_workers_ai" not in eligible  # unknown
    assert {"groq", "gemini", "anthropic"} <= eligible


def test_anthropic_adapter_must_be_marked_paid(config_dir: Path) -> None:
    path = config_dir / "llm_providers.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for p in data["providers"]:
        if p["id"] == "anthropic":
            p["paid"] = False
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="paid"):
        load_config(config_dir)


def test_settings_defaults() -> None:
    s = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert s.product_name == "Digest"
    assert s.llm_daily_budget_usd == 0
    assert s.fulltext_retention_hours == 72
    assert s.state_store == "github_release"
    assert s.zone.key == "Europe/Stockholm"


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUMMARY_MODE", "links")
    monkeypatch.setenv("CONTENT_REPO", "owner/digest-content")
    monkeypatch.setenv("CONTENT_REPO_TOKEN", "not-a-real-token")
    monkeypatch.setenv("SITE_URL", "https://digest-dev.pages.dev/")
    s = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert s.summary_mode == "links"
    assert s.content_repo == "owner/digest-content"
    assert "not-a-real-token" not in repr(s)  # secrets never print
    assert s.contact_url == "https://digest-dev.pages.dev/bot"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("SUMMARY_WORDS_MIN", "80"),
        ("TZ", "Mars/Olympus_Mons"),
        ("CONTENT_REPO", "no-slash"),
        ("SUMMARY_MODE", "magic"),
        ("LLM_DAILY_BUDGET_USD", "-1"),
    ],
)
def test_invalid_settings(monkeypatch: pytest.MonkeyPatch, key: str, value: str) -> None:
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
