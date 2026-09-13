"""Durable execution tables (M1 foundation).

These tables back the controller described in ADR-0004 (controller owns the
lifecycle), ADR-0005 (durable execution and unknown_outcome), ADR-0009 (human
gates authorize a specific decision) and ADR-0013 (budgets and usage ledger).

Every closed status enum gets a CHECK constraint; ``flow_runs.status`` is
additionally mirrored by the ``FlowStatus`` state machine in
``forge.durable.controller`` (a unit test asserts the two stay in sync).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from forge.models.base import Base

#: Closed set of flow run lifecycle statuses (ADR-0004). The linear order and
#: the transition graph live in ``forge.durable.controller``.
FLOW_STATUSES: tuple[str, ...] = (
    "accepted",
    "preflight",
    "planning",
    "waiting_approval",
    "proposing",
    "validating",
    "committing",
    "ensuring_draft_mr",
    "waiting_ci",
    "evaluating_ci",
    "reviewing",
    "ready_for_human",
    "blocked",
    "failed",
    "cancelled",
)

_INBOX_STATUSES: tuple[str, ...] = ("pending", "processed", "rejected")
_STEP_STATUSES: tuple[str, ...] = ("running", "succeeded", "failed", "skipped")
_ACTION_STATUSES: tuple[str, ...] = ("requested", "succeeded", "failed", "unknown_outcome")
_LLM_STATUSES: tuple[str, ...] = ("ok", "failed", "cancelled")


def _status_check(name: str, values: tuple[str, ...]) -> CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"status IN ({allowed})", name=name)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EventInbox(Base):
    """Durable webhook inbox (ADR-0005).

    ``source_event_id`` is a content-derived identity (see
    :func:`forge.durable.inbox.build_source_event_id`: sha256 over project id,
    object kind, iid, action and the GitLab delivery id). The unique index on
    it makes duplicate webhook deliveries idempotent at ingestion time.
    """

    __tablename__ = "event_inbox"
    __table_args__ = (_status_check("ck_event_inbox_status", _INBOX_STATUSES),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_event_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    project_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    handler_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class FlowRun(Base):
    """One factory run: identity, status and refs — not a file history (ADR-0004)."""

    __tablename__ = "flow_runs"
    __table_args__ = (_status_check("ck_flow_runs_status", FLOW_STATUSES),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    project_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    issue_iid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mr_iid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="accepted")
    status_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    base_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    candidate_shas: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    plan_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    config_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Incremental ADR-0008 evidence: plan digest/summary, review verdict+sha,
    #: pipeline id/url/status — written as the run accumulates proof.
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    #: ADR-0004 commit-cycle budget counter: 1 = initial candidate; each
    #: bounded code repair increments it (max = FORGE_MAX_COMMIT_CYCLES).
    commit_cycle: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )


class StepRun(Base):
    """A bounded unit of work within a run, with a renewable worker lease (ADR-0005)."""

    __tablename__ = "step_runs"
    __table_args__ = (_status_check("ck_step_runs_status", _STEP_STATUSES),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flow_run_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("flow_runs.id"),
        nullable=False,
        index=True,
    )
    step_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GateApproval(Base):
    """A human gate authorizing one specific decision (ADR-0009).

    Single consumption is service-enforced:
    :func:`forge.durable.gates.consume_approval` sets ``consumed_at`` exactly
    once via a conditional ``UPDATE ... WHERE consumed_at IS NULL``. SQL cannot
    express "NULL, and once set never NULL again" — never write ``consumed_at``
    back to ``NULL``.
    """

    __tablename__ = "gate_approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flow_run_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("flow_runs.id"),
        nullable=False,
        index=True,
    )
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    base_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    approver_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class Outbox(Base):
    """Durable outbox: written in the same transaction as the state it announces (ADR-0005)."""

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flow_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class ActionLog(Base):
    """Audit trail for external writes: intent first, outcome second (ADR-0005).

    Every external write records a ``requested`` row *before* dispatching and
    completes it afterwards with ``succeeded`` / ``failed`` /
    ``unknown_outcome``. Terminal statuses are final — the service layer
    (:meth:`forge.durable.controller.Controller.complete_action`) refuses any
    further change.
    """

    __tablename__ = "action_log"
    __table_args__ = (_status_check("ck_action_log_status", _ACTION_STATUSES),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flow_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    action_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    params_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="requested")
    remote_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class LLMCall(Base):
    """Usage ledger row for a single model call, failures included (ADR-0013).

    Token counters are nullable because unknown usage must be recorded as
    unknown, never as zero.
    """

    __tablename__ = "llm_calls"
    __table_args__ = (_status_check("ck_llm_calls_status", _LLM_STATUSES),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flow_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ok")
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
