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
        # Per-minute tokens left as the provider reported them, and when that resets.
        self._tokens_left: dict[str, tuple[int, dt.datetime]] = {}
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
        """Learn from rate-limit headers.

        ``x-ratelimit-remaining-requests: 0`` marks the model used up until its reset. The
        provider's own per-minute token count (Groq counts input plus the requested
        ``max_tokens``, more than we record) is kept until it resets, so the next call
        waits for room instead of drawing a 429.
        """
        if not headers:
            return
        from digest.llm.adapters import parse_duration

        if headers.get("x-ratelimit-remaining-requests") == "0":
            seconds = parse_duration(headers.get("x-ratelimit-reset-requests"))
            self.mark_exhausted(key, now + dt.timedelta(seconds=seconds) if seconds else None, now)
        left = headers.get("x-ratelimit-remaining-tokens")
        reset = parse_duration(headers.get("x-ratelimit-reset-tokens"))
        if left is not None and left.isdigit() and reset:
            self._tokens_left[key] = (int(left), now + dt.timedelta(seconds=reset))

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
        """Calls and tokens since ``since`` for ``provider/model``, or a bare ``provider``."""
        provider, _, model = key.partition("/")
        where = [llm_calls.c.provider == provider, llm_calls.c.created_at >= iso(since)]
        if model:
            where.append(llm_calls.c.model == model)
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
                ).where(*where)
            ).one()
        return Window(int(n), int(tokens))

    def has_room(self, provider: Provider, model: ModelSpec, tokens: int, now: dt.datetime) -> bool:
        key = model_key(provider, model)
        until = self._exhausted.get(key)
        if until is not None and until > now:
            return False
        if self._header_wait(key, tokens, now) > 0:
            return False
        if self._minute_drop(provider, model, tokens, now) != 0:
            return False
        since = now - dt.timedelta(days=1)
        for lim, k in ((model.limits, key), (provider.limits, provider.id)):
            if lim.rpd is not None or lim.tpd is not None:
                day = self._window(k, since)
                if lim.rpd is not None and day.requests + 1 > lim.rpd:
                    return False
                if lim.tpd is not None and day.tokens + tokens > lim.tpd:
                    return False
        return True

    def _header_wait(self, key: str, tokens: int, now: dt.datetime) -> float:
        """Seconds until the provider's reported per-minute tokens allow ``tokens``."""
        known = self._tokens_left.get(key)
        if known is None:
            return 0.0
        left, reset_at = known
        if reset_at <= now or left >= tokens:
            return 0.0
        return (reset_at - now).total_seconds()

    def _minute_drop(
        self, provider: Provider, model: ModelSpec, tokens: int, now: dt.datetime
    ) -> float:
        """0 if the configured per-minute limits allow the call now; otherwise seconds until
        enough of the window's calls have aged out (inf if the call alone is too big)."""
        lim = model.limits
        if lim.rpm is None and lim.tpm is None:
            return 0.0
        if lim.tpm is not None and tokens > lim.tpm:
            return float("inf")
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(
                    llm_calls.c.created_at,
                    func.coalesce(llm_calls.c.input_tokens, 0)
                    + func.coalesce(llm_calls.c.output_tokens, 0),
                )
                .where(
                    llm_calls.c.provider == provider.id,
                    llm_calls.c.model == model.id,
                    llm_calls.c.created_at >= iso(now - dt.timedelta(minutes=1)),
                )
                .order_by(llm_calls.c.created_at)
            ).all()
        count, used = len(rows), sum(int(t) for _, t in rows)
        for dropped in range(len(rows) + 1):
            fits_r = lim.rpm is None or count - dropped + 1 <= lim.rpm
            fits_t = lim.tpm is None or used + tokens <= lim.tpm
            if fits_r and fits_t:
                if dropped == 0:
                    return 0.0
                # One second past the edge: the window includes calls exactly a minute old.
                aged = parse_iso(rows[dropped - 1][0]) + dt.timedelta(minutes=1)
                return max(1.0, (aged - now).total_seconds() + 1)
            if dropped < len(rows):
                used -= int(rows[dropped][1])
        return float("inf")  # pragma: no cover - dropping every call always fits

    def minute_wait(
        self, provider: Provider, model: ModelSpec, tokens: int, now: dt.datetime
    ) -> float:
        """Seconds until per-minute limits allow the call (0 if now); for pacing, not skipping.

        Daily limits and 429 marks are not waited for: those mean the model is used up.
        """
        key = model_key(provider, model)
        return max(
            self._header_wait(key, tokens, now), self._minute_drop(provider, model, tokens, now)
        )


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


BUDGET_STEP_HOURS = 4  # six portions a day: the owner wants 4-6 update rounds (2026-10-27)


def released_share(now: dt.datetime, zone: ZoneInfo) -> float:
    """How much of the daily paid budget may be spent by ``now`` (local day).

    It is released in steps of ``BUDGET_STEP_HOURS`` (1/6 from midnight, 2/6 from 04:00,
    ...) so the evening still has money for updates; what a quiet step leaves is kept.
    """
    local = now.astimezone(zone)
    step = local.hour // BUDGET_STEP_HOURS + 1
    return min(1.0, step * BUDGET_STEP_HOURS / 24)


def cost_micro_usd(model: ModelSpec, input_tokens: int, output_tokens: int) -> int:
    usd = (
        input_tokens * model.price_input_per_mtok + output_tokens * model.price_output_per_mtok
    ) / 1_000_000
    return round(usd * 1_000_000)
