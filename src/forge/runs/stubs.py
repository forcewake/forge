"""Stub planner and implementer for the M1 vertical slice.

No LLM anywhere: both are pure deterministic functions so the durable loop
can be exercised end-to-end. The real planner/implementer agents replace
these classes without touching the controller or the service.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from forge.repository.changeset import Change, ChangeSet, Operation

if TYPE_CHECKING:
    from forge.durable.models import FlowRun


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


class StubPlanner:
    """Deterministic markdown plan — stands in for the real planner agent."""

    def plan(self, issue_title: str, issue_description: str) -> str:
        description = (issue_description or "").strip() or "_(no description)_"
        return (
            "## Implementation plan\n\n"
            f"- **Issue:** {issue_title}\n"
            f"- **Request:** {description}\n"
            "- **Approach:** create a single demo file describing the change\n"
            "- **Verification:** project CI pipeline on the Draft MR\n"
            "- **Scope:** one file under `forge-demo/`, no CI or config changes\n"
        )


class StubImplementer:
    """Deterministic one-file ChangeSet — stands in for the real implementer.

    Proposes exactly one CREATE under ``forge-demo/``; the branch and commit
    message are derived from the run identity, never from issue text.
    """

    def propose(self, run: FlowRun, issue_title: str) -> ChangeSet:
        short_id = short_run_id(run.id)
        path = f"forge-demo/run-{short_id}.md"
        # Placeholder digest reference: M1 has no plan text stored on the run,
        # so the file records the gate-bound digest (or "unknown" pre-planning).
        digest_placeholder = getattr(run, "plan_digest", None) or "<plan-digest>"
        content = (
            f"# Forge run {short_id}\n\n"
            f"- **Run id:** {run.id}\n"
            f"- **Issue:** {issue_title}\n"
            f"- **Plan digest:** {digest_placeholder}\n"
            f"- **Branch:** {factory_branch(run.issue_iid, run.id)}\n"
        )
        return ChangeSet(
            branch=factory_branch(run.issue_iid, run.id),
            commit_message=f"forge: implement {run.issue_iid or 0} (run {short_id})",
            changes=[Change(path=path, operation=Operation.CREATE, content=content)],
        )
