"""Committed effect intents, fenced dispatch and monotonic unfenced outcomes."""

from datetime import datetime, timezone
import json
from typing import cast
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from forge_protocol import LaneDispatch
from forge.port_values import RepoRef
from forge.store.dispatch_intents import derive_nonce, nonce_digest, read_dispatch

from forge.store.models_execution import EngineEffect
from forge.store.models_runs import EngineRun
from forge.store.records import (
    EFFECT_STATES,
    KNOWN_OUTCOMES,
    EffectRecord,
    EffectState,
    Lease,
    ObservationState,
    ProbeState,
    StoreConflict,
)
from forge.store.transactions import (
    SessionFactory,
    canonical_json,
    fence,
    payload_digest as digest_of,
    transaction,
    stored_time,
)


def effect_record(row: EngineEffect) -> EffectRecord:
    if row.state not in EFFECT_STATES:
        raise StoreConflict("unrecognized persisted effect state")
    return EffectRecord(
        row.id,
        row.key,
        row.kind,
        row.run_id,
        row.step_id,
        row.lease_epoch,
        row.payload_digest,
        row.payload_json,
        cast(EffectState, row.state),
        row.version,
        row.outcome_json,
        tuple(json.loads(row.observations_json)),
        row.repo_key,
        row.head_branch,
        stored_time(row.dispatched_at) if row.dispatched_at is not None else None,
        stored_time(row.dispatch_deadline_at) if row.dispatch_deadline_at is not None else None,
        row.command_id,
    )


