"""Exercise the callback's exact SQL on an explicitly supplied disposable PostgreSQL DB."""

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
import hashlib
import os
from pathlib import Path
import sys
from uuid import uuid4

import asyncpg

sys.path.insert(0, str(Path(__file__).with_name("litellm")))
from guard_ledger import NativeLedger  # noqa: E402
from guard_policy import BudgetDenied  # noqa: E402


class Transaction:
    def __init__(self, connection):
        self.connection = connection

    async def execute_raw(self, sql, *parameters):
        return await self.connection.execute(sql, *parameters)

    async def query_raw(self, sql, *parameters):
        return [dict(row) for row in await self.connection.fetch(sql, *parameters)]


class Database:
    """Only adapts Prisma's raw-query/transaction surface to the test transport."""

    def __init__(self, pool):
        self.pool = pool

    @asynccontextmanager
    async def tx(self):
        async with self.pool.acquire() as connection, connection.transaction():
            yield Transaction(connection)

    async def execute_raw(self, sql, *parameters):
        async with self.pool.acquire() as connection:
            return await connection.execute(sql, *parameters)

    async def query_raw(self, sql, *parameters):
        async with self.pool.acquire() as connection:
            return [dict(row) for row in await connection.fetch(sql, *parameters)]


async def main(url):
    pool = await asyncpg.create_pool(
        url.replace("postgresql+asyncpg://", "postgresql://"), min_size=1, max_size=5
    )
    db = Database(pool)
    ledger = NativeLedger(db)
    await ledger.initialize()
    await db.execute_raw("""CREATE TABLE IF NOT EXISTS "LiteLLM_VerificationToken" (
        token TEXT PRIMARY KEY,key_alias TEXT,blocked BOOLEAN,expires TIMESTAMPTZ,max_budget DOUBLE PRECISION)""")

    async def native_key(token, alias):
        await db.execute_raw(
            """INSERT INTO "LiteLLM_VerificationToken" (token,key_alias,blocked,expires,max_budget)
            VALUES($1,$2,FALSE,CURRENT_TIMESTAMP+INTERVAL '1 hour',1)""",
            token,
            alias,
        )

    token = hashlib.sha256(uuid4().bytes).hexdigest()
    alias = "forge-guard-test-" + uuid4().hex
    requests = [uuid4().hex for _ in range(2)]
    await native_key(token, alias)
    try:
        outcomes = await asyncio.gather(
            *(
                ledger.reserve(token, alias, request, Decimal("1"), Decimal("0.7"))
                for request in requests
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(item, BudgetDenied) for item in outcomes) == 1
        assert sum(item is None for item in outcomes) == 1
        summary = await ledger.summary(token)
        assert Decimal(summary["held_usd"]) == Decimal("0.7") and summary["complete"] is False
        rows = await db.query_raw(
            "SELECT request_id FROM forge_budget_guard_holds WHERE token_digest=$1", token
        )
        request = rows[0]["request_id"]
        # A new adapter/process does not release an unknown dispatched reservation.
        restarted = NativeLedger(Database(pool))
        assert await restarted.summary(token) == summary
        try:
            await restarted.reserve(token, alias, request, Decimal("1"), Decimal("0.7"))
        except BudgetDenied:
            pass
        else:
            raise AssertionError("a replay was admitted")
        await ledger.settle(token, request, Decimal("0.2"), 1, 12)
        await ledger.settle(token, request, Decimal("0.2"), 1, 12)
        summary = await ledger.summary(token)
        assert Decimal(summary["held_usd"]) == 0 and Decimal(summary["settled_usd"]) == Decimal(
            "0.2"
        )
        assert summary["calls"] == 1 and summary["tokens"] == 12 and summary["complete"] is True
        await ledger.reserve(token, alias, uuid4().hex, Decimal("1"), Decimal("0.7"))
        exact_token = hashlib.sha256(uuid4().bytes).hexdigest()
        exact_alias = alias + "-exact"
        exact_request = uuid4().hex
        amount = Decimal("0.123456789012345678")
        await native_key(exact_token, exact_alias)
        await ledger.reserve(exact_token, exact_alias, exact_request, Decimal("1"), amount)
        assert Decimal((await ledger.summary(exact_token))["held_usd"]) == amount
        await ledger.settle(exact_token, exact_request, Decimal("0.023456789012345678"), 1, None)
        assert (await ledger.summary(exact_token))["tokens"] is None
        # An overrun is recorded as a conflict and prevents any later dispatch.
        conflict_token = hashlib.sha256(uuid4().bytes).hexdigest()
        conflict_request = uuid4().hex
        await native_key(conflict_token, alias + "-conflict")
        await ledger.reserve(
            conflict_token, alias + "-conflict", conflict_request, Decimal("1"), Decimal("0.2")
        )
        await ledger.settle(conflict_token, conflict_request, Decimal("0.3"), 1, 1)
        assert (await ledger.summary(conflict_token))["blocked"] is True
        try:
            await ledger.reserve(
                conflict_token, alias + "-conflict", uuid4().hex, Decimal("1"), Decimal("0.01")
            )
        except BudgetDenied:
            pass
        else:
            raise AssertionError("a blocked native account admitted another request")
        # An unreachable database cannot authorize an external request.
        await pool.close()
        try:
            await ledger.reserve(token, alias, uuid4().hex, Decimal("1"), Decimal("0.01"))
        except Exception:
            pass
        else:
            raise AssertionError("unavailable database admitted a request")
        print(
            "Native callback SQL passed: concurrent cap, replay, restart/unknown hold, exact money, idempotent settlement, partial counts, overrun block and database refusal"
        )
    finally:
        if not pool.is_closing():
            await pool.close()


if __name__ == "__main__":
    asyncio.run(main(os.environ["FORGE_BUDGET_GUARD_TEST_URL"]))
