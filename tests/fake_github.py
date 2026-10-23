"""A small in-memory stand-in for the GitHub releases API, for state-store tests."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import httpx


@dataclass
class FakeGitHub:
    repo: str = "owner/content"
    releases: dict[str, int] = field(default_factory=dict)  # tag -> release id
    assets: dict[int, tuple[int, str, bytes]] = field(
        default_factory=dict
    )  # id -> (rel, name, data)
    created_payloads: list[dict[str, object]] = field(default_factory=list)
    fail_uploads: bool = False
    _next_id: int = 100

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-token"
        path, method = request.url.path, request.method
        base = f"/repos/{self.repo}"

        if m := re.fullmatch(rf"{base}/releases/tags/(.+)", path):
            rel = self.releases.get(m.group(1))
            return httpx.Response(200, json={"id": rel}) if rel else httpx.Response(404)

        if path == f"{base}/releases" and method == "POST":
            payload = json.loads(request.content)
            self.created_payloads.append(payload)
            rel = self._id()
            self.releases[payload["tag_name"]] = rel
            return httpx.Response(201, json={"id": rel})

        if m := re.fullmatch(rf"{base}/releases/(\d+)/assets", path):
            rel = int(m.group(1))
            if method == "POST":
                assert request.url.host == "uploads.github.com"
                if self.fail_uploads:
                    return httpx.Response(500)
                name = request.url.params["name"]
                if any(r == rel and n == name for r, n, _ in self.assets.values()):
                    return httpx.Response(422)
                aid = self._id()
                self.assets[aid] = (rel, name, request.content)
                return httpx.Response(201, json={"id": aid, "name": name})
            page = int(request.url.params.get("page", "1"))
            per_page = int(request.url.params.get("per_page", "30"))
            listing = [
                {"id": aid, "name": n, "state": "uploaded"}
                for aid, (r, n, _) in sorted(self.assets.items())
                if r == rel
            ]
            return httpx.Response(200, json=listing[(page - 1) * per_page : page * per_page])

        if m := re.fullmatch(rf"{base}/releases/assets/(\d+)", path):
            aid = int(m.group(1))
            if aid not in self.assets:
                return httpx.Response(404)
            if method == "DELETE":
                del self.assets[aid]
                return httpx.Response(204)
            assert request.headers["Accept"] == "application/octet-stream"
            return httpx.Response(200, content=self.assets[aid][2])

        return httpx.Response(599, text=f"unhandled {method} {path}")

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def names(self) -> list[str]:
        return sorted(n for _, n, _ in self.assets.values())