def append_observation(
    row: EngineEffect, *, state: str, evidence_json: str, outcome_json: str | None
) -> None:
    observation = json.dumps(
        {
            "state": state,
            "evidence": json.loads(evidence_json),
            "outcome": json.loads(outcome_json) if outcome_json is not None else None,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    observations: list[str] = json.loads(row.observations_json)
    if observation not in observations:
        observations.append(observation)
        row.observations_json = json.dumps(observations, separators=(",", ":"), ensure_ascii=False)


class EffectStore:
    def __init__(self, factory: SessionFactory) -> None:
        self.factory = factory

    async def request_once(
        self,
        lease: Lease,
        *,
        key: str,
        kind: str,
        payload_json: str,
        payload_digest: str,
        repo_key: str | None = None,
        head_branch: str | None = None,
    ) -> EffectRecord:
        if (
            not key
            or len(key) > 300
            or not kind
            or len(kind) > 100
            or digest_of(payload_json) != payload_digest
        ):
            raise StoreConflict("invalid effect identity or canonical payload digest")
        if kind == "ensure_draft" and (not repo_key or not head_branch):
            raise StoreConflict("draft effect requires canonical repository and head branch")
        try:
            async with transaction(self.factory) as session:
                await fence(session, lease, datetime.now(timezone.utc))
                nonce = (
                    await derive_nonce(session, lease, payload_json)
                    if kind == "dispatch_lane"
                    else None
                )
                row = await session.scalar(
                    select(EngineEffect).where(EngineEffect.key == key).with_for_update()
                )
                if row is not None:
                    require_run_owner(row)
                    same_root = kind == "ensure_draft" and await same_lineage(
                        session, row.run_id, lease.run_id
                    )
                    if (
                        row.kind,
                        row.payload_digest,
                        row.payload_json,
                        row.repo_key,
                        row.head_branch,
                        row.dispatch_nonce_digest,
                    ) != (kind, payload_digest, payload_json, repo_key, head_branch, nonce) or (
                        row.run_id != lease.run_id and not same_root
                    ):
                        raise StoreConflict("effect key reused with different frozen inputs")
                    return effect_record(row)
                row = EngineEffect(
                    id=uuid4().hex,
                    key=key,
                    kind=kind,
                    run_id=lease.run_id,
                    step_id=lease.step_id,
                    lease_epoch=lease.epoch,
                    payload_digest=payload_digest,
                    payload_json=payload_json,
                    repo_key=repo_key,
                    head_branch=head_branch,
                    dispatch_nonce_digest=nonce,
                )
                session.add(row)
                await session.flush()
                return effect_record(row)
        except IntegrityError as exc:
            existing = await self.get(key)
            if existing is not None:
                return await self.request_once(
                    lease,
                    key=key,
                    kind=kind,
                    payload_json=payload_json,
                    payload_digest=payload_digest,
                    repo_key=repo_key,
                    head_branch=head_branch,
                )
            raise StoreConflict(
                "effect key or repository/head draft identity already claimed"
            ) from exc

    async def mark_dispatched(
        self, effect_id: str, lease: Lease, *, expected_version: int
    ) -> EffectRecord:
        async with transaction(self.factory) as session:
            await fence(session, lease, datetime.now(timezone.utc))
            row = await self._locked(session, effect_id)
            if row.command_id is not None:
                raise StoreConflict("run step cannot dispatch a command-owned effect")
            if row.version != expected_version or row.state != "requested":
                raise StoreConflict("effect dispatch version or state changed")
            same_owner = row.run_id == lease.run_id and row.step_id == lease.step_id
            if not same_owner and row.kind == "ensure_draft":
                same_owner = await same_lineage(session, row.run_id, lease.run_id)
            if not same_owner:
                raise StoreConflict("effect belongs to a different logical step or lineage")
            row.step_id, row.run_id = lease.step_id, lease.run_id
            row.lease_epoch, row.state = lease.epoch, "dispatched"
            row.dispatched_at = datetime.now(timezone.utc)
            row.dispatch_deadline_at = lease.expires_at
            row.version += 1
            await session.flush()
            return effect_record(row)

    async def record_observation(
        self, effect_id: str, *, state: ObservationState, outcome_json: str
    ) -> EffectRecord:
        canonical_json(outcome_json)
        if state not in {"committed", "conflict", "failed", "unknown"}:
            raise ValueError("invalid observed outcome state")
        async with transaction(self.factory) as session:
            row = await self._locked(session, effect_id)
            append_observation(
                row, state=state, evidence_json=outcome_json, outcome_json=outcome_json
            )
            if row.state not in KNOWN_OUTCOMES:
                if row.state == "requested" and row.dispatched_at is None:
                    raise StoreConflict("effect has not been dispatched")
                row.state, row.outcome_json = state, outcome_json
            row.version += 1
            await session.flush()
            return effect_record(row)

    async def resolve_probe(
        self,
        effect_id: str,
        *,
        expected_version: int,
        state: ProbeState,
        evidence_json: str,
        outcome_json: str | None = None,
    ) -> EffectRecord:
        canonical_json(evidence_json)
        if outcome_json is not None:
            canonical_json(outcome_json)
        if state not in {"adopted", "unknown", "requested"} or (
            state == "adopted" and outcome_json is None
        ):
            raise ValueError("invalid probe resolution")
        async with transaction(self.factory) as session:
            row = await self._locked(session, effect_id)
            if row.version != expected_version:
                raise StoreConflict("effect probe version changed")
            append_observation(
                row, state=state, evidence_json=evidence_json, outcome_json=outcome_json
            )
            if row.state not in KNOWN_OUTCOMES:
                if row.state not in {"dispatched", "unknown"}:
                    raise StoreConflict("effect is not awaiting a probe")
                row.state, row.outcome_json = state, outcome_json
            row.version += 1
            await session.flush()
            return effect_record(row)

    async def get(self, key: str) -> EffectRecord | None:
        async with self.factory() as session:
            row = await session.scalar(select(EngineEffect).where(EngineEffect.key == key))
            return effect_record(row) if row is not None else None

    async def dispatch_for_nonce(self, repo: RepoRef, nonce: str) -> LaneDispatch | None:
        async with self.factory() as session:
            row = await session.scalar(
                select(EngineEffect).where(
                    EngineEffect.dispatch_nonce_digest == nonce_digest(nonce)
                )
            )
            return await read_dispatch(session, row, repo, nonce) if row is not None else None

    async def unresolved(self, limit: int = 100) -> tuple[EffectRecord, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("unresolved scan limit out of bounds")
        async with self.factory() as session:
            rows = (
                await session.scalars(
                    select(EngineEffect)
                    .where(EngineEffect.state.in_(("requested", "dispatched", "unknown")))
                    .order_by(EngineEffect.updated_at, EngineEffect.id)
                    .limit(limit)
                )
            ).all()
            return tuple(effect_record(row) for row in rows)

    @staticmethod
    async def _locked(session: AsyncSession, effect_id: str) -> EngineEffect:
        row = await session.scalar(
            select(EngineEffect).where(EngineEffect.id == effect_id).with_for_update()
        )
        if row is None:
            raise StoreConflict("effect does not exist")
        return row


def require_run_owner(row: EngineEffect) -> None:
    if row.command_id is not None:
        raise StoreConflict("run step cannot acquire command-owned effect authority")


async def same_lineage(session: AsyncSession, original_id: str | None, current_id: str) -> bool:
    original = await session.get(EngineRun, original_id) if original_id is not None else None
    current = await session.get(EngineRun, current_id)
    return (
        original is not None and current is not None and original.root_run_id == current.root_run_id
    )
