"""Alembic environment. Run through ``digest migrate`` (or ``uv run poe migrate``)."""

from __future__ import annotations

from alembic import context
from sqlalchemy import Connection

from digest.db import make_engine
from digest.db.schema import metadata

config = context.config


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=metadata,
        render_as_batch=True,  # SQLite needs batch mode for ALTER TABLE
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    from pathlib import Path

    url = config.get_main_option("sqlalchemy.url") or ""
    engine = make_engine(Path(url.removeprefix("sqlite:///")))
    with engine.connect() as connection:
        _run(connection)
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
