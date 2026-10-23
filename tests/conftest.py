from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from digest.config import Config, Settings, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Tests never see the developer's .env or real tokens, and run outside the repo."""
    import os

    for key in list(os.environ):
        if key.startswith(("CONTENT_REPO", "STATE_", "SUMMARY_", "GROQ", "GEMINI", "CLOUDFLARE")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A writable copy of the shipped config/ directory."""
    dest = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, dest)
    return dest


TEST_SOURCES = """
- id: news
  name: News Example
  homepage: https://news.example/
  feeds:
    - {url: "https://news.example/feed.xml", kind: rss}
  lang: en
  country: GB
  category_hints: [world]
  usage_policy: facts_only
  fetch_article_pages: true
  terms_checked_on: 2026-10-01
  enabled: true
- id: radio
  name: Radio Example
  homepage: https://radio.example/
  feeds:
    - {url: "https://radio.example/rss", kind: rss}
  lang: sv
  country: SE
  usage_policy: link_only
  terms_checked_on: 2026-10-01
  enabled: true
- id: dormant
  name: Disabled Example
  homepage: https://dormant.example/
  feeds:
    - {url: "https://dormant.example/rss", kind: rss}
  lang: en
  country: GB
  usage_policy: facts_only
  enabled: false
"""


@pytest.fixture
def ingest_config_dir(config_dir: Path) -> Path:
    """Shipped config plus three test sources on ``.example`` hosts."""
    (config_dir / "sources.yaml").write_text(TEST_SOURCES, encoding="utf-8")
    return config_dir


@pytest.fixture
def ingest_config(ingest_config_dir: Path) -> Config:
    return load_config(ingest_config_dir)


@pytest.fixture
def settings(tmp_path: Path, ingest_config_dir: Path) -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        config_dir=ingest_config_dir,
        state_store="local_file",
        state_path=tmp_path / "work" / "digest.sqlite",
        status_file=tmp_path / "status" / "last_run.json",
        bot_contact_url="https://digest.example/bot",
    )
