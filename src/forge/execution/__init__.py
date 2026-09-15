"""Execution adapters (ADR-0019/0020/0024): where the implementer runs.

The builtin backend runs the model inside the forge worker; the execution
adapters delegate it to the *target repository's* CI (never forge
infrastructure, ADR-0002) and bring the candidate back as a proposal-only
artifact for the trusted publisher (ADR-0016):

- :mod:`forge.execution.github_actions` — GitHub Actions as the second
  execution adapter (ADR-0020): ``workflow_dispatch`` on a human-applied
  harness workflow, candidate returned as an Actions artifact.
- :mod:`forge.execution.azure_pipelines` — Azure Pipelines as the third
  execution adapter (ADR-0024): Runs-API dispatch of a human-applied lane
  pipeline, candidate returned as a pipeline artifact (runId == buildId).
"""

from forge.execution.azure_pipelines import (
    AzurePipelinesExecutor,
    AzurePipelinesHandle,
)
from forge.execution.github_actions import (
    ActionsHandle,
    GitHubActionsExecutor,
    artifact_name_for,
)

__all__ = [
    "ActionsHandle",
    "AzurePipelinesExecutor",
    "AzurePipelinesHandle",
    "GitHubActionsExecutor",
    "artifact_name_for",
]
