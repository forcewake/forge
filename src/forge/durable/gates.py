"""Consume one approval for one exact run and input digest."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from forge.durable.controller import RunNotFound, as_aware_utc
from forge.durable.models import FlowRun, GateApproval


class ControllerGateError(Exception):
    """Base class for gate errors."""


class GateNotFound(ControllerGateError):
    """The referenced gate approval does not exist."""


class GateAlreadyConsumed(ControllerGateError):
    """The gate approval has already been consumed and cannot be used again."""


async def record_approval(
    session: AsyncSession,
    *,
    flow_run_id: str,
    plan_digest: str,
    base_sha: str,
    policy_digest: str,
    approver_user_id: int,
    source_event_id: str,
    expires_at: datetime,
) -> GateApproval:
    """Persist a fresh, unconsumed gate approval."""
    if await session.get(FlowRun, flow_run_id) is None:
        raise RunNotFound(f"flow run {flow_run_id!r} not found")
    current = (
        await session.execute(
            select(func.max(GateApproval.generation)).where(GateApproval.flow_run_id == flow_run_id)
        )
    ).scalar_one()
    gate = GateApproval(
        flow_run_id=flow_run_id,
        generation=(current + 1) if current is not None else 0,
        plan_digest=plan_digest,
        base_sha=base_sha,
        policy_digest=policy_digest,
        approver_user_id=approver_user_id,
        source_event_id=source_event_id,
        expires_at=expires_at,
    )
    session.add(gate)
    await session.flush()
    return gate


async def consume_approval(session: AsyncSession, gate_id: int, now: datetime) -> GateApproval:
    """Consume a gate exactly once."""
    gate = await session.get(GateApproval, gate_id)
    if gate is None:
        raise GateNotFound(f"gate approval {gate_id} not found")
    result = await session.execute(
        update(GateApproval)
        .where(GateApproval.id == gate_id, GateApproval.consumed_at.is_(None))
        .values(consumed_at=now)
    )
    # rowcount is the conditional UPDATE's matched-row count; SQLAlchemy 2.0 stubs only type
    # it on CursorResult, so access it via the runtime attr.
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise GateAlreadyConsumed(f"gate approval {gate_id} was already consumed")
    # Keep the identity-mapped instance consistent with the conditional UPDATE.
    gate.consumed_at = now
    await session.flush()
    return gate


def is_valid(
    gate: GateApproval,
    now: datetime,
    *,
    plan_digest: str | None = None,
    base_sha: str | None = None,
    policy_digest: str | None = None,
    spec_digest: str | None = None,
) -> bool:
    """Check that *gate* is unconsumed, unexpired and matches the expectations."""
    if gate.consumed_at is not None:
        return False
    if as_aware_utc(gate.expires_at) <= as_aware_utc(now):
        return False
    if plan_digest is not None and gate.plan_digest != plan_digest:
        return False
    if base_sha is not None and gate.base_sha != base_sha:
        return False
    if policy_digest is not None and gate.policy_digest != policy_digest:
        return False
    if spec_digest is not None and gate.spec_digest != spec_digest:
        return False
    return True
