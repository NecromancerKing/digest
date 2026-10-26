"""``LLMClient``: every LLM call goes through here (spec section 9, client requirements).

It picks a model by role, quality and quota; refuses ``link_only`` text (rule L6); checks
the paid daily budget before a paid call; records every attempt in ``llm_calls``; retries
5xx and network errors with jittered backoff (at most 3 times); moves to the next model on
a 429; retries schema-invalid output once on another model and a safety block once on
another provider; and validates every reply against its Pydantic schema.

Routing:

- synthesize: highest ``quality`` first, so readers see one style as long as possible;
  the ``strong`` paid model first for top stories when allowed.
- extract: the lowest-quality model that passed the extraction eval (``extract_ok``),
  which keeps the stronger models' quota for writing.
- verify: highest quality first, but a model other than the writer when one has quota.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import Engine

from digest.config import LLMProviders, ModelSpec, Provider, Role, Settings
from digest.db.schema import llm_calls
from digest.llm.adapters import (
    Adapter,
    AnthropicAdapter,
    OpenAICompatibleAdapter,
    ProviderError,
    RateLimited,
    Reply,
    Request,
    SafetyBlock,
    Usage,
)
from digest.llm.prompts import Prompt
from digest.llm.quota import Quotas, cost_micro_usd, model_key, spent_today_usd
from digest.llm.schemas import strict_schema
from digest.nlp.embed import LinkOnlyViolation
from digest.timeutil import iso, utcnow

log = logging.getLogger(__name__)

# D28: full article text, key facts and longer summaries need room. Extract stays under
# 8,000 with a full 5,000-token input (Groq's free tier allows 8,000 tokens a minute).
MAX_OUTPUT_TOKENS: Mapping[Role, int] = {"extract": 2800, "synthesize": 1600, "verify": 1200}
TEMPERATURE: Mapping[Role, float] = {"extract": 0.0, "synthesize": 0.3, "verify": 0.0}
MAX_TRANSIENT_RETRIES = 3
# Groq's free tier allows 8,000 tokens a minute, about one large call: waiting for the
# next minute beats giving up on the model. The run's time budget still bounds the stage.
MAX_PACING_WAIT = 65.0


class LLMUnavailable(Exception):
    """No model produced a valid answer: no quota, every provider down, or blocks/invalid."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason  # no_quota | safety_block | invalid_output | error


class InputTooLarge(ValueError):
    """The prompt exceeds ``LLM_MAX_INPUT_TOKENS``; the caller must split it."""


@dataclass(frozen=True)
class Result[T: BaseModel]:
    value: T
    provider: str
    model: str
    prompt_version: str
    usage: Usage


@dataclass(frozen=True)
class Candidate:
    provider: Provider
    model: ModelSpec

    @property
    def key(self) -> str:
        return model_key(self.provider, self.model)


def api_key(settings: Settings, env_name: str) -> str | None:
    """A provider's key: the matching ``Settings`` field (which reads ``.env``), else the env."""
    secret = getattr(settings, env_name.lower(), None)
    if secret is not None and hasattr(secret, "get_secret_value"):
        value = str(secret.get_secret_value())
        if value:
            return value
    return os.environ.get(env_name) or None


