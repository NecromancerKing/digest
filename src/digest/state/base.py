"""``StateStore``: where the SQLite state file lives between runs (spec section 7).

The logic (pick the newest good file, fall back on damage, prune only after a successful
upload, snapshot retention) lives here once; backends only move named blobs.
"""

from __future__ import annotations

import datetime as dt
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from digest.state import files

log = logging.getLogger(__name__)


class StateStoreError(Exception):
    """The backend could not list, read, write or delete a file."""


class Blobs(Protocol):
    """A flat namespace of named files: a local folder, or the assets of one release."""

    def list(self) -> list[str]: ...
    def get(self, name: str, dest: Path) -> None: ...
    def put(self, src: Path, name: str) -> None: ...
    def delete(self, name: str) -> None: ...


@dataclass(frozen=True)
class PullResult:
    """What ``pull`` found. ``source`` is None when no usable state existed (fresh start)."""

    source: str | None
    rejected: list[str] = field(default_factory=lambda: [])

    @property
    def fell_back(self) -> bool:
        """True when a newer file was damaged; the owner should be alerted."""
        return bool(self.rejected)


class StateStore:
    def __init__(
        self,
        blobs: Blobs,
        *,
        keep_state_files: int = 3,
        keep_daily_snapshots: int = 14,
        keep_weekly_snapshots: int = 8,
    ) -> None:
        self.blobs = blobs
        self.keep_state_files = keep_state_files
        self.keep_daily_snapshots = keep_daily_snapshots
        self.keep_weekly_snapshots = keep_weekly_snapshots

    def pull(self, dest: Path) -> PullResult:
        """Download the newest state file that passes ``PRAGMA integrity_check`` to ``dest``.

        Damaged files are skipped (newest first) and reported in ``rejected``. With no usable
        file, ``dest`` is left absent and ``source`` is None: the caller starts an empty state.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        rejected: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            gz, raw = Path(tmp) / "state.gz", Path(tmp) / "state.sqlite"
            for name in files.state_names_newest_first(self.blobs.list()):
                self.blobs.get(name, gz)
                try:
                    files.gunzip_file(gz, raw)
                except (OSError, EOFError):
                    ok = False
                else:
                    ok = files.integrity_ok(raw)
                if not ok:
                    log.warning("state_file_rejected", extra={"file": name})
                    rejected.append(name)
                    continue
                dest.unlink(missing_ok=True)
                raw.replace(dest)
                return PullResult(source=name, rejected=rejected)
        dest.unlink(missing_ok=True)
        return PullResult(source=None, rejected=rejected)

    def push(self, src: Path, now: dt.datetime | None = None) -> str:
        """Upload a checkpoint of ``src``; delete older state files only after it succeeds."""
        name = files.state_name(now or dt.datetime.now(dt.UTC))
        with tempfile.TemporaryDirectory() as tmp:
            copy, gz = Path(tmp) / "copy.sqlite", Path(tmp) / name
            files.consistent_copy(src, copy)
            files.gzip_file(copy, gz)
            self.blobs.put(gz, name)
        names = self.blobs.list()
        if name not in names:
            raise StateStoreError(f"uploaded {name} but it is not listed")
        for old in files.states_to_delete(names, self.keep_state_files):
            self.blobs.delete(old)
        return name

    def push_snapshot(self, src: Path, day: dt.date) -> str:
        """Upload a daily backup with full text removed, then apply snapshot retention."""
        name = files.snapshot_name(day)
        with tempfile.TemporaryDirectory() as tmp:
            snap, gz = Path(tmp) / "snapshot.sqlite", Path(tmp) / name
            files.make_snapshot(src, snap)
            files.gzip_file(snap, gz)
            names = self.blobs.list()
            if name in names:  # re-run on the same day: replace it
                self.blobs.delete(name)
            self.blobs.put(gz, name)
        names = self.blobs.list()
        for old in files.snapshots_to_delete(
            names, self.keep_daily_snapshots, self.keep_weekly_snapshots
        ):
            self.blobs.delete(old)
        return name

    def snapshots(self) -> list[str]:
        """Snapshot names, newest first."""
        return sorted((n for n in self.blobs.list() if files.snapshot_day(n)), reverse=True)

    def pull_snapshot(self, name: str, dest: Path) -> bool:
        """Download and unpack one snapshot; True if it passes the integrity check."""
        with tempfile.TemporaryDirectory() as tmp:
            gz = Path(tmp) / name
            self.blobs.get(name, gz)
            files.gunzip_file(gz, dest)
        return files.integrity_ok(dest)
