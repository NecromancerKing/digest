from __future__ import annotations

import datetime as dt
import gzip
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import insert

from digest import db
from digest.config import AppSettings, Settings
from digest.db.schema import items, kv
from digest.state import StateStore, StateStoreError, files, make_state_store
from digest.state.github_release import GitHubReleaseBlobs
from digest.state.local_file import LocalBlobs
from tests.fake_github import FakeGitHub

T0 = dt.datetime(2026, 10, 23, 9, 0, tzinfo=dt.UTC)
SECRET_TEXT = "CANARY full article text that must never reach a snapshot"


def _state_with(path: Path, value: str) -> Path:
    db.migrate(path)
    with db.make_engine(path).begin() as conn:
        conn.execute(kv.delete())
        conn.execute(insert(kv).values(key="marker", value=value))
    return path


def _marker(path: Path) -> str:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT value FROM kv WHERE key = 'marker'").fetchone()[0]


# --------------------------------------------------------------------------- pure helpers


def test_state_names_sort_by_time() -> None:
    a = files.state_name(T0)
    b = files.state_name(T0 + dt.timedelta(milliseconds=1))
    c = files.state_name(T0 + dt.timedelta(days=1))
    assert a == "state-20261023T090000000Z.sqlite.gz"
    assert files.state_names_newest_first([a, "other.txt", c, b]) == [c, b, a]


def test_state_retention_keeps_newest() -> None:
    names = [files.state_name(T0 + dt.timedelta(minutes=30 * i)) for i in range(5)]
    assert files.states_to_delete(names, 3) == sorted(names)[:2][::-1]


def test_snapshot_retention_keeps_14_daily_and_8_weekly() -> None:
    days = [dt.date(2026, 10, 23) - dt.timedelta(days=i) for i in range(120)]
    names = [files.snapshot_name(d) for d in days]
    doomed = set(files.snapshots_to_delete(names, keep_daily=14, keep_weekly=8))
    kept = sorted((d for d in days if files.snapshot_name(d) not in doomed), reverse=True)
    assert kept[:14] == days[:14]
    weekly = kept[14:]
    assert len(weekly) == 8
    assert len({d.isocalendar()[:2] for d in weekly}) == 8  # one per week
    assert all(d.isocalendar()[:2] not in {x.isocalendar()[:2] for x in days[:14]} for d in weekly)


def test_integrity_check_rejects_non_databases(tmp_path: Path) -> None:
    junk = tmp_path / "junk.sqlite"
    junk.write_bytes(b"not a database at all" * 100)
    assert not files.integrity_ok(junk)
    assert files.integrity_ok(_state_with(tmp_path / "good.sqlite", "x"))


def test_snapshot_contains_no_full_text(tmp_path: Path) -> None:
    src = _state_with(tmp_path / "state.sqlite", "x")
    with db.make_engine(src).begin() as conn:
        conn.execute(
            insert(items).values(
                source_id="s",
                url="https://news.example.org/a",
                url_hash=b"\x01" * 32,
                title="Synthetic",
                fetched_at="2026-10-23T09:00:00Z",
                usage_policy="facts_only",
                full_text=SECRET_TEXT,
                full_text_purge_at="2026-10-26T09:00:00Z",
            )
        )
    snap = tmp_path / "snap.sqlite"
    files.make_snapshot(src, snap)
    assert SECRET_TEXT.encode() not in snap.read_bytes()
    with closing(sqlite3.connect(snap)) as conn:
        assert conn.execute("SELECT full_text, title FROM items").fetchall() == [
            (None, "Synthetic")
        ]
    assert SECRET_TEXT.encode() in src.read_bytes()  # the live state is untouched


# --------------------------------------------------------------------------- store logic


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(LocalBlobs(tmp_path / "store"))


def test_pull_with_no_files_starts_fresh(store: StateStore, tmp_path: Path) -> None:
    dest = tmp_path / "work" / "state.sqlite"
    result = store.pull(dest)
    assert result.source is None and not result.fell_back
    assert not dest.exists()


def test_push_then_pull_round_trips(store: StateStore, tmp_path: Path) -> None:
    src = _state_with(tmp_path / "a.sqlite", "v1")
    name = store.push(src, now=T0)
    dest = tmp_path / "work" / "state.sqlite"
    result = store.pull(dest)
    assert result.source == name and not result.fell_back
    assert _marker(dest) == "v1"


def test_push_prunes_to_newest_three(store: StateStore, tmp_path: Path) -> None:
    src = _state_with(tmp_path / "a.sqlite", "v")
    pushed = [store.push(src, now=T0 + dt.timedelta(minutes=30 * i)) for i in range(5)]
    assert store.blobs.list() == sorted(pushed[-3:])


def test_pull_falls_back_when_newest_is_damaged(store: StateStore, tmp_path: Path) -> None:
    store.push(_state_with(tmp_path / "a.sqlite", "good"), now=T0)
    bad_gz = tmp_path / "bad.gz"
    with gzip.open(bad_gz, "wb") as f:
        f.write(b"SQLite format 3\x00" + b"\xff" * 4096)
    newer = files.state_name(T0 + dt.timedelta(minutes=30))
    store.blobs.put(bad_gz, newer)
    newest = files.state_name(T0 + dt.timedelta(minutes=60))
    (tmp_path / "notgzip").write_bytes(b"garbage")
    store.blobs.put(tmp_path / "notgzip", newest)

    dest = tmp_path / "work" / "state.sqlite"
    result = store.pull(dest)
    assert result.rejected == [newest, newer]
    assert result.fell_back
    assert result.source == files.state_name(T0)
    assert _marker(dest) == "good"


