"""URL canonicalisation and hashing for de-duplication (stage 1, step 4)."""

from __future__ import annotations

import fnmatch
import hashlib
from collections.abc import Iterable
from urllib.parse import unquote_plus, urlsplit, urlunsplit

DEFAULT_PORTS = {"http": 80, "https": 443}


def canonicalize(url: str, drop_params: Iterable[str] = ("utm_*", "fbclid", "gclid")) -> str:
    """Lowercase scheme and host, drop the fragment, default port and tracking parameters.

    Other query parameters are kept in their original order; the path is left as is.
    ``drop_params`` entries are matched case-insensitively and may end in ``*``.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    netloc = host
    if parts.port is not None and parts.port != DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"
    patterns = [p.lower() for p in drop_params]
    # Filter the raw "k=v" pairs without decoding them, so kept parameters stay byte-identical.
    query = [
        pair
        for pair in parts.query.split("&")
        if pair
        and not any(
            fnmatch.fnmatchcase(unquote_plus(pair.split("=")[0]).lower(), p) for p in patterns
        )
    ]
    return urlunsplit((scheme, netloc, parts.path or "/", "&".join(query), ""))


def url_hash(canonical_url: str) -> bytes:
    """``items.url_hash``: sha256 of the canonical URL."""
    return hashlib.sha256(canonical_url.encode("utf-8")).digest()
