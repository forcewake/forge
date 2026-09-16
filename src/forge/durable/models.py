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

from sqlalchemy import (
    JSON,
    CheckConstraint,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge.models.base import Base

#: Closed set of flow run lifecycle statuses (ADR-0004, extended by ADR-0015
#: with ``waiting_harness``). The linear order and the transition graph live
#: in ``forge.durable.controller``.
FLOW_STATUSES: tuple[str, ...] = (
    "accepted",
    "preflight",
    "planning",
    "waiting_approval",
    "proposing",
    "validating",
    "committing",
    "waiting_harness",
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
_STEP_STATUSES: tuple[str, ...] = (
    "scheduled",
    "running",
    "succeeded",
    "failed",
    "skipped",
    "dead",
    # ADR-0018 §4: a cancel request withdraws steps that were never claimed.
    "cancelled",
)
_ACTION_STATUSES: tuple[str, ...] = ("requested", "succeeded", "failed", "unknown_outcome")
_LLM_STATUSES: tuple[str, ...] = ("ok", "failed", "cancelled")
#: ADR-0018 §5 (F22): closed set of run-budget lifecycle statuses. ``exhausted``
#: budgets stop granting reservations; ``closed`` is terminal (run finished).
_BUDGET_STATUSES: tuple[str, ...] = ("open", "exhausted", "closed")


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


#: ADR-0017 (F12): one active run per (project, issue). Terminal statuses live
#: in the predicate as a literal list (mirrors Controller.TERMINAL_STATUSES —
#: a unit test asserts the two stay in sync); ``ready_for_human`` is terminal
#: per ADR-0004, so it does NOT block a fresh /implement.
_TERMINAL_STATUS_PREDICATE = text(
    "status NOT IN ('ready_for_human', 'blocked', 'failed', 'cancelled')"
)


class FlowRun(Base):
    """One factory run: identity, status and refs — not a file history (ADR-0004)."""

    __tablename__ = "flow_runs"
    __table_args__ = (
        _status_check("ck_flow_runs_status", FLOW_STATUSES),
        Index(
            "uq_active_run_per_issue",
            "project_id",
            "issue_iid",
            unique=True,
            postgresql_where=_TERMINAL_STATUS_PREDICATE,
            sqlite_where=_TERMINAL_STATUS_PREDICATE,
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    project_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    issue_iid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: E3a: which integration owns the subject — ``gitlab`` (default) or
    #: ``github``. For GitHub runs ``project_id`` is the webhook's numeric
    #: repository id and ``issue_iid`` the issue number, so the partial
    #: unique index below enforces one active run per (repo, issue) too.
    provider: Mapped[str] = mapped_column(
        String(20), nullable=False, default="gitlab", server_default="gitlab"
    )
    #: GitHub subject identity for ``provider='github'`` runs.
    github_repo_full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    github_issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mr_iid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="accepted")
    status_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    base_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    candidate_shas: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    plan_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    config_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: ADR-0018 §1 (F14): digest of the immutable RunSpec frozen at plan
    #: acceptance — the value the pending decision binds (drift ⇒ re-approval).
    spec_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: ADR-0018 §4 (F13): a durable cancel request. Set BEFORE the terminal
    #: cancelled transition; in-flight publication legs re-read it and stand
    #: down, and a verified candidate for a cancelled run stays superseded.
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
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


class RunSpec(Base):
    """The immutable, versioned specification of one run (ADR-0018 §1, F14).

    Frozen at plan acceptance, *before* the plan is published: subject,
    source snapshot OID, plan/task/policy digests, the resolved backend
    config and the budgets. Changing a setting mid-run never changes an
    approved RunSpec — the run executes its spec or requests re-approval.
    ``digest`` is the sha256 over the canonical (sorted-key) JSON of
    ``document``; the pending decision carries it and a ``/go`` whose run's
    current spec digest differs is invalid.
    """

    __tablename__ = "run_specs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    run_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("flow_runs.id"),
        nullable=False,
        index=True,
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    document: Mapped[dict] = mapped_column(JSON, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class StepRun(Base):
    """A bounded unit of work within a run, with a renewable worker lease (ADR-0005).

    ADR-0017 step runtime: steps are ``scheduled`` with a ``due_at`` timer and
    claimed by workers through a conditional UPDATE that grants the lease and
    bumps the per-row ``fence_token`` — a stale owner's completion (old fence)
    is rejected by that predicate. ``attempt`` counts failures (+1 on every
    failure and lease reaper pass); ``attempt >= max_attempts`` parks the step
    as ``dead`` with the last error kept in ``output`` (poison pill, never
    deleted). Command steps (``start_run`` / ``go`` / ``cancel``) are bound to
    their run only at execution time, so ``flow_run_id`` is nullable and the
    command's inbox identity rides in ``source_event_id``.
    """

    __tablename__ = "step_runs"
    __table_args__ = (_status_check("ck_step_runs_status", _STEP_STATUSES),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flow_run_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("flow_runs.id"),
        nullable=True,
        index=True,
    )
    step_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="scheduled")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    #: Per-row monotonic fence token, incremented only by the lease-granting
    #: claim UPDATE (ADR-0017 §3 — the DB row is both grantor and storage).
    fence_token: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: Persisted inputs — the step can be re-executed by any worker after a
    #: crash (checkpoint replay, not deterministic replay).
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    #: Command steps only: the EventInbox identity of the webhook that
    #: scheduled them (wake-up addressing + step↔inbox correlation).
    source_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
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

    ``generation`` (ADR-0017 §4): one gate per (run, approval generation) is a
    DB invariant via the unique index — ``record_approval`` allocates
    ``max(generation) + 1`` so re-approval rounds coexist while duplicates of
    the same round cannot.
    """

    __tablename__ = "gate_approvals"
    __table_args__ = (
        Index("uq_gate_per_run_generation", "flow_run_id", "generation", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flow_run_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("flow_runs.id"),
        nullable=False,
        index=True,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    base_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ADR-0018 §2 (F15): the pending decision carries the RunSpec digest it
    #: freezes and the issue-text snapshot digest at plan time (drift between
    #: approval and execution is detected, never silent).
    spec_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 0 while the decision is pending; set to the consuming approver's id.
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
    unknown, never as zero. For harness-candidate receipts (ADR-0016 §4,
    F22 lite) ``driver`` carries the harness driver id and ``completeness``
    says how trustworthy the counters are ("exact" | "aggregate" |
    "unknown"); both are NULL for forge-side LLM calls.
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
    driver: Mapped[str | None] = mapped_column(String(50), nullable=True)
    completeness: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class RunBudget(Base):
    """Per-run budget ledger with reserved/consumed counters (ADR-0018 §5, F22).

    Enforcement is *reserve, then reconcile* (ADR-0013): a dispatch reserves
    calls/tokens BEFORE the provider is contacted; the provider receipt
    reconciles the hold against actuals afterwards. All counter moves are
    single conditional UPDATEs against the row — never read-modify-write —
    so parallel attempts cannot overshoot a limit between measurement points.
    Exposure is ``consumed + reserved + unresolved``: budget already spent,
    or spent with an unreported amount, is never grantable again.

    A ``NULL`` limit means unlimited on that dimension; the consumed counters
    keep recording actuals regardless of status, so failed and exhausted runs
    stay explainable. ``run_id`` is UNIQUE: one budget per run —
    :func:`forge.durable.budgets.open_budget` is idempotent per run.
    """

    __tablename__ = "run_budgets"
    __table_args__ = (_status_check("ck_run_budgets_status", _BUDGET_STATUSES),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    run_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("flow_runs.id"),
        nullable=False,
        index=True,
        unique=True,
    )
    #: The RunSpec digest the limits were frozen from (ADR-0018 §1); NULL when
    #: the budget was opened outside a spec.
    spec_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Recorded limits — NULL = unlimited on that dimension.
    wallclock_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_calls: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Holds granted to in-flight dispatches (moved back out at reconcile).
    reserved_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Actuals recorded from provider receipts / harness usage evidence.
    consumed_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consumed_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Usage a receipt failed to report (unknown ≠ zero, ADR-0013) — parked
    #: here so it keeps fencing capacity instead of reading as spendable.
    unresolved_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unresolved_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )


class BudgetReservation(Base):
    """Audit row for one granted budget hold (ADR-0018 §5, F22).

    Written when a reservation is granted; ``released`` flips to true exactly
    once at :func:`forge.durable.budgets.reconcile_actual` — the conditional
    flip is what makes reconciliation exactly-once (a crash between the flip
    and the counter move cannot double-apply the move).
    """

    __tablename__ = "budget_reservations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    run_budget_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("run_budgets.id"),
        nullable=False,
        index=True,
    )
    #: Caller-supplied attempt identity (e.g. the flow run id of the dispatch).
    attempt_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reserved_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    released: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
