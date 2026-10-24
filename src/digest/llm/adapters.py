"""Provider adapters (spec section 9): ``openai_compatible`` and ``anthropic``.

An adapter sends one request and reports what came back in provider-neutral terms. It
doesn't retry, route or count quota; ``LLMClient`` does. Outcomes:

- ``Reply``: the model's text plus token usage.
- ``RateLimited``: HTTP 429 or a used-up quota, with when it resets if the provider says.
- ``SafetyBlock``: a content filter or refusal, a normal outcome (retry elsewhere).
- ``ProviderError``: anything else; ``retryable`` for 5xx and network errors.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from digest.config import ModelSpec, Provider


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    cached_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass(frozen=True)
class Request:
    model: ModelSpec
    system: str
    user: str
    schema_name: str
    schema: dict[str, Any]
    max_tokens: int
    temperature: float | None


@dataclass(frozen=True)
class Reply:
    text: str
    usage: Usage
    truncated: bool = False  # stopped at max_tokens
    remaining: Mapping[str, str] | None = None  # rate-limit headers, lower-case names


class RateLimited(Exception):
    def __init__(self, reset_at: dt.datetime | None, usage: Usage | None = None) -> None:
        super().__init__("rate_limited")
        self.reset_at = reset_at
        self.usage = usage


class SafetyBlock(Exception):
    def __init__(self, usage: Usage | None = None) -> None:
        super().__init__("safety_block")
        self.usage = usage


class ProviderError(Exception):
    def __init__(self, reason: str, *, retryable: bool, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.status = status


class Adapter(Protocol):
    def send(self, req: Request) -> Reply: ...


# --------------------------------------------------------------------------- helpers

_DURATION = re.compile(
    r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?$"
)


def parse_duration(value: str | None) -> float | None:
    """Seconds from ``"7.66s"``, ``"2m59.56s"``, ``"1h2m"``, ``"250ms"`` or ``"30"``."""
    if not value:
        return None
    v = value.strip()
    try:
        return float(v)
    except ValueError:
        pass
    m = _DURATION.match(v)
    if not m or not any(m.groups()):
        return None
    h, mins, s, ms = (float(x) if x else 0.0 for x in m.groups())
    return h * 3600 + mins * 60 + s + ms / 1000


def reset_time(headers: Mapping[str, str], now: dt.datetime) -> dt.datetime | None:
    """When a 429 lifts: ``retry-after``, else the later of the x-ratelimit-reset headers."""
    seconds = parse_duration(headers.get("retry-after"))
    if seconds is None:
        resets = [
            parse_duration(headers.get(k))
            for k in ("x-ratelimit-reset-requests", "x-ratelimit-reset-tokens")
        ]
        known = [r for r in resets if r is not None]
        seconds = max(known) if known else None
    return now + dt.timedelta(seconds=seconds) if seconds is not None else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# --------------------------------------------------------------------------- OpenAI-compatible


class OpenAICompatibleAdapter:
    """Chat Completions over httpx: Groq, Gemini, Cloudflare Workers AI, OpenRouter."""

    def __init__(
        self,
        provider: Provider,
        api_key: str,
        client: httpx.Client | None = None,
        now: Any = None,
    ) -> None:
        if provider.base_url is None:
            raise ValueError(f"{provider.id}: no base_url")
        self.provider = provider
        self._url = str(provider.base_url).rstrip("/") + "/chat/completions"
        self._key = api_key
        self._client = client or httpx.Client(timeout=provider.timeout_seconds)
        self._now = now or (lambda: dt.datetime.now(dt.UTC))

    def body(self, req: Request) -> dict[str, Any]:
        system = req.system
        if req.model.json_mode == "json_object":
            # No schema enforcement on the provider side: state it, then validate the reply.
            system += "\nThe JSON object must match this JSON Schema:\n" + json.dumps(req.schema)
            response_format: dict[str, Any] = {"type": "json_object"}
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": req.schema_name, "schema": req.schema, "strict": True},
            }
        body: dict[str, Any] = {
            "model": req.model.id,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": req.user},
            ],
            "max_tokens": req.max_tokens,
            "response_format": response_format,
        }
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.model.reasoning is not None:
            body["reasoning_effort"] = req.model.reasoning
        return body

    def send(self, req: Request) -> Reply:
        try:
            resp = self._client.post(
                self._url,
                json=self.body(req),
                headers={"Authorization": f"Bearer {self._key}"},
            )
        except httpx.TimeoutException as e:
            # Not retried on the same model: a slow provider would eat the run's time budget.
            raise ProviderError("timeout", retryable=False) from e
        except httpx.HTTPError as e:
            raise ProviderError("network", retryable=True) from e
        headers = {k.lower(): v for k, v in resp.headers.items()}
        if resp.status_code == 429:
            raise RateLimited(reset_time(headers, self._now()))
        if resp.status_code >= 500:
            raise ProviderError(f"http_{resp.status_code}", retryable=True, status=resp.status_code)
        if resp.status_code >= 400:
            if _looks_like_safety(resp):
                raise SafetyBlock()
            raise ProviderError(
                f"http_{resp.status_code}", retryable=False, status=resp.status_code
            )
        try:
            data: dict[str, Any] = resp.json()
            choice: dict[str, Any] = data["choices"][0]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise ProviderError("bad_response", retryable=True) from e
        usage = _openai_usage(data.get("usage"))
        finish = choice.get("finish_reason")
        message: dict[str, Any] = choice.get("message") or {}
        if finish in ("content_filter", "safety") or message.get("refusal"):
            raise SafetyBlock(usage)
        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            if finish == "length":
                return Reply("", usage, truncated=True, remaining=headers)
            raise SafetyBlock(usage)  # Gemini returns empty content when it blocks
        return Reply(text, usage, truncated=finish == "length", remaining=headers)


def _looks_like_safety(resp: httpx.Response) -> bool:
    try:
        text = json.dumps(resp.json()).lower()
    except ValueError:
        return False
    return "safety" in text or "content_filter" in text or "blocked" in text


def _openai_usage(raw: object) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    u: dict[str, Any] = raw  # pyright: ignore[reportUnknownVariableType]
    prompt_details: dict[str, Any] = u.get("prompt_tokens_details") or {}
    completion_details: dict[str, Any] = u.get("completion_tokens_details") or {}
    return Usage(
        input_tokens=_int(u.get("prompt_tokens")),
        cached_tokens=_int(prompt_details.get("cached_tokens")),
        output_tokens=_int(u.get("completion_tokens")),
        reasoning_tokens=_int(completion_details.get("reasoning_tokens")),
    )


# --------------------------------------------------------------------------- Anthropic (paid)


class AnthropicAdapter:
    """Claude through the official SDK. Paid: the client checks the budget before calling.

    Current models take no sampling parameters, so ``temperature`` is never sent; depth is
    set with ``output_config.effort``. A refusal (``stop_reason: "refusal"``) is a
    ``SafetyBlock``: the client retries on another provider, so the API's own fallbacks
    are not used (they would bill outside our budget check).
    """

    def __init__(self, api_key: str, client: Any = None, now: Any = None) -> None:
        if client is None:
            import anthropic  # paid path only; imported when a paid provider is enabled

            client = anthropic.Anthropic(api_key=api_key, max_retries=0)
        self._client = client
        self._now = now or (lambda: dt.datetime.now(dt.UTC))

    def send(self, req: Request) -> Reply:
        import anthropic

        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": req.schema},
        }
        if req.model.reasoning in ("low", "medium", "high"):
            output_config["effort"] = req.model.reasoning
        try:
            msg: Any = self._client.messages.create(
                model=req.model.id,
                max_tokens=req.max_tokens,
                system=[
                    {"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}
                ],
                messages=[{"role": "user", "content": req.user}],
                output_config=output_config,
            )
        except anthropic.RateLimitError as e:
            headers = {k.lower(): v for k, v in e.response.headers.items()}
            raise RateLimited(reset_time(headers, self._now())) from e
        except anthropic.APIStatusError as e:
            raise ProviderError(
                f"http_{e.status_code}", retryable=e.status_code >= 500, status=e.status_code
            ) from e
        except anthropic.APIConnectionError as e:
            raise ProviderError("network", retryable=True) from e
        u = msg.usage
        usage = Usage(
            input_tokens=(u.input_tokens or 0)
            + (getattr(u, "cache_creation_input_tokens", 0) or 0),
            cached_tokens=getattr(u, "cache_read_input_tokens", None),
            output_tokens=u.output_tokens,
        )
        if msg.stop_reason == "refusal":
            raise SafetyBlock(usage)
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        return Reply(text, usage, truncated=msg.stop_reason == "max_tokens")
