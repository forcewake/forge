"""PostgreSQL authority for native request holds; unknowns have no TTL or refund."""

from decimal import Decimal, localcontext
import re
from typing import Any

try:
    from .guard_policy import BudgetDenied, money
except ImportError:
    from guard_policy import BudgetDenied, money

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS forge_budget_guard_accounts (
        token_digest TEXT PRIMARY KEY, alias TEXT UNIQUE NOT NULL,
        ceiling NUMERIC(38,18) NOT NULL CHECK (ceiling > 0),
        held NUMERIC(38,18) NOT NULL DEFAULT 0 CHECK (held >= 0),
        settled NUMERIC(38,18) NOT NULL DEFAULT 0 CHECK (settled >= 0),
        blocked BOOLEAN NOT NULL DEFAULT FALSE)""",
    """CREATE TABLE IF NOT EXISTS forge_budget_guard_holds (
        request_id TEXT PRIMARY KEY, token_digest TEXT NOT NULL REFERENCES forge_budget_guard_accounts(token_digest),
        reserved NUMERIC(38,18) NOT NULL CHECK (reserved > 0),
        actual NUMERIC(38,18), calls BIGINT, tokens BIGINT,
        state TEXT NOT NULL DEFAULT 'held' CHECK (state IN ('held','settled','conflict')),
        CHECK (actual IS NULL OR actual >= 0), CHECK (calls IS NULL OR calls >= 0),
        CHECK (tokens IS NULL OR tokens >= 0))""",
)


class NativeLedger:
    def __init__(self, database: Any) -> None:
        self.db = database

    async def initialize(self) -> None:
        for statement in SCHEMA:
            await self.db.execute_raw(statement)

    async def reserve(
        self, token: str, alias: str, request_id: str, ceiling: Decimal, amount: Decimal
    ) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", token) or not re.fullmatch(
            r"[0-9a-f]{32}", request_id
        ):
            raise BudgetDenied("invalid native reservation identity")
        ceiling, amount = money(ceiling), money(amount)
        if ceiling <= 0 or amount <= 0 or not alias or len(alias) > 200:
            raise BudgetDenied("positive bounded native allowance is required")
        async with self.db.tx() as tx:
            native = await tx.query_raw(
                """SELECT key_alias FROM "LiteLLM_VerificationToken"
                WHERE token=$1 AND (blocked IS NULL OR blocked=FALSE)
                AND expires>CURRENT_TIMESTAMP AND max_budget>=CAST(CAST($2 AS TEXT) AS DOUBLE PRECISION)
                FOR UPDATE""",
                token,
                str(ceiling),
            )
            if len(native) != 1 or native[0]["key_alias"] != alias:
                raise BudgetDenied("authoritative native key is blocked, expired or changed")
            await tx.execute_raw(
                """INSERT INTO forge_budget_guard_accounts(token_digest,alias,ceiling)
                VALUES($1,$2,CAST($3 AS NUMERIC)) ON CONFLICT(token_digest) DO NOTHING""",
                token,
                alias,
                str(ceiling),
            )
            rows = await tx.query_raw(
                "SELECT * FROM forge_budget_guard_accounts WHERE token_digest=$1 FOR UPDATE", token
            )
            if len(rows) != 1 or rows[0]["alias"] != alias or rows[0]["blocked"]:
                raise BudgetDenied("native account is not available")
            if ceiling < money(str(rows[0]["ceiling"])):
                raise BudgetDenied("native cap reduction requires an explicit blocked account")
            prior = await tx.query_raw(
                "SELECT * FROM forge_budget_guard_holds WHERE request_id=$1", request_id
            )
            if prior:
                if prior[0]["token_digest"] != token or money(str(prior[0]["reserved"])) != amount:
                    raise BudgetDenied("native request identity changed")
                raise BudgetDenied("native request was already dispatched or reserved")
            changed = await tx.query_raw(
                """UPDATE forge_budget_guard_accounts
                SET ceiling=CAST($2 AS NUMERIC),held=held+CAST($3 AS NUMERIC)
                WHERE token_digest=$1 AND NOT blocked
                AND settled+held+CAST($3 AS NUMERIC)<=CAST($2 AS NUMERIC) RETURNING token_digest""",
                token,
                str(ceiling),
                str(amount),
            )
            if not changed:
                raise BudgetDenied("native cumulative monetary reservation exhausted")
            await tx.execute_raw(
                """INSERT INTO forge_budget_guard_holds(request_id,token_digest,reserved)
                VALUES($1,$2,CAST($3 AS NUMERIC))""",
                request_id,
                token,
                str(amount),
            )

    async def settle(
        self, token: str, request_id: str, cost: Decimal, calls: int | None, tokens: int | None
    ) -> None:
        cost = money(cost)
        for value in (calls, tokens):
            if value is not None and (type(value) is not int or not 0 <= value <= 2**63 - 1):
                raise BudgetDenied("native usage count is not exact")
        async with self.db.tx() as tx:
            account = await tx.query_raw(
                "SELECT * FROM forge_budget_guard_accounts WHERE token_digest=$1 FOR UPDATE", token
            )
            rows = await tx.query_raw(
                "SELECT * FROM forge_budget_guard_holds WHERE request_id=$1", request_id
            )
            if len(account) != 1 or len(rows) != 1 or rows[0]["token_digest"] != token:
                raise BudgetDenied("native response has no authenticated hold")
            row = rows[0]
            if row["state"] == "settled":
                if (money(str(row["actual"])), row["calls"], row["tokens"]) != (
                    cost,
                    calls,
                    tokens,
                ):
                    raise BudgetDenied("native settled observation changed")
                return
            if row["state"] != "held" or cost > money(str(row["reserved"])):
                prior = max(money(str(row["reserved"])), money(str(row["actual"] or 0)))
                cost = max(cost, money(str(row["actual"] or 0)))
                with localcontext() as context:
                    context.prec = 80
                    extra = max(cost - prior, Decimal(0))
                await tx.execute_raw(
                    "UPDATE forge_budget_guard_accounts SET blocked=TRUE,held=held+CAST($2 AS NUMERIC) WHERE token_digest=$1",
                    token,
                    str(extra),
                )
                await tx.execute_raw(
                    "UPDATE forge_budget_guard_holds SET state='conflict',actual=CAST($2 AS NUMERIC) WHERE request_id=$1",
                    request_id,
                    str(cost),
                )
                return
            await tx.execute_raw(
                """UPDATE forge_budget_guard_accounts SET held=held-CAST($2 AS NUMERIC),settled=settled+CAST($3 AS NUMERIC)
                WHERE token_digest=$1""",
                token,
                str(row["reserved"]),
                str(cost),
            )
            await tx.execute_raw(
                """UPDATE forge_budget_guard_holds SET actual=CAST($2 AS NUMERIC),calls=$3,tokens=$4,state='settled'
                WHERE request_id=$1""",
                request_id,
                str(cost),
                calls,
                tokens,
            )

    async def summary(self, token: str) -> dict:
        accounts = await self.db.query_raw(
            "SELECT * FROM forge_budget_guard_accounts WHERE token_digest=$1", token
        )
        rows = await self.db.query_raw(
            "SELECT * FROM forge_budget_guard_holds WHERE token_digest=$1", token
        )
        if len(accounts) != 1:
            native = await self.db.query_raw(
                'SELECT blocked FROM "LiteLLM_VerificationToken" WHERE token=$1', token
            )
            if rows or len(native) != 1 or native[0]["blocked"] is not True:
                raise BudgetDenied("native account is unknown")
            # Every upstream call first commits an account and hold. Once the
            # authoritative key is blocked, their absence proves no dispatch.
            return {
                "settled_usd": "0",
                "held_usd": "0",
                "blocked": True,
                "complete": True,
                "calls": 0,
                "tokens": 0,
            }
        unknown = any(row["state"] != "settled" for row in rows)
        return {
            "settled_usd": str(accounts[0]["settled"]),
            "held_usd": str(accounts[0]["held"]),
            "blocked": accounts[0]["blocked"],
            "complete": not unknown,
            "calls": None
            if unknown or any(row["calls"] is None for row in rows)
            else sum(row["calls"] for row in rows),
            "tokens": None
            if unknown or any(row["tokens"] is None for row in rows)
            else sum(row["tokens"] for row in rows),
        }
