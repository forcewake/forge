"""Prepared plans and atomic gate commits, with no provider calls in transactions."""

from datetime import datetime, timedelta
import json
from uuid import UUID, uuid4, uuid5

from pydantic import TypeAdapter
from forge_protocol import digest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.approved_input import ApprovedInput
from forge.domain.run_spec import RunSpec2
from forge.domain.states import State
from forge.port_values import Applied, CommentOutcome, SubjectRef
from forge.store.approval_values import (
    ApprovalCommit,
    ApprovalDecision,
    ConsumeGate,
    OpenGate,
    PreparedPlan,
)
from forge.store.models_execution import EngineEffect
from forge.store.models_runs import EngineApprovedInput, EngineGate, EngineRun, EngineRunSpec
from forge.store.records import Lease, StoreConflict
from forge.store.transactions import (
    SessionFactory,
    aware,
    fence,
    locked_run,
    stored_time,
    transaction,
)


async def load_prepared(session: AsyncSession, run_id: str) -> PreparedPlan | None:
    spec_row = await session.scalar(select(EngineRunSpec).where(EngineRunSpec.run_id == run_id))
    gate = await session.scalar(
        select(EngineGate).where(EngineGate.run_id == run_id, EngineGate.decision_key == "initial")
    )
    if spec_row is None or gate is None:
        return None
    spec = RunSpec2.model_validate_json(spec_row.document_json)
    if spec.spec_json != spec_row.document_json or spec.digest != spec_row.digest:
        raise StoreConflict("frozen specification is inconsistent")
    return PreparedPlan(run_id, gate.id, spec, stored_time(gate.expires_at), gate.state)


class ApprovalStore:
    def __init__(self, factory: SessionFactory) -> None:
        self.factory = factory

    async def load(self, run_id: str) -> PreparedPlan | None:
        async with self.factory() as session:
            return await load_prepared(session, run_id)

    async def prepare(
        self, lease: Lease, spec: RunSpec2, *, ttl_seconds: int, now: datetime
    ) -> PreparedPlan:
        if not 0 < ttl_seconds <= 86400:
            raise ValueError("approval lifetime must be bounded")
        async with transaction(self.factory) as session:
            await fence(session, lease, now)
            run = await locked_run(session, lease.run_id)
            if run.state != State.PLANNING.value:
                raise StoreConflict("only planning may prepare input")
            existing = await load_prepared(session, run.id)
            if existing is not None:
                if existing.spec != spec:
                    raise StoreConflict("frozen specification changed")
                return existing
            spec_row = await session.scalar(
                select(EngineRunSpec).where(EngineRunSpec.run_id == run.id)
            )
            if spec_row is not None:
                raise StoreConflict("specification exists without its prepared gate")
            gate_id = uuid5(UUID(run.id), "approval:" + spec.digest).hex
            expires = aware(now) + timedelta(seconds=ttl_seconds)
            session.add(
                EngineRunSpec(
                    id=uuid4().hex, run_id=run.id, digest=spec.digest, document_json=spec.spec_json
                )
            )
            session.add(
                EngineGate(
                    id=gate_id,
                    run_id=run.id,
                    decision_key="initial",
                    state="prepared",
                    approved_digest=ApprovedInput.from_spec(spec).approved_digest,
                    actor_scope_json=run.subject_key,
                    expires_at=expires,
                )
            )
            await session.flush()
            return PreparedPlan(run.id, gate_id, spec, expires, "prepared")


async def apply_approval_commit(
    session: AsyncSession, lease: Lease, commit: ApprovalCommit, now: datetime
) -> ApprovalDecision:
    run = await locked_run(session, lease.run_id)
    prepared = await load_prepared(session, run.id)
    if prepared is None:
        raise StoreConflict("prepared input missing")
    gate_id = commit.gate_id if isinstance(commit, OpenGate) else commit.claim.gate_id
    gate = await session.scalar(
        select(EngineGate).where(EngineGate.id == gate_id).with_for_update()
    )
    if (
        gate is None
        or gate.id != prepared.gate_id
        or gate.run_id != run.id
        or gate.actor_scope_json != run.subject_key
    ):
        return ApprovalDecision(False, "gate_scope_mismatch")
    if gate.state == "consumed":
        return ApprovalDecision(False, "gate_already_consumed")
    if stored_time(gate.expires_at) <= aware(now):
        if gate.state in {"prepared", "open"}:
            gate.state = "expired"
        return ApprovalDecision(False, "approval_expired", block=True)
    approved = prepared.approved
    if gate.approved_digest != approved.approved_digest:
        raise StoreConflict("gate disagrees with immutable input")
    if isinstance(commit, OpenGate):
        return await open_gate(session, run, gate, prepared, commit)
    return await consume_gate(session, lease, run, gate, approved, commit, now)


