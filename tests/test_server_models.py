"""Server paid calls prove scoped credentials, live fences and durable admission."""

from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
import json

import httpx
import pytest
from sqlalchemy import select

from forge.budget.server_models import ModelUnavailable, ServerModels
from forge.budget.strict import TrustedPolicy
from forge.domain.budget import BudgetExhausted, Resources
from forge.store.budget_calls import ModelCallStore
from forge.store.models_budget import EngineUsage
from forge.store.records import StaleLease, StoreConflict
from forge.store.steps import StepStore
from tests.test_budget_engine import case as case, prepare


class Bytes(httpx.AsyncByteStream):
    def __init__(self, raw):
        self.raw = raw

    async def __aiter__(self):
        for offset in range(0, len(self.raw), 8192):
            yield self.raw[offset : offset + 8192]


def wire(body=None, *, raw=None, status=200, headers=None):
    body = body or {
        "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": "answer"}}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
    }
    return httpx.Response(
        status,
        headers={"x-litellm-response-cost": "0.00003", **(headers or {})},
        stream=Bytes(raw if raw is not None else json.dumps(body).encode()),
    )


@asynccontextmanager
async def server(case, handler):
    prepared = await prepare(case, "planning", case["frozen"].planning_allowance)
    assert prepared.kind == "applied"
    models = ServerModels(
        "https://gateway.invalid",
        case["keys"],
        ModelCallStore(case["factory"]),
        case["gateway"],
        TrustedPolicy.parse(case["native"].policy),
        case["clock"],
        transport=httpx.MockTransport(handler),
    )
    model = models.client(
        lambda: case["lease"],
        alias=prepared.value.alias,
        model=case["frozen"].model,
        purpose="planning",
    )
    inputs = dict(
        model=case["frozen"].model,
        system="system",
        prompt="prompt",
        credential_alias=prepared.value.alias,
        max_output_tokens=100,
    )
    try:
        yield models, model, inputs
    finally:
        await models.aclose()


async def test_server_http_request_has_live_scope_and_durable_unknown_receipt_before_send(case):
    calls = []

    async def native(request):
        async with case["factory"]() as session:
            rows = (await session.scalars(select(EngineUsage))).all()
            assert len(rows) == 1 and rows[0].completeness == "unknown" and not rows[0].final
            row = await case["keys"].get(rows[0].alias)
            assert request.headers["authorization"] == "Bearer " + case["keys"].reveal(row)
            assert "synthetic-admin" not in request.headers["authorization"]
        assert request.url == "https://gateway.invalid/v1/chat/completions"
        body = json.loads(request.content)
        assert body["messages"] == [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "prompt"},
        ]
        assert body["num_retries"] == body["max_retries"] == 0 and body["stream"] is False
        calls.append(request)
        return wire()

    async with server(case, native) as (models, client, inputs):
        assert models.minimum(case["frozen"].model) == Resources(
            usd=Decimal("0.72"), calls=1, tokens=1
        )
        reply = await client.complete(**inputs)
        assert reply.text == "answer" and reply.usage.usd == Decimal("0.00003")
        assert reply.usage.calls == 1 and reply.usage.tokens == 14
    async with case["factory"]() as session:
        row = await session.scalar(select(EngineUsage))
        assert row.final and row.completeness == "exact" and row.usd == Decimal("0.00003")
        assert "sk-forge" not in row.receipt_json + reply.evidence
    assert len(calls) == 1


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
async def test_server_ambiguous_or_refused_http_never_retries_or_credits_missing_usage(
    case, status
):
    requests = []

    def native(request):
        requests.append(request)
        return wire(status=status, headers={"location": "https://alternate.invalid"})

    async with server(case, native) as (_, client, inputs):
        with pytest.raises(ModelUnavailable):
            await client.complete(**inputs)
    async with case["factory"]() as session:
        row = await session.scalar(select(EngineUsage))
        assert not row.final and row.usd is row.calls is row.tokens is None
    assert len(requests) == 1


@pytest.mark.parametrize(
    "headers",
    [
        {"x-litellm-response-cost": "NaN"},
        {"x-litellm-response-cost": "0"},
        {"x-litellm-response-cost": "unavailable"},
    ],
)
async def test_server_missing_or_invalid_price_is_unknown_while_known_counts_survive(case, headers):
    async with server(case, lambda request: wire(headers=headers)) as (_, client, inputs):
        reply = await client.complete(**inputs)
        assert reply.usage.usd is None and reply.usage.calls == 1 and reply.usage.tokens == 14
    async with case["factory"]() as session:
        row = await session.scalar(select(EngineUsage))
        assert not row.final and row.usd is None and row.calls == 1 and row.tokens == 14


async def test_server_native_float_price_is_rounded_up_to_exact_storage_precision(case):
    async with server(
        case, lambda request: wire(headers={"x-litellm-response-cost": "0.0048679999999999995"})
    ) as (_, client, inputs):
        reply = await client.complete(**inputs)
        assert reply.usage.usd == Decimal("0.004868000000000000")


@pytest.mark.parametrize(
    "raw",
    [
        b'{"choices":[],"choices":[]}',
        b'{"choices":NaN}',
        b"x" * 1048577,
        b'{"choices":[42]}',
        b'{"choices":[{"finish_reason":"length","message":{"role":"assistant","content":"partial"}}]}',
    ],
)
async def test_server_rejects_malformed_duplicate_nonfinite_oversize_or_incomplete_model_output(
    case, raw
):
    async with server(case, lambda request: wire(raw=raw)) as (_, client, inputs):
        with pytest.raises(ModelUnavailable):
            await client.complete(**inputs)


async def test_server_lease_source_observes_renewal_before_every_paid_call(case):
    steps = StepStore(case["factory"])
    case["lease"] = await steps.renew(case["lease"], now=case["clock"].now(), lease_seconds=30)
    async with server(case, lambda request: wire()) as (_, client, inputs):
        await client.complete(**inputs)
        case["clock"].instant += timedelta(seconds=20)
        case["lease"] = await steps.renew(case["lease"], now=case["clock"].now(), lease_seconds=60)
        case["clock"].instant += timedelta(seconds=15)
        await client.complete(**inputs)
    async with case["factory"]() as session:
        rows = (await session.scalars(select(EngineUsage))).all()
        assert len(rows) == 2 and all(row.final for row in rows)


async def test_server_cancelled_or_cross_scoped_request_never_reaches_model_http(case):
    requests = []
    async with server(case, lambda request: requests.append(request) or wire()) as (
        _,
        client,
        inputs,
    ):
        with pytest.raises((BudgetExhausted, StoreConflict)):
            await client.complete(**{**inputs, "credential_alias": "other"})
        await case["service"].store.load(case["run"].id, "implementation")
        from forge.store.runs import RunStore

        await RunStore(case["factory"]).request_cancel(case["run"].id, expected_version=1)
        with pytest.raises(StaleLease):
            await client.complete(**inputs)
    assert requests == []
