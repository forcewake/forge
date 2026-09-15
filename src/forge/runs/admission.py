"""Admission gate (ADR-0018 §3): refuse a run before the first paid call.

``/implement`` passes an admission check *before* the planner is invoked —
an actor who may never approve a plan must never be able to burn LLM budget
either, and a configuration that lists the bot username among the human
approvers is a contradiction the runtime refuses rather than exploits.

The check is pure policy over settings (no I/O, no model calls), so the
start_run step handler can apply it synchronously: a denial parks the run as
``blocked(admission_denied: …)`` with a journaled note and the planner is
never constructed a prompt.
"""

from __future__ import annotations

from dataclasses import dataclass

from forge.config import ForgeConfig, Settings


@dataclass(frozen=True)
class AdmissionDecision:
    """The outcome of the pre-spend admission check for one /implement."""

    allowed: bool
    reason: str


def _split_names(raw: str | None) -> list[str]:
    return [name.strip() for name in (raw or "").split(",") if name.strip()]


def _approvers_field(provider: str, settings: Settings) -> str:
    """The settings field *provider*'s approver list is read from.

    ``FORGE_GITHUB_APPROVERS`` scopes the GitHub connection and
    ``FORGE_AZDO_APPROVERS`` the Azure DevOps one (ADR-0024); empty falls
    back to the shared ``FORGE_APPROVERS`` for single-list deployments.
    """
    if provider == "github" and str(getattr(settings, "FORGE_GITHUB_APPROVERS", "") or "").strip():
        return "FORGE_GITHUB_APPROVERS"
    if (
        provider == "azure_devops"
        and str(getattr(settings, "FORGE_AZDO_APPROVERS", "") or "").strip()
    ):
        return "FORGE_AZDO_APPROVERS"
    return "FORGE_APPROVERS"


def approvers_for(provider: str, settings: Settings) -> frozenset[str]:
    """The connection-scoped approver set for *provider* (ADR-0018 §3).

    The GitHub connection resolves its own ``FORGE_GITHUB_APPROVERS`` logins
    (empty → the ``FORGE_APPROVERS`` fallback); every other provider —
    GitLab — reads ``FORGE_APPROVERS`` only. The lists never merge, so a
    GitLab username in the shared list can never approve (or spend on) a
    GitHub run, and vice versa.
    """
    field = _approvers_field(provider, settings)
    return frozenset(_split_names(str(getattr(settings, field, "") or "")))


def check_admission(
    settings: Settings,
    forge_config: ForgeConfig,
    project_id: int,
    actor: str,
    provider: str = "gitlab",
) -> AdmissionDecision:
    """Decide whether *actor* may start a run on *project_id* (ADR-0018 §3).

    Denied — with a reason, and without any LLM/paid call — when the actor
    is not in *provider*'s approver list (only trusted approvers may spend),
    or when the configured bot username appears in it (forge must never be
    able to approve its own plans). *forge_config* and *project_id* scope
    the signature for the project-level policy (denylist / onboarding) that
    lands with the onboarding gate.
    """
    approvers = approvers_for(provider, settings)
    bot = str(getattr(settings, "FORGE_BOT_USERNAME", "") or "").strip()
    if bot and bot in approvers:
        return AdmissionDecision(
            allowed=False,
            reason=(
                f"config error: bot username @{bot} must not appear in "
                f"{_approvers_field(provider, settings)}"
            ),
        )
    if actor not in approvers:
        return AdmissionDecision(allowed=False, reason=f"actor @{actor} not in approvers")
    return AdmissionDecision(allowed=True, reason="admitted")