class LLMClient:
    def __init__(
        self,
        settings: Settings,
        providers: LLMProviders,
        engine: Engine,
        *,
        run_id: int | None = None,
        adapters: Mapping[str, Adapter] | None = None,
        now: Callable[[], dt.datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        eval_mode: bool = False,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.run_id = run_id
        self.now = now
        self.sleep = sleep
        self.jitter = jitter
        self.eval_mode = eval_mode
        self.quotas = Quotas(engine, now())
        self._adapters: dict[str, Adapter] = dict(adapters or {})
        self.providers = [p for p in providers.eligible() if self._usable(p)]
        self.calls = 0

    # --- setup

    def _usable(self, p: Provider) -> bool:
        if p.paid and self.settings.llm_daily_budget_usd <= 0:
            return False  # never call a paid provider with a budget of 0
        if p.id in self._adapters:
            return True
        key = api_key(self.settings, p.api_key_env)
        if not key:
            return False
        if p.adapter == "anthropic":
            self._adapters[p.id] = AnthropicAdapter(key, now=self.now)
            return True
        if p.base_url is None:
            return False
        self._adapters[p.id] = OpenAICompatibleAdapter(p, key, now=self.now)
        return True

    # --- routing

    def candidates(
        self, role: Role, *, avoid: str | None = None, strong: bool = False
    ) -> list[Candidate]:
        out = [Candidate(p, m) for p in self.providers for m in p.models if role in m.roles]
        if role == "extract":
            ok = [c for c in out if c.model.extract_ok]
            out = sorted(ok, key=lambda c: c.model.quality) + sorted(
                [c for c in out if not c.model.extract_ok], key=lambda c: -c.model.quality
            )
        else:
            out.sort(key=lambda c: -c.model.quality)
        if role == "synthesize":
            want_strong = strong and self.settings.use_strong_model_for_top
            out.sort(key=lambda c: not (want_strong and c.model.strong))
            if not want_strong:
                out = [c for c in out if not c.model.strong] + [c for c in out if c.model.strong]
        if avoid is not None:  # the writer's model goes last, used only if nothing else has room
            out.sort(key=lambda c: c.model.id == avoid)
        return out

    def available(self, role: Role) -> bool:
        """Any model for ``role`` with room right now (a cheap check before building prompts)."""
        now = self.now()
        return any(self.quotas.has_room(c.provider, c.model, 1, now) for c in self.candidates(role))

    # --- the call

    def call[T: BaseModel](
        self,
        prompt: Prompt,
        schema: type[T],
        *,
        avoid_model: str | None = None,
        strong: bool = False,
        pin: str | None = None,
    ) -> Result[T]:
        """``pin`` (``provider/model``) uses only that model: the eval compares models."""
        if "link_only" in prompt.policies:
            raise LinkOnlyViolation("link_only text may not be sent to an LLM")
        if prompt.approx_tokens > self.settings.llm_max_input_tokens:
            raise InputTooLarge(f"{prompt.approx_tokens} tokens")
        role = prompt.purpose
        max_out = MAX_OUTPUT_TOKENS[role]
        need = prompt.approx_tokens + max_out
        json_schema = strict_schema(schema)
        invalid_retry = safety_retry = rate_retry = False
        blocked_providers: set[str] = set()
        tried = 0
        last = "no_quota"
        candidates = self.candidates(role, avoid=avoid_model, strong=strong)
        if pin is not None:
            candidates = [c for c in candidates if c.key == pin]
        for i, c in enumerate(candidates):  # may grow by one: see invalid output below
            if c.provider.id in blocked_providers:
                continue
            now = self.now()
            if not self.quotas.has_room(c.provider, c.model, need, now):
                wait = self.quotas.minute_wait(c.provider, c.model, need, now)
                if 0 < wait <= MAX_PACING_WAIT and self.quotas.has_room(  # pace, don't skip
                    c.provider, c.model, need, now + dt.timedelta(seconds=wait)
                ):
                    self.sleep(wait)
                else:
                    continue
            if c.provider.paid and not self._budget_allows(c.model, prompt.approx_tokens, max_out):
                continue
            tried += 1
            req = Request(
                model=c.model,
                system=prompt.system,
                user=prompt.user,
                schema_name=schema.__name__,
                schema=json_schema,
                max_tokens=max_out,
                temperature=None if c.provider.adapter == "anthropic" else TEMPERATURE[role],
            )
            try:
                reply, latency = self._send_with_retries(c, req, role)
            except RateLimited as e:
                now = self.now()
                self.quotas.mark_exhausted(c.key, e.reset_at, now)
                last = "no_quota"
                wait = (e.reset_at - now).total_seconds() if e.reset_at else None
                log.info(
                    "llm_rate_limited",
                    extra={
                        "stage": role,
                        "provider": c.provider.id,
                        "model": c.model.id,
                        "limit": e.limit,
                        "retry_after_s": round(wait) if wait is not None else None,
                    },
                )
                rest = candidates[i + 1 :]
                if (
                    not rate_retry
                    and wait is not None
                    and 0 < wait <= MAX_PACING_WAIT
                    and all(o.provider.id in blocked_providers for o in rest)
                ):  # a per-minute limit with no other model left: wait it out once
                    rate_retry = True
                    self.sleep(wait)
                    candidates.append(c)
                continue
            except SafetyBlock:
                last = "safety_block"
                if safety_retry:
                    break
                safety_retry = True
                blocked_providers.add(c.provider.id)
                continue
            except ProviderError:
                last = "error"
                continue
            value = self._parse(reply, schema)
            if value is None:
                self._record(c, role, reply.usage, "invalid_output", latency)
                log.info(
                    "llm_invalid_output",
                    extra={
                        "stage": role,
                        "provider": c.provider.id,
                        "model": c.model.id,
                        "reason": reply.rejected
                        or ("truncated" if reply.truncated else "unparsed"),
                        "chars": reply.rejected_chars,
                        "output_tokens": reply.usage.output_tokens,
                        "reasoning_tokens": reply.usage.reasoning_tokens,
                        "max_tokens": max_out,
                    },
                )
                last = "invalid_output"
                if invalid_retry:
                    break
                invalid_retry = True
                if all(o.provider.id in blocked_providers for o in candidates[i + 1 :]):
                    candidates.append(c)  # no other model left: the retry goes to this one
                continue
            self._record(c, role, reply.usage, "ok", latency)
            self.quotas.note_headers(c.key, reply.remaining, self.now())
            return Result(value, c.provider.id, c.model.id, prompt.version, reply.usage)
        log.info("llm_unavailable", extra={"stage": role, "reason": last, "attempts": tried})
        raise LLMUnavailable(last)

    def _send_with_retries(self, c: Candidate, req: Request, role: Role) -> tuple[Reply, int]:
        """The reply and its latency in ms. Failed attempts are recorded here."""
        adapter = self._adapters[c.provider.id]
        attempt = 0
        while True:
            t0 = time.monotonic()
            try:
                return adapter.send(req), _ms_since(t0)
            except RateLimited as e:
                self._record(c, role, e.usage, "rate_limited", _ms_since(t0))
                raise
            except SafetyBlock as e:
                self._record(c, role, e.usage, "safety_block", _ms_since(t0))
                raise
            except ProviderError as e:
                self._record(c, role, None, "error", _ms_since(t0))
                log.info(
                    "llm_error",
                    extra={
                        "provider": c.provider.id,
                        "model": c.model.id,
                        "reason": e.reason,
                        "http_status": e.status,
                        "attempt": attempt,
                    },
                )
                if not e.retryable or attempt == MAX_TRANSIENT_RETRIES:
                    raise
                self.sleep(min(30.0, 2.0**attempt) * (0.5 + self.jitter()))
                attempt += 1

    @staticmethod
    def _parse[T: BaseModel](reply: Reply, schema: type[T]) -> T | None:
        if reply.truncated:
            return None
        text = reply.text.strip()
        if text.startswith("```"):  # some models fence JSON despite JSON mode
            text = text.strip("`").removeprefix("json").strip()
        try:
            return schema.model_validate(json.loads(text))
        except (ValueError, ValidationError):
            return None

    def _budget_allows(self, model: ModelSpec, input_tokens: int, max_out: int) -> bool:
        budget = self.settings.llm_daily_budget_usd
        if budget <= 0:
            return False
        spent = spent_today_usd(self.engine, self.now(), self.settings.zone)
        worst = cost_micro_usd(model, input_tokens, max_out) / 1_000_000
        return spent + worst <= budget

    def _record(
        self, c: Candidate, role: Role, usage: Usage | None, outcome: str, latency: int
    ) -> None:
        u = usage or Usage()
        cost = (
            cost_micro_usd(c.model, u.input_tokens or 0, u.output_tokens or 0)
            if c.provider.paid
            else 0
        )
        values: dict[str, Any] = {
            "run_id": self.run_id,
            "purpose": "eval" if self.eval_mode else role,
            "provider": c.provider.id,
            "model": c.model.id,
            "input_tokens": u.input_tokens,
            "cached_tokens": u.cached_tokens,
            "output_tokens": u.output_tokens,
            "reasoning_tokens": u.reasoning_tokens,
            "cost_micro_usd": cost,
            "outcome": outcome,
            "latency_ms": latency,
            "created_at": iso(self.now()),
        }
        with self.engine.begin() as conn:
            conn.execute(llm_calls.insert().values(**values))
        self.calls += 1
        log.info(
            "llm_call",
            extra={
                "stage": role,
                "status": outcome,
                "provider": c.provider.id,
                "model": c.model.id,
                "latency_ms": latency,
            },
        )


def _ms_since(t0: float) -> int:
    return round((time.monotonic() - t0) * 1000)
