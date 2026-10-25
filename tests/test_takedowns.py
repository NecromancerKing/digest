from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine, select

from digest import db
from digest.config import Config, Settings, load_config
from digest.db.schema import clusters, stories, story_revisions
from digest.takedowns import TakedownError, apply, corrections, fetch, parse, run_takedowns
from digest.timeutil import iso

from .conftest import CONFIG_DIR

NOW = dt.datetime(2026, 10, 25, 9, 0, tzinfo=dt.UTC)
SOURCES = json.dumps(
    [{"source_id": "x", "name": "SVT", "url": "https://svt.example/1", "published_at": iso(NOW)}]
)
FILE = """
- story: https://digest-dev.pages.dev/story/2026-10-24/quake-hits-japan
  note: Taken down because it gave the wrong magnitude.
- story: /story/2026-10-24/svt-label
  note: "Taken down on request."
"""


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    path = tmp_path / "state.sqlite"
    db.migrate(path)
    e = db.make_engine(path)
    with e.begin() as conn:
        for sid, slug, kind in ((1, "quake-hits-japan", "llm"), (2, "svt-label", "links")):
            conn.execute(
                clusters.insert().values(
                    id=sid, created_at=iso(NOW), last_item_at=iso(NOW), source_count=1
                )
            )
            conn.execute(
                stories.insert().values(
                    id=sid,
                    cluster_id=sid,
                    edition_lang="en",
                    slug=slug,
                    path=f"stories/2026-10-24/{slug}.yaml",
                    category="world",
                    status="approved",
                    current_rev=1,
                    updated_at=iso(NOW),
                )
            )
            conn.execute(
                story_revisions.insert().values(
                    story_id=sid,
                    rev_no=1,
                    headline="Quake hits Japan" if kind == "llm" else "SVT: a source headline",
                    claims="[]",
                    sources=SOURCES,
                    verification="{}",
                    created_by="pipeline",
                    kind=kind,
                    created_at=iso(NOW),
                )
            )
    return e


def statuses(engine: Engine) -> dict[int, str]:
    with engine.connect() as conn:
        return dict(conn.execute(select(stories.c.id, stories.c.status)).all())  # pyright: ignore[reportUnknownArgumentType]


def test_parse_reads_addresses_and_skips_bad_entries() -> None:
    entries, bad = parse(FILE + "- story: nowhere\n  note: x\n- note: no story\n- just text\n")
    assert [(e.day, e.slug) for e in entries] == [
        ("2026-10-24", "quake-hits-japan"),
        ("2026-10-24", "svt-label"),
    ]
    assert bad == 3
    assert parse(None) == ([], 0) and parse("# nothing yet\n") == ([], 0)
    with pytest.raises(TakedownError):
        parse("story: not a list")


def test_takedown_unpublishes_then_restores(engine: Engine) -> None:
    entries, _ = parse(FILE + "- story: /story/2026-10-24/gone\n  note: Never existed.\n")
    assert apply(engine, entries, "en", NOW) == {"down": 2, "restored": 0, "unmatched": 1}
    assert statuses(engine) == {1: "unpublished", 2: "unpublished"}
    later = NOW + dt.timedelta(hours=1)
    assert apply(engine, entries, "en", later)["down"] == 0  # idempotent
    fixes = corrections(engine)
    assert [c.headline for c in fixes] == ["Quake hits Japan", None]  # never a source headline
    assert fixes[0].note == "Taken down because it gave the wrong magnitude."
    assert all(c.at == NOW for c in fixes)  # the first time it was applied
    assert apply(engine, entries[:1], "en", later) == {"down": 0, "restored": 1, "unmatched": 0}
    assert statuses(engine) == {1: "unpublished", 2: "approved"}
    assert len(corrections(engine)) == 1


def test_unpublished_story_is_frozen(engine: Engine) -> None:
    from digest.stories import find_story, is_frozen

    apply(engine, parse(FILE)[0], "en", NOW)
    with engine.connect() as conn:
        story = find_story(conn, 1)
    assert story is not None and is_frozen(story)


def test_fetch_from_the_content_repository() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/me/content/contents/takedowns.yaml"
        assert req.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, text=FILE)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert fetch("me/content", "tok", client) == FILE
    none = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(404)))
    assert fetch("me/content", "tok", none) is None
    broken = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
    with pytest.raises(TakedownError):
        fetch("me/content", "tok", broken)


def test_unreadable_file_changes_nothing(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    import digest.takedowns as mod

    assert run_takedowns(engine, None, None, "en", NOW) == {"skipped": "not_configured"}
    monkeypatch.setattr(mod, "fetch", lambda _repo, _tok: "{ not: [valid")  # pyright: ignore[reportUnknownLambdaType]
    assert run_takedowns(engine, "me/content", "tok", "en", NOW) == {"failed": 1}
    assert statuses(engine) == {1: "approved", 2: "approved"}


def test_takedown_deploys_at_once_and_is_listed(engine: Engine, tmp_path: Path) -> None:
    from digest.publish.deploy import publish

    from .test_publish import FakeDeployer

    config: Config = load_config(CONFIG_DIR)
    settings = Settings(min_minutes_between_deploys=30)
    d = FakeDeployer()
    out = tmp_path / "site"
    assert publish(engine, settings, config, NOW, deployer=d, out=out)["deployed"] == 1
    soon = NOW + dt.timedelta(minutes=5)
    assert publish(engine, settings, config, soon, deployer=d, out=out) == {"skipped": "too_soon"}
    apply(engine, parse(FILE)[0][:1], "en", soon)
    assert publish(engine, settings, config, soon, deployer=d, out=out)["deployed"] == 1
    assert not (out / "story/2026-10-24/quake-hits-japan.html").exists()
    page = (out / "corrections.html").read_text(encoding="utf-8")
    assert "Quake hits Japan" in page and "wrong magnitude" in page
