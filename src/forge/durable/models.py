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
#: A11: the retryability class of a revival attempt (why the run is retryable)
#: — deliberately ORTHOGONAL to the A12 effect-certainty states above: a
#: transient_infrastructure cause says nothing about whether a remote effect
#: is unresolved, and an unknown publication blocks revival regardless.
_REVIVAL_RETRYABILITY: tuple[str, ...] = (
    "transient_infrastructure",
    "operator_override",
    "verification_timeout",
)
#: A11: the revival-attempt dispatch state. ``pending`` = the dispatch leg has
#: not been claimed yet (a crash before the claim); ``dispatched`` = a driver
#: claimed the dispatch (the recovery scan never re-drives a claimed attempt
#: without first proving the leg never journaled its intent).
_REVIVAL_DISPATCH_STATES: tuple[str, ...] = ("pending", "dispatched")
#: A11: the in-flight arbiter for revival attempts — ONE open (``requested``)
#: ``retry_requested``/``auto_revive`` row per run, enforced by the DB so two
#: reconcilers (or a redelivered /retry racing a fresh one) collapse to one
#: attempt at the index, not by hope.
_REVIVAL_ATTEMPT_INFLIGHT = text(
    "action_kind IN ('retry_requested', 'auto_revive') AND status = 'requested'"
)
_REVIVAL_RETRYABILITY_SQL = ", ".join(f"'{value}'" for value in _REVIVAL_RETRYABILITY)
_REVIVAL_DISPATCH_SQL = ", ".join(f"'{value}'" for value in _REVIVAL_DISPATCH_STATES)
_LLM_STATUSES: tuple[str, ...] = ("ok", "failed", "cancelled")
#: Q39-05 (#324): the honest completeness vocabulary INCLUDING the streamed
#: partial state — a partial artifact is not "unknown" (counters are known)
#: and not "aggregate" (more calls may still arrive); it is its own state,
#: and the durable row must be able to say so.
_USAGE_COMPLETENESS: tuple[str, ...] = ("exact", "aggregate", "partial", "unknown")
#: ADR-0018 §5 (F22): closed set of run-budget lifecycle statuses. ``exhausted``
#: budgets stop granting reservations; ``closed`` is terminal (run finished).
_BUDGET_STATUSES: tuple[str, ...] = ("open", "exhausted", "closed")
#: R40-04 (#340): the DISTINCT budget dimensions an amendment can name.
#: USD exposure, dispatch calls, tokens and wall-clock are separate
#: enforcement axes — a top-up names exactly one; conversion between them
#: requires a stated versioned policy, never an implicit guess.
_BUDGET_AXES: tuple[str, ...] = ("usd", "calls", "tokens", "wallclock")
#: R40-04 (#340): an amendment row is either applied to the enforcement
#: resource or refused with the limiting axis named — every
#: successful/refused command stays visible for audit.
_BUDGET_AMENDMENT_STATUSES: tuple[str, ...] = ("applied", "refused")
#: R40-02 (#338): the review-round lifecycle. ``admitted`` = the round row
#: and its child run exist (the dispatch may not have started — the crash
#: window the reconciler pass re-drives); ``dispatched`` = the correction
#: cycle's advance leg ran; ``stale`` = the re-drive found the MR head moved
#: (the typed stale-head conflict — human edits preserved, nothing
#: dispatched); ``completed`` / ``ended`` close the round (the child run
#: reached a terminal status — ready_for_human or anything else) and free
#: the ONE outstanding round per lineage the partial unique index enforces.
_REVIEW_ROUND_STATUSES: tuple[str, ...] = (
    "admitted",
    "dispatched",
    "stale",
    "completed",
    "ended",
)

#: R40-02 (#338): the round statuses that still hold the lineage's ONE
#: outstanding correction slot (the partial unique index below makes "one
#: outstanding review round per delivery lineage" a DB invariant — two
#: authorized /fix notes racing the same head collapse to one round at the
#: index, never by hope).
_REVIEW_ROUND_OPEN = text("status IN ('admitted', 'dispatched')")

