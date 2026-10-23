"""Cached robots.txt and site-wide TDM status per host, in the ``host_policy`` table."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import ClassVar

from sqlalchemy import Engine, select

from digest.db.schema import host_policy
from digest.timeutil import iso, parse_iso


@dataclass(frozen=True)
class Cached:
    text: str
    fetched_at: dt.datetime


class HostPolicyCache:
    """Read and write cached policy files. ``kind`` is ``robots`` or ``tdmrep``."""

    _COLUMNS: ClassVar[dict[str, tuple[str, str]]] = {
        "robots": ("robots_txt", "robots_fetched_at"),
        "tdmrep": ("tdmrep_json", "tdm_checked_at"),
    }

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def get(self, host: str, kind: str) -> Cached | None:
        text_col, at_col = self._COLUMNS[kind]
        with self.engine.connect() as conn:
            row = conn.execute(
                select(host_policy.c[text_col], host_policy.c[at_col]).where(
                    host_policy.c.host == host
                )
            ).first()
        if row is None or row[0] is None or row[1] is None:
            return None
        return Cached(text=str(row[0]), fetched_at=parse_iso(str(row[1])))

    def put(self, host: str, kind: str, text: str, at: dt.datetime) -> None:
        text_col, at_col = self._COLUMNS[kind]
        values = {text_col: text, at_col: iso(at)}
        with self.engine.begin() as conn:
            updated = conn.execute(
                host_policy.update().where(host_policy.c.host == host).values(**values)
            ).rowcount
            if not updated:
                conn.execute(host_policy.insert().values(host=host, **values))