async def open_gate(
    session: AsyncSession,
    run: EngineRun,
    gate: EngineGate,
    prepared: PreparedPlan,
    commit: OpenGate,
) -> ApprovalDecision:
    if run.state != State.PLANNING.value or gate.state != "prepared":
        return ApprovalDecision(False, "gate_not_prepared")
    effect = await session.scalar(
        select(EngineEffect).where(EngineEffect.key == commit.comment_effect_key)
    )
    if (
        effect is None
        or effect.run_id != run.id
        or effect.kind != "post_comment"
        or effect.state not in {"committed", "adopted"}
        or effect.outcome_json is None
    ):
        return ApprovalDecision(False, "plan_comment_unproven", block=True)
    codec: TypeAdapter[CommentOutcome] = TypeAdapter(CommentOutcome)
    try:
        outcome = codec.validate_json(effect.outcome_json)
        payload = json.loads(effect.payload_json)
        if not isinstance(payload, dict):
            return ApprovalDecision(False, "plan_comment_unproven", block=True)
        payload_subject = SubjectRef.model_validate(payload.get("subject"))
    except (ValueError, TypeError):
        return ApprovalDecision(False, "plan_comment_unproven", block=True)
    if (
        not isinstance(outcome, Applied)
        or digest(payload) != effect.payload_digest
        or payload.get("body") != prepared.comment
        or payload.get("marker") != prepared.marker
        or payload_subject != outcome.value.subject
    ):
        return ApprovalDecision(False, "plan_comment_unproven", block=True)
    subject = outcome.value.subject
    if (
        subject.kind,
        str(subject.number),
        subject.repo.identity.provider,
        subject.repo.identity.connection_id,
        subject.repo.identity.repository_id,
    ) != (run.subject_kind, run.subject_id, run.provider, run.connection_id, run.repository_id):
        return ApprovalDecision(False, "plan_comment_scope", block=True)
    gate.state = "open"
    return ApprovalDecision(True)


async def consume_gate(
    session: AsyncSession,
    lease: Lease,
    run: EngineRun,
    gate: EngineGate,
    approved: ApprovedInput,
    commit: ConsumeGate,
    now: datetime,
) -> ApprovalDecision:
    claim = commit.claim
    if run.state != State.AWAITING_APPROVAL.value or gate.state != "open":
        return ApprovalDecision(
            False, "gate_already_consumed" if gate.state == "consumed" else "gate_not_open"
        )
    if claim.actor.is_bot or (claim.actor.provider, claim.actor.connection_id) != (
        run.provider,
        run.connection_id,
    ):
        return ApprovalDecision(False, "actor_scope_mismatch")
    if (
        claim.approved_digest,
        claim.plan_digest,
        claim.spec_digest,
        claim.base_sha,
        claim.revision,
    ) != (
        approved.approved_digest,
        approved.plan_digest,
        approved.spec_digest,
        approved.base_sha,
        approved.revision,
    ):
        return ApprovalDecision(False, "approval_input_mismatch")
    if commit.observed_base != approved.base_sha:
        return ApprovalDecision(False, "approval_base_drift", block=True)
    existing = await session.scalar(
        select(EngineApprovedInput).where(
            EngineApprovedInput.run_id == run.id, EngineApprovedInput.revision == approved.revision
        )
    )
    if existing is not None:
        raise StoreConflict("approval revision exists before gate consumption")
    session.add(
        EngineApprovedInput(
            id=uuid4().hex,
            run_id=run.id,
            revision=approved.revision,
            digest=approved.approved_digest,
            spec_digest=approved.spec_digest,
            base_sha=approved.base_sha,
            document_json=approved.canonical_bytes.decode(),
        )
    )
    gate.state, gate.consumed_at, gate.consumed_by = "consumed", aware(now), claim.actor.digest
    gate.consumed_step_id = lease.step_id
    return ApprovalDecision(True)
