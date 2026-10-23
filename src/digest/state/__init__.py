"""State storage between runs: ``local_file`` (development, tests) or ``github_release``."""

from __future__ import annotations

from pathlib import Path

from digest.config import AppSettings, Settings
from digest.state.base import Blobs, PullResult, StateStore, StateStoreError
from digest.state.github_release import GitHubReleaseBlobs
from digest.state.local_file import LocalBlobs

__all__ = ["Blobs", "PullResult", "StateStore", "StateStoreError", "make_state_store"]


def make_state_store(
    settings: Settings, app: AppSettings, local_root: Path | None = None
) -> StateStore:
    """The store selected by ``STATE_STORE``."""
    blobs: Blobs
    if settings.state_store == "github_release":
        if not settings.content_repo or settings.content_repo_token is None:
            raise StateStoreError("github_release needs CONTENT_REPO and CONTENT_REPO_TOKEN")
        blobs = GitHubReleaseBlobs(
            settings.content_repo,
            settings.content_repo_token.get_secret_value(),
            settings.state_release_tag,
        )
    else:
        blobs = LocalBlobs(local_root or settings.state_path.parent / "store")
    return StateStore(
        blobs,
        keep_state_files=app.state.keep_state_files,
        keep_daily_snapshots=app.state.keep_daily_snapshots,
        keep_weekly_snapshots=app.state.keep_weekly_snapshots,
    )
