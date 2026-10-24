from __future__ import annotations

import datetime as dt
import io
import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from digest.config import Config, Settings
from digest.logging import PublicJsonFormatter, setup_logging
from digest.runner import TimeBudget, run_once
from digest.state import StateStore, StateStoreError
from digest.state.local_file import LocalBlobs
from tests.fake_embed import FakeEmbedder
from tests.fake_web import FakeWeb, item, rss

T0 = dt.datetime(2026, 10, 23, 9, 0, tzinfo=dt.UTC)  # 11:00 in Stockholm
DATE = "Fri, 23 Oct 2026 08:00:00 GMT"
CANARIES = [
    "CANARYTITLE7f3a",
    "CANARYDESC91bc",
    "canaryslug55de",
    "CANARYBODY0e21",
    "CANARYRADIO4410",
]


@pytest.fixture(autouse=True)
def _no_real_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("digest.ingest.fetcher.time.sleep", lambda _s: None)


@pytest.fixture
def canary_web() -> FakeWeb:
    w = FakeWeb()
    for host in ("news.example", "radio.example"):
        w.route(f"https://{host}/robots.txt", httpx.Response(404))
    w.route("https://news.example/.well-known/tdmrep.json", httpx.Response(404))
    w.route(
        "https://news.example/feed.xml",
        httpx.Response(
            200,
            content=rss(
                item(
                    f"Minister resigns {CANARIES[0]}",
                    f"https://news.example/{CANARIES[2]}",
                    desc=f"Details {CANARIES[1]}",
                    date=DATE,
                )
            ),
        ),
    )
    w.route(
        f"https://news.example/{CANARIES[2]}",
        httpx.Response(
            200,
            content=(
                b"<html><body><article><p>"
                + f"Body {CANARIES[3]} of the page. ".encode() * 30
                + b"</p></article></body></html>"
            ),
            headers={"content-type": "text/html"},
        ),
    )
    w.route(
        "https://radio.example/rss",
        httpx.Response(
            200,
            content=rss(item(f"Radio {CANARIES[4]}", "https://radio.example/r1", date=DATE)),
        ),
    )
    return w


@pytest.fixture
def captured() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    setup_logging(logging.DEBUG, stream=buf)
    yield buf
    logging.getLogger().handlers.clear()


def store_for(settings: Settings) -> StateStore:
    return StateStore(LocalBlobs(settings.state_path.parent / "store"))


def run(settings: Settings, config: Config, web: FakeWeb, now: dt.datetime = T0, **kw: object):
    return run_once(
        settings,
        config,
        store=store_for(settings),
        http_client=web.client(),
        now=lambda: now,
        embedder=FakeEmbedder(),
        **kw,  # pyright: ignore[reportArgumentType]
    )


def items_in(path: Path) -> list[tuple[str, str | None, str | None]]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(
            "SELECT title, feed_summary, full_text FROM items ORDER BY id"
        ).fetchall()


# --------------------------------------------------------------------------- end to end


def test_run_once_ingests_and_checkpoints(
    settings: Settings, ingest_config: Config, canary_web: FakeWeb
) -> None:
    result = run(settings, ingest_config, canary_web)
    assert result.ok, result.error_type
    assert result.stats["ingest"]["items_new"] == 2
    assert result.stats["ingest"]["articles_stored"] == 1
    assert result.stats["fetch"]["blocked_blocklist"] == 0
    assert result.stats["checkpoints"] == 3  # after ingest, after grouping, at the end
    names = store_for(settings).blobs.list()
    assert len([n for n in names if n.startswith("state-")]) == 3
    assert "snapshot-2026-10-23.sqlite.gz" in names  # first run after 03:00

    with closing(sqlite3.connect(settings.state_path)) as conn:
        ok, stats = conn.execute("SELECT ok, stats FROM runs").fetchone()
    assert ok == 1 and json.loads(stats)["ingest"]["items_new"] == 2


def test_state_carries_over_between_runs(
    settings: Settings, ingest_config: Config, canary_web: FakeWeb
) -> None:
    run(settings, ingest_config, canary_web)
    settings.state_path.unlink()  # a new runner starts with nothing on disk
    result = run(settings, ingest_config, canary_web, now=T0 + dt.timedelta(minutes=30))
    assert result.ok
    assert result.stats["state"]["fresh"] is False
    assert result.stats["ingest"]["items_new"] == 0 and result.stats["ingest"]["duplicates"] == 2
    with closing(sqlite3.connect(settings.state_path)) as conn:
        assert conn.execute("SELECT count(*) FROM runs").fetchone() == (2,)


def test_no_third_party_text_leaves_the_state(
    settings: Settings, ingest_config: Config, canary_web: FakeWeb, captured: io.StringIO
) -> None:
    result = run(settings, ingest_config, canary_web)
    assert result.ok
    # The canaries really were ingested ...
    stored = json.dumps(items_in(settings.state_path))
    assert all(c in stored for c in CANARIES if c != CANARIES[2])
    # ... but never reach the log, the run stats or the status file.
    public = [
        captured.getvalue(),
        json.dumps(result.stats),
        settings.status_file.read_text(encoding="utf-8"),
    ]
    for text in public:
        for c in CANARIES:
            assert c.lower() not in text.lower(), c
    assert "feed_polled" in captured.getvalue()


