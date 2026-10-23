from __future__ import annotations

import shutil
from pathlib import Path

import pytest

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
