"""Durable M2 run loop (ADR-0004): issue → gate → MR → CI → ready.

Public surface:

- :class:`forge.runs.service.RunService` — owns the whole advance loop, with
  constructor-injected factory agents (LLM-driven by default).
- :func:`forge.runs.reconciler.run_reconciler` — the poll-based CI/harness
  reconciler.
- :mod:`forge.runs.backends` — the ADR-0015 pluggable implementer backends
  (``builtin`` and ``ci_harness``).
- :mod:`forge.runs.stubs` — deterministic planner/implementer/reviewer
  stand-ins used by tests and offline runs.
- :mod:`forge.runs.ci_contract` — the ADR-0008 quality contract and CI
  failure classification.
"""

from forge.runs.backends import (
    CITharnessBackend,
    BuiltinBackend,
    HarnessOutcome,
    ImplementerBackend,
    build_backend,
    is_harness_backend,
)
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
    "BuiltinBackend",
    "CITharnessBackend",
    "GATE_TTL_SECONDS",
    "HarnessOutcome",
    "ImplementerBackend",
    "RunService",
    "StubImplementer",
    "StubPlanner",
    "StubReviewer",
    "build_backend",
    "classify_failure",
    "execute_run_command",
    "evaluate_quality_contract",
    "factory_branch",
    "is_harness_backend",
    "plan_digest_of",
    "run_reconciler",
    "short_run_id",
]
