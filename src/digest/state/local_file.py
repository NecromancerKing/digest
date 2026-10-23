"""``local_file`` backend: state files in a local folder (development and tests)."""

from __future__ import annotations

import shutil
from pathlib import Path

from digest.state.base import StateStoreError


class LocalBlobs:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise StateStoreError(f"invalid file name {name!r}")
        return self.root / name

    def list(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_file())

    def get(self, name: str, dest: Path) -> None:
        try:
            shutil.copyfile(self._path(name), dest)
        except FileNotFoundError as e:
            raise StateStoreError(f"no such file {name!r}") from e

    def put(self, src: Path, name: str) -> None:
        tmp = self._path(name).with_suffix(".part")
        shutil.copyfile(src, tmp)
        tmp.replace(self._path(name))

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)
