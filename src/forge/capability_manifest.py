"""Reachability-based capability manifest (review NXT-02, M0/P1).

The problem this module exists to end: adaptive surfaces exist as models and
seams, and prose (docstrings, research summaries) presents them more strongly
than the checked production path justifies. ``forge doctor`` and the tests
must be able to answer ONE question per capability honestly: **can a customer
reach this today, from which entry point, with what evidence?**

Every row states the strongest *proven* degree along four tiers (the review's
"four degrees", weakest → strongest):

``domain_contract``
    Models/schemas/pure rules exist and are unit-tested, but NO production
    entry point reaches them. ``entry_point`` is ``None`` — "not wired" is a
    first-class, explicit state, never prose.
``production_wiring``
    A production entry point reaches the capability (ingress → dispatch →
    backend verified in code and contract tests), but no recorded real-
    provider run of THIS surface exists.
``real_provider_scenario``
    Production-wired AND a recorded artifact of a real-provider run from
    that entry point exists (an evaluation document under ``docs/``).
``cross_process_recovery``
    A real-provider scenario whose kill/restart/cross-process recovery is
    also proven. No row claims this today — the tier exists so the ladder
    has an honest top, not an aspirational one.

Honesty rules enforced by :func:`manifest_problems` (structural, pure) and
:func:`validate_manifest` (adds on-disk evidence checks):

- A wired tier (``production_wiring`` and above) with ``entry_point=None``
  is an integrity error — the honesty guard NXT-02 demands.
- A ``domain_contract`` row with a non-None entry point is equally wrong:
  wired code is at least ``production_wiring``.
- A ``real_provider_scenario`` row must cite at least one recorded artifact
  under ``docs/`` (a test file alone never proves a provider ran).
- Every ``domain_contract`` row must carry a ``note`` saying WHY it is not
  wired (the ``not_run`` discipline of :mod:`forge.release_manifest`).
- Rows that name operator commands are cross-checked against the LIVE
  ingress command sets (:func:`ingress_routed_commands`): the manifest may
  never report a command the checked ingress does not route. Remove a
  binding in the gateway and this manifest FAILS, not silently passes.

The seeded registry below was derived by grepping the actual dispatch path
(gateway routers → ``RunService.run_command`` → backends / lane templates),
not from docstrings. ``tests/test_capability_manifest.py`` holds it to every
rule above; ``forge doctor --capabilities`` prints it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

__all__ = [
    "ADAPTIVE_OPERATOR_COMMANDS",
    "CAPABILITIES",
    "CLASSIC_OPERATOR_COMMANDS",
    "TIER_LADDER",
    "TIER_LEGEND",
    "Capability",
    "CapabilityManifestError",
    "Tier",
    "capabilities",
    "format_matrix",
    "ingress_routed_commands",
    "manifest_problems",
    "validate_manifest",
]

Tier = Literal[
    "domain_contract",
    "production_wiring",
    "real_provider_scenario",
    "cross_process_recovery",
]

#: The ladder, weakest → strongest. Order is load-bearing: a row's tier must
#: be a prefix-provable position, never a skip.
TIER_LADDER: Final[tuple[Tier, ...]] = (
    "domain_contract",
    "production_wiring",
    "real_provider_scenario",
    "cross_process_recovery",
)

TIER_LEGEND: Final[dict[str, str]] = {
    "domain_contract": "models/rules + unit tests only; NOT reachable from any "
    "production entry point (entry_point is None)",
    "production_wiring": "reachable from a verified production entry point; "
    "contract-tested; no recorded real-provider run of this surface",
    "real_provider_scenario": "production-wired AND a recorded real-provider run "
    "from that entry point (docs/ evaluation artifact)",
    "cross_process_recovery": "a real-provider scenario with proven cross-process "
    "kill/restart recovery — the honest top of the ladder (no row claims it today)",
}

#: Tiers that require a non-None entry point (the honesty guard).
WIRED_TIERS: Final[frozenset[str]] = frozenset(
    {"production_wiring", "real_provider_scenario", "cross_process_recovery"}
)

#: The classic operator commands the checked ingress routes (verified against
#: all three provider gateways by :func:`ingress_routed_commands`).
CLASSIC_OPERATOR_COMMANDS: Final[tuple[str, ...]] = (
    "/implement",
    "/go",
    "/cancel",
    "/retry",
    "/security",
    "/status",
    "/why-blocked",
    "/reconcile",
)

#: The adaptive operator commands the three provider ingresses route through
#: :mod:`forge.adaptive.command_router` (NXT-10) — gated behind
#: ``FORGE_ADAPTIVE_COMMANDS_ENABLED`` (default OFF; with the flag off the
#: verbs are not parsed at all). Verified against the gateway command sets
#: by :func:`ingress_routed_commands`.
ADAPTIVE_OPERATOR_COMMANDS: Final[tuple[str, ...]] = ("/pause", "/steer", "/answer", "/resume")


class CapabilityManifestError(Exception):
    """The capability manifest cannot be validated honestly."""


@dataclass(frozen=True)
class Capability:
    """One capability row: tier, production entry point, evidence.

    ``entry_point`` is the PRODUCTION dispatch descriptor (module symbol or
    config/template route) or ``None`` when nothing wires the capability.
    ``evidence`` holds repo-relative pointers (test files, evaluation docs,
    CI templates). ``commands`` names operator slash-commands the row claims
    as supported — cross-checked against the live ingress sets.
    """

    name: str
    tier: Tier
    entry_point: str | None
    evidence: tuple[str, ...]
    commands: tuple[str, ...] = ()
    note: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "tier": self.tier,
            "entry_point": self.entry_point,
            "evidence": list(self.evidence),
            "commands": list(self.commands),
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# The seeded registry — honest state of the tree at NXT-02 (2026-09-22).
#
# Every entry_point below was verified by reading the dispatch path:
# gateway/{router,github_webhook,azure_webhook} → runs/service.run_command →
# runs/backends → ci/templates. Adaptive rows carry entry_point=None because
# grep found no caller from that path — do not "fix" them by trusting a
# docstring; wire the path, then flip the row with the wiring's evidence.
# ---------------------------------------------------------------------------

_RUN_COMMAND_ENTRY = (
    "forge.gateway.router._RUN_COMMANDS -> forge.runs.service.RunService.run_command"
)

CAPABILITIES: Final[tuple[Capability, ...]] = (
    # ---- operator commands (the exact supported surface, NXT-02 AC-3) ----
    Capability(
        name="operator-commands/classic",
        tier="production_wiring",
        entry_point=_RUN_COMMAND_ENTRY,
        evidence=("tests/test_slash_routing.py", "tests/test_runs_revival.py"),
        commands=CLASSIC_OPERATOR_COMMANDS,
        note="All three provider ingresses (GitLab router, GitHub webhook, Azure "
        "webhook) route exactly this set onto the durable run-command path; "
        "anything else the ingress receives is 'Unknown run command' and ignored.",
    ),
    Capability(
        name="operator-commands/adaptive",
        tier="production_wiring",
        entry_point=(
            "forge.gateway.{router,github_webhook,azure_webhook} -> "
            "forge.adaptive.command_router.ControlCommandRouter -> "
            "forge.adaptive.wiring.OperatorControlService "
            "(FORGE_ADAPTIVE_COMMANDS_ENABLED, default OFF)"
        ),
        evidence=(
            "tests/test_adaptive_command_router.py",
            "tests/test_gateway_issue_lifecycle.py",
            "tests/test_github_webhook.py",
            "tests/test_azure_webhook.py",
        ),
        commands=ADAPTIVE_OPERATOR_COMMANDS,
        note="NXT-10: all three provider ingresses parse /pause /resume /steer "
        "/answer and route them through ControlCommandRouter — approver-gated "
        "like /go (never authorship), short-prefix run resolution scoped to the "
        "note's issue, ONE journaled reply note per note id — behind "
        "FORGE_ADAPTIVE_COMMANDS_ENABLED, default OFF (with the flag off the "
        "verbs are not parsed at all: zero routing). Honest bounds: the "
        "mailbox the commands land in is the in-memory reference "
        "(adaptive/durable-mailbox owns the Postgres swap), the lane-side "
        "consumer runs only with FORGE_STEERING_ENABLED, and no real-provider "
        "operator→mailbox→lane cycle is recorded yet.",
    ),
    # ---- batch CI harnesses (the four shipped one-shot drivers) ----
    Capability(
        name="harness/batch-claude-code",
        tier="real_provider_scenario",
        entry_point=(
            "FORGE_IMPLEMENTER_BACKEND=ci_harness[:claude-code] -> "
            "forge.runs.backends.CITharnessBackend.start -> "
            "ci/templates/claude-code.gitlab-ci.yml | ci/templates/forge-harness.github.yml"
        ),
        evidence=(
            "docs/reference/dogfooding.md",
            ".github/workflows/forge-harness.yml",
            "tests/test_harness_selection.py",
        ),
        note="Default driver; the dogfood loop on this repo (/implement → plan → "
        "/go → Actions agent → Draft PR) is the recorded real-provider run.",
    ),
    Capability(
        name="harness/batch-grok-build",
        tier="production_wiring",
        entry_point=(
            "FORGE_IMPLEMENTER_BACKEND=ci_harness:grok-build -> "
            "forge.runs.backends.CITharnessBackend.start -> "
            "ci/templates/grok.gitlab-ci.yml"
        ),
        evidence=("ci/templates/grok.gitlab-ci.yml", "tests/test_harness_selection.py"),
        note="Template + selection + credential checks wired; no recorded "
        "real-provider run artifact in-tree.",
    ),
    Capability(
        name="harness/batch-opencode",
        tier="production_wiring",
        entry_point=(
            "FORGE_IMPLEMENTER_BACKEND=ci_harness:opencode -> "
            "forge.runs.backends.CITharnessBackend.start -> "
            "ci/templates/opencode.gitlab-ci.yml"
        ),
        evidence=(
            "ci/templates/opencode.gitlab-ci.yml",
            "tests/test_harness_selection.py",
        ),
        note="Batch one-shot lane wired; no recorded real-provider run artifact "
        "in-tree (the interactive opencode-sdk-lane row is separate).",
    ),
    Capability(
        name="harness/batch-copilot",
        tier="production_wiring",
        entry_point=(
            "FORGE_IMPLEMENTER_BACKEND=ci_harness:copilot -> "
            "forge.runs.backends.CITharnessBackend.start -> "
            "ci/templates/copilot.gitlab-ci.yml"
        ),
        evidence=(
            "ci/templates/copilot.gitlab-ci.yml",
            "tests/test_harness_selection.py",
        ),
        note="Template + selection wired; no recorded real-provider run artifact in-tree.",
    ),
    # ---- the three interactive SDK lanes ----
    Capability(
        name="sdk-lane/claude",
        tier="real_provider_scenario",
        entry_point=(
            "FORGE_IMPLEMENTER_BACKEND=ci_harness:claude-sdk-lane -> "
            "ci/templates/claude-sdk-lane.gitlab-ci.yml -> "
            "python -m forge.lane_driver (drive_lane)"
        ),
        evidence=(
            "docs/evaluation/2026-09-21-drivers/claude-sdk-lane-runner-README.md",
            "docs/evaluation/2026-09-21-drivers/claude-sdk-lane-runner-trace.log",
            "tests/test_adaptive_driver_claude_sdk.py",
        ),
        note="The PROVEN runner cycle: issue #34 (forge-lab) → /implement → /go → "
        "pipeline 344 → lane_driver drove ClaudeSDKDriverClient → candidate → "
        "CI verification PASSED → Draft MR !21. Not cross_process_recovery: no "
        "pause→checkpoint→resume-on-another-runner has run.",
    ),
    Capability(
        name="sdk-lane/codex",
        tier="production_wiring",
        entry_point=(
            "FORGE_IMPLEMENTER_BACKEND=ci_harness:codex-sdk-lane -> "
            "ci/templates/codex-sdk-lane.gitlab-ci.yml -> "
            "python -m forge.lane_driver --driver codex (drive_codex_lane)"
        ),
        evidence=(
            "ci/templates/codex-sdk-lane.gitlab-ci.yml",
            "docs/evaluation/2026-09-21-drivers/codex-live.json",
            "docs/evaluation/2026-09-21-drivers/codex-e2e.json",
            "tests/test_adaptive_driver_codex_app.py",
        ),
        note="Lane wired and the model route pinned (FORGE_CODEX_MODEL > "
        "CODEX_MODEL > CLI default — a wrong default 'completes' in seconds "
        "with no work); the 2026-09-21 smokes prove the DRIVER CLIENT "
        "(wire + tiny edit/test), not a runner cycle through a real pipeline — "
        "the honesty bounds in docs/evaluation/2026-09-21-drivers/README.md say "
        "exactly that, so the tier stays production_wiring until a lane cycle "
        "is recorded.",
    ),
    Capability(
        name="sdk-lane/opencode",
        tier="production_wiring",
        entry_point=(
            "FORGE_IMPLEMENTER_BACKEND=ci_harness:opencode-sdk-lane -> "
            "ci/templates/opencode-sdk-lane.gitlab-ci.yml -> "
            "python -m forge.lane_driver --driver opencode (run_opencode_lane)"
        ),
        evidence=(
            "ci/templates/opencode-sdk-lane.gitlab-ci.yml",
            "docs/evaluation/2026-09-21-drivers/opencode-live.json",
            "docs/evaluation/2026-09-21-drivers/opencode-e2e.json",
            "tests/test_adaptive_driver_opencode.py",
        ),
        note="Lane wired; upstream npm distribution was broken (LIVE-found: the "
        "npm route 400s under opencode v2.0.10) and the template switched to "
        "the official installer — awaiting a first runner cycle, so the tier "
        "stays production_wiring. Driver-client smokes (live + e2e) are green.",
    ),
    # ---- adaptive substrate: domain contracts, NOT wired ----
    Capability(
        name="adaptive/discovery-service",
        tier="production_wiring",
        entry_point="runs/github_service._plan_and_publish -> maybe_run_discovery",
        evidence=(
            "tests/test_adaptive_discovery_stage.py",
            "docs/adaptive/discovery-splice.md",
        ),
        note="NXT-05 landed: the durable discovery stage (replay/recovery "
        "semantics, content-addressed evidence, evidence:<id> citation "
        "validation) is SPLICED into the GitHub /implement planning path — "
        "DISABLED BY DEFAULT (FORGE_DISCOVERY_ENABLED); the classic flow is "
        "byte-for-byte identical while off. GitLab/Azure planning paths not "
        "spliced yet.",
    ),
    Capability(
        name="adaptive/plan-revisions",
        tier="domain_contract",
        entry_point=None,
        evidence=("tests/test_adaptive_revisions.py",),
        note="Revision lifecycle rules (tactical/material split, CAS "
        "authorization epochs, question routing) are pure and tested; nothing "
        "in the production dispatch path calls them.",
    ),
    Capability(
        name="adaptive/durable-mailbox",
        tier="domain_contract",
        entry_point=None,
        evidence=("tests/test_adaptive_control.py", "tests/test_adaptive_wiring.py"),
        note="NXT-09: PostgresMailbox (migration 020, work-scoped dedup, the "
        "7-rung CAS ladder, dedup-before-epoch) is FI-tested on real Postgres "
        "— but the lane still mounts the IN-MEMORY mailbox; no production "
        "caller selects PostgresMailbox yet (the swap is one constructor "
        "away, MailboxSurface-compatible).",
    ),
    Capability(
        name="adaptive/pause-resume-checkpoint",
        tier="domain_contract",
        entry_point=None,
        evidence=("tests/test_adaptive_control.py", "tests/test_adaptive_runtime.py"),
        note="NXT-15..18: pause is now a REAL fence+checkpoint transaction "
        "(verified content-addressed WIP capture, honest partial/failed "
        "states, fresh-epoch resume with re-checked authorization; "
        "cross-instance restore proven in tests). Still NOT reachable from "
        "ingress: /pause is not routed, and no cross-RUNNER recovery cycle "
        "has run live (tests destroy and restore store instances, not "
        "runners).",
    ),
    Capability(
        name="adaptive/steering-bridge",
        tier="production_wiring",
        entry_point="lane_driver steering attach (FORGE_STEERING_ENABLED, default OFF)",
        evidence=("tests/test_adaptive_lane_control.py",),
        note="NXT-11: lane_driver ATTACHES the steering session on every lane "
        "behind FORGE_STEERING_ENABLED (default OFF; effect-intent ladder "
        "NXT-12: dispatching → vendor_accepted → application_observed, "
        "outcome_unknown never silently retried). Still off: /steer is not "
        "routed by any ingress and the lane-local mailbox is in-memory, so "
        "ON would be a structural no-op. The 2026-09-21 live smokes prove "
        "the DRIVER primitives, not the operator→mailbox→lane bridge.",
    ),
    Capability(
        name="adaptive/work-package-coordination",
        tier="domain_contract",
        entry_point=None,
        evidence=("tests/test_adaptive_workpackage.py",),
        note="WorkPackageCoordination drives children through a caller-supplied "
        "child-run factory; no production caller exists (review §13: "
        "coordination currently phases CALLS, not completed works).",
    ),
    Capability(
        name="adaptive/candidate-set-verification",
        tier="domain_contract",
        entry_point=None,
        evidence=("tests/test_adaptive_verification_sets.py",),
        note="The trusted verification lane / frozen CandidateSet recipes are "
        "pure rules; the production waiting_ci path still verifies per-run "
        "candidates, not frozen multi-repo candidate sets (review §12).",
    ),
    Capability(
        name="adaptive/executed-evidence-claims",
        tier="domain_contract",
        entry_point=None,
        evidence=("tests/test_adaptive_executed_evidence.py",),
        note="The executed/skipped/unsupported classifier over GitHub Actions "
        "runs is a library (gh CLI, read-only); nothing in the release or "
        "dispatch path consumes it yet.",
    ),
)


def capabilities() -> tuple[Capability, ...]:
    """The seeded reachability registry."""
    return CAPABILITIES


def ingress_routed_commands() -> frozenset[str]:
    """The slash-commands the CHECKED production ingress actually routes.

    Read live from the three provider gateways — the authoritative set the
    manifest's command claims are cross-checked against. If a binding is
    removed here, a manifest row that still claims the command becomes an
    integrity error (doctor fails), never a silent over-claim. The adaptive
    verbs count as routed when ANY gateway still binds them (the per-gateway
    ``_ADAPTIVE_NOTE_COMMANDS`` attributes — a missing binding reads as
    empty, so unbinding the verb everywhere breaks the row's claim).
    """
    from forge.gateway import azure_webhook, github_webhook
    from forge.gateway import router as gateway_router

    classic = (
        gateway_router._RUN_COMMANDS
        | github_webhook._GITHUB_RUN_COMMANDS
        | azure_webhook._AZDO_RUN_COMMANDS
    )
    adaptive = (
        getattr(gateway_router, "_ADAPTIVE_NOTE_COMMANDS", frozenset())
        | getattr(github_webhook, "_ADAPTIVE_NOTE_COMMANDS", frozenset())
        | getattr(azure_webhook, "_ADAPTIVE_NOTE_COMMANDS", frozenset())
    )
    return frozenset(classic) | frozenset(adaptive)


def manifest_problems(
    rows: tuple[Capability, ...] | list[Capability],
    *,
    routed_commands: frozenset[str] | None = None,
) -> list[str]:
    """Structural honesty check — pure, no filesystem.

    Returns every problem found (doctor renders them all). With
    *routed_commands* supplied, command-carrying rows are also verified
    against the live ingress sets: a manifest may never report an
    unavailable command as supported.
    """
    problems: list[str] = []
    seen: set[str] = set()
    live = ingress_routed_commands() if routed_commands is None else routed_commands

    for row in rows:
        where = f"{row.name!r}"
        if not row.name:
            problems.append("row with empty name")
            continue
        if row.name in seen:
            problems.append(f"{where}: duplicate capability name")
        seen.add(row.name)

        if row.tier not in TIER_LADDER:
            problems.append(f"{where}: unknown tier {row.tier!r}")

        # The honesty guard (NXT-02): no wired claim without an entry point,
        # and no "not wired" row that secretly has one.
        if row.tier in WIRED_TIERS and row.entry_point is None:
            problems.append(
                f"{where}: tier {row.tier!r} claims production wiring but "
                "entry_point is None — a wired capability must name its entry point"
            )
        if row.tier == "domain_contract" and row.entry_point is not None:
            problems.append(
                f"{where}: tier 'domain_contract' but entry_point is "
                f"{row.entry_point!r} — wired code is at least production_wiring"
            )

        if row.tier == "domain_contract" and not row.note:
            problems.append(f"{where}: a not-wired row must say WHY it is not wired")
        if row.tier != "domain_contract" and not row.evidence:
            problems.append(f"{where}: tier {row.tier!r} requires evidence pointers")
        if row.tier in {"real_provider_scenario", "cross_process_recovery"} and not any(
            pointer.startswith("docs/") for pointer in row.evidence
        ):
            problems.append(
                f"{where}: tier {row.tier!r} requires a recorded real-provider "
                "artifact under docs/ (a test file alone proves no provider ran)"
            )

        unsupported = sorted(set(row.commands) - live)
        if unsupported:
            problems.append(
                f"{where}: claims command(s) {unsupported} the checked ingress "
                "does not route — never report an unavailable command as supported"
            )

    return problems


def validate_manifest(
    rows: tuple[Capability, ...] | list[Capability] | None = None,
    root: Path | None = None,
) -> None:
    """Full validation: structural rules + on-disk evidence pointers.

    Raises :class:`CapabilityManifestError` on the first class of problem;
    used by tests and doctor so a drifted manifest fails loudly.
    """
    checked = list(rows) if rows is not None else list(CAPABILITIES)
    problems = manifest_problems(checked)
    if problems:
        raise CapabilityManifestError("; ".join(problems))

    base = root if root is not None else Path.cwd()
    missing = [
        f"{row.name}: {pointer}"
        for row in checked
        for pointer in row.evidence
        if not (base / pointer).is_file()
    ]
    if missing:
        raise CapabilityManifestError(
            "evidence pointer(s) do not exist in the tree: " + "; ".join(missing)
        )


def format_matrix(rows: tuple[Capability, ...] | list[Capability] | None = None) -> str:
    """The human-readable matrix ``forge doctor --capabilities`` prints.

    Not-wired rows are explicit — ``not wired`` is printed, never omitted,
    so a reader cannot mistake an unfinished path for a shipped one.
    """
    checked = list(rows) if rows is not None else list(CAPABILITIES)
    name_w = max((len(row.name) for row in checked), default=4)
    tier_w = max((len(row.tier) for row in checked), default=4)
    lines = [
        f"forge capabilities — {len(checked)} rows (tiers: domain_contract → "
        "production_wiring → real_provider_scenario → cross_process_recovery)",
        "",
    ]
    for row in checked:
        entry = row.entry_point if row.entry_point is not None else "not wired"
        lines.append(f"  {row.name:<{name_w}}  {row.tier:<{tier_w}}  {entry}")
        if row.commands:
            lines.append(f"  {'':<{name_w}}  commands: {', '.join(row.commands)}")
        if row.note:
            lines.append(f"  {'':<{name_w}}  {row.note}")
    not_wired = sum(1 for row in checked if row.entry_point is None)
    lines.append("")
    lines.append(
        f"  {not_wired} of {len(checked)} capabilities are NOT wired to any production entry point"
    )
    return "\n".join(lines)
