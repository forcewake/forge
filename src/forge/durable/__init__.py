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
    EventInbox,
    FlowRun,
    GateApproval,
    LLMCall,
    Outbox,
    RunSpec,
    StepRun,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "FLOW_STATUSES",
    "ActionLog",
    "ActionLogNotFound",
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
    "RunNotFound",
    "RunSpec",
    "TERMINAL_STATUSES",
    "TRANSITION_EVENT_TYPE",
    "StepRun",
    "StepRunNotFound",
    "as_aware_utc",
    "build_source_event_id",
    "consume_approval",
    "factory_branch",
    "ingest_event",
    "is_valid",
    "mark_processed",
    "mark_rejected",
    "plan_digest_of",
    "record_approval",
    "short_run_id",
]
