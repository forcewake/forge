"""Execution adapters (ADR-0019/0020/0024): where the implementer runs.

The builtin backend runs the model inside the forge worker; the execution
adapters delegate it to the *target repository's* CI (never forge
infrastructure, ADR-0002) and bring the candidate back as a proposal-only
artifact for the trusted publisher (ADR-0016):"""

from forge.execution.github_actions import (
    ActionsHandle,
    GitHubActionsExecutor,
    artifact_name_for,
)

__all__ = [
    "ActionsHandle",
    "GitHubActionsExecutor",
    "artifact_name_for",
]
