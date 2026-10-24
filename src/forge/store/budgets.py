"""Run-locked cumulative pools, durable amendment intents and usage receipts."""

from datetime import datetime
import json
from typing import Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.budget import BudgetConflict, Observation, Pool, Reservation, Resources, partition
from forge.domain.run_spec import RunPolicy, RunSpec2
from forge.store.models_budget import (
    EngineBudgetAmendment,
    EngineBudgetPool,
    EngineBudgetReservation,
    EngineUsage,
)
from forge.store.models_runs import EngineRunSpec
from forge.store.models_execution import EngineStep
from forge.store.records import Lease, StoreConflict
from forge.store.transactions import (
    SessionFactory,
    fence,
    locked_run,
    transaction,
    canonical_json,
    payload_digest,
)

PoolPurpose = Literal["implementation", "closing"]


class BudgetStore:
    def __init__(self, factory: SessionFactory) -> None:
        self.factory = factory

    async def initialize(self, lease: Lease, spec: RunPolicy, *, now: datetime) -> None:
        pools = partition(spec.limits, spec.closing_reserve)
        async with transaction(self.factory) as session:
            await fence(session, lease, now)
            policy = await self._policy(session, lease.run_id)
            supplied = RunPolicy.model_validate_json(
                json.dumps(
                    {name: spec.model_dump(mode="json")[name] for name in RunPolicy.model_fields}
                )
            )
            if policy != supplied:
                raise StoreConflict("budget initialization requires the exact persisted policy")
            for purpose, capacity in (
                ("implementation", pools.implementation),
                ("closing", pools.closing),
            ):
                existing = await session.scalar(
                    select(EngineBudgetPool).where(
                        EngineBudgetPool.run_id == lease.run_id, EngineBudgetPool.purpose == purpose
                    )
                )
                if existing is not None:
                    if Pool.model_validate_json(existing.state_json).capacity != capacity:
                        raise StoreConflict("cumulative pool cannot be reset")
                    continue
                pool = Pool(identity=f"{lease.run_id}:{purpose}", capacity=capacity)
                session.add(
                    EngineBudgetPool(
                        id=uuid4().hex,
                        run_id=lease.run_id,
                        purpose=purpose,
                        usd=capacity.usd,
                        calls=capacity.calls,
                        tokens=capacity.tokens,
                        state_json=pool.model_dump_json(),
                    )
                )

    @staticmethod
    async def _policy(session: AsyncSession, run_id: str) -> RunPolicy:
        frozen = await session.scalar(select(EngineRunSpec).where(EngineRunSpec.run_id == run_id))
        if frozen is not None:
            spec = RunSpec2.model_validate_json(frozen.document_json)
            if spec.digest != frozen.digest or spec.spec_json != frozen.document_json:
                raise StoreConflict("persisted run specification is not canonical or digest-bound")
            document = {name: spec.model_dump(mode="json")[name] for name in RunPolicy.model_fields}
            return RunPolicy.model_validate_json(json.dumps(document))
        from forge.review.native_values import NativeReviewPolicy
        from forge.store.models_review import EngineReactiveReview

        review = await session.scalar(
            select(EngineReactiveReview).where(EngineReactiveReview.run_id == run_id)
        )
        if review is not None:
            policy = NativeReviewPolicy.model_validate_json(review.policy_json)
            if policy.canonical_bytes.decode() != review.policy_json:
                raise StoreConflict("native review policy is not canonical")
            return policy.budget
        rows = await session.scalars(
            select(EngineStep)
            .where(EngineStep.run_id == run_id, EngineStep.handler == "plan")
            .order_by(EngineStep.created_at)
        )
        policies = []
        for row in rows:
            body = json.loads(canonical_json(row.payload_json))
            if isinstance(body, dict) and "policy" in body:
                policies.append(RunPolicy.model_validate_json(json.dumps(body["policy"])))
        if not policies or any(item != policies[0] for item in policies):
            raise StoreConflict("planning requires one immutable persisted admission policy")
        return policies[0]

    @staticmethod
    async def _pool(session: AsyncSession, run_id: str, purpose: PoolPurpose) -> EngineBudgetPool:
        row = await session.scalar(
            select(EngineBudgetPool).where(
                EngineBudgetPool.run_id == run_id, EngineBudgetPool.purpose == purpose
            )
        )
        if row is None:
            raise StoreConflict("cumulative pool is not initialized")
        return row

    async def load(self, run_id: str, purpose: PoolPurpose) -> Pool:
        async with self.factory() as session:
            return Pool.model_validate_json((await self._pool(session, run_id, purpose)).state_json)

    @staticmethod
    async def _save(session: AsyncSession, row: EngineBudgetPool, pool: Pool) -> None:
        row.state_json, row.version = pool.model_dump_json(), row.version + 1
        for reservation in pool.reservations:
            item = await session.scalar(
                select(EngineBudgetReservation).where(
                    EngineBudgetReservation.pool_id == row.id,
                    EngineBudgetReservation.reservation_key == reservation.identity,
                )
            )
            if item is None:
                item = EngineBudgetReservation(
                    id=uuid4().hex, pool_id=row.id, reservation_key=reservation.identity, version=1
                )
                session.add(item)
            else:
                item.version += 1
            item.requested_json = reservation.requested.model_dump_json()
            item.minimum_json = reservation.minimum.model_dump_json()
            item.allowance_json = reservation.allowance.model_dump_json()
            item.state = "reserved" if reservation.status == "open" else reservation.status
            seen = reservation.observed
            item.observed_usd = None if seen is None else seen.usd
            item.observed_calls = None if seen is None else seen.calls
            item.observed_tokens = None if seen is None else seen.tokens

    async def reserve(
        self,
        lease: Lease,
        purpose: PoolPurpose,
        key: str,
        requested: Resources,
        minimum: Resources,
        *,
        planning: bool = False,
        now: datetime,
    ) -> Reservation:
        if planning and purpose != "implementation":
            raise ValueError("planning draws from implementation capacity")
        async with transaction(self.factory) as session:
            await fence(session, lease, now)
            row = await self._pool(session, lease.run_id, purpose)
            pool = Pool.model_validate_json(row.state_json)
            existing = next((item for item in pool.reservations if item.identity == key), None)
            if existing is not None:
                return await self._existing(session, row, existing, requested, minimum)
            original_request = requested
            if planning:
                limit = (await self._policy(session, lease.run_id)).planning_allowance
                prior = Resources()
                for item in pool.reservations:
                    if item.identity.startswith("planning:") and item.identity != key:
                        prior = prior + item.allowance.high_water(
                            item.observed.lower_bound if item.observed else Resources()
                        )
                requested = requested.bounded_by(limit.subtract(prior.bounded_by(limit)))
                if not key.startswith("planning:"):
                    raise ValueError("planning reservation needs its explicit purpose prefix")
            updated = pool.reserve(key, requested, minimum)
            if planning:
                updated = Pool(
                    identity=updated.identity,
                    capacity=updated.capacity,
                    amendments=updated.amendments,
                    reservations=tuple(
                        Reservation(
                            identity=item.identity,
                            requested=original_request,
                            allowance=item.allowance,
                            minimum=item.minimum,
                        )
                        if item.identity == key
                        else item
                        for item in updated.reservations
                    ),
                )
            await self._save(session, row, updated)
            return next(item for item in updated.reservations if item.identity == key)

    @staticmethod
    async def _existing(
        session: AsyncSession,
        row: EngineBudgetPool,
        existing: Reservation,
        requested: Resources,
        minimum: Resources,
    ) -> Reservation:
        amendments = await session.scalars(
            select(EngineBudgetAmendment).where(
                EngineBudgetAmendment.pool_id == row.id,
                EngineBudgetAmendment.reservation_key == existing.identity,
                EngineBudgetAmendment.state == "applied",
            )
        )
        original = existing.requested
        for amendment in amendments:
            original = original.subtract(Resources.model_validate_json(amendment.amount_json))
        if original != requested or existing.minimum != minimum:
            raise StoreConflict("reservation identity was reused with different input")
        return existing

    async def observe(
        self, run_id: str, purpose: PoolPurpose, key: str, usage: Observation | None, *, final: bool
    ) -> Pool:
        async with transaction(self.factory) as session:
            await locked_run(session, run_id)
            row = await self._pool(session, run_id, purpose)
            updated = Pool.model_validate_json(row.state_json).observe(key, usage, final=final)
            await self._save(session, row, updated)
            return updated

    async def authorize_amendment(
        self,
        lease: Lease,
        purpose: PoolPurpose,
        command: str,
        key: str,
        amount: Resources,
        actor: str,
        reason: str,
        *,
        now: datetime,
    ) -> Resources:
        if (
            amount == Resources()
            or not command
            or command.strip() != command
            or len(command) > 300
            or any(ord(character) < 32 for character in command)
            or not actor.strip()
            or len(actor) > 200
            or not reason.strip()
            or len(reason) > 300
        ):
            raise ValueError(
                "amendment requires bounded command identity, authority, reason and positive amount"
            )
        async with transaction(self.factory) as session:
            await fence(session, lease, now)
            row = await self._pool(session, lease.run_id, purpose)
            existing = await session.scalar(
                select(EngineBudgetAmendment).where(
                    EngineBudgetAmendment.run_id == lease.run_id,
                    EngineBudgetAmendment.command_key == command,
                )
            )
            if existing is not None:
                if (
                    existing.pool_id,
                    existing.reservation_key,
                    existing.amount_json,
                    existing.actor,
                    existing.reason,
                ) != (row.id, key, amount.model_dump_json(), actor, reason):
                    raise StoreConflict("amendment command changed")
                if existing.target_json is None:
                    raise StoreConflict("amendment predates authenticated target storage")
                return Resources.model_validate_json(existing.target_json)
            pending = await session.scalar(
                select(EngineBudgetAmendment).where(
                    EngineBudgetAmendment.pool_id == row.id,
                    EngineBudgetAmendment.reservation_key == key,
                    EngineBudgetAmendment.state.in_(("authorized", "unknown")),
                )
            )
            if pending is not None:
                raise StoreConflict("another amendment for this reservation is unresolved")
            pool = Pool.model_validate_json(row.state_json)
            reservation = next((item for item in pool.reservations if item.identity == key), None)
            if reservation is None or reservation.status != "open":
                raise StoreConflict("only an open gateway reservation may be amended")
            target = reservation.allowance + amount
            amendment = EngineBudgetAmendment(
                id=uuid4().hex,
                run_id=lease.run_id,
                pool_id=row.id,
                command_key=command,
                actor=actor,
                reason=reason,
                amount_json=amount.model_dump_json(),
                state="authorized",
                gateway_effect_key=f"{lease.run_id}:amend:{command}",
                reservation_key=key,
                target_json=target.model_dump_json(),
            )
            session.add(amendment)
            await self.stage_in(session, amendment)
            return target

    async def apply_amendment(self, run_id: str, command: str) -> None:
        async with transaction(self.factory) as session:
            await locked_run(session, run_id)
            amendment = await session.scalar(
                select(EngineBudgetAmendment).where(
                    EngineBudgetAmendment.run_id == run_id,
                    EngineBudgetAmendment.command_key == command,
                )
            )
            if amendment is None:
                raise StoreConflict("amendment was not authorized")
            await self.apply_in(session, amendment)

    @staticmethod
    async def apply_in(session: AsyncSession, amendment: EngineBudgetAmendment) -> None:
        if amendment.state == "applied":
            return
        await BudgetStore.stage_in(session, amendment)
        amendment.state = "applied"

    @staticmethod
    async def stage_in(session: AsyncSession, amendment: EngineBudgetAmendment) -> None:
        row = await session.get(EngineBudgetPool, amendment.pool_id)
        assert row is not None
        pool = Pool.model_validate_json(row.state_json)
        amount = Resources.model_validate_json(amendment.amount_json)
        if amendment.target_json is None:
            raise StoreConflict("amendment predates authenticated target storage")
        target = Resources.model_validate_json(amendment.target_json)
        previous = next(
            (item for item in pool.amendments if item.command_id == amendment.command_key), None
        )
        if previous is not None:
            if previous.amount != amount or not any(
                item.identity == amendment.reservation_key and item.allowance == target
                for item in pool.reservations
            ):
                raise BudgetConflict("staged amendment differs from its exact target")
            return
        rows = []
        for item in pool.reservations:
            if item.identity == amendment.reservation_key:
                if item.allowance + amount != target or item.status != "open":
                    raise BudgetConflict("amendment overlaps another unapplied update")
                item = Reservation(
                    identity=item.identity,
                    requested=item.requested + amount,
                    allowance=target,
                    minimum=item.minimum,
                    status=item.status,
                    observed=item.observed,
                )
            rows.append(item)
        updated = pool.amend(amendment.command_key, amount)
        updated = Pool(
            identity=pool.identity,
            capacity=pool.capacity,
            amendments=updated.amendments,
            reservations=tuple(rows),
        )
        await BudgetStore._save(session, row, updated)

    @staticmethod
    async def unstage_in(session: AsyncSession, amendment: EngineBudgetAmendment) -> None:
        row = await session.get(EngineBudgetPool, amendment.pool_id)
        assert row is not None
        pool = Pool.model_validate_json(row.state_json)
        amount = Resources.model_validate_json(amendment.amount_json)
        if not any(item.command_id == amendment.command_key for item in pool.amendments):
            return
        reservations = tuple(
            Reservation(
                identity=item.identity,
                requested=item.requested.subtract(amount),
                allowance=item.allowance.subtract(amount),
                minimum=item.minimum,
                status=item.status,
                observed=item.observed,
            )
            if item.identity == amendment.reservation_key
            else item
            for item in pool.reservations
        )
        restored = Pool(
            identity=pool.identity,
            capacity=pool.capacity,
            reservations=reservations,
            amendments=tuple(
                item for item in pool.amendments if item.command_id != amendment.command_key
            ),
        )
        await BudgetStore._save(session, row, restored)

    async def refuse_amendment(self, run_id: str, command: str) -> None:
        from pydantic import TypeAdapter
        from forge.port_values import GatewayOutcome, Refused
        from forge.store.models_execution import EngineEffect
        from forge.store.models_budget import EngineGatewayKey

        async with transaction(self.factory) as session:
            await locked_run(session, run_id)
            amendment = await session.scalar(
                select(EngineBudgetAmendment).where(
                    EngineBudgetAmendment.run_id == run_id,
                    EngineBudgetAmendment.command_key == command,
                )
            )
            if amendment is None or amendment.state == "applied":
                raise StoreConflict("amendment has no refundable pending allocation")
            effect = await session.scalar(
                select(EngineEffect).where(
                    EngineEffect.key == amendment.gateway_effect_key, EngineEffect.run_id == run_id
                )
            )
            if (
                effect is None
                or effect.kind != "gw_update"
                or effect.state != "failed"
                or effect.outcome_json is None
            ):
                raise StoreConflict("amendment has no exact native refusal")
            assert amendment.reservation_key is not None
            purpose, attempt, generation = amendment.reservation_key.split(":")
            alias = await session.scalar(
                select(EngineGatewayKey.alias).where(
                    EngineGatewayKey.run_id == run_id,
                    EngineGatewayKey.purpose == purpose,
                    EngineGatewayKey.attempt == int(attempt),
                    EngineGatewayKey.generation == int(generation),
                )
            )
            result: GatewayOutcome = TypeAdapter(GatewayOutcome).validate_json(effect.outcome_json)
            if (
                not isinstance(result, Refused)
                or alias is None
                or json.loads(effect.payload_json).get("alias") != alias
                or effect.payload_digest != payload_digest(effect.payload_json)
                or json.loads(effect.payload_json).get("allowance_json") != amendment.target_json
            ):
                raise StoreConflict("amendment has no exact native refusal")
            await self.unstage_in(session, amendment)
            amendment.state = "refused"

    async def record_unmetered(self, lease: Lease, source: str, *, now: datetime) -> None:
        async with transaction(self.factory) as session:
            await fence(session, lease, now)
            row = await session.scalar(select(EngineUsage).where(EngineUsage.source_key == source))
            if row is not None:
                if (row.run_id, row.attempt, row.generation, row.completeness) != (
                    lease.run_id,
                    lease.attempt,
                    lease.generation,
                    "unmetered",
                ):
                    raise StoreConflict("usage source changed")
                return
            session.add(
                EngineUsage(
                    id=uuid4().hex,
                    source_key=source,
                    run_id=lease.run_id,
                    attempt=lease.attempt,
                    generation=lease.generation,
                    completeness="unmetered",
                    final=True,
                    receipt_json='{"policy":"copilot-scoped-unmetered"}',
                )
            )
