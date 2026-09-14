"""Execution adapters (ADR-0019/0020): where the implementer actually runs.

The builtin backend runs the model inside the forge worker; the execution
adapters delegate it to the *target repository's* CI (never forge
infrastructure, ADR-0002) and bring the candidate back as a proposal-only
artifact for the trusted publisher (ADR-0016):

- :mod:`forge.execution.github_actions` — GitHub Actions as the second
  execution adapter (ADR-0020): ``workflow_dispatch`` on a human-applied
  harness workflow, candidate returned as an Actions artifact.
"""

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
