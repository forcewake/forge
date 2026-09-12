from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from forge.agents.base import ForgeAgent


def get_agent_class(agent_type: str) -> type[ForgeAgent]:
    """Return the agent class for the given type identifier.

    Imports are deferred to avoid circular dependencies.
    Falls back to ``CodeReviewAgent`` for unknown types (backward compat).
    """
    from forge.agents.chat import ChatAgent
    from forge.agents.code_reviewer import CodeReviewAgent
    from forge.agents.pipeline_debugger import PipelineDebuggerAgent
    from forge.agents.security_triage import SecurityTriageAgent

    _AGENT_TYPES: dict[str, type[ForgeAgent]] = {
        "code-reviewer": CodeReviewAgent,
        "chat": ChatAgent,
        "pipeline-debugger": PipelineDebuggerAgent,
        "security-triage": SecurityTriageAgent,
    }

    return _AGENT_TYPES.get(agent_type, CodeReviewAgent)
