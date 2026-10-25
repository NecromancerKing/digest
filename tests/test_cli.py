from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from digest import db
from digest.cli import app
from digest.state import StateStore
from digest.state.local_file import LocalBlobs
from tests.conftest import CONFIG_DIR

runner = CliRunner()


@pytest.fixture(autouse=True)
def _config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONFIG_DIR", str(CONFIG_DIR))


def test_validate_reports_counts() -> None:
    result = runner.invoke(app, ["validate"])
    assert result.exit_code == 0, result.output
    assert "5 categories" in result.output


def test_validate_fails_on_bad_config(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (config_dir / "categories.yaml").write_text("- id: Bad Id\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    result = runner.invoke(app, ["validate"])
    assert result.exit_code == 1
    assert "config error" in result.output


def test_migrate_creates_state_file(tmp_path: Path) -> None:
    target = tmp_path / "s" / "state.sqlite"
    result = runner.invoke(app, ["migrate", "--path", str(target)])
    assert result.exit_code == 0, result.output
    assert target.exists()


def test_state_pull_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STATE_STORE", "local_file")
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "work" / "digest.sqlite"))
    assert runner.invoke(app, ["state", "pull"]).exit_code == 1  # nothing stored yet

    src = tmp_path / "src.sqlite"
    db.migrate(src)
    StateStore(LocalBlobs(tmp_path / "work" / "store")).push(src)
    result = runner.invoke(app, ["state", "pull"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "work" / "digest.sqlite").exists()
