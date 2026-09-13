"""Stub planner, implementer and reviewer for tests and offline runs.

Deterministic stand-ins behind the exact same seams the real factory agents
(:mod:`forge.factory.planner` / ``.implementer`` / ``.reviewer``) occupy:
``RunService`` constructor-injects agents, defaulting to the real LLM ones;
tests inject these stubs (or fakes) instead. Like the real agents, the
methods are ``async`` and accept the same optional keyword arguments — they
just answer instantly without a model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from forge.durable import factory_branch, plan_digest_of, short_run_id
from forge.repository.changeset import Change, ChangeSet, Operation

if TYPE_CHECKING:
    from forge.durable.models import FlowRun

__all__ = [
    "StubImplementer",
    "StubPlanner",
    "StubReviewResult",
    "StubReviewer",
    "factory_branch",
    "plan_digest_of",
    "short_run_id",
    "stub_plan_json",
]


class StubPlanner:
    """Deterministic markdown plan — stands in for the LLM planner agent."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Accepts (and ignores) the LLM client / settings the real agent takes,
        # so tests can swap it in wherever LLMPlanner is constructed.
        self.last_plan: dict[str, Any] | None = None

    async def plan(
        self,
        issue_title: str,
        issue_description: str,
        *,
        flow_run_id: str | None = None,
    ) -> str:
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
    """Deterministic one-file ChangeSet — stands in for the LLM implementer.

    Proposes exactly one CREATE under ``forge-demo/``; the branch and commit
    message are derived from the run identity, never from issue text.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Same constructor tolerance as StubPlanner (see above).
        pass

    async def propose(
        self,
        run: FlowRun,
        issue_title: str,
        *,
        plan_summary: str = "",
        files_hint: list[str] | None = None,
        repair_context: str = "",
        attempt_base: str | None = None,
    ) -> ChangeSet:
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


@dataclass(frozen=True)
class StubReviewResult:
    """Attribute-compatible stand-in for the real reviewer's verdict object."""

    verdict: str = "ok"
    summary: str = "Stub review: no concerns."
    findings: tuple = field(default=())


class StubReviewer:
    """Deterministic "ok" verdict — stands in for the LLM reviewer."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = StubReviewResult()

    async def review(
        self,
        *,
        project_id: int,
        issue_title: str,
        plan_summary: str,
        base_sha: str,
        candidate_sha: str,
        flow_run_id: str | None = None,
    ) -> StubReviewResult:
        self.calls.append(
            {
                "project_id": project_id,
                "issue_title": issue_title,
                "plan_summary": plan_summary,
                "base_sha": base_sha,
                "candidate_sha": candidate_sha,
            }
        )
        return self.result


def stub_plan_json(title: str, description: str) -> str:
    """A valid planner-style JSON response, handy for scripted fake LLMs."""
    return json.dumps(
        {
            "summary": f"Implement: {title}",
            "steps": ["create the demo file"],
            "risks": [],
            "files_hint": [],
        }
    )
