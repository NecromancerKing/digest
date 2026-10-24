from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select

from digest import db
from digest.config import ModelLimits, Settings
from digest.db.schema import kv, llm_calls
from digest.llm.adapters import (
    AnthropicAdapter,
    OpenAICompatibleAdapter,
    ProviderError,
    RateLimited,
    Reply,
    Request,
    SafetyBlock,
    Usage,
    parse_duration,
)
from digest.llm.client import InputTooLarge, LLMClient, LLMUnavailable
from digest.llm.prompts import Prompt
from digest.llm.schemas import GradeClaims, RecordFacts, WriteStory, strict_schema
from digest.nlp.embed import LinkOnlyViolation
from tests.fake_llm import FakeProvider, providers, reply

T0 = dt.datetime(2026, 10, 24, 9, 0, tzinfo=dt.UTC)


class Clock:
    def __init__(self) -> None:
        self.t = T0
        self.slept: list[float] = []

    def now(self) -> dt.datetime:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += dt.timedelta(seconds=s)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    path = tmp_path / "state.sqlite"
    db.migrate(path)
    return db.make_engine(path)


def grade_prompt(policies: frozenset[str] = frozenset({"facts_only"})) -> Prompt:
    return Prompt("verify", "verify-v1", "system", "user", policies)


GRADES = {"results": [{"claim_index": 0, "verdict": "supported", "reason": "ok"}]}


def make(
    engine: Engine,
    fakes: dict[str, FakeProvider],
    *,
    budget: float = 0.0,
    paid: bool = False,
    allow_training: bool = True,
    clock: Clock | None = None,
    **settings: Any,
) -> LLMClient:
    clock = clock or Clock()
    s = Settings(_env_file=None, llm_daily_budget_usd=budget, **settings)  # pyright: ignore[reportCallIssue]
    return LLMClient(
        s,
        providers(paid=paid, allow_training=allow_training),
        engine,
        adapters=fakes,  # pyright: ignore[reportArgumentType]
        now=clock.now,
        sleep=clock.sleep,
        jitter=lambda: 0.5,
    )


def all_fakes(**handlers: Any) -> dict[str, FakeProvider]:
    out = {p: FakeProvider() for p in ("alpha", "beta", "claude")}
    for p in out.values():
        p.handlers["GradeClaims"] = lambda _r: reply(GRADES)
    return out


def calls(engine: Engine) -> list[tuple[str, str, str, str]]:
    with engine.connect() as conn:
        return [
            (r.purpose, r.provider, r.model, r.outcome)
            for r in conn.execute(select(llm_calls).order_by(llm_calls.c.id))
        ]


# --------------------------------------------------------------------------- guard and routing


def test_link_only_text_never_reaches_a_provider(engine: Engine) -> None:
    fakes = all_fakes()
    client = make(engine, fakes)
    with pytest.raises(LinkOnlyViolation):
        client.call(grade_prompt(frozenset({"facts_only", "link_only"})), GradeClaims)
    assert all(not f.requests for f in fakes.values())


def test_oversized_input_is_refused(engine: Engine) -> None:
    client = make(engine, all_fakes(), llm_max_input_tokens=10)
    with pytest.raises(InputTooLarge):
        client.call(Prompt("verify", "v", "x" * 100, "y"), GradeClaims)


def test_roles_route_by_quality(engine: Engine) -> None:
    client = make(engine, all_fakes())
    assert [c.model.id for c in client.candidates("synthesize")] == ["big", "mid"]
    assert client.candidates("extract")[0].model.id == "small"
    assert [c.model.id for c in client.candidates("verify", avoid="big")] == ["mid", "small", "big"]


def test_training_providers_excluded_when_not_allowed(engine: Engine) -> None:
    client = make(engine, all_fakes(), allow_training=False)
    assert {p.id for p in client.providers} == {"alpha"}


def test_call_records_and_validates(engine: Engine) -> None:
    fakes = all_fakes()
    result = make(engine, fakes).call(grade_prompt(), GradeClaims)
    assert result.model == "big" and result.value.results[0].verdict == "supported"
    assert calls(engine) == [("verify", "alpha", "big", "ok")]
    assert fakes["alpha"].requests[0].temperature == 0.0


# --------------------------------------------------------------------------- failures


def test_rate_limit_switches_model_and_is_remembered(engine: Engine) -> None:
    fakes = all_fakes()

    def limited(_req: Request) -> Reply:
        raise RateLimited(T0 + dt.timedelta(hours=5))

    fakes["alpha"].by_model["big"] = limited
    client = make(engine, fakes)
    assert client.call(grade_prompt(), GradeClaims).model == "mid"
    with engine.connect() as conn:
        marks = json.loads(
            conn.execute(select(kv.c.value).where(kv.c.key == "llm_exhausted")).scalar_one()
        )
    assert "alpha/big" in marks
    again = make(engine, fakes)  # a later run in the same day
    assert again.call(grade_prompt(), GradeClaims).model == "mid"
    assert fakes["alpha"].models().count("big") == 1