def test_failed_upload_keeps_previous_state(tmp_path: Path) -> None:
    class FlakyBlobs(LocalBlobs):
        fail = False

        def put(self, src: Path, name: str) -> None:
            if self.fail:
                raise StateStoreError("upload failed")
            super().put(src, name)

    blobs = FlakyBlobs(tmp_path / "store")
    store = StateStore(blobs, keep_state_files=2)
    src = _state_with(tmp_path / "a.sqlite", "v1")
    store.push(src, now=T0)
    store.push(src, now=T0 + dt.timedelta(minutes=30))
    before = blobs.list()

    blobs.fail = True
    _state_with(src, "v2")
    with pytest.raises(StateStoreError):
        store.push(src, now=T0 + dt.timedelta(minutes=60))
    assert blobs.list() == before
    dest = tmp_path / "work" / "state.sqlite"
    store.pull(dest)
    assert _marker(dest) == "v1"


def test_snapshot_push_replaces_same_day_and_applies_retention(
    store: StateStore, tmp_path: Path
) -> None:
    src = _state_with(tmp_path / "a.sqlite", "v")
    store.keep_daily_snapshots, store.keep_weekly_snapshots = 2, 0
    for i in range(4):
        store.push_snapshot(src, dt.date(2026, 10, 20 + i))
    store.push_snapshot(src, dt.date(2026, 10, 23))  # second run the same day
    assert store.snapshots() == ["snapshot-2026-10-23.sqlite.gz", "snapshot-2026-10-22.sqlite.gz"]
    restored = tmp_path / "restored.sqlite"
    assert store.pull_snapshot(store.snapshots()[0], restored)
    assert _marker(restored) == "v"


def test_local_blobs_reject_path_tricks(tmp_path: Path) -> None:
    blobs = LocalBlobs(tmp_path / "store")
    for bad in ("../escape", "a/b", "..\\x", ".hidden", ""):
        with pytest.raises(StateStoreError):
            blobs.delete(bad)


def test_factory_selects_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STATE_STORE", "local_file")
    local = make_state_store(Settings(_env_file=None), AppSettings(), tmp_path / "s")  # pyright: ignore[reportCallIssue]
    assert isinstance(local.blobs, LocalBlobs)

    monkeypatch.setenv("STATE_STORE", "github_release")
    with pytest.raises(StateStoreError, match="CONTENT_REPO"):
        make_state_store(Settings(_env_file=None), AppSettings())  # pyright: ignore[reportCallIssue]

    monkeypatch.setenv("CONTENT_REPO", "owner/content")
    monkeypatch.setenv("CONTENT_REPO_TOKEN", "test-token")
    gh = make_state_store(Settings(_env_file=None), AppSettings())  # pyright: ignore[reportCallIssue]
    assert isinstance(gh.blobs, GitHubReleaseBlobs)


# --------------------------------------------------------------------------- GitHub backend


@pytest.fixture
def fake() -> FakeGitHub:
    return FakeGitHub()


def _gh_store(fake: FakeGitHub, tag: str = "pipeline-state") -> StateStore:
    return StateStore(GitHubReleaseBlobs(fake.repo, "test-token", tag, client=fake.client()))


def test_github_release_round_trip(fake: FakeGitHub, tmp_path: Path) -> None:
    store = _gh_store(fake)
    assert store.pull(tmp_path / "none.sqlite").source is None
    assert fake.created_payloads == [
        {
            "tag_name": "pipeline-state",
            "name": "pipeline-state",
            "body": fake.created_payloads[0]["body"],
            "draft": False,
            "prerelease": False,
            "make_latest": "false",
        }
    ]

    src = _state_with(tmp_path / "a.sqlite", "v1")
    for i in range(4):
        store.push(src, now=T0 + dt.timedelta(minutes=30 * i))
    assert len(fake.names()) == 3

    # A new process (fresh client, release already exists) sees the same files.
    again = _gh_store(fake)
    dest = tmp_path / "work" / "state.sqlite"
    assert again.pull(dest).source == files.state_name(T0 + dt.timedelta(minutes=90))
    assert _marker(dest) == "v1"
    assert len(fake.created_payloads) == 1


def test_github_release_failed_upload_deletes_nothing(fake: FakeGitHub, tmp_path: Path) -> None:
    store = _gh_store(fake)
    src = _state_with(tmp_path / "a.sqlite", "v1")
    for i in range(3):
        store.push(src, now=T0 + dt.timedelta(minutes=30 * i))
    before = fake.names()
    fake.fail_uploads = True
    with pytest.raises(StateStoreError, match="HTTP 500"):
        store.push(src, now=T0 + dt.timedelta(hours=5))
    assert fake.names() == before


def test_github_release_lists_beyond_one_page(fake: FakeGitHub, tmp_path: Path) -> None:
    blobs = GitHubReleaseBlobs(fake.repo, "test-token", client=fake.client())
    f = tmp_path / "x"
    f.write_bytes(b"x")
    for i in range(130):
        blobs.put(f, f"file-{i:03d}")
    assert len(blobs.list()) == 130


def test_pull_never_renames_across_folders(
    store: StateStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On GitHub runners /tmp and the workspace are different filesystems."""
    src = _state_with(tmp_path / "src.sqlite", "v1")
    store.push(src, T0)
    dest = tmp_path / "work" / "digest.sqlite"
    real_replace = Path.replace

    def same_tree_only(self: Path, target: Path | str) -> Path:
        if Path(target).parent not in (self.parent, *self.parents):
            raise OSError(18, "Invalid cross-device link")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", same_tree_only)
    assert store.pull(dest).source is not None
    assert _marker(dest) == "v1"
