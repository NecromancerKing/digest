"""Database access: engine, tables and migrations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, create_engine, event

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"


def make_engine(db_path: Path) -> Engine:
    """SQLite engine for ``db_path`` with foreign keys enforced on every connection."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn: Any, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        cur.close()

    return engine


def alembic_config(db_path: Path) -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


def migrate(db_path: Path, revision: str = "head") -> None:
    """Upgrade the state file at ``db_path`` to ``revision``, creating it if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(db_path), revision)
