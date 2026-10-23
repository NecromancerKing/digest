"""``github_release`` backend: state files as assets of one release in the content repository.

The release (tag ``pipeline-state`` by default) exists only to hold files and is never
marked as the latest release. It is created on first use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import httpx

from digest.state.base import StateStoreError

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
RELEASE_BODY = (
    "Holds the pipeline's state and snapshot files. Managed by the pipeline; do not edit."
)


class GitHubReleaseBlobs:
    def __init__(
        self,
        repo: str,
        token: str,
        tag: str = "pipeline-state",
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.repo = repo
        self.tag = tag
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(30.0, read=120.0),
            follow_redirects=True,  # httpx drops Authorization on cross-origin redirects
            transport=httpx.HTTPTransport(retries=3),
        )
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "digest-pipeline",
        }
        self._release_id: int | None = None
        self._assets: dict[str, int] | None = None

    # -- HTTP helpers

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = {**self._headers, **kwargs.pop("headers", {})}
        try:
            resp = self._client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as e:
            raise StateStoreError(f"{method} {url}: {type(e).__name__}") from e
        return resp

    @staticmethod
    def _check(resp: httpx.Response, what: str) -> None:
        if resp.is_error:
            raise StateStoreError(f"{what}: HTTP {resp.status_code}", resp.status_code)

    # -- release and asset index

    def _release(self) -> int:
        if self._release_id is not None:
            return self._release_id
        resp = self._request("GET", f"{API}/repos/{self.repo}/releases/tags/{self.tag}")
        if resp.status_code == 404:
            resp = self._request(
                "POST",
                f"{API}/repos/{self.repo}/releases",
                json={
                    "tag_name": self.tag,
                    "name": self.tag,
                    "body": RELEASE_BODY,
                    "draft": False,
                    "prerelease": False,
                    "make_latest": "false",
                },
            )
            self._check(resp, f"create release {self.tag}")
        else:
            self._check(resp, f"get release {self.tag}")
        self._release_id = int(cast(dict[str, Any], resp.json())["id"])
        return self._release_id

    def _index(self, refresh: bool = False) -> dict[str, int]:
        if self._assets is not None and not refresh:
            return self._assets
        release = self._release()
        assets: dict[str, int] = {}
        page = 1
        while True:
            resp = self._request(
                "GET",
                f"{API}/repos/{self.repo}/releases/{release}/assets",
                params={"per_page": 100, "page": page},
            )
            self._check(resp, "list assets")
            batch = cast(list[dict[str, Any]], resp.json())
            for a in batch:
                if a.get("state", "uploaded") == "uploaded":
                    assets[str(a["name"])] = int(a["id"])
            if len(batch) < 100:
                break
            page += 1
        self._assets = assets
        return assets

    # -- Blobs

    def list(self) -> list[str]:
        return sorted(self._index(refresh=True))

    def get(self, name: str, dest: Path) -> None:
        asset_id = self._index().get(name)
        if asset_id is None:
            raise StateStoreError(f"no such asset {name!r}")
        url = f"{API}/repos/{self.repo}/releases/assets/{asset_id}"
        headers = {**self._headers, "Accept": "application/octet-stream"}
        try:
            with self._client.stream("GET", url, headers=headers) as resp:
                self._check(resp, f"download {name}")
                with dest.open("wb") as f:
                    for chunk in resp.iter_bytes():
                        f.write(chunk)
        except httpx.HTTPError as e:
            raise StateStoreError(f"download {name}: {type(e).__name__}") from e

    def put(self, src: Path, name: str) -> None:
        release = self._release()
        resp = self._request(
            "POST",
            f"{UPLOADS}/repos/{self.repo}/releases/{release}/assets",
            params={"name": name},
            headers={"Content-Type": "application/gzip"},
            content=src.read_bytes(),
        )
        self._check(resp, f"upload {name}")
        self._index()[name] = int(cast(dict[str, Any], resp.json())["id"])

    def delete(self, name: str) -> None:
        asset_id = self._index().get(name)
        if asset_id is None:
            return
        resp = self._request("DELETE", f"{API}/repos/{self.repo}/releases/assets/{asset_id}")
        if resp.status_code != 404:
            self._check(resp, f"delete {name}")
        self._index().pop(name, None)

    def delete_release(self) -> None:
        """Remove the release and its tag. Used by the integration test's cleanup only."""
        release = self._release()
        self._check(
            self._request("DELETE", f"{API}/repos/{self.repo}/releases/{release}"),
            "delete release",
        )
        resp = self._request("DELETE", f"{API}/repos/{self.repo}/git/refs/tags/{self.tag}")
        if resp.status_code not in (204, 404, 422):
            self._check(resp, "delete tag")
        self._release_id = None
        self._assets = None
