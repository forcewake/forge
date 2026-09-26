"""Q39-17 (issue #336): the closure matrix — per capability, the six
closure levels with the CURRENT evidence class recorded as DATA.

ADR-0033 §4's contract, extended by ADR-0034 (#353) with the seventh
capability (the budget-amendment application decision). A GitHub issue
closed is not every level proven: each of the decision owners
(ADR-0033 §1 — which plan is approved, which credential, which
attempt, which checkpoint, which candidate verified, which receipt
counts; ADR-0034 §1 — whether an operator amendment applies) carries a
small matrix over
six levels — domain contract / wired caller / executed process /
native execution / cross-process recovery / customer acceptance — and
each cell names its evidence CLASS and the repo-relative PATH of the
evidence it cites. The classes (the review's vocabulary):

- ``unit-proven`` — the owner's contract suite pins the decision;
- ``pe-proven`` — a production-entry trace (``tests/production_entry``)
  drives the same entry a customer invokes: real processes, real
  worker restarts, the modeled native surface;
- ``live`` — a committed live-provider record proves it against the
  REAL provider, with its failures recorded as failures;
- ``pending-human`` — the evidence chain is complete up to the human
  record; the approval is the only missing member;
- ``pending`` — the honest gap; the cell names where the gap is
  documented.

Two rules keep the matrix honest by construction:

- :meth:`ClosureMatrix.validate` checks EVERY evidence path exists
  under the repository root (a missing path is a validation failure —
  the matrix may never cite a phantom trace), that every capability
  carries exactly the six levels in order, that every class is in the
  vocabulary, and that every non-proven cell carries a note naming its
  gap (a silent gap is a fabricated closure);
- the render (:meth:`CapabilityClosure.render`,
  :meth:`ClosureMatrix.render`) prints each capability × level with
  its class and path, and a PENDING level renders pending — never
  proven. The publication is ``docs/operations/closure-matrix.md``
  (regenerated from :meth:`ClosureMatrix.render`; a drift test holds
  the doc in sync).

Import boundary (ADR-0027 §3): pure stdlib data + ``pathlib`` — the
matrix must be loadable by tooling and tests without importing any
owner module it cites.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CLOSURE_LEVELS",
    "CLOSURE_MATRIX_STAMP",
    "CapabilityClosure",
    "ClosureMatrix",
    "EVIDENCE_CLASSES",
    "LevelEvidence",
    "PENDING_EVIDENCE_CLASSES",
    "PROVEN_EVIDENCE_CLASSES",
    "closure_matrix",
]

#: The schema discriminator of the published matrix view.
CLOSURE_MATRIX_STAMP = "forge.closure.matrix/1"

#: The six closure levels, weakest first (ADR-0033 §4 / the issue's
#: scope item 5 — the order is the ladder: each level presumes the one
#: before it, and no class substitutes for another).
CLOSURE_LEVELS: tuple[str, ...] = (
    "domain-contract",
    "wired-caller",
    "executed-process",
    "native-execution",
    "cross-process-recovery",
    "customer-acceptance",
)

#: The evidence-class vocabulary. ``pending`` and ``pending-human``
#: are NOT closures — they render pending, never proven.
EVIDENCE_CLASSES: tuple[str, ...] = (
    "unit-proven",
    "pe-proven",
    "live",
    "pending-human",
    "pending",
)

#: The classes that PROVE their level.
PROVEN_EVIDENCE_CLASSES = frozenset({"unit-proven", "pe-proven", "live"})

#: The classes that render as a gap (honest, named, never proven).
PENDING_EVIDENCE_CLASSES = frozenset({"pending-human", "pending"})


@dataclass(frozen=True)
class LevelEvidence:
    """One capability × level cell: the class and the path it cites.

    *path* is repo-relative and MUST exist (validation). A pending
    cell's path names where the GAP is documented — the nearest honest
    anchor (the counterexample, the runbook, the record that carries
    the failing live arm), never a fabricated substitute.
    """

    level: str
    evidence_class: str
    path: str
    note: str = ""

    def render(self) -> str:
        """The honest cell text — pending renders pending, never proven."""
        if self.evidence_class in PROVEN_EVIDENCE_CLASSES:
            status = f"proven ({self.evidence_class})"
        elif self.evidence_class == "pending-human":
            status = "PENDING (human record missing)"
        else:
            status = "PENDING"
        return status


@dataclass(frozen=True)
class CapabilityClosure:
    """One decision owner's six levels (ADR-0033 §1's map, as data)."""

    #: The short capability key (``closure_of`` looks up by this).
    capability: str
    #: The decision this owner owns (ADR-0033 §1's phrasing).
    decision: str
    #: The ONE production owner module (or the owner pair the registry
    #: already records).
    owner: str
    #: The consumer contract callers compose (the schema word or the
    #: vocabulary the owner publishes).
    consumer_contract: str
    #: Exactly the six levels, in :data:`CLOSURE_LEVELS` order.
    evidence: tuple[LevelEvidence, ...]

    def level(self, name: str) -> LevelEvidence:
        """The cell for one level — the honest lookup (KeyError names
        the six)."""
        for cell in self.evidence:
            if cell.level == name:
                return cell
        raise KeyError(f"capability {self.capability!r} carries no level {name!r}")

    def proven_levels(self) -> tuple[str, ...]:
        """The levels whose evidence class proves them."""
        return tuple(cell.level for cell in self.evidence if cell.render().startswith("proven"))

    def pending_levels(self) -> tuple[str, ...]:
        """The levels that render as a gap — an issue closed in GitHub
        does not empty this set."""
        return tuple(cell.level for cell in self.evidence if not cell.render().startswith("proven"))

    def render(self) -> str:
        """The honest per-capability table."""
        lines = [
            f"### {self.capability} — {self.decision}",
            "",
            f"Owner: `{self.owner}` · consumer contract: `{self.consumer_contract}`",
            "",
            "| Level | Status | Evidence | Note |",
            "| --- | --- | --- | --- |",
        ]
        for cell in self.evidence:
            note = cell.note.replace("|", "\\|")
            lines.append(f"| {cell.level} | {cell.render()} | `{cell.path}` | {note} |")
        proven = len(self.proven_levels())
        lines.append("")
        lines.append(f"Closure: {proven}/6 levels proven; pending: {self.pending_levels() or '—'}")
        return "\n".join(lines)


@dataclass(frozen=True)
class ClosureMatrix:
    """The six decision owners' closure matrix (ADR-0033 §4)."""

    capabilities: tuple[CapabilityClosure, ...]

    def closure_of(self, capability: str) -> CapabilityClosure:
        """One capability's honest table — the lookup the publication
        and the operator surface consume."""
        for item in self.capabilities:
            if item.capability == capability:
                return item
        known = [item.capability for item in self.capabilities]
        raise KeyError(f"no closure capability named {capability!r} — known: {known}")

    def capability_names(self) -> tuple[str, ...]:
        return tuple(item.capability for item in self.capabilities)

    def validate(self, repo_root: Path) -> list[str]:
        """The honesty checks — every finding is a validation failure:

        - every evidence path EXISTS under *repo_root* (the matrix may
          never cite a phantom trace);
        - every capability carries exactly the six levels in order;
          every class is in the vocabulary;
        - every PENDING cell carries a note naming its gap (a silent
          gap is a fabricated closure);
        - no proven cell cites a path outside the repository.
        """
        findings: list[str] = []
        root = Path(repo_root)
        for item in self.capabilities:
            if [cell.level for cell in item.evidence] != list(CLOSURE_LEVELS):
                findings.append(
                    f"{item.capability}: levels must be exactly {list(CLOSURE_LEVELS)}, "
                    f"got {[cell.level for cell in item.evidence]}"
                )
            for cell in item.evidence:
                if cell.evidence_class not in EVIDENCE_CLASSES:
                    findings.append(
                        f"{item.capability}/{cell.level}: unknown evidence class "
                        f"{cell.evidence_class!r} (vocabulary: {list(EVIDENCE_CLASSES)})"
                    )
                if not (root / cell.path).exists():
                    findings.append(
                        f"{item.capability}/{cell.level}: evidence path {cell.path!r} "
                        "does not exist under the repository — the matrix cites a "
                        "phantom trace"
                    )
                if cell.evidence_class in PENDING_EVIDENCE_CLASSES and not cell.note.strip():
                    findings.append(
                        f"{item.capability}/{cell.level}: a pending cell without a "
                        "note fabricates closure — name the gap"
                    )
        return findings

    def render(self) -> str:
        """The published view (docs/operations/closure-matrix.md)."""
        total = sum(len(item.proven_levels()) for item in self.capabilities)
        cells = len(self.capabilities) * len(CLOSURE_LEVELS)
        lines = [
            "<!-- generated by forge.adaptive.closure_matrix.render -- begin -->",
            f"stamp: `{CLOSURE_MATRIX_STAMP}` · capabilities: {len(self.capabilities)} "
            f"· levels proven: {total}/{cells}",
            "",
        ]
        for item in self.capabilities:
            lines.append(item.render())
            lines.append("")
        return "\n".join(lines)