def test_failed_run_uploads_nothing_more(
    settings: Settings,
    ingest_config: Config,
    canary_web: FakeWeb,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run(settings, ingest_config, canary_web)
    before = store_for(settings).blobs.list()

    def broken(*_: object, **__: object) -> None:
        raise RuntimeError(f"boom {CANARIES[0]}")

    monkeypatch.setattr("digest.runner.run_ingest", broken)
    result = run(settings, ingest_config, canary_web, now=T0 + dt.timedelta(minutes=30))
    assert not result.ok and result.error_type == "RuntimeError"
    assert store_for(settings).blobs.list() == before


def test_github_store_is_never_written_from_a_local_run(
    settings: Settings, ingest_config: Config, canary_web: FakeWeb
) -> None:
    class ReadOnly(LocalBlobs):
        def put(self, src: Path, name: str) -> None:
            raise StateStoreError("must not upload")

    local = settings.model_copy(update={"state_store": "github_release", "state_upload": False})
    store = StateStore(ReadOnly(settings.state_path.parent / "store"))
    result = run_once(
        local,
        ingest_config,
        store=store,
        http_client=canary_web.client(),
        now=lambda: T0,
        embedder=FakeEmbedder(),
    )
    assert result.ok, result.error_type
    assert "checkpoints" not in result.stats


def test_budget_spent_means_no_requests(
    settings: Settings, ingest_config: Config, canary_web: FakeWeb
) -> None:
    clock = iter([0.0] + [11 * 60.0] * 100)
    result = run(
        settings, ingest_config, canary_web, budget=TimeBudget(11, monotonic=lambda: next(clock))
    )
    assert result.ok
    assert result.stats["ingest"]["feeds_skipped_for_budget"] == 2
    assert canary_web.requests == []


def test_daily_snapshot_after_three(
    settings: Settings, ingest_config: Config, canary_web: FakeWeb
) -> None:
    night = dt.datetime(2026, 10, 23, 0, 30, tzinfo=dt.UTC)  # 02:30 Stockholm
    run(settings, ingest_config, canary_web, now=night)
    assert store_for(settings).snapshots() == []
    run(settings, ingest_config, canary_web, now=T0)
    run(settings, ingest_config, canary_web, now=T0 + dt.timedelta(minutes=30))
    assert store_for(settings).snapshots() == ["snapshot-2026-10-23.sqlite.gz"]


def test_status_file_is_rewritten_once_a_day(
    settings: Settings, ingest_config: Config, canary_web: FakeWeb
) -> None:
    run(settings, ingest_config, canary_web)
    first = settings.status_file.read_text(encoding="utf-8")
    run(settings, ingest_config, canary_web, now=T0 + dt.timedelta(minutes=30))
    assert settings.status_file.read_text(encoding="utf-8") == first
    run(settings, ingest_config, canary_web, now=T0 + dt.timedelta(days=1))
    data = json.loads(settings.status_file.read_text(encoding="utf-8"))
    assert data["day"] == "2026-10-24" and data["ok"] is True
    assert all(isinstance(v, int | float) for v in data["counts"].values())


# --------------------------------------------------------------------------- log filter


def _format(
    msg: str,
    args: tuple[object, ...] = (),
    name: str = "digest.x",
    extra: dict[str, object] | None = None,
    **fields: object,
) -> dict[str, object]:
    record = logging.LogRecord(name, logging.INFO, "f.py", 1, msg, args, None)
    record.__dict__.update(extra or {}, **fields)
    return json.loads(PublicJsonFormatter().format(record))


def test_filter_keeps_events_counts_and_safe_ids() -> None:
    out = _format("feed_polled", source_id="svt", entries=3, ok=True, ratio=0.5, ids=[1, 2])
    assert out["event"] == "feed_polled"
    assert (out["source_id"], out["entries"], out["ok"], out["ratio"], out["ids"]) == (
        "svt",
        3,
        True,
        0.5,
        [1, 2],
    )


@pytest.mark.parametrize(
    ("msg", "args", "extra"),
    [
        ("got %s", ("CANARY",), {}),
        ("Free text with CANARY", (), {}),
        ("feed_polled", (), {"title": "CANARY"}),
        ("feed_polled", (), {"url": "https://news.example/CANARY"}),
        ("feed_polled", (), {"titles": ["CANARY"]}),
        ("feed_polled", (), {"counts": {"CANARY text": 1}}),
    ],
)
def test_filter_redacts_text(msg: str, args: tuple[object, ...], extra: dict[str, object]) -> None:
    assert "CANARY" not in json.dumps(_format(msg, args, extra=extra))


def test_filter_hides_library_messages_and_exception_text() -> None:
    assert "CANARY" not in json.dumps(_format("GET https://x/CANARY", name="httpx"))
    try:
        raise ValueError("CANARY in message")
    except ValueError:
        import sys

        record = logging.LogRecord("digest", logging.ERROR, "f", 1, "failed", (), sys.exc_info())
    out = json.loads(PublicJsonFormatter().format(record))
    assert out["error_type"] == "ValueError" and "CANARY" not in json.dumps(out)
