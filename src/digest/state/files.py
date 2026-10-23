"""State and snapshot file names, compression, integrity checks and retention rules."""

from __future__ import annotations

import datetime as dt
import gzip
import re
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

STATE_RE = re.compile(r"^state-(\d{8}T\d{9}Z)\.sqlite\.gz$")
SNAPSHOT_RE = re.compile(r"^snapshot-(\d{4}-\d{2}-\d{2})\.sqlite\.gz$")


def state_name(now: dt.datetime) -> str:
    """``state-20261023T220501123Z.sqlite.gz``: UTC, millisecond precision, sorts by time."""
    u = now.astimezone(dt.UTC)
    return f"state-{u:%Y%m%dT%H%M%S}{u.microsecond // 1000:03d}Z.sqlite.gz"


def snapshot_name(day: dt.date) -> str:
    return f"snapshot-{day.isoformat()}.sqlite.gz"


def state_names_newest_first(names: list[str]) -> list[str]:
    return sorted((n for n in names if STATE_RE.match(n)), reverse=True)


def snapshot_day(name: str) -> dt.date | None:
    m = SNAPSHOT_RE.match(name)
    return dt.date.fromisoformat(m.group(1)) if m else None


def states_to_delete(names: list[str], keep: int) -> list[str]:
    """State files beyond the newest ``keep``."""
    return state_names_newest_first(names)[keep:]


def snapshots_to_delete(names: list[str], keep_daily: int, keep_weekly: int) -> list[str]:
    """Snapshots outside the retention set.

    Kept: the newest ``keep_daily`` snapshots, plus the newest snapshot of each of the
    ``keep_weekly`` most recent ISO weeks that are not already covered by the daily set.
    """
    dated = sorted(((d, n) for n in names if (d := snapshot_day(n)) is not None), reverse=True)
    keep = {n for _, n in dated[:keep_daily]}
    covered_weeks = {d.isocalendar()[:2] for d, _ in dated[:keep_daily]}
    weekly: dict[tuple[int, int], str] = {}
    for d, n in dated[keep_daily:]:
        week = d.isocalendar()[:2]
        if week not in covered_weeks and week not in weekly:
            weekly[week] = n
    keep.update(list(weekly.values())[:keep_weekly])
    return [n for _, n in dated if n not in keep]


def gzip_file(src: Path, dest: Path) -> None:
    with src.open("rb") as f_in, gzip.open(dest, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out)


def gunzip_file(src: Path, dest: Path) -> None:
    with gzip.open(src, "rb") as f_in, dest.open("wb") as f_out:
        shutil.copyfileobj(f_in, f_out)


def integrity_ok(db_path: Path) -> bool:
    """``PRAGMA integrity_check`` on a SQLite file; False for anything that is not one."""
    try:
        with closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)) as conn:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError:
        return False
    return rows == [("ok",)]


def consistent_copy(src: Path, dest: Path) -> None:
    """Copy a live SQLite file safely (online backup API), so a checkpoint is never torn."""
    dest.unlink(missing_ok=True)
    with (
        closing(sqlite3.connect(src)) as s,
        closing(sqlite3.connect(dest)) as d,
    ):
        s.backup(d)


def make_snapshot(src: Path, dest: Path) -> None:
    """Backup copy of the state with all full article text removed (rule L5)."""
    consistent_copy(src, dest)
    with closing(sqlite3.connect(dest)) as conn:
        has_items = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'items'"
        ).fetchone()
        if has_items:
            conn.execute("UPDATE items SET full_text = NULL, full_text_purge_at = NULL")
            conn.commit()
        conn.execute("VACUUM")  # drop the freed pages, so no text survives in the file