def _ev(level: str, evidence_class: str, path: str, note: str = "") -> LevelEvidence:
    return LevelEvidence(level=level, evidence_class=evidence_class, path=path, note=note)


def closure_matrix() -> ClosureMatrix:
    """The CURRENT matrix — data validated against the repository by
    ``tests/test_closure_matrix.py`` (a path that leaves the tree is a
    test failure, so the data below is the honest state, not an
    aspiration)."""
    return ClosureMatrix(
        capabilities=(
            # ---------------------------------------------------------
            # 1. which plan is approved (#321)
            # ---------------------------------------------------------
            CapabilityClosure(
                capability="approved-plan",
                decision="which plan is approved (the executor brief binds the ACTIVE revision's text)",
                owner="forge.adaptive.revisions",
                consumer_contract="forge.revision.approved-input/1",
                evidence=(
                    _ev(
                        "domain-contract",
                        "unit-proven",
                        "tests/test_adaptive_revisions.py",
                        "ApprovedInput resolution, the digest-verified revision content, "
                        "the labeled spec/spec-legacy adapters and the typed rebind refusals",
                    ),
                    _ev(
                        "wired-caller",
                        "unit-proven",
                        "tests/test_conformance_consumers.py",
                        "the #327 consumer arm drives a REAL RunService dispatch: the "
                        "persisted executor-input digest equals the recomputation from "
                        "the recorded dispatch variables (the boundary rule lives in "
                        "tests/test_architecture_boundaries.py)",
                    ),
                    _ev(
                        "executed-process",
                        "pe-proven",
                        "tests/production_entry/test_gitlab_revision_rebind.py",
                        "RB-1: the dispatched FORGE_PLAN carries revision 2's TEXT (not "
                        "the spec brief); the three-way executor digest equality holds",
                    ),
                    _ev(
                        "native-execution",
                        "pending",
                        "docs/evaluation/2026-09-25-combined-steering/README.md",
                        "the live counterexample that motivated #321; no live rebind "
                        "trace exists yet — the rebind is proven against the modeled "
                        "native surface only (the ladder's later rung)",
                    ),
                    _ev(
                        "cross-process-recovery",
                        "pe-proven",
                        "tests/production_entry/test_gitlab_revision_rebind.py",
                        "RB-1's restarted worker + re-dispatch; RB-2's stale-authority "
                        "arms (a late approval, a delayed steer, the legacy pointer)",
                    ),
                    _ev(
                        "customer-acceptance",
                        "pending",
                        "docs/releases/profile-records.md",
                        "no design-partner acceptance of a revision-bound run is "
                        "recorded; the profile store defines where it would land",
                    ),
                ),
            ),
            # ---------------------------------------------------------
            # 2. which credential authorizes the operation (#320)
            # ---------------------------------------------------------
            CapabilityClosure(
                capability="operation-grant",
                decision="which credential authorizes the operation (the persisted grant, not project membership)",
                owner="forge.adaptive.credential_broker (+ forge.api_lane_control as the judging endpoint)",
                consumer_contract="forge.credential.operation-grant/1",
                evidence=(
                    _ev(
                        "domain-contract",
                        "unit-proven",
                        "tests/test_credential_broker.py",
                        "the grant mints at dispatch authorization (the ABSOLUTE "
                        "deadline), merges idempotently per attempt+route+ref, and a "
                        "malformed document refuses typed (fail closed)",
                    ),
                    _ev(
                        "wired-caller",
                        "unit-proven",
                        "tests/test_conformance_consumers.py",
                        "the #327 consumer arms redeem through the REAL ASGI endpoint "
                        "joined on grant_id; the parked-DTO trap authorizes nothing "
                        "(the boundary rule lives in tests/test_architecture_boundaries.py)",
                    ),
                    _ev(
                        "executed-process",
                        "pe-proven",
                        "tests/production_entry/test_credential_dispatch.py",
                        "CD-2/CD-3: the real lane-control router over HTTP — the "
                        "granted route redeems, the sibling route refuses typed with "
                        "ZERO broker calls",
                    ),
                    _ev(
                        "native-execution",
                        "pending",
                        "docs/operations/credential-consumption.md",
                        "consumption/rotation proven at the runner boundary, but the "
                        "live flow ran BYOK/ambient — no live redemption under a grant "
                        "exists yet (the ladder's later rung)",
                    ),
                    _ev(
                        "cross-process-recovery",
                        "pe-proven",
                        "tests/production_entry/test_credential_dispatch.py",
                        "CD-4/CD-8: a NEW authorized attempt re-resolves; the old "
                        "attempt's token retires at the boundary",
                    ),
                    _ev(
                        "customer-acceptance",
                        "pending",
                        "docs/releases/profile-records.md",
                        "no customer record redeems under an operation grant yet",
                    ),
                ),
            ),
            # ---------------------------------------------------------
            # 3. which attempt is active (the continuation decision)
            # ---------------------------------------------------------
            CapabilityClosure(
                capability="active-attempt",
                decision="which attempt is active (the continuation decision over recorded evidence)",
                owner="forge.adaptive.continuation",
                consumer_contract="the resume words (fresh/required/restart) + runs.revival.retry_rejection",
                evidence=(
                    _ev(
                        "domain-contract",
                        "unit-proven",
                        "tests/test_adaptive_continuation.py",
                        "the decision table over recorded evidence; UNCERTAIN never "
                        "dispatches; resume_mode() refuses the undecidable",
                    ),
                    _ev(
                        "wired-caller",
                        "unit-proven",
                        "tests/test_architecture_boundaries.py",
                        "modes are decided by the owner, never re-derived at a "
                        "dispatch (rule 5 + the provider-conformance table: all three "
                        "providers consult the ONE refusal table)",
                    ),
                    _ev(
                        "executed-process",
                        "pe-proven",
                        "tests/production_entry/test_production_entry.py",
                        "PE-2 (a retry before useful work dispatches fresh) and PE-3 "
                        "(a required resume never becomes a silent restart — zero "
                        "vendor events)",
                    ),
                    _ev(
                        "native-execution",
                        "live",
                        "qualification/records/gitlab-ce-v1@live.json",
                        "the R37-08 live interrupt/resume arm: the pause/checkpoint/"
                        "cancel/resume mechanics ran against the live provider — "
                        "INFORMATIONAL, delivery FAILED (two resumed turns with empty "
                        "candidates); the cross-runner file-preservation claim stays open",
                    ),
                    _ev(
                        "cross-process-recovery",
                        "pe-proven",
                        "tests/production_entry/test_gitlab_ce_entry.py",
                        "CE-2: the runner loss classifies blocked with ZERO forge-side "
                        "model calls; /retry re-dispatches a second runner under the "
                        "same decision",
                    ),
                    _ev(
                        "customer-acceptance",
                        "pending",
                        "docs/releases/profile-records.md",
                        "the live arm failed delivery — no acceptance record exists "
                        "for the resume contract",
                    ),
                ),
            ),
            # ---------------------------------------------------------
            # 4. which checkpoint restores (the resume spec)
            # ---------------------------------------------------------
            CapabilityClosure(
                capability="checkpoint",
                decision="which checkpoint restores (the resume spec / activation receipts)",
                owner="forge.adaptive.checkpoint_repository + forge.api_checkpoint_channel",
                consumer_contract="the checkpoint lookup-outcome vocabulary + /lane/controls/resume-spec",
                evidence=(
                    _ev(
                        "domain-contract",
                        "unit-proven",
                        "tests/test_checkpoint_repository.py",
                        "the lookup-outcome vocabulary, the cutover fence, the CAS "
                        "pins and the GC locks",
                    ),
                    _ev(
                        "wired-caller",
                        "unit-proven",
                        "tests/test_architecture_boundaries.py",
                        "resolve_repository is the ONE authority composition point; "
                        "the legacy chain is confined to the enumerated allow-set",
                    ),
                    _ev(
                        "executed-process",
                        "pe-proven",
                        "tests/production_entry/test_production_entry.py",
                        "PE-1: a real capture/upload over HTTP through the lane-control "
                        "+ checkpoint-channel routers; PE-3: a rotted blob halts "
                        "wip_restore_failed",
                    ),
                    _ev(
                        "native-execution",
                        "live",
                        "qualification/records/gitlab-ce-v1@live.json",
                        "the live pause/checkpoint arm ran on the configured (unmarked "
                        "filesystem) authority, observed 2026-09-24; the postgres "
                        "authority is lab-proven (PE-4, PG-gated)",
                    ),
                    _ev(
                        "cross-process-recovery",
                        "pe-proven",
                        "tests/production_entry/test_gitlab_ce_entry.py",
                        "CE-2: the resumed lane rebuilds the exact generation; CE-4: a "
                        "required-restore failure starts no model turn",
                    ),
                    _ev(
                        "customer-acceptance",
                        "pending",
                        "docs/releases/profile-records.md",
                        "the live delivery arm failed — no acceptance record exists "
                        "for cross-runner recovery",
                    ),
                ),
            ),
            # ---------------------------------------------------------
            # 5. which candidate is verified (the verification binding)
            # ---------------------------------------------------------
            CapabilityClosure(
                capability="verified-candidate",
                decision="which candidate counts as verified (subject binding, freshness, applicability)",
                owner="forge.adaptive.verification_binding (+ verification_sets, runs.usecases)",
                consumer_contract="the one verdict vocabulary (WAIT is never a verdict)",
                evidence=(
                    _ev(
                        "domain-contract",
                        "unit-proven",
                        "tests/test_verification_binding.py",
                        "subject binding, freshness, the required-report inventory and "
                        "applicability invalidation",
                    ),
                    _ev(
                        "wired-caller",
                        "unit-proven",
                        "tests/test_architecture_boundaries.py",
                        "the verification_applicability boundary: no provider counts a "
                        "green harness job as independent verification",
                    ),
                    _ev(
                        "executed-process",
                        "pe-proven",
                        "tests/production_entry/test_system_verification_executor.py",
                        "the trusted executor routes every applicability decision "
                        "through the owner (freeze_tested_world / record_evidence / "
                        "applicable_to)",
                    ),
                    _ev(
                        "native-execution",
                        "live",
                        "qualification/records/gitlab-ce-v1@live.json",
                        "the live six-case oracle green on the exact candidate sha "
                        "(glm-5.3-flash through the customer's gateway, claude-code "
                        "2.1.273 on a real runner)",
                    ),
                    _ev(
                        "cross-process-recovery",
                        "pe-proven",
                        "tests/production_entry/test_gitlab_ce_entry.py",
                        "CE-2's verification arm: a green pipeline on the STALE base "
                        "sha never verifies; CE-3: the old attempt's late callback "
                        "cannot publish after the resume",
                    ),
                    _ev(
                        "customer-acceptance",
                        "pending-human",
                        "docs/releases/profile-records.md",
                        "the profile derives supported from live evidence; the "
                        "human-gated manifest holds it pending-approval — the "
                        "approval record is the only missing member",
                    ),
                ),
            ),
            # ---------------------------------------------------------
            # 6. which receipt counts (the usage identity)
            # ---------------------------------------------------------
            CapabilityClosure(
                capability="usage-receipt",
                decision="which receipt counts (the usage identity: run, attempt, receipt, source)",
                owner="forge.durable.budgets.ingest_usage_receipt (+ forge.adaptive.usage_ingestion, the artifact front door)",
                consumer_contract="the natural key — idempotent by construction, final replaces partial",
                evidence=(
                    _ev(
                        "domain-contract",
                        "unit-proven",
                        "tests/test_usage_receipts.py",
                        "the deterministic receipt identity, the normalization honesty "
                        "table and ON CONFLICT idempotence (R23)",
                    ),
                    _ev(
                        "wired-caller",
                        "unit-proven",
                        "tests/test_usage_ingestion.py",
                        "the artifact front door (the collector's artifact contract in "
                        "harness_entry, the R38-09 ingestion rules) is unit-pinned; no "
                        "boundary rule guards the usage identity yet — the extraction "
                        "ladder's next rung (ADR-0033 §3)",
                    ),
                    _ev(
                        "executed-process",
                        "pending",
                        "docs/operations/delivery-economics.md",
                        "the durable mapping folds usage_receipts rows into the "
                        "report, but no production-entry trace ingests a lane usage "
                        "artifact end to end — the R38-09 recorded gap closed at the "
                        "unit level only",
                    ),
                    _ev(
                        "native-execution",
                        "pending",
                        "qualification/records/gitlab-ce-v1@live.json",
                        "the live SDK lane reported receipt costs while the durable "
                        "rows stayed empty — live ingestion is the follow-up the "
                        "record names",
                    ),
                    _ev(
                        "cross-process-recovery",
                        "pending",
                        "tests/test_usage_ingestion.py",
                        "partial-to-final reconciliation and the crash re-run are "
                        "unit-pinned; no cross-process trace drives them",
                    ),
                    _ev(
                        "customer-acceptance",
                        "pending",
                        "docs/releases/profile-records.md",
                        "no customer economics record exists",
                    ),
                ),
            ),
            # ---------------------------------------------------------
            # 7. whether an operator amendment applies (#340, #353)
            # ---------------------------------------------------------
            CapabilityClosure(
                capability="budget-amendment",
                decision=(
                    "whether an operator budget amendment applies (one durable table,"
                    " the originating command identity, the enforcement resource moved)"
                ),
                owner="forge.durable.budgets.apply_budget_amendment (+ closing_budget.amendment_ledger_document, the one projection)",
                consumer_contract=(
                    "BudgetAmendmentCommand (run, command_id, axis, amount, reason)"
                    " — budget_amendments UNIQUE per (run, command)"
                ),
                evidence=(
                    _ev(
                        "domain-contract",
                        "unit-proven",
                        "tests/test_budget_amendment.py",
                        "the command identity (two identical commands are two "
                        "decisions, a redelivery applies once), per-axis atomic "
                        "application, typed refusals naming the limiting axis, the "
                        "closing partition's protected share",
                    ),
                    _ev(
                        "wired-caller",
                        "unit-proven",
                        "tests/test_architecture_boundaries.py",
                        "the applicants allow-set (R40-17/#353): only the owner and "
                        "the two provider continuation routes construct/apply the "
                        "command — the GitLab operator route and the GitHub leg "
                        "migrated off its legacy evidence ledger in the same change "
                        "(tests/test_github_runs.py pins the adoption)",
                    ),
                    _ev(
                        "executed-process",
                        "pe-proven",
                        "tests/production_entry/test_mutation_gates.py",
                        "MG-2: a calls-axis amendment re-opens the REAL guard for "
                        "exactly one reviewer call; the evidence-only mutation "
                        "cannot buy one",
                    ),
                    _ev(
                        "native-execution",
                        "pending",
                        "docs/operations/closing-budget.md",
                        "no live amendment record exists — the operator command "
                        "surface is proven against the modeled native surface and "
                        "the real guard only",
                    ),
                    _ev(
                        "cross-process-recovery",
                        "pe-proven",
                        "tests/production_entry/test_mutation_gates.py",
                        "MG-2's redelivery arm: the same command after completion "
                        "refuses honestly and the ledger holds exactly ONE applied "
                        "row — the provider stays quiet",
                    ),
                    _ev(
                        "customer-acceptance",
                        "pending",
                        "docs/releases/profile-records.md",
                        "no customer record exercises an operator amendment yet",
                    ),
                ),
            ),
        )
    )
