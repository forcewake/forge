"""Run identity helpers (ADR-0006/0009).

Branch naming, short ids and the plan digest the human gate binds to. Kept
dependency-free so both the durable layer and the factory agents can use them
without import cycles.
"""

from __future__ import annotations

import hashlib


def factory_branch(issue_iid: int | None, run_id: str) -> str:
    """The run-owned branch: ``factory/<issue-iid>/<run-id[:8]>`` (ADR-0006).

    No user-supplied text appears in the mandatory part of the ref.
    """
    return f"factory/{issue_iid or 0}/{run_id[:8]}"


def short_run_id(run_id: str) -> str:
    """Canonical short form of a run id used in branch names and messages."""
    return run_id[:8]


def plan_digest_of(plan: str) -> str:
    """sha256 hex digest of the plan text (what the gate binds to)."""
    return hashlib.sha256(plan.encode("utf-8")).hexdigest()
