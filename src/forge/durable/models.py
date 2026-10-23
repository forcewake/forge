"""Durable execution tables with constrained lifecycle and journal vocabularies."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    CheckConstraint,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge.models.base import Base
from forge.store.inbox_models import EventInbox as EventInbox

#: Closed set of flow run lifecycle statuses (ADR-0004, extended by ADR-0015 with
#: ``waiting_harness``). The linear order and the transition graph live in
#: ``forge.durable.controller``.
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
#: the retryability class of a revival attempt (why the run is retryable) — deliberately
#: ORTHOGONAL to the effect-certainty states above: a transient_infrastructure cause says
#: nothing about whether a remote effect is unresolved, and an unknown publication blocks
#: revival regardless.
_REVIVAL_RETRYABILITY: tuple[str, ...] = (
    "transient_infrastructure",
    "operator_override",
    "verification_timeout",
)
#: the revival-attempt dispatch state. ``pending`` = the dispatch leg has not been claimed
#: yet (a crash before the claim); ``dispatched`` = a driver claimed the dispatch (the
#: recovery scan never re-drives a claimed attempt without first proving the leg never
#: journaled its intent).
_REVIVAL_DISPATCH_STATES: tuple[str, ...] = ("pending", "dispatched")
#: the in-flight arbiter for revival attempts — ONE open (``requested``)
#: ``retry_requested``/``auto_revive`` row per run, enforced by the DB so two reconcilers
#: (or a redelivered /retry racing a fresh one) collapse to one attempt at the index, not by
#: hope.
_REVIVAL_ATTEMPT_INFLIGHT = text(
    "action_kind IN ('retry_requested', 'auto_revive') AND status = 'requested'"
)
_REVIVAL_RETRYABILITY_SQL = ", ".join(f"'{value}'" for value in _REVIVAL_RETRYABILITY)
_REVIVAL_DISPATCH_SQL = ", ".join(f"'{value}'" for value in _REVIVAL_DISPATCH_STATES)
_LLM_STATUSES: tuple[str, ...] = ("ok", "failed", "cancelled")
#: the honest completeness vocabulary INCLUDING the streamed partial state — a partial
#: artifact is not "unknown" (counters are known) and not "aggregate" (more calls may still
#: arrive); it is its own state, and the durable row must be able to say so.
_USAGE_COMPLETENESS: tuple[str, ...] = ("exact", "aggregate", "partial", "unknown")
#: ADR-0018 §5 (F22): closed set of run-budget lifecycle statuses. ``exhausted`` budgets
#: stop granting reservations; ``closed`` is terminal (run finished).
_BUDGET_STATUSES: tuple[str, ...] = ("open", "exhausted", "closed")
#: the DISTINCT budget dimensions an amendment can name.
_BUDGET_AXES: tuple[str, ...] = ("usd", "calls", "tokens", "wallclock")
#: an amendment row is either applied to the enforcement resource or refused with the
#: limiting axis named — every successful/refused command stays visible for audit.
_BUDGET_AMENDMENT_STATUSES: tuple[str, ...] = ("applied", "refused")
#: the review-round lifecycle. ``admitted`` = the round row and its child run exist (the
#: dispatch may not have started — the crash window the reconciler pass re-drives);
#: ``dispatched`` = the correction cycle's advance leg ran; ``stale`` = the re-drive found
#: the MR head moved (the typed stale-head conflict — human edits preserved, nothing
#: dispatched); ``completed`` / ``ended`` close the round (the child run reached a terminal
#: status — ready_for_human or anything else) and free the ONE outstanding round per lineage
#: the partial unique index enforces.
_REVIEW_ROUND_STATUSES: tuple[str, ...] = (
    "admitted",
    "dispatched",
    "stale",
    "completed",
    "ended",
)

#: the round statuses that still hold the lineage's ONE outstanding correction slot (the
#: partial unique index below makes "one outstanding review round per delivery lineage" a DB
#: invariant — two authorized /fix notes racing the same head collapse to one round at the
#: index, never by hope).
_REVIEW_ROUND_OPEN = text("status IN ('admitted', 'dispatched')")

#: closed set of publication-intent lifecycle states. ``requested`` rows exist before the
#: HTTP effect (intent-before-I/O); ``dispatched`` rows have an effect in flight or one
#: whose outcome was lost (crash / timeout); the four outcomes ``committed`` | ``adopted`` |
#: ``duplicated`` | ``unknown`` and ``failed`` are terminal — ``adopted`` means a probe
#: found a PREVIOUS attempt's effect by identity and adopted it; ``duplicated`` means the
#: ref moved away from the intent (someone else / a later repair owns the head); ``unknown``
#: is inconclusive and blocks the run (ADR-0005), never retried blind.
_PUBLICATION_INTENT_STATES: tuple[str, ...] = (
    "requested",
    "dispatched",
    "probing",
    "committed",
    "adopted",
    "duplicated",
    "unknown",
    "failed",
)


def _status_check(name: str, values: tuple[str, ...]) -> CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"status IN ({allowed})", name=name)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


#: ADR-0017: one active run per (provider, project, issue).
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
            "provider",
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
    #: ADR-0018 §1 (F14): digest of the immutable RunSpec frozen at plan acceptance — the
    #: value the pending decision binds (drift ⇒ re-approval).
    spec_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: ADR-0018 §4 (F13): a durable cancel request.
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: publication-grant generation: bumped atomically WITH ``cancel_requested``
    #: by :meth:`Controller.request_cancel` (one UPDATE).
    cancellation_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: Incremental ADR-0008 evidence: plan digest/summary, review verdict+sha, pipeline
    #: id/url/status — written as the run accumulates proof.
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    #: the run's COLLABORATION target — the persisted branch/MR surface its whole delivery
    #: lineage shares.
    target_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    #: ADR-0004 commit-cycle budget counter: 1 = initial candidate; each bounded code repair
    #: increments it (max = FORGE_MAX_COMMIT_CYCLES).
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
    """The immutable, versioned specification of one run (ADR-0018 §1, F14)."""

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
    """A bounded unit of work within a run, with a renewable worker lease (ADR-0005)."""

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
    #: Per-row monotonic fence token, incremented only by the lease-granting claim UPDATE
    #: (ADR-0017 §3 — the DB row is both grantor and storage).
    fence_token: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: Persisted inputs — the step can be re-executed by any worker after a crash
    #: (checkpoint replay, not deterministic replay).
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    #: Command steps only: the EventInbox identity of the webhook that scheduled them
    #: (wake-up addressing + step↔inbox correlation).
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
    """A human gate authorizing one specific decision (ADR-0009)."""

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
    #: ADR-0018 §2 (F15): the pending decision carries the RunSpec digest it freezes and the
    #: issue-text snapshot digest at plan time (drift between approval and execution is
    #: detected, never silent).
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
    """Audit trail for external writes: intent first, outcome second (ADR-0005)."""

    __tablename__ = "action_log"
    __table_args__ = (
        _status_check("ck_action_log_status", _ACTION_STATUSES),
        CheckConstraint(
            f"retryability IS NULL OR retryability IN ({_REVIVAL_RETRYABILITY_SQL})",
            name="ck_action_log_retryability",
        ),
        CheckConstraint(
            f"dispatch_state IS NULL OR dispatch_state IN ({_REVIVAL_DISPATCH_SQL})",
            name="ck_action_log_dispatch_state",
        ),
        Index(
            "uq_revival_attempt_inflight",
            "flow_run_id",
            unique=True,
            postgresql_where=_REVIVAL_ATTEMPT_INFLIGHT,
            sqlite_where=_REVIVAL_ATTEMPT_INFLIGHT,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flow_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    action_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    params_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="requested")
    remote_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    #: revival-attempt identity: the triggering command's delivery id or the auto-revive
    #: window key (NULL for every non-revival action).
    idempotency_key: Mapped[str | None] = mapped_column(String(150), nullable=True, index=True)
    #: why the run is retryable (the retryability class, revival rows only).
    retryability: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: the revival dispatch state — pending until a driver claims the dispatch leg (revival
    #: rows only).
    dispatch_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class MRReservation(Base):
    """The ONE logical Draft-MR intent per run+branch (B03, migration 019)."""

    __tablename__ = "mr_reservations"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'confirmed')", name="ck_mr_reservation_status"),
        UniqueConstraint("flow_run_id", "branch", name="uq_mr_reservation_run_branch"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flow_run_id: Mapped[str] = mapped_column(String(32), nullable=False)
    branch: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    mr_iid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LLMCall(Base):
    """Usage ledger row for a single model call, failures included (ADR-0013)."""

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
    """Per-run budget ledger with reserved/consumed counters (ADR-0018 §5, F22)."""

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
    #: The RunSpec digest the limits were frozen from (ADR-0018 §1); NULL when the budget
    #: was opened outside a spec.
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
    #: Usage a receipt failed to report (unknown ≠ zero, ADR-0013) — parked here so it keeps
    #: fencing capacity instead of reading as spendable.
    unresolved_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unresolved_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: the closing share of the numeric axes, partitioned BEFORE coding starts through the
    #: real reservation path — the implementation purpose cannot reserve into it, the
    #: closing purpose (the reviewer leg) can.
    closing_reserved_calls: Mapped[int | None] = mapped_column(Integer, nullable=True)
    closing_reserved_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    closing_partition_policy: Mapped[str | None] = mapped_column(String(40), nullable=True)
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
    """Audit row for one granted budget hold (ADR-0018 §5, F22)."""

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


class BudgetAmendment(Base):
    """ONE persisted budget amendment, keyed by the ORIGINATING NATIVE COMMAND identity."""

    __tablename__ = "budget_amendments"
    __table_args__ = (
        CheckConstraint(
            f"axis IN ({', '.join(f"'{value}'" for value in _BUDGET_AXES)})",
            name="ck_budget_amendments_axis",
        ),
        CheckConstraint(
            f"status IN ({', '.join(f"'{value}'" for value in _BUDGET_AMENDMENT_STATUSES)})",
            name="ck_budget_amendments_status",
        ),
        CheckConstraint(
            "(axis = 'usd' AND amount_usd IS NOT NULL"
            " AND amount_calls IS NULL AND amount_tokens IS NULL"
            " AND amount_wallclock_s IS NULL)"
            " OR (axis = 'calls' AND amount_calls IS NOT NULL"
            " AND amount_usd IS NULL AND amount_tokens IS NULL"
            " AND amount_wallclock_s IS NULL)"
            " OR (axis = 'tokens' AND amount_tokens IS NOT NULL"
            " AND amount_usd IS NULL AND amount_calls IS NULL"
            " AND amount_wallclock_s IS NULL)"
            " OR (axis = 'wallclock' AND amount_wallclock_s IS NOT NULL"
            " AND amount_usd IS NULL AND amount_calls IS NULL"
            " AND amount_tokens IS NULL)",
            name="ck_budget_amendments_one_axis_amount",
        ),
        UniqueConstraint("run_id", "command_id", name="uq_budget_amendment_command"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    run_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("flow_runs.id"),
        nullable=False,
        index=True,
    )
    #: The originating native command's identity — the delivery id the operator surface
    #: carried (note id / inbox source event id).
    command_id: Mapped[str] = mapped_column(String(150), nullable=False)
    axis: Mapped[str] = mapped_column(String(20), nullable=False)
    amount_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount_calls: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount_wallclock_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    operator: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="applied")
    #: The typed refusal — names the limiting axis (``calls``, the budget status,...). NULL
    #: on applied rows.
    refusal_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    #: The approved budget as it stood before / after the application — the original limits
    #: stay visible as history, never overwritten.
    limit_before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    limit_after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )


class UsageReceipt(Base):
    """The durable identity of ONE ingested harness usage receipt."""

    __tablename__ = "usage_receipts"
    __table_args__ = (
        Index(
            "uq_usage_receipt_identity",
            "run_id",
            "attempt_id",
            "receipt_id",
            "source_namespace",
            unique=True,
        ),
        CheckConstraint(
            "completeness IN (" + ", ".join(f"'{value}'" for value in _USAGE_COMPLETENESS) + ")",
            name="ck_usage_receipts_completeness",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    run_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("flow_runs.id"),
        nullable=False,
        index=True,
    )
    #: The lane's attempt identity (GitHub's ``<run_id>:<run_attempt>``, or the GitLab
    #: ``pipeline:<id>`` shape).
    attempt_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    #: sha256 over (run id, attempt id, normalized usage JSON) — computed at emit time by
    #: the lane or recomputed identically at ingest.
    receipt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: the source NAMESPACE — the natural key's fourth component (the lane artifact label,
    #: the SDK-receipt label, the planner ledger label,...). Legacy rows backfill from
    #: ``source``.
    source_namespace: Mapped[str] = mapped_column(
        String(100), nullable=False, default="", server_default=""
    )
    #: the stable sha256 over the FULL four-part identity — the canonical durable identity
    #: recorded beside its column parts, so an overlength component hashed into its column
    #: stays joinable.
    identity_digest: Mapped[str] = mapped_column(
        String(80), nullable=False, default="", server_default=""
    )
    driver: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completeness: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    #: the streamed receipt's finality — ``False`` marks a partial artifact accepted during
    #: streaming; a later FINAL for the same identity replaces it, a late partial never
    #: downgrades it.
    final: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    #: The economics projection of ``raw``: the reported/estimated cost figure and its
    #: basis, the pricing card, the route version, the attribution segment and the source
    #: artifact digest.
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_basis: Mapped[str | None] = mapped_column(String(30), nullable=True)
    rate_card_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    route_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    segment: Mapped[str | None] = mapped_column(String(80), nullable=True)
    artifact_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: The verbatim usage block as received — never normalized in place.
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class UsageIngestionConflict(Base):
    """ONE surfaced ingestion conflict or refusal — never silently dropped."""

    __tablename__ = "usage_ingestion_conflicts"
    __table_args__ = (
        Index(
            "uq_usage_ingestion_conflict_identity",
            "run_id",
            "attempt_id",
            "receipt_id",
            "source_namespace",
            "kind",
            "content_digest",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    run_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    attempt_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    receipt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_namespace: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    #: ``conflicting-final`` | ``conflicting-content`` | ``attribution-refused`` |
    #: ``identity-rejected``.
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    #: sha256 over the DELIVERED content (the refused claim or the conflicting row) — the
    #: replay idempotency key's last component.
    content_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    #: The delivered/claimed values beside the trusted/standing ones — identity-only
    #: (work/attempt/receipt/source labels, digests, notes); never a credential and never
    #: spend authority.
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class CredentialRedemption(Base):
    """ONE append-only credential-redemption receipt."""

    __tablename__ = "credential_redemptions"

    receipt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    work_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("flow_runs.id"),
        nullable=False,
        index=True,
    )
    #: Operation grant identity recorded by the redemption owner.
    grant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    attempt_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The delivery route label (the provider the redemption served).
    route: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    credential_ref: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    resolver: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    subject: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    provider: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    binding_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The redemption outcome (``redeemed``; the refusal paths never write a row — zero
    #: successful retrievals).
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, default="redeemed")
    #: A DISTINCT later delivery that identifies itself as a retry of THIS logical
    #: redemption (its own receipt id, linked back); a re-delivery of the SAME receipt id
    #: instead bumps ``retry_count``.
    retry_of: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: How many times this SAME receipt id was re-delivered (lost-response retries) — the
    #: logical total stays ONE redemption.
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    broker_receipt_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    resolved_version_kind: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    credential_policy: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    #: Where the row came from: ``live`` (the redemption endpoint) or ``legacy-embedded``
    #: (backfilled from the pre-table evidence list).
    provenance: Mapped[str] = mapped_column(String(30), nullable=False, default="live")
    #: The verbatim value-free audit document (the exact entry the endpoint built) plus
    #: retention/investigation labels.
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class OperationGrant(Base):
    """ONE keyed operation-grant authority row."""

    __tablename__ = "operation_grants"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'revoked')", name="ck_operation_grants_status"),
        UniqueConstraint(
            "work_id", "attempt_generation", "provider", name="uq_operation_grant_key"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    work_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("flow_runs.id"),
        nullable=False,
        index=True,
    )
    #: The attempt generation component of the canonical grant key.
    attempt_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The provider ROUTE component of the canonical grant key (the evidence key's
    #: ``"<attempt>:<provider>"`` second half).
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    grant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The full grant document — refs and metadata ONLY, never a value.
    document: Mapped[dict] = mapped_column(JSON, nullable=False)
    #: The ABSOLUTE redemption deadline (fixed at first authorization).
    redemption_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )


class PublicationIntent(Base):
    """The durable intent to produce ONE remote publication effect."""

    __tablename__ = "publication_intents"
    __table_args__ = (
        _status_check("ck_publication_intents_status", _PUBLICATION_INTENT_STATES),
        Index(
            "uq_publication_intent_key",
            "provider",
            "repo",
            "target_ref",
            "idempotency_scope",
            "operation_key",
            unique=True,
        ),
        Index("ix_publication_intents_state_probe", "status", "next_probe_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    run_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("flow_runs.id"),
        nullable=False,
        index=True,
    )
    #: ``gitlab`` | ``github`` (FlowRun.provider values).
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    repo: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The effect kind: ``commit`` (branch publication) today, extensible to ``pr`` /
    #: ``comment`` / ``dispatch``.
    operation: Mapped[str] = mapped_column(String(20), nullable=False, default="commit")
    #: The branch the effect targets (comments/dispatches would use their anchor here).
    target_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Logical retry scope WITHIN (run, ref): one commit cycle per scope, so a repair cycle
    #: mints a NEW intent (and key) while retries of the same cycle reuse this row.
    idempotency_scope: Mapped[str] = mapped_column(String(100), nullable=False)
    #: ``(forge-op:<key>)`` — STABLE across retries; minted at creation.
    operation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ADR-0004 commit-cycle counter copied from the run (1 = initial).
    commit_cycle: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: Candidate tree/content digest — belt-and-braces probe verification.
    content_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Branch head captured at intent time (the CAS base); NULL = the intent expected a root
    #: commit (empty parent list).
    expected_parent_oid: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: Caller-pinned drift guard (mirrors the writer's ``expected_head``).
    expected_head: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="requested")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    #: Jittered backoff for the recovery scanner (NULL = due now).
    next_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: shape (deadline-before-I/O); copied from the owning step when one drives the leg.
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The commit sha / PR number / build id once known (probe-found or response-carried).
    provider_object_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    remote_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )


class ReviewRound(Base):
    """ONE linked post-readiness review round."""

    __tablename__ = "review_rounds"
    __table_args__ = (
        _status_check("ck_review_rounds_status", _REVIEW_ROUND_STATUSES),
        Index(
            "uq_review_round_open_per_root",
            "root_run_id",
            unique=True,
            postgresql_where=_REVIEW_ROUND_OPEN,
            sqlite_where=_REVIEW_ROUND_OPEN,
        ),
        UniqueConstraint("parent_run_id", "note_id", name="uq_review_round_request"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    parent_run_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("flow_runs.id"),
        nullable=False,
        index=True,
    )
    child_run_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("flow_runs.id"),
        nullable=False,
        index=True,
    )
    root_run_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("flow_runs.id"),
        nullable=False,
        index=True,
    )
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    note_id: Mapped[str] = mapped_column(String(150), nullable=False)
    mr_iid: Mapped[int] = mapped_column(Integer, nullable=False)
    base_head_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    decision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="admitted")
    status_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )


#: the target lifecycle. ``active`` rows carry the validated collaboration surface; a
#: ``refused`` row is the durable record that a legacy topology could NOT be resolved — it
#: carries the typed reason and NEVER a branch (an unresolved topology is recorded, never
#: inferred).
_COLLABORATION_TARGET_STATUSES: tuple[str, ...] = ("active", "refused")
#: the partial-unique predicate making "one active target per (provider, repository, source
#: branch)" a DB invariant — two admissions racing the same lineage collapse to one row at
#: the index (refused rows carry NULL branches and stay outside it).
_ACTIVE_COLLABORATION_TARGET = text("status = 'active'")


class CollaborationTarget(Base):
    """The canonical collaboration surface of ONE delivery lineage."""

    __tablename__ = "collaboration_targets"
    __table_args__ = (
        _status_check("ck_collaboration_targets_status", _COLLABORATION_TARGET_STATUSES),
        CheckConstraint(
            "provenance IN ('live', 'legacy')",
            name="ck_collaboration_targets_provenance",
        ),
        CheckConstraint(
            "(status = 'active' AND source_branch IS NOT NULL AND target_branch IS NOT NULL"
            " AND refusal_reason IS NULL)"
            " OR (status = 'refused' AND source_branch IS NULL AND target_branch IS NULL"
            " AND refusal_reason IS NOT NULL)",
            name="ck_collaboration_target_shape",
        ),
        Index(
            "uq_collaboration_target_branch",
            "provider",
            "project_ref",
            "source_branch",
            unique=True,
            postgresql_where=_ACTIVE_COLLABORATION_TARGET,
            sqlite_where=_ACTIVE_COLLABORATION_TARGET,
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    #: FlowRun.provider values — the repository identity is only unique WITHIN a provider.
    provider: Mapped[str] = mapped_column(
        String(20), nullable=False, default="gitlab", server_default="gitlab"
    )
    #: The provider-scoped repository identity (GitLab project id as a string). Two
    #: repositories sharing an MR number and a branch label key apart here — they never
    #: cross.
    project_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    issue_iid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Delivery 1 of the lineage (the round-reference anchor).
    root_run_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("flow_runs.id"),
        nullable=False,
        index=True,
    )
    #: The run-owned ref the MR stays open on (immutable once set).
    source_branch: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: The MR destination branch.
    target_branch: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: The lineage's ONE MR identity (once one exists; never moved).
    mr_iid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    refusal_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    #: Where the row came from: ``live`` (the admission that owns the work materialized it)
    #: or ``legacy`` (the adapter / migration derived it ONCE, validated against recorded
    #: MR/source info).
    provenance: Mapped[str] = mapped_column(
        String(20), nullable=False, default="live", server_default="live"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )
