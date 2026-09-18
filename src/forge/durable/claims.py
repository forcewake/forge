"""Execution claims (R10): the bridge from queue ownership to effect ownership.

The step runtime (ADR-0017) mints a fence token when a worker claims a step
row — the QUEUE side of the contract. :class:`ExecutionClaim` is the EFFECT
side: the same identity, carried into the claim's effectful handlers so every
downstream guard (publication grant, run transitions) can answer "is THIS
execution still the owner?" rather than "is the run still alive for
someone?".

The claim rides the existing dispatch as an ambient task context
(:func:`current_claim` / :func:`bind_claim`): the step runtime binds it
around :func:`forge.runs.execute_run_command`, and the write boundaries
(publisher reservation, guarded transitions) consult it — no service-level
signature has to change for the fence to hold. A contextvar is task-local by
construction, so concurrent steps on one worker cannot see each other's
claims.

``cancellation_generation`` is the publication-grant generation observed at
claim time (``None`` when the step was not bound to a run yet — command
steps bind at execution time). The publisher only grants a NEW reservation
while the run's generation still equals the pinned one; a cancel bumps the
generation (:meth:`forge.durable.controller.Controller.request_cancel`) and
instantly fences every claim minted before it.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class ExecutionClaim:
    """One claimed step's execution identity (R10).

    Minted by the step runtime at claim time; immutable for the life of the
    execution. ``fence_token`` fences the STEP row's commit/abort CASes,
    ``cancellation_generation`` fences the RUN's publication grant.
    """

    step_id: int
    attempt: int
    owner: str
    fence_token: int
    #: The run's cancellation generation when the claim was minted (``None``
    #: = no run binding known at claim time; only the flag-level grant check
    #: applies then).
    cancellation_generation: int | None = None


_claim_var: ContextVar[ExecutionClaim | None] = ContextVar("forge_execution_claim", default=None)


def current_claim() -> ExecutionClaim | None:
    """The claim bound to the current task, if any."""
    return _claim_var.get()


@contextmanager
def bind_claim(claim: ExecutionClaim) -> Iterator[None]:
    """Bind *claim* as the current task's execution claim for the block.

    Reset on exit (including exceptions), so a reclaimed/retried step never
    inherits a predecessor's identity.
    """
    token = _claim_var.set(claim)
    try:
        yield
    finally:
        _claim_var.reset(token)
