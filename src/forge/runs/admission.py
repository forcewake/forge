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


def check_admission(
    settings: Settings,
    forge_config: ForgeConfig,
    project_id: int,
    actor: str,
) -> AdmissionDecision:
    """Decide whether *actor* may start a run on *project_id* (ADR-0018 §3).

    Denied — with a reason, and without any LLM/paid call — when the actor is
    not in ``FORGE_APPROVERS`` (only trusted approvers may spend), or when the
    configured bot username appears in ``FORGE_APPROVERS`` (forge must never
    be able to approve its own plans). *forge_config* and *project_id* scope
    the signature for the project-level policy (denylist / onboarding) that
    lands with the onboarding gate.
    """
    approvers = _split_names(getattr(settings, "FORGE_APPROVERS", "") or "")
    bot = str(getattr(settings, "FORGE_BOT_USERNAME", "") or "").strip()
    if bot and bot in approvers:
        return AdmissionDecision(
            allowed=False,
            reason=f"config error: bot username @{bot} must not appear in FORGE_APPROVERS",
        )
    if actor not in approvers:
        return AdmissionDecision(allowed=False, reason=f"actor @{actor} not in approvers")
    return AdmissionDecision(allowed=True, reason="admitted")