def test_configured_daily_limit_is_respected(engine: Engine) -> None:
    fakes = all_fakes()
    client = make(engine, fakes)
    client.providers[0] = client.providers[0].model_copy(
        update={
            "models": [
                m.model_copy(update={"limits": m.limits.model_copy(update={"rpd": 1})})
                for m in client.providers[0].models
            ]
        }
    )
    assert client.call(grade_prompt(), GradeClaims).model == "big"
    assert client.call(grade_prompt(), GradeClaims).model == "mid"


def test_invalid_output_retries_once_on_another_model(engine: Engine) -> None:
    fakes = all_fakes()
    fakes["alpha"].by_model["big"] = lambda _r: Reply("not json", Usage())
    client = make(engine, fakes)
    assert client.call(grade_prompt(), GradeClaims).model == "mid"
    fakes["beta"].by_model["mid"] = lambda _r: reply({"results": "nope"})
    with pytest.raises(LLMUnavailable) as e:
        make(engine, fakes).call(grade_prompt(), GradeClaims)
    assert e.value.reason == "invalid_output"
    assert fakes["alpha"].models().count("small") == 0  # stopped after one retry


def test_safety_block_retries_on_another_provider(engine: Engine) -> None:
    fakes = all_fakes()

    def blocked(_req: Request) -> Reply:
        raise SafetyBlock()

    fakes["alpha"].by_model["big"] = blocked
    result = make(engine, fakes).call(grade_prompt(), GradeClaims)
    assert result.provider == "beta"
    assert "small" not in fakes["alpha"].models()  # same provider is skipped
    fakes["beta"].by_model["mid"] = blocked
    with pytest.raises(LLMUnavailable) as e:
        make(engine, fakes).call(grade_prompt(), GradeClaims)
    assert e.value.reason == "safety_block"


def test_server_errors_retry_with_backoff_then_move_on(engine: Engine) -> None:
    fakes = all_fakes()

    def down(_req: Request) -> Reply:
        raise ProviderError("http_503", retryable=True, status=503)

    fakes["alpha"].by_model["big"] = down
    clock = Clock()
    result = make(engine, fakes, clock=clock).call(grade_prompt(), GradeClaims)
    assert result.model == "mid"
    assert fakes["alpha"].models().count("big") == 4  # first try + 3 retries
    assert clock.slept == [1.0, 2.0, 4.0]


def test_every_provider_down_means_unavailable(engine: Engine) -> None:
    fakes = all_fakes()

    def down(_req: Request) -> Reply:
        raise ProviderError("network", retryable=False)

    for f in fakes.values():
        f.handlers["GradeClaims"] = down
    with pytest.raises(LLMUnavailable) as e:
        make(engine, fakes).call(grade_prompt(), GradeClaims)
    assert e.value.reason == "error"


# --------------------------------------------------------------------------- paid budget


def test_paid_provider_never_called_with_zero_budget(engine: Engine) -> None:
    fakes = all_fakes()
    client = make(engine, fakes, paid=True, budget=0)
    assert "claude" not in {p.id for p in client.providers}
    client.call(grade_prompt(), GradeClaims)
    assert not fakes["claude"].requests


def test_paid_calls_stop_at_the_daily_budget(engine: Engine) -> None:
    fakes = all_fakes()
    fakes["claude"].handlers["GradeClaims"] = lambda _r: Reply(
        json.dumps(GRADES), Usage(input_tokens=100_000, output_tokens=180_000)
    )
    client = make(engine, fakes, paid=True, budget=1.0)  # haiku: $1 in, $5 out per M
    assert client.call(grade_prompt(), GradeClaims).provider == "claude"  # costs $1.00
    assert client.call(grade_prompt(), GradeClaims).provider == "alpha"  # budget used up
    with engine.connect() as conn:
        cost = conn.execute(
            select(llm_calls.c.cost_micro_usd).where(llm_calls.c.provider == "claude")
        ).scalar_one()
    assert cost == 1_000_000


# --------------------------------------------------------------------------- adapters


def test_schemas_are_strict() -> None:
    for model in (RecordFacts, WriteStory, GradeClaims):
        schema = json.dumps(strict_schema(model))
        assert '"additionalProperties": true' not in schema and '"default"' not in schema
    story = strict_schema(WriteStory)
    assert story["required"] == list(story["properties"]) and story["additionalProperties"] is False


def test_parse_duration() -> None:
    assert parse_duration("7.66s") == pytest.approx(7.66)
    assert parse_duration("2m59.56s") == pytest.approx(179.56)
    assert parse_duration("250ms") == pytest.approx(0.25)
    assert parse_duration("30") == 30
    assert parse_duration("soon") is None


def openai_adapter(
    handler: Any, json_mode: str = "json_schema"
) -> tuple[OpenAICompatibleAdapter, Request]:
    p = providers().providers[0]
    model = p.models[0].model_copy(update={"json_mode": json_mode, "reasoning": "low"})
    adapter = OpenAICompatibleAdapter(
        p, "k", client=httpx.Client(transport=httpx.MockTransport(handler)), now=lambda: T0
    )
    req = Request(model, "sys", "user", "GradeClaims", strict_schema(GradeClaims), 800, 0.0)
    return adapter, req


def ok_body(content: str | None, finish: str = "stop") -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "completion_tokens_details": {"reasoning_tokens": 2},
        },
    }


def test_openai_adapter_request_and_reply() -> None:
    seen: list[dict[str, Any]] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(json.loads(r.content))
        assert r.headers["authorization"] == "Bearer k"
        assert str(r.url) == "https://alpha.example/v1/chat/completions"
        return httpx.Response(200, json=ok_body('{"results": []}'))

    adapter, req = openai_adapter(handler)
    out = adapter.send(req)
    assert out.text == '{"results": []}' and out.usage.reasoning_tokens == 2
    body = seen[0]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["reasoning_effort"] == "low" and body["temperature"] == 0.0
    adapter, req = openai_adapter(handler, "json_object")
    adapter.send(req)
    assert seen[1]["response_format"] == {"type": "json_object"}
    assert "JSON Schema" in seen[1]["messages"][0]["content"]


def test_openai_adapter_outcomes() -> None:
    def limited(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "12"})

    with pytest.raises(RateLimited) as e:
        openai_adapter(limited)[0].send(openai_adapter(limited)[1])
    assert e.value.reset_at == T0 + dt.timedelta(seconds=12)

    for resp in (
        httpx.Response(200, json=ok_body("", "content_filter")),
        httpx.Response(200, json=ok_body(None)),
        httpx.Response(400, json={"error": {"message": "blocked by safety settings"}}),
    ):
        adapter, req = openai_adapter(lambda _r, resp=resp: resp)
        with pytest.raises(SafetyBlock):
            adapter.send(req)
    adapter, req = openai_adapter(lambda _r: httpx.Response(503))
    with pytest.raises(ProviderError) as pe:
        adapter.send(req)
    assert pe.value.retryable
    adapter, req = openai_adapter(lambda _r: httpx.Response(200, json=ok_body('{"a"', "length")))
    assert adapter.send(req).truncated


class FakeMessages:
    def __init__(self, stop_reason: str) -> None:
        self.kwargs: dict[str, Any] = {}
        self.stop_reason = stop_reason

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="text", text='{"results": []}')],
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=5,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=3,
            ),
        )


def test_anthropic_adapter_sends_no_sampling_and_maps_refusals() -> None:
    model = providers(paid=True).providers[2].models[0].model_copy(update={"reasoning": "low"})
    req = Request(model, "sys", "user", "GradeClaims", strict_schema(GradeClaims), 800, None)
    messages = FakeMessages("end_turn")
    out = AnthropicAdapter("k", client=SimpleNamespace(messages=messages)).send(req)
    assert out.text == '{"results": []}' and out.usage.cached_tokens == 3
    assert "temperature" not in messages.kwargs
    assert messages.kwargs["output_config"]["effort"] == "low"
    assert messages.kwargs["output_config"]["format"]["type"] == "json_schema"
    with pytest.raises(SafetyBlock):
        AnthropicAdapter("k", client=SimpleNamespace(messages=FakeMessages("refusal"))).send(req)


def test_full_minute_window_waits_instead_of_skipping(engine: Engine) -> None:
    fakes = all_fakes()
    clock = Clock()
    client = make(engine, fakes, clock=clock)
    tight = client.providers[0].models[0].model_copy(update={"limits": ModelLimits(tpm=900)})
    client.providers[0] = client.providers[0].model_copy(
        update={"models": [tight, *client.providers[0].models[1:]]}
    )
    assert client.call(grade_prompt(), GradeClaims).model == "big"
    assert client.call(grade_prompt(), GradeClaims).model == "big"  # same model, a minute later
    assert clock.slept == [61.0]


def test_timeouts_move_to_the_next_model_at_once() -> None:
    def slow(_r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    adapter, req = openai_adapter(slow)
    with pytest.raises(ProviderError) as e:
        adapter.send(req)
    assert e.value.reason == "timeout" and not e.value.retryable
