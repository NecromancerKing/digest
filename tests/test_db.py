from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, insert, inspect, select
from sqlalchemy.exc import IntegrityError

from digest import db
from digest.db.schema import cluster_items, clusters, feeds, items, metadata

NOW = "2026-10-23T09:00:00Z"


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    path = tmp_path / "state.sqlite"
    db.migrate(path)
    return db.make_engine(path)


def _item(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "source_id": "example",
        "url": "https://news.example.org/a",
        "url_hash": b"\x01" * 32,
        "title": "Synthetic title",
        "fetched_at": NOW,
        "usage_policy": "facts_only",
    }
    row.update(overrides)
    return row


def test_migrations_create_every_table(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names())
    assert tables == set(metadata.tables) | {"alembic_version"}


def test_migrations_match_the_table_definitions(engine: Engine) -> None:
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), metadata)
    assert diff == []


def test_migrate_is_idempotent_and_reversible(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    db.migrate(path)
    db.migrate(path)
    command.downgrade(db.alembic_config(path), "base")
    assert inspect(db.make_engine(path)).get_table_names() == ["alembic_version"]


def test_link_only_items_cannot_hold_embeddings_or_facts(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(insert(items).values(**_item(usage_policy="link_only")))
    for derived in ({"embedding": b"\x00" * 16}, {"facts": "[]"}):
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                insert(items).values(
                    **_item(usage_policy="link_only", url_hash=b"\x02" * 32, **derived)
                )
            )


def test_check_constraints_reject_unknown_values(engine: Engine) -> None:
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(insert(items).values(**_item(status="published")))
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(insert(feeds).values(url="https://x.example/f", source_id="x", kind="html"))


def test_foreign_keys_are_enforced(engine: Engine) -> None:
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(insert(cluster_items).values(cluster_id=999, item_id=999, added_at=NOW))


def test_guid_is_unique_per_source_but_may_be_null(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(insert(items).values(**_item(guid=None)))
        conn.execute(insert(items).values(**_item(guid=None, url_hash=b"\x03" * 32)))
        conn.execute(insert(items).values(**_item(guid="g1", url_hash=b"\x04" * 32)))
        conn.execute(
            insert(items).values(**_item(guid="g1", source_id="other", url_hash=b"\x05" * 32))
        )
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(insert(items).values(**_item(guid="g1", url_hash=b"\x06" * 32)))


def test_deleting_an_item_removes_its_cluster_links(engine: Engine) -> None:
    with engine.begin() as conn:
        item_id = conn.execute(insert(items).values(**_item())).lastrowid
        cluster_id = conn.execute(
            insert(clusters).values(created_at=NOW, last_item_at=NOW)
        ).lastrowid
        conn.execute(
            insert(cluster_items).values(cluster_id=cluster_id, item_id=item_id, added_at=NOW)
        )
        conn.execute(items.delete().where(items.c.id == item_id))
        assert conn.execute(select(cluster_items)).all() == []
