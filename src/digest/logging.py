"""JSON logs that are safe to print in a public GitHub Actions log (spec sections 13, 17).

Logs hold counts, ids, timings and our own words only, never third-party text. The filter
works by allow-list, so a forgotten field is redacted rather than leaked:

- The message must be a constant event name such as ``feed_polled``; anything else (format
  arguments, free text, messages from libraries) is replaced.
- Extra fields pass if they are numbers, booleans or None, lists of those, or dicts of
  those; strings pass only under the keys in ``SAFE_STRING_KEYS``.
- Exceptions are reduced to their type name: messages and tracebacks can quote URLs and
  page text.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sys
from collections.abc import Mapping, Sequence
from typing import Any, TextIO

EVENT_RE = re.compile(r"^[a-z][a-z0-9_.]*$")
REDACTED = "[redacted]"

# String values allowed through. Only registry config, our own ids and fixed vocabularies:
# never titles, summaries, article text or item URLs.
SAFE_STRING_KEYS = frozenset(
    {
        "env",
        "error_type",
        "feed_kind",
        "feed_url",  # from config/sources.yaml, which is public
        "file",
        "gate",  # G1-G5 and synthesis: a fixed vocabulary
        "host",
        "kind",
        "mode",
        "model",  # LLM model id, from config/llm_providers.yaml
        "policy",
        "provider",
        "reason",
        "source_id",
        "stage",
        "state_store",
        "status",
        "verdict",
    }
)

# Attributes every LogRecord has; anything else came in through ``extra=``.
_STANDARD = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def _safe_scalar(v: object) -> bool:
    return v is None or isinstance(v, bool | int | float)


def _safe_value(key: str, v: object) -> bool:
    if _safe_scalar(v):
        return True
    if isinstance(v, str):
        return key in SAFE_STRING_KEYS
    if isinstance(v, Mapping):
        items = list(v.items())  # pyright: ignore[reportUnknownArgumentType]
        return all(isinstance(k, str) and EVENT_RE.match(k) and _safe_scalar(x) for k, x in items)
    if isinstance(v, Sequence) and not isinstance(v, bytes | bytearray):
        return all(_safe_scalar(x) for x in v)  # pyright: ignore[reportUnknownVariableType]
    return False


def _ours(name: str) -> bool:
    return name == "digest" or name.startswith("digest.")


class PublicJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname.lower(),
            "logger": record.name,
        }
        if not _ours(record.name):
            out["event"] = "library_log"
        elif isinstance(record.msg, str) and not record.args and EVENT_RE.match(record.msg):
            out["event"] = record.msg
        else:
            out["event"] = "redacted_message"
        for key, value in record.__dict__.items():
            if key in _STANDARD or key.startswith("_"):
                continue
            out[key] = value if _ours(record.name) and _safe_value(key, value) else REDACTED
        if record.exc_info and record.exc_info[0] is not None:
            out["error_type"] = record.exc_info[0].__name__
        return json.dumps(out, ensure_ascii=False, default=lambda _: REDACTED)


def setup_logging(level: int = logging.INFO, stream: TextIO | None = None) -> None:
    """Route all logging (and warnings) through the public filter, to stdout."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(PublicJsonFormatter())
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level)
    # Libraries log request lines with full URLs at INFO; the filter would hide the text,
    # but there is no reason to emit them at all.
    for name in ("httpx", "httpcore", "trafilatura", "htmldate", "courlan", "charset_normalizer"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.captureWarnings(True)


def safe_excepthook(
    exc_type: type[BaseException], exc: BaseException, tb: object, /
) -> None:  # pragma: no cover - exercised through the CLI
    """Replace Python's default traceback printer: report only the exception type."""
    logging.getLogger("digest").critical(
        "uncaught_exception", extra={"error_type": exc_type.__name__}
    )
