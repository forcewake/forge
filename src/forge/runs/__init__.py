"""Durable M2 run loop (ADR-0004): issue → gate → MR → CI → ready.

Public surface:

- :class:`forge.runs.service.RunService` — owns the whole advance loop, with
  constructor-injected factory agents (LLM-driven by default).
- :func:`forge.runs.reconciler.run_reconciler` — the poll-based CI reconciler.
- :mod:`forge.runs.stubs` — deterministic planner/implementer/reviewer
  stand-ins used by tests and offline runs.
- :mod:`forge.runs.ci_contract` — the ADR-0008 quality contract and CI
  failure classification.
"""

from forge.runs.ci_contract import classify_failure, evaluate_quality_contract
from forge.runs.reconciler import run_reconciler
from forge.runs.service import GATE_TTL_SECONDS, RunService, execute_run_command
from forge.runs.stubs import (
    StubImplementer,
    StubPlanner,
    StubReviewer,
    factory_branch,
    plan_digest_of,
    short_run_id,
)

__all__ = [
    "GATE_TTL_SECONDS",
    "RunService",
    "StubImplementer",
    "StubPlanner",
    "StubReviewer",
    "classify_failure",
    "execute_run_command",
    "evaluate_quality_contract",
    "factory_branch",
    "plan_digest_of",
    "run_reconciler",
    "short_run_id",
]
