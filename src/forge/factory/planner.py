"""LLM planner agent (ADR-0004): issue -> implementation plan.

Pure: takes an :class:`~forge.factory.llm.LLMClient`, never touches the
controller or GitLab. Returns the rendered plan markdown (the exact text the
human gate's plan digest binds to, ADR-0009); the structured JSON it parsed
is kept on ``last_plan`` so the service can persist ``files_hint`` and a
summary into the run's evidence for the implementer and reviewer.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from forge.factory.llm import LLMClient, parse_json, truncate_chars

if TYPE_CHECKING:
    from forge.config import Settings

logger = logging.getLogger(__name__)

#: The strong tier: planning needs reasoning more than speed.
PLANNER_TIER = "strong"

#: Deterministic input budget (chars) for the planning prompt (ADR-0013).
PLANNER_MAX_INPUT_CHARS = 12000

#: How much of the plan markdown is kept as the evidence summary.
PLAN_SUMMARY_CHARS = 1500

_SYSTEM_PROMPT = (
    "You are the planning agent of a code-writing bot for GitLab projects.\n"
    "Given an issue, produce a short, concrete implementation plan.\n"
    "Respond with ONLY a JSON object with exactly these keys:\n"
    '{"summary": "<1-3 sentences>", "steps": ["<step>", ...], '
    '"risks": ["<risk>", ...], "files_hint": ["<path or extension>", ...]}\n'
    "Rules: steps and risks must be strings; files_hint lists paths or "
    'extensions (e.g. "src/app.py", ".py") likely to need changes; '
    "never propose changes to CI config, dependency lockfiles or forge's "
    "own configuration; do not invent file contents."
)


class LLMPlanner:
    """Plans an implementation from an issue via the LiteLLM proxy."""

    def __init__(self, llm: LLMClient, settings: Settings | None = None) -> None:
        self._llm = llm
        self._settings = settings
        #: Parsed JSON of the most recent plan (dict) or None on failure.
        self.last_plan: dict[str, Any] | None = None

    async def plan(
        self,
        issue_title: str,
        issue_description: str,
        *,
        flow_run_id: str | None = None,
        path_scope: list[str] | None = None,
    ) -> str:
        """Return the plan as markdown (the text the gate digest binds to).

        *path_scope* (v0.7 monorepo scoping) carries the project's
        ``implement.paths`` globs; when set, the prompt states the
        restriction so the plan aims inside it from the start.
        """
        self.last_plan = None
        user = f"Issue title: {issue_title}\n\nIssue description:\n{issue_description or '(empty)'}"
        if path_scope:
            user += "\n\nOnly modify files under: " + ", ".join(f"`{glob}`" for glob in path_scope)
        result = await self._llm.complete(
            tier=PLANNER_TIER,
            system=_SYSTEM_PROMPT,
            user=truncate_chars(user, PLANNER_MAX_INPUT_CHARS),
            role="planner",
            flow_run_id=flow_run_id,
            json_mode=True,
        )
        parsed = parse_json(result.text)
        self.last_plan = parsed
        return self.render(parsed)

    @staticmethod
    def render(plan: dict[str, Any]) -> str:
        """Render the parsed plan JSON as the plan-comment markdown."""
        summary = str(plan.get("summary", "")).strip() or "_(no summary)_"
        steps = _as_lines(plan.get("steps"))
        risks = _as_lines(plan.get("risks"))
        hint_list = [str(h) for h in (plan.get("files_hint") or []) if str(h).strip()]
        hints = ", ".join(f"`{h}`" for h in hint_list) or "_(none)_"

        parts = [
            "## Implementation plan",
            "",
            f"**Summary:** {summary}",
            "",
            "**Steps:**",
            steps,
            "",
            "**Risks:**",
            risks,
            "",
            f"**Files (hints):** {hints}",
        ]
        return "\n".join(parts).rstrip()

    def plan_summary(self, plan_markdown: str) -> str:
        """The evidence summary for this plan (markdown, capped)."""
        return truncate_chars(plan_markdown, PLAN_SUMMARY_CHARS)

    def files_hint(self) -> list[str]:
        """The files_hint of the most recent plan (empty when unknown)."""
        if not self.last_plan:
            return []
        return [str(h) for h in (self.last_plan.get("files_hint") or []) if str(h).strip()]


def _as_lines(value: Any) -> str:
    """Render a JSON list (or string) as markdown bullet lines."""
    if isinstance(value, str):
        items = [value] if value.strip() else []
    elif isinstance(value, list):
        items = [str(item) for item in value if str(item).strip()]
    else:
        items = []
    if not items:
        return "- _(none)_"
    return "\n".join(f"- {item}" for item in items)


__all__ = [
    "PLANNER_MAX_INPUT_CHARS",
    "PLANNER_TIER",
    "PLAN_SUMMARY_CHARS",
    "LLMPlanner",
]
