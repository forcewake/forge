"""Bind external effects to the worker claim that still owns the step."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class ExecutionClaim:
    """One claimed step's execution identity."""

    step_id: int
    attempt: int
    owner: str
    fence_token: int
    #: The run's cancellation generation when the claim was minted (``None`` = no run
    #: binding known at claim time; only the flag-level grant check applies then).
    cancellation_generation: int | None = None


_claim_var: ContextVar[ExecutionClaim | None] = ContextVar("forge_execution_claim", default=None)


def current_claim() -> ExecutionClaim | None:
    """The claim bound to the current task, if any."""
    return _claim_var.get()


@contextmanager
def bind_claim(claim: ExecutionClaim) -> Iterator[None]:
    """Bind *claim* as the current task's execution claim for the block."""
    token = _claim_var.set(claim)
    try:
        yield
    finally:
        _claim_var.reset(token)
