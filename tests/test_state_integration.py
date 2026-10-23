"""Round trip through a real release in the content repository (Phase 0 acceptance).

Runs only when DIGEST_IT_CONTENT_REPO and DIGEST_IT_TOKEN are set. It uses a throwaway
release tag, never the production ``pipeline-state`` release, and deletes it afterwards.
"""

from __future__ import annotations

import datetime as dt
import os
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import insert

from digest import db
from digest.db.schema import kv
from digest.state import StateStore
from digest.state.github_release import GitHubReleaseBlobs

REPO = os.environ.get("DIGEST_IT_CONTENT_REPO")
TOKEN = os.environ.get("DIGEST_IT_TOKEN")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (REPO and TOKEN), reason="needs DIGEST_IT_CONTENT_REPO and DIGEST_IT_TOKEN"
    ),
]


def test_state_round_trips_through_a_real_release(tmp_path: Path) -> None:
    assert REPO and TOKEN
    tag = f"pipeline-state-test-{secrets.token_hex(4)}"
    blobs = GitHubReleaseBlobs(REPO, TOKEN, tag)
    store = StateStore(blobs, keep_state_files=2)
    try:
        src = tmp_path / "state.sqlite"
        db.migrate(src)
        t0 = dt.datetime.now(dt.UTC)
        for i in range(3):
            with db.make_engine(src).begin() as conn:
                conn.execute(kv.delete())
                conn.execute(insert(kv).values(key="marker", value=f"v{i}"))
            store.push(src, now=t0 + dt.timedelta(seconds=i))
        assert len(blobs.list()) == 2

        fresh = StateStore(GitHubReleaseBlobs(REPO, TOKEN, tag))
        dest = tmp_path / "pulled.sqlite"
        result = fresh.pull(dest)
        assert result.source is not None and not result.fell_back
        with closing(sqlite3.connect(dest)) as conn:
            assert conn.execute("SELECT value FROM kv").fetchone() == ("v2",)
    finally:
        blobs.delete_release()
