"""Real scoped server calls and independent PostgreSQL sessions for retained amendment pins."""

from contextlib import asynccontextmanager
from decimal import Decimal
from uuid import uuid4

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import text

from forge.budget.server_models import ServerModels
from forge.budget.strict import TrustedPolicy
from forge.domain.budget import Resources
from forge.domain.states import Handler
from forge.store.base import Base
from forge.store.budget_calls import ModelCallStore
from forge.store.records import StepRequest
from forge.store.steps import StepStore
from tests.test_budget_engine import prepare
from tests.test_server_models import wire


async def closing_grant(case, *, calls=1, tokens=1000):
    steps = StepStore(case["factory"])
    await steps.complete_and_enqueue(
        case["lease"],
        "{}",
        (StepRequest(case["run"].id, Handler.REVIEW, "closing", "{}", case["clock"].now()),),
        now=case["clock"].now(),
    )
    (case["lease"],) = await steps.claim_due("closing", case["clock"].now(), 600)
    return await prepare(case, "closing", Resources(usd=Decimal("2"), calls=calls, tokens=tokens))


@asynccontextmanager
async def model_client(case, alias, *, fail=False):
    requests = []

    def transport(request):
        requests.append(request)
        if fail:
            raise httpx.ReadTimeout("synthetic lost model response", request=request)
        return wire()

    models = ServerModels(
        "https://gateway.invalid",
        case["keys"],
        ModelCallStore(case["factory"]),
        case["gateway"],
        TrustedPolicy.parse(case["native"].policy),
        case["clock"],
        transport=httpx.MockTransport(transport),
    )
    try:
        client = models.client(
            lambda: case["lease"], alias=alias, model="test-model", purpose="closing"
        )
        yield client, requests
    finally:
        await models.aclose()


async def call(client, alias):
    return await client.complete(
        model="test-model", system="s", prompt="u", credential_alias=alias, max_output_tokens=1
    )


async def rows(case, model):
    from sqlalchemy import select

    async with case["factory"]() as session:
        return list((await session.scalars(select(model))).all())


async def amend(
    case,
    command="native:42:1",
    amount=None,
    *,
    purpose="implementation",
    reason="authorized review",
    actor="human:alice",
):
    return await case["service"].amend(
        case["lease"], purpose, command, amount or Resources(calls=1), actor, reason
    )


@asynccontextmanager
async def postgres_case(url):
    # A unique schema is owned by this proof, including under a shared CI test database.
    schema = "forge_amend_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text('CREATE SCHEMA "' + schema + '"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        await admin.dispose()
