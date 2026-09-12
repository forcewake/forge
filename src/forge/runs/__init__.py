"""Durable M1 run loop (ADR-0004): issue note → gate → MR → CI → ready.

Public surface:

- :class:`forge.runs.service.RunService` — owns the whole advance loop.
- :func:`forge.runs.reconciler.run_reconciler` — the poll-based CI reconciler.
- :mod:`forge.runs.stubs` — deterministic planner/implementer stand-ins.
"""

from forge.runs.reconciler import run_reconciler
from forge.runs.service import GATE_TTL_SECONDS, RunService, execute_run_command
from forge.runs.stubs import (
    StubImplementer,
    StubPlanner,
    factory_branch,
    plan_digest_of,
    short_run_id,
)

__all__ = [
    "GATE_TTL_SECONDS",
    "RunService",
    "StubImplementer",
    "StubPlanner",
    "execute_run_command",
    "factory_branch",
    "plan_digest_of",
    "run_reconciler",
    "short_run_id",
]
