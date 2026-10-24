"""Quota and budget checks before every call (spec section 9, routing rules).

A model may be called when its configured limits, counted from ``llm_calls`` over the last
minute and the last 24 hours, leave room for the request, and when no 429 or rate-limit
header has marked it used up. A ``null`` limit is unknown: then only 429s and headers
count. Rolling 24 hours is stricter than the providers' calendar days, never looser.

Marks from 429s are kept in ``kv`` so the next run (30 minutes later) still knows that a
daily quota is gone.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, func, select

from digest.config import ModelSpec, Provider
from digest.db.schema import kv, llm_calls
from digest.timeutil import iso, parse_iso

EXHAUSTED_KEY = "llm_exhausted"
DEFAULT_COOLDOWN = dt.timedelta(seconds=60)  # a 429 without any reset hint


@dataclass(frozen=True)
class Window:
    requests: int
    tokens: int


def model_key(provider: Provider | str, model: ModelSpec | str) -> str:
    p = provider if isinstance(provider, str) else provider.id
    m = model if isinstance(model, str) else model.id
    return f"{p}/{m}"


class Quotas:
    def __init__(self, engine: Engine, now: dt.datetime) -> None:
        self.engine = engine
        self._exhausted: dict[str, dt.datetime] = {}
        with engine.connect() as conn:
            raw = conn.execute(select(kv.c.value).where(kv.c.key == EXHAUSTED_KEY)).scalar()
        if raw:
            for k, v in json.loads(raw).items():
                until = parse_iso(v)
                if until > now:
                    self._exhausted[k] = until

    # --- marks

    def mark_exhausted(self, key: str, until: dt.datetime | None, now: dt.datetime) -> None:
        self._exhausted[key] = max(until or now + DEFAULT_COOLDOWN, now + dt.timedelta(seconds=1))
        self._save(now)

    def note_headers(self, key: str, headers: Mapping[str, str] | None, now: dt.datetime) -> None:
        """``x-ratelimit-remaining-requests: 0`` marks the model used up until its reset."""
        if not headers:
            return
        from digest.llm.adapters import parse_duration

        if headers.get("x-ratelimit-remaining-requests") == "0":
            seconds = parse_duration(headers.get("x-ratelimit-reset-requests"))
            self.mark_exhausted(key, now + dt.timedelta(seconds=seconds) if seconds else None, now)

    def _save(self, now: dt.datetime) -> None:
        live = {k: iso(v) for k, v in self._exhausted.items() if v > now}
        with self.engine.begin() as conn:
            value = json.dumps(live, sort_keys=True)
            if not conn.execute(
                kv.update().where(kv.c.key == EXHAUSTED_KEY).values(value=value)
            ).rowcount:
                conn.execute(kv.insert().values(key=EXHAUSTED_KEY, value=value))

    # --- checks

    def _window(self, key: str, since: dt.datetime) -> Window:
        provider, model = key.split("/", 1)
        with self.engine.connect() as conn:
            n, tokens = conn.execute(
                select(
                    func.count(),
                    func.coalesce(
                        func.sum(
                            func.coalesce(llm_calls.c.input_tokens, 0)
                            + func.coalesce(llm_calls.c.output_tokens, 0)
                        ),
                        0,
                    ),
                ).where(
                    llm_calls.c.provider == provider,
                    llm_calls.c.model == model,
                    llm_calls.c.created_at >= iso(since),
                )
            ).one()
        return Window(int(n), int(tokens))

    def has_room(self, provider: Provider, model: ModelSpec, tokens: int, now: dt.datetime) -> bool:
        key = model_key(provider, model)
        until = self._exhausted.get(key)
        if until is not None and until > now:
            return False
        lim = model.limits
        if lim.rpm is not None or lim.tpm is not None:
            minute = self._window(key, now - dt.timedelta(minutes=1))
            if lim.rpm is not None and minute.requests + 1 > lim.rpm:
                return False
            if lim.tpm is not None and minute.tokens + tokens > lim.tpm:
                return False
        if lim.rpd is not None or lim.tpd is not None:
            day = self._window(key, now - dt.timedelta(days=1))
            if lim.rpd is not None and day.requests + 1 > lim.rpd:
                return False
            if lim.tpd is not None and day.tokens + tokens > lim.tpd:
                return False
        return True

    def minute_wait(
        self, provider: Provider, model: ModelSpec, tokens: int, now: dt.datetime
    ) -> float:
        """Seconds until the per-minute window frees up, or 0; for pacing instead of skipping."""
        lim = model.limits
        if lim.rpm is None and lim.tpm is None:
            return 0.0
        key = model_key(provider, model)
        with self.engine.connect() as conn:
            oldest = conn.execute(
                select(func.min(llm_calls.c.created_at)).where(
                    llm_calls.c.provider == provider.id,
                    llm_calls.c.model == model.id,
                    llm_calls.c.created_at >= iso(now - dt.timedelta(minutes=1)),
                )
            ).scalar()
        if oldest is None:
            return 0.0
        minute = self._window(key, now - dt.timedelta(minutes=1))
        over_r = lim.rpm is not None and minute.requests + 1 > lim.rpm
        over_t = lim.tpm is not None and minute.tokens + tokens > lim.tpm
        if not (over_r or over_t):
            return 0.0
        # One second past the edge: the window includes calls made exactly a minute ago.
        return max(0.0, (parse_iso(oldest) + dt.timedelta(minutes=1) - now).total_seconds() + 1)


def spent_today_usd(engine: Engine, now: dt.datetime, zone: ZoneInfo) -> float:
    """Paid spending on the current local (Europe/Stockholm) day."""
    day = now.astimezone(zone).date()
    start = dt.datetime.combine(day, dt.time(), zone)
    with engine.connect() as conn:
        micro = conn.execute(
            select(func.coalesce(func.sum(llm_calls.c.cost_micro_usd), 0)).where(
                llm_calls.c.created_at >= iso(start)
            )
        ).scalar_one()
    return int(micro) / 1_000_000


def cost_micro_usd(model: ModelSpec, input_tokens: int, output_tokens: int) -> int:
    usd = (
        input_tokens * model.price_input_per_mtok + output_tokens * model.price_output_per_mtok
    ) / 1_000_000
    return round(usd * 1_000_000)