#: R11: closed set of publication-intent lifecycle states. ``requested`` rows
#: exist before the HTTP effect (intent-before-I/O); ``dispatched`` rows have
#: an effect in flight or one whose outcome was lost (crash / timeout); the
#: four outcomes ``committed`` | ``adopted`` | ``duplicated`` | ``unknown``
#: and ``failed`` are terminal — ``adopted`` means a probe found a PREVIOUS
#: attempt's effect by identity and adopted it; ``duplicated`` means the ref
#: moved away from the intent (someone else / a later repair owns the head);
#: ``unknown`` is inconclusive and blocks the run (ADR-0005), never retried
#: blind. The live transition graph is enforced by
#: :func:`forge.durable.intents.complete_intent` (mirroring complete_action).
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


#: ADR-0017 (F12), R03: one active run per (provider, project, issue). The
#: provider leads the key because numeric subject ids are only unique WITHIN
#: a provider — a GitLab ``project_id=5`` and a GitHub repository internal
#: id ``5`` are unrelated subjects and must never collide. Terminal statuses
#: live in the predicate as a literal list (mirrors
#: Controller.TERMINAL_STATUSES — a unit test asserts the two stay in sync);
#: ``ready_for_human`` is terminal per ADR-0004, so it does NOT block a
#: fresh /implement.
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
    #: R03: which integration owns the subject — ``gitlab`` (default),
    #: ``github`` or ``azure_devops``. NOT NULL with a server default of
    #: ``gitlab`` because forge started GitLab-only: legacy rows keep their
    #: lane without a backfill decision. For non-GitLab runs ``project_id``
    #: carries the provider's numeric subject id (GitHub repository id, AzDO
    #: project id) and ``issue_iid`` the issue/PR number or work-item id —
    #: numerically disjoint namespaces, so the partial unique index
    #: ``uq_active_run_per_issue`` above keys on (provider, project_id,
    #: issue_iid) to enforce one active run per subject PER PROVIDER
    #: (migration 012 rebuilt it that way).
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
    #: R10 publication-grant generation: bumped atomically WITH
    #: ``cancel_requested`` by :meth:`Controller.request_cancel` (one UPDATE).
    #: An execution claim pins the generation it was minted under; the
    #: publisher only grants a NEW reservation while the run's generation
    #: still equals the pinned one — so a cancel that lands between the
    #: claim and the publication instantly fences every claim minted before
    #: it (queue ownership implies effect ownership, R10).
    cancellation_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
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

    A11: the revival kinds (``retry_requested`` / ``auto_revive``) double as
    the durable REVIVAL ATTEMPT record — the row written in the SAME
    transaction as the CAS revival transition, carrying:

    - ``idempotency_key`` — the delivery/event id of the triggering command
      (``delivery:<note id>``) or the auto-revive window identity
      (``revive:<run id>:<stamp count>``). A redelivered /retry with the SAME
      key is a no-op (no second cycle bump, no second dispatch); a DIFFERENT
      key is refused while an attempt is still open.
    - ``retryability`` — the typed :data:`_REVIVAL_RETRYABILITY` class.
    - ``dispatch_state`` — ``pending`` → ``dispatched``; the recovery scan
      (``forge.runs.revival.evaluate_attempt_recovery``) re-drives pending
      attempts whose dispatch never started, exactly once.

    The partial unique index ``uq_revival_attempt_inflight`` makes "one
    in-flight revival attempt per run" a DB invariant.
    """

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
    #: A11 revival-attempt identity: the triggering command's delivery id or
    #: the auto-revive window key (NULL for every non-revival action).
    idempotency_key: Mapped[str | None] = mapped_column(String(150), nullable=True, index=True)
    #: A11: why the run is retryable (the retryability class, revival rows only).
    retryability: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: A11: the revival dispatch state — pending until a driver claims the
    #: dispatch leg (revival rows only).
    dispatch_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class MRReservation(Base):
    """The ONE logical Draft-MR intent per run+branch (B03, migration 019).

    Separates the *reservation* — "exactly one MR for this run+branch",
    durable and committed BEFORE any provider I/O, ``FOR UPDATE``-serialized
    across concurrent creators — from the immutable ``action_log`` attempt
    history. ``open → confirmed``; ``confirmed.mr_iid`` is the adopted or
    created MR. The journal keeps every attempt/observation row (create
    attempts, adoptions, reconciliations) — resolving a lost response
    never rewrites a terminal action row.
    """

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
    #: R40-04 (#340): the closing share of the numeric axes, partitioned
    #: BEFORE coding starts through the real reservation path — the
    #: implementation purpose cannot reserve into it, the closing purpose
    #: (the reviewer leg) can. NULL = no partition (every axis fully
    #: shared); the versioned policy that sized it rides the column below.
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


class BudgetAmendment(Base):
    """ONE persisted budget amendment, keyed by the ORIGINATING NATIVE
    COMMAND identity (R40-04, #340).

    The recorded defect: ``continue_review_only`` recorded amount/reason/
    operator into run EVIDENCE, released the marker and re-ran the review
    against the SAME ``RunBudget`` limits — a dollar-only annotation
    cannot reopen an exhausted call or token budget. From #340 an
    amendment is a DURABLE row applied ATOMICALLY to the enforcement
    resource in the same transaction that inserts it:

    - ``command_id`` is the native command identity (the note/delivery id
      the operator surface rode, e.g. ``run:continue_review:42:918``).
      Two identical amount/reason commands are TWO decisions (two ids);
      a redelivery of ONE command hits the UNIQUE ``(run_id,
      command_id)`` index and replays — applied exactly once.
    - ``axis`` names the DISTINCT dimension the amendment changes
      (``usd`` | ``calls`` | ``tokens`` | ``wallclock``). Cross-axis
      conversion is refused — it would need a stated versioned policy,
      never an implicit guess.
    - ``calls`` / ``tokens`` / ``wallclock`` raise the ``run_budgets``
      limits and re-open a budget that was ``exhausted`` (a ``closed``
      budget is terminal and refuses); ``usd`` changes the closing
      gate's effective cap (the durable row IS the enforcement record —
      the USD axis is enforced over the usage receipts, not the guard's
      counters).
    - ``limit_before``/``limit_after`` keep the original approved budget
      as history: the moved limit is auditable against what stood
      before, and a refused amendment keeps its typed
      ``refusal_reason`` (the limiting axis named).
    """

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
    #: The originating native command's identity — the delivery id the
    #: operator surface carried (note id / inbox source event id). An
    #: empty id never reaches this table: the applying surface refuses
    #: an amendment without its command identity up front.
    command_id: Mapped[str] = mapped_column(String(150), nullable=False)
    axis: Mapped[str] = mapped_column(String(20), nullable=False)
    amount_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount_calls: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount_wallclock_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    operator: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="applied")
    #: The typed refusal — names the limiting axis (``calls``, the
    #: budget status, ...). NULL on applied rows.
    refusal_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    #: The approved budget as it stood before / after the application —
    #: the original limits stay visible as history, never overwritten.
    limit_before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    limit_after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )


class UsageReceipt(Base):
    """The durable identity of ONE ingested harness usage receipt (R23).

    Harness spend arrives as candidate-artifact receipts that the control
    plane polls at-least-once (a repeated reconciler tick, a crash between
    ingest and transition, a re-downloaded artifact). This table is the
    idempotency arbiter. Q39-05 (#324) makes the durable identity match the
    in-memory contract EXACTLY: the unique index on ``(run_id, attempt_id,
    receipt_id, source_namespace)`` — the receipt identity the lane
    computed over its normalized usage PLUS the source namespace it
    arrived under — so the same label delivered by two sources stays two
    rows, and a FINAL receipt can conditionally replace the stored PARTIAL
    it reconciles (``ON CONFLICT ... DO UPDATE ... WHERE final IS NOT
    TRUE``; a late partial NEVER downgrades a final). ``identity_digest``
    is the stable sha256 over the FULL four-part identity recorded beside
    the column-shaped parts — an overlength component is stored as a
    ``sha256:``-prefixed digest of the full value (never silently
    truncated; the full value rides ``raw`` under ``identity_overlength``),
    and a work id longer than the run id column is REFUSED outright (no
    run row could ever own it).

    The canonical counters are recorded with the honesty rules of ADR-0013:
    unknown stays ``NULL`` (never zero), cache counters stay OUT of
    ``input_tokens`` (Anthropic-shaped counters are disjoint; the spend
    total is computed at reconciliation, not folded in here), and ``raw``
    keeps the verbatim usage block so spend stays reconstructable. The
    Q39-05 economics columns (``cost_usd`` / ``cost_basis`` /
    ``rate_card_id`` / ``route_version`` / ``segment`` /
    ``artifact_digest`` / ``final``) are the queryable projection of what
    ``raw`` carries — the digest binds the row to the exact source bytes
    (a changed artifact under one identity is a conflict, never a silent
    overwrite).
    """

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
    #: The lane's attempt identity (GitHub's ``<run_id>:<run_attempt>``, or
    #: the GitLab ``pipeline:<id>`` shape). Empty means the meta carried no
    #: attempt identity (v1 metas); the identity then rests on the run and
    #: the usage content alone.
    attempt_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    #: sha256 over (run id, attempt id, normalized usage JSON) — computed at
    #: emit time by the lane or recomputed identically at ingest.
    receipt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Q39-05 (#324): the source NAMESPACE — the natural key's fourth
    #: component (the lane artifact label, the SDK-receipt label, the
    #: planner ledger label, ...). Legacy R23 rows backfill from ``source``.
    source_namespace: Mapped[str] = mapped_column(
        String(100), nullable=False, default="", server_default=""
    )
    #: Q39-05 (#324): the stable sha256 over the FULL four-part identity —
    #: the canonical durable identity recorded beside its column parts, so
    #: an overlength component hashed into its column stays joinable.
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
    #: Q39-05 (#324): the streamed receipt's finality — ``False`` marks a
    #: partial artifact accepted during streaming; a later FINAL for the
    #: same identity replaces it, a late partial never downgrades it.
    final: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    #: The economics projection of ``raw`` (Q39-05): the reported/estimated
    #: cost figure and its basis, the pricing card, the route version, the
    #: attribution segment and the source artifact digest.
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
    """ONE surfaced ingestion conflict or refusal — never silently dropped
    (Q39-05, #324).

    Two shapes land here, both diagnostics (no spend row is written from
    either):

    - ``conflicting-final`` / ``conflicting-content`` — the same durable
      identity delivered twice with DIFFERENT content while the standing
      row is already final (or the incoming delivery is not an upgrade);
      the FIRST row stands, the difference is preserved here — never
      averaged, never silently merged;
    - ``attribution-refused`` — a payload claiming work-B (or attempt-B)
      delivered on a transport the trusted caller attributed to work-A:
      NO row is written to the payload-claimed work; the refusal and both
      identities are preserved here.

    ``content_digest`` makes the record idempotent under replay: the same
    conflicting delivery re-delivered any number of times leaves exactly
    one row per (identity, delivered content) pair.
    """

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
    #: ``conflicting-final`` | ``conflicting-content`` | ``attribution-refused``
    #: | ``identity-rejected``.
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    #: sha256 over the DELIVERED content (the refused claim or the
    #: conflicting row) — the replay idempotency key's last component.
    content_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    #: The delivered/claimed values beside the trusted/standing ones —
    #: identity-only (work/attempt/receipt/source labels, digests, notes);
    #: never a credential and never spend authority.
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class CredentialRedemption(Base):
    """ONE append-only credential-redemption receipt (Q39-03, #322).

    The audit ledger behind ``GET /lane/credentials/redeem`` — refs and
    metadata ONLY: no value slot, no value digest, nothing from which the
    credential could be reconstructed. The row is INSERT-only (a
    redemption never rewrites history); a re-delivered receipt id bumps
    ``retry_count`` on the SAME logical row (a lost-response retry never
    inflates logical totals while each retry observation stays
    inspectable through the counter and ``last_retry_at``).

    ``grant_id`` is the join key to the operation grant that authorized
    the redemption (the #Q39-01 grant type): a plain string the grant
    owner populates — EMPTY (with the fact labelled in ``details``) for
    rows backfilled from the legacy embedded evidence or redeemed before
    grants landed; a grant identity is never invented. The bounded
    ``run.evidence["credential_redemptions"]`` list is a VERSIONED
    PROJECTION of this table (see
    :func:`forge.adaptive.credential_audit.record_redemption`), never the
    audit authority: the full history lives here regardless of the
    projection's last-50 cap.
    """

    __tablename__ = "credential_redemptions"

    receipt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    work_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("flow_runs.id"),
        nullable=False,
        index=True,
    )
    #: The operation grant that authorized this redemption (Q39-01's
    # grant type; a plain string the grant owner populates). EMPTY for
    # legacy/backfilled rows — labelled in ``details``, never invented.
    grant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    attempt_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The delivery route label (the provider the redemption served).
    route: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    credential_ref: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    resolver: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    subject: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    provider: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    binding_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The redemption outcome (``redeemed``; the refusal paths never write
    #: a row — zero successful retrievals).
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, default="redeemed")
    #: A DISTINCT later delivery that identifies itself as a retry of THIS
    #: logical redemption (its own receipt id, linked back); a re-delivery
    #: of the SAME receipt id instead bumps ``retry_count``.
    retry_of: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: How many times this SAME receipt id was re-delivered (lost-response
    #: retries) — the logical total stays ONE redemption.
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    broker_receipt_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    resolved_version_kind: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    credential_policy: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    #: Where the row came from: ``live`` (the redemption endpoint) or
    #: ``legacy-embedded`` (backfilled from the pre-table evidence list).
    provenance: Mapped[str] = mapped_column(String(30), nullable=False, default="live")
    #: The verbatim value-free audit document (the exact entry the
    #: endpoint built) plus retention/investigation labels.
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class OperationGrant(Base):
    """ONE keyed operation-grant authority row (R40-05, #341).

    THE transactional authority behind dispatch-time authorization.
    Q39-01 (#320) minted the grant and persisted it with an unconditional
    whole-document evidence overwrite — the review b521e1a R40-05 defect:
    a concurrent native-handle / checkpoint / review write between the
    read and the write was silently erased, and two concurrent initial
    grants could return different effective identities. From #341 the
    grant lives HERE, UNIQUE per canonical (work, attempt, route), and
    the ``run.evidence["credential_operation_grants"]`` map becomes a
    DERIVED projection rewritten through a targeted compare-and-swap
    (see :func:`forge.api_lane_control.persist_operation_grant`):

    - creation is serialized by the unique index itself (INSERT ... ON
      CONFLICT DO NOTHING, then read): every contender returns the
      COMMITTED effective grant, never its own locally minted object;
    - an exact replay keeps the first authorized ``grant_id`` and the
      ABSOLUTE ``redemption_deadline`` (the window never re-anchors);
    - a rotation (a DIFFERENT credential ref at the same key) replaces
      the document through a conditional UPDATE guarded by the row the
      writer judged — the broker's documented merge rule, applied
      authority-side;
    - a CORRUPT document is a typed persistence failure, never silently
      replaced with a fresh window;
    - ``revoked`` is the explicit operator retirement of the key: a
      replay or a rotation against a revoked row refuses — a revocation
      is never resurrected by a re-dispatch.

    ``document`` is the full value-free grant document (the exact
    ``CredentialOperationGrant.as_document()`` JSON shape); the columns
    beside it are its queryable projections. No value slot, by
    construction.
    """

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
    #: The provider ROUTE component of the canonical grant key (the
    #: evidence key's ``"<attempt>:<provider>"`` second half).
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
    """The durable intent to produce ONE remote publication effect (R11).

    Written in the same local transaction as the ``action_log`` intent row
    and strictly BEFORE the HTTP effect; the remote effect carries the row's
    ``operation_key`` as a ``(forge-op:<key>)`` message marker, so an outcome
    lost to a crash/timeout is resolved by PROBING the remote by identity —
    never by a blind replay:

    - ``operation_key`` is minted ONCE at intent creation and reused across
      every retry of this intent (the writer's per-``apply`` uuid4 was the
      bug — a crashed attempt's commit was unfindable by a fresh key);
    - ``expected_parent_oid`` is the branch head captured pre-dispatch; a
      probe match requires the found commit's parents to equal it, so a later
      repair commit (different key, repeating human message) is never
      attributed to this intent;
    - state machine (``_PUBLICATION_INTENT_STATES``): ``requested`` →
      ``dispatched`` → ``committed`` | ``adopted`` | ``duplicated`` |
      ``unknown`` (| ``probing`` | ``failed``). ``adopted`` is a first-class
      outcome: the remote effect of a PREVIOUS attempt exists and the caller
      advances the run on it exactly as if it had committed itself.

    The unique index makes find-or-create races collapse at the DB; the
    ``(status, next_probe_at)`` index serves the recovery scanner (due open
    intents first). Fields reuse forge's existing vocabulary — ``FlowRun``
    ids, ``ActionLog``-style ``remote_result`` JSON, ``StepRun``-style
    deadline columns.
    """

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
    #: ``gitlab`` | ``github`` | ``azure_devops`` (FlowRun.provider values).
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    #: Provider-scoped subject: GitLab project id, GitHub ``owner/name``,
    #: AzDO ``project/repo`` — probe correlation only.
    repo: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The effect kind: ``commit`` (branch publication) today, extensible to
    #: ``pr`` / ``comment`` / ``dispatch``.
    operation: Mapped[str] = mapped_column(String(20), nullable=False, default="commit")
    #: The branch the effect targets (comments/dispatches would use their
    #: anchor here).
    target_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Logical retry scope WITHIN (run, ref): one commit cycle per scope, so
    #: a repair cycle mints a NEW intent (and key) while retries of the same
    #: cycle reuse this row.
    idempotency_scope: Mapped[str] = mapped_column(String(100), nullable=False)
    #: ``(forge-op:<key>)`` — STABLE across retries; minted at creation.
    operation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ADR-0004 commit-cycle counter copied from the run (1 = initial).
    commit_cycle: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: Candidate tree/content digest — belt-and-braces probe verification.
    content_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Branch head captured at intent time (the CAS base); NULL = the intent
    #: expected a root commit (empty parent list).
    expected_parent_oid: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: Caller-pinned drift guard (mirrors the writer's ``expected_head``).
    expected_head: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="requested")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    #: Jittered backoff for the recovery scanner (NULL = due now).
    next_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: R17 shape (deadline-before-I/O); copied from the owning step when one
    #: drives the leg.
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The commit sha / PR number / build id once known (probe-found or
    #: response-carried).
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
    """ONE linked post-readiness review round (R40-02, #338).

    The recorded gap: the correction window closed at ``ready_for_human``,
    so reviewer feedback on the Draft MR earned ``correction_window_closed``
    — while the natural journey asks for corrections AFTER readiness is
    announced. The fix never reopens the terminal record: a follow-up round
    is a LINKED attempt/work unit — a NEW child ``FlowRun`` with its own
    admission, scope, numerical budget, base head and execution generation
    — and supersession is a RELATIONSHIP between deliveries, recorded here,
    never a rewrite of the earlier verified verdict or its evidence.

    - ``parent_run_id`` — the terminal run whose ready delivery the round
      corrects; its status, verdict, verification and candidate list are
      immutable historical evidence for the whole round's life.
    - ``child_run_id`` — the round's own work unit, created in the SAME
      transaction as this row (walked to ``proposing``; the MR stays the
      collaboration surface — the child carries the parent's ``mr_iid``
      and a confirmed reservation on the SAME branch).
    - ``root_run_id`` — the lineage root (delivery 1); round bounding and
      the one-outstanding-correction slot key on the LINEAGE, so a round
      two can itself be corrected by a round three without forking the
      accounting.
    - ``round_number`` — 1 is the original delivery (never a row here);
      the first post-readiness round is 2.
    - ``note_id`` — the originating /fix note (with ``parent_run_id`` the
      admission's idempotency key: one reviewer comment admits at most one
      round however many times the webhook replays).
    - ``base_head_sha`` — the EXACT approved current MR head at admission
      (human additions included); the child's ``base_sha``, so the round's
      candidate descends from the human-approved head and any later move
      is the existing stale-head / branch-drift conflict — never a
      force-push, never a replay against the old base.
    - ``decision_id`` — the deterministic correction decision identity
      (``correction_decision_id``), the authority name the round's
      active-plan seed and the request's lifecycle both carry.
    """

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
