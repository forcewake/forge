"""Durable execution foundation (M1).

Public surface:

- :class:`Controller` and :class:`FlowStatus` — the ADR-0004 lifecycle state
  machine with atomic outbox writes, action journaling and worker leases.
- :func:`forge.durable.gates.record_approval` / ``consume_approval`` — ADR-0009
  human gates.
- :func:`forge.durable.inbox.ingest_event` — idempotent webhook ingestion
  (ADR-0005).
- The six durable tables in :mod:`forge.durable.models`.
"""

from forge.durable.budgets import (
    BUDGET_EXHAUSTED,
    BudgetGuard,
    BudgetLimits,
    Reservation,
    budget_for_run,
    budget_limits_from_spec,
    close_budget,
    load_budget_guard,
    open_budget,
    open_budget_from_spec,
    reconcile_actual,
    reconcile_harness_receipt,
    reserve,
)
from forge.durable.claims import (
    ExecutionClaim,
    bind_claim,
    current_claim,
)
from forge.durable.controller import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    TRANSITION_EVENT_TYPE,
    ActionLogNotFound,
    Controller,
    ControllerError,
    FlowStatus,
    InvalidActionTransition,
    InvalidTransition,
    RunNotFound,
    StaleClaimError,
    StepRunNotFound,
    as_aware_utc,
)
from forge.durable.gates import (
    ControllerGateError,
    GateAlreadyConsumed,
    GateNotFound,
    consume_approval,
    is_valid,
    record_approval,
)
from forge.durable.identity import factory_branch, plan_digest_of, short_run_id
from forge.durable.inbox import (
    build_source_event_id,
    ingest_event,
    mark_processed,
    mark_rejected,
)
from forge.durable.models import (
    FLOW_STATUSES,
    ActionLog,
    BudgetReservation,
    EventInbox,
    FlowRun,
    GateApproval,
    LLMCall,
    Outbox,
    RunBudget,
    RunSpec,
    StepRun,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "BUDGET_EXHAUSTED",
    "ExecutionClaim",
    "FLOW_STATUSES",
    "ActionLog",
    "ActionLogNotFound",
    "BudgetGuard",
    "BudgetLimits",
    "BudgetReservation",
    "Controller",
    "ControllerError",
    "ControllerGateError",
    "EventInbox",
    "FlowRun",
    "FlowStatus",
    "GateAlreadyConsumed",
    "GateApproval",
    "GateNotFound",
    "InvalidActionTransition",
    "InvalidTransition",
    "LLMCall",
    "Outbox",
    "Reservation",
    "RunBudget",
    "RunNotFound",
    "RunSpec",
    "StaleClaimError",
    "TERMINAL_STATUSES",
    "TRANSITION_EVENT_TYPE",
    "StepRun",
    "StepRunNotFound",
    "as_aware_utc",
    "bind_claim",
    "budget_for_run",
    "budget_limits_from_spec",
    "build_source_event_id",
    "close_budget",
    "consume_approval",
    "current_claim",
    "factory_branch",
    "ingest_event",
    "is_valid",
    "load_budget_guard",
    "mark_processed",
    "mark_rejected",
    "open_budget",
    "open_budget_from_spec",
    "plan_digest_of",
    "reconcile_actual",
    "reconcile_harness_receipt",
    "record_approval",
    "reserve",
    "short_run_id",
]
