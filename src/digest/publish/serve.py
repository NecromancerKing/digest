"""A local preview server that resolves URLs the way Cloudflare Pages does.

``/story/2026-10-24/storm`` serves ``story/2026-10-24/storm.html``, and unknown paths get
``404.html``. Development only; it listens on 127.0.0.1.
"""

from __future__ import annotations

import functools
import http.server
from pathlib import Path


class _Handler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        local = Path(super().translate_path(path))
        if not local.exists() and local.with_suffix(".html").is_file():
            return str(local.with_suffix(".html"))
        return str(local)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        page = Path(self.directory) / "404.html"
        if code != 404 or not page.is_file():
            super().send_error(code, message, explain)
            return
        body = page.read_bytes()
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(root: Path, port: int) -> None:
    handler = functools.partial(_Handler, directory=str(root))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        httpd.serve_forever()
