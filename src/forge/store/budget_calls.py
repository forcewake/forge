"""Per-request server model admission inside an already reserved gateway allowance."""

from datetime import datetime
import json
from uuid import uuid4

from sqlalchemy import select

from forge.domain.budget import BudgetExhausted, Observation, Resources
from forge.domain.states import Handler
from forge.store.models_budget import EngineGatewayKey, EngineUsage
from forge.store.records import Lease, StoreConflict
from forge.store.transactions import SessionFactory, fence, locked_run, stored_time, transaction
from forge.port_values import GatewayKeyRef


class ModelCallStore:
    def __init__(self, factory: SessionFactory) -> None:
        self.factory = factory

    async def admit(
        self, lease: Lease, alias: str, model: str, tokens: int, *, now: datetime
    ) -> str:
        requested = Resources(calls=1, tokens=tokens)
        purpose = {
            Handler.PLAN: "planning",
            Handler.REVIEW: "closing",
            Handler.FEEDBACK_REVIEW: "closing",
        }.get(lease.handler)
        async with transaction(self.factory) as session:
            await fence(session, lease, now)
            key = await session.scalar(
                select(EngineGatewayKey).where(EngineGatewayKey.alias == alias)
            )
            if (
                key is None
                or (key.run_id, key.attempt, key.generation)
                != (lease.run_id, lease.attempt, lease.generation)
                or key.state != "active"
                or stored_time(key.expires_at) <= now
                or purpose is None
                or key.purpose != purpose
            ):
                raise StoreConflict("server model key is not an active planning or closing grant")
            if key.reference_json is None:
                raise StoreConflict("gateway key has no authenticated reference")
            reference = GatewayKeyRef.model_validate_json(key.reference_json)
            if reference.model != model:
                raise StoreConflict("model differs from the frozen gateway grant")
            rows = await session.scalars(select(EngineUsage).where(EngineUsage.alias == alias))
            used = Resources()
            for row in rows:
                reserved = Resources.model_validate_json(
                    json.loads(row.receipt_json)["reserved_json"]
                )
                observed = Observation(usd=row.usd, calls=row.calls, tokens=row.tokens)
                counts = (
                    observed.lower_bound if row.final else reserved.high_water(observed.lower_bound)
                )
                used = used + Resources(calls=counts.calls, tokens=counts.tokens)
            capacity = Resources(calls=reference.allowance.calls, tokens=reference.allowance.tokens)
            if not capacity.covers(used + requested):
                raise BudgetExhausted("server model call exceeds its cumulative allowance")
            source = f"model-call:{uuid4().hex}"
            session.add(
                EngineUsage(
                    id=uuid4().hex,
                    source_key=source,
                    run_id=lease.run_id,
                    attempt=lease.attempt,
                    generation=lease.generation,
                    alias=alias,
                    completeness="unknown",
                    final=False,
                    receipt_json=json.dumps(
                        {"reserved_json": requested.model_dump_json()}, separators=(",", ":")
                    ),
                )
            )
            return source

    async def observe(self, source: str, usage: Observation | None) -> None:
        async with transaction(self.factory) as session:
            row = await session.scalar(select(EngineUsage).where(EngineUsage.source_key == source))
            if row is None:
                raise StoreConflict("model call was not admitted")
            await locked_run(session, row.run_id)
            await session.refresh(row)
            previous = Observation(usd=row.usd, calls=row.calls, tokens=row.tokens)
            if row.final and usage != previous:
                raise StoreConflict("final model usage cannot change")
            seen = previous if usage is None else previous.high_water(usage)
            row.usd, row.calls, row.tokens = seen.usd, seen.calls, seen.tokens
            row.final = seen.complete
            row.completeness = "exact" if row.final else "unknown" if usage is None else "partial"
