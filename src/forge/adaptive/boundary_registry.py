"""R36-20 (issue #279): the declarative OWNERSHIP registry for the
authority boundaries — the review's "same decision made in two places"
defect class made mechanically visible.

ADR-0029 §1 named five composition boundaries and their owner map as
PROSE. Every sibling since (collector identity #260, typed continuation
#261/#262, GC locks #263, cutover fence #264, envelope v2 #265,
installer ladder #266, PG gate #267, CE entry #268, closure #269,
discovery authority #270, live cohort #271, revision proof #272,
verification binding #273, operator API #274, lab pilot #275,
measurement #276, durable saga #277, system verification #278, ops
drills #280, profile records #281) LANDED one owning module per
decision. This registry is the machine-readable consolidation: each
boundary records the decision it owns, the owner modules, the
production callers that may depend on the owner's surface, the
mechanical confinement rule that guards it, and the negative contract —
what must NOT decide the same thing elsewhere. R38-17 (#318) added the
seventh entry (execution_delivery_spec, ADR-0032): the versioned
execution/delivery specification the lane templates consume.

The registry is consumed by ``tests/test_architecture_boundaries.py``:

- every ``src/forge`` module that IMPORTS an owner module must appear
  in that boundary's ``allowed_dependents`` — a NEW module entering an
  owned decision without registration fails the architectural test with
  an instruction to register (or route through the owner);
- the symbol-confinement allow-sets below are the single source the
  AST checks iterate — changing an allow-set is a reviewed registry
  edit, never a silent code drift;
- since R37-19 (#300) the same registration covers the labelled
  evaluation package ``forge.adaptive.reference``: its scenario modules
  compose owner CONTRACTS (registered like any caller) while the
  runtime entry points are barred from importing the package at all —
  the dependency-direction rules live in
  ``tests/test_reference_separation.py``.

This module is deliberately IMPORT-LIGHT (pure stdlib data, no forge
imports): the registry must be loadable by tooling and tests without
importing any owner.

Observability spelling (the review's names):
``architecture.legacy_call_sites`` — the confined legacy helpers and
their exhaustive allow-sets (LEGACY_LOOKUP_*); ``contract.provider_
conformance`` — the provider-conformance table the tests parametrize;
``compatibility.active_version_population`` — the compat-fixture
inventory (``forge.adaptive.compat_fixtures.compat_inventory``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "AuthorityBoundary",
    "BOUNDARIES",
    "ATTEMPT_START_CONSTRUCTORS",
    "COMPOSED_DISPATCH_ENTRIES",
    "CONTINUATION_MODE_CONSTRUCTION_MODULES",
    "GC_SWEEP_ENTRYPOINTS",
    "GC_UNLINK_EXEMPT_RECEIVERS",
    "LEGACY_LOOKUP_CHAIN_MODULES",
    "LEGACY_LOOKUP_CONFINED_FUNCTION",
    "MODE_VOCABULARY_HOME",
    "RESOLVE_REPOSITORY_CALLERS",
    "boundary_by_name",
    "owner_modules",
]


@dataclass(frozen=True)
class AuthorityBoundary:
    """One owned decision: its owners, its registered callers, its rule.

    ``allowed_dependents`` is the registration surface: a production
    module that imports an owner module MUST be listed here (tests and
    fixtures are outside ``src/forge`` and never consult the registry).
    Listing a module here is a reviewed claim that it routes the
    decision THROUGH the owner — it is not permission to re-implement
    the decision; the negative contract still applies.
    """

    #: The short name (ADR-0030's vocabulary).
    name: str
    #: The decision this boundary OWNS — one sentence.
    decision: str
    #: The module(s) that decide it. One decision, one owner entry.
    owner_modules: tuple[str, ...]
    #: Registered production dependents (importers of an owner).
    allowed_dependents: tuple[str, ...]
    #: The mechanical rule that guards the boundary (enforced by the
    #: architectural test; names the concrete check).
    enforcement: str
    #: The negative contract: what must NOT decide this elsewhere.
    negative_contract: str
    #: Honest, documented non-adoption (never fabricated parity).
    honest_gaps: str = ""
    #: Extra allow-set rows the enforcement consumes (rule-specific).
    rule_sets: tuple[tuple[str, ...], ...] = field(default=())


#: The owning boundaries: the six of the R36 session (ADR-0030) plus
#: the execution/delivery spec contract (R38-17 / #318, ADR-0032).
BOUNDARIES: tuple[AuthorityBoundary, ...] = (
    AuthorityBoundary(
        name="repository_identity_checkpoint_lifecycle",
        decision=(
            "WHICH authority answers checkpoint presence, upload and "
            "retention (filesystem index / postgres metadata / the "
            "authenticated channel proxy), and the lifecycle rules over "
            "it: the cutover fence, pins, the pending-GC journal and "
            "the sweep locks."
        ),
        owner_modules=(
            "forge.adaptive.checkpoint_repository",
            "forge.api_checkpoint_channel",
        ),
        allowed_dependents=(
            "forge.adaptive.checkpoint_migration",
            "forge.adaptive.compat_fixtures",
            "forge.adaptive.continuation",
            "forge.adaptive.operator_snapshot",
            "forge.adaptive.ops_drills",
            "forge.adaptive.revisions",
            "forge.adaptive.wiring",
            "forge.main",
            "forge.runs.github_service",
            "forge.runs.revival",
            "forge.runs.service",
        ),
        enforcement=(
            "resolve_repository(...) may be CALLED only from "
            "RESOLVE_REPOSITORY_CALLERS; the legacy lookup chain "
            "(_legacy_http_lookup, CheckpointStore._load_index, direct "
            "CheckpointStore construction) only from "
            "LEGACY_LOOKUP_CHAIN_MODULES — and inside runs.revival "
            "only within _legacy_http_lookup itself; blob unlinks only "
            "inside the GC_SWEEP_ENTRYPOINTS lock holds."
        ),
        negative_contract=(
            "No dispatch, control or retry path may read a checkpoint "
            "index directly, construct a store, consult a different "
            "authority on failure, or resolve durability from the "
            "environment on its own — a second authority read is the "
            "Q35-03 defect class this boundary exists to close."
        ),
    ),
    AuthorityBoundary(
        name="continuation_authorization",
        decision=(
            "WHICH recoverable state a retry continues from (fresh / "
            "required / restart / uncertain), from recorded evidence "
            "with lineage and the pinned checkpoint digest — plus the "
            "typed /retry command grammar that authorizes a discard."
        ),
        owner_modules=("forge.adaptive.continuation",),
        allowed_dependents=(
            "forge.adaptive.compat_fixtures",
            "forge.runs.github_service",
            "forge.runs.service",
        ),
        enforcement=(
            "Only continuation.decide_continuation / parse_recovery_"
            "request produce modes; the lane-dispatch vocabulary "
            "constants (LANE_RESUME_MODE_*) live in github_service and "
            "may be referenced there only as the documented initial-"
            "dispatch default and vocabulary validation — never to "
            "re-derive a mode a decision should select; ContinuationMode "
            "construction stays inside the owner. The GitLab lane "
            "(runs.service, R37-07/#288) consumes the SAME owner: it "
            "selects decisions via the owner's table and dispatches "
            "decision.resume_mode() through its pipeline-variable "
            "envelope, never a mode of its own derivation."
        ),
        negative_contract=(
            "No service may select a resume mode from its own reading "
            "of the evidence, honor the restart verb from prose, or "
            "collapse an unprovable checkpoint state into a dispatch "
            "decision — the decision is owned, persisted and reused."
        ),
        honest_gaps=(
            "The lane-resume dispatch contract is wired on GitHub "
            "(R32-04) and GitLab CE (R37-07/#288 — pipeline-variable "
            "envelope, attempt-scoped credentials, proven offline at "
            "production-entry discipline; the LIVE cross-runner drill "
            "is the R37-08 step). Azure remains the honest gap: it "
            "carries no resume-mode selection (nothing to pass it to). "
            "All three lanes share the retry REFUSAL semantics through "
            "runs.revival.retry_rejection."
        ),
    ),
    AuthorityBoundary(
        name="native_occupancy",
        decision=(
            "WHEN an execution slot is occupied and when it frees: the "
            "CAS lease, the derived occupancy vocabulary and the "
            "evidence-based release (never a local status guess)."
        ),
        owner_modules=(
            "forge.adaptive.admission",
            "forge.runs.admission",
        ),
        allowed_dependents=(
            "forge.adaptive.command_router",
            "forge.adaptive.operator_snapshot",
            "forge.adaptive.ops_drills",
            "forge.runs",
            "forge.runs.azure_service",
            "forge.runs.github_service",
            "forge.runs.service",
        ),
        enforcement=(
            "Import-registration only (the CAS is a database invariant, "
            "not an AST shape): a module touching the lease surface "
            "must be a registered dependent."
        ),
        negative_contract=(
            "No provider service may release a slot without evidence, "
            "derive occupancy from the run's local status, or hold two "
            "leases for one run — uq_execution_lease_open_run decides, "
            "not a read-then-write."
        ),
    ),
    AuthorityBoundary(
        name="candidate_publication",
        decision=(
            "WHETHER and HOW a candidate's effects are published: "
            "intent persisted before the provider call, adoption by "
            "native correlation on recovery, human-edit parks, and the "
            "durable two-writer phase admission."
        ),
        owner_modules=(
            "forge.adaptive.publication_saga",
            "forge.adaptive.saga_durable",
        ),
        allowed_dependents=(
            "forge.adaptive.two_writer_qualification",
            # #295 / R37-14: the NATIVE effect adapters (GitLab/GitHub
            # clients behind the saga's effect interface) compose the
            # durable saga's PublicationProvider seam with each provider's
            # REAL preconditions — client-side CAS on GitLab, native CAS on
            # GitHub, correlation by listed native identity only.
            "forge.adaptive.saga_native",
            # R37-19 / #300: the in-process reference remote (the
            # provider-shaped evaluation object extracted from saga_
            # durable) composes the SAME owner contracts — the typed
            # provider errors and the NativeCommit identity record —
            # from the labelled evaluation package. It decides nothing
            # a provider would; registration keeps its dependency on
            # the owner visible like any other caller's.
            "forge.adaptive.reference.native_shaped_remote",
        ),
        enforcement=(
            "Import-registration only: the publication write boundary "
            "itself is ADR-0016/0026's (mr_reservations + the one "
            "validated write path through runs.publisher policy); the "
            "registry pins WHO may compose sagas. The reference "
            "remote's import of the owner is a CONTRACT composition "
            "(typed errors + NativeCommit), never a runtime default — "
            "saga_durable's compat re-export is lazy so production "
            "imports stay reference-free."
        ),
        negative_contract=(
            "No orchestration path may create a provider effect "
            "without a persisted intent first, adopt an effect it "
            "cannot correlate natively, or force-overwrite a branch a "
            "human moved — superseded stands, never pretend-undone."
        ),
    ),
    AuthorityBoundary(
        name="verification_applicability",
        decision=(
            "WHETHER a verification verdict applies to the exact "
            "candidate and tested world it is cited against: subject "
            "binding, freshness, required-report inventory, applicability "
            "invalidation — and the one verdict vocabulary."
        ),
        owner_modules=(
            "forge.adaptive.verification_binding",
            "forge.adaptive.verification_sets",
            "forge.runs.usecases",
        ),
        allowed_dependents=(
            "forge.adaptive.independent_checks",
            "forge.adaptive.system_verification",
            "forge.adaptive.two_writer_qualification",
            # R37-15 (#296): the trusted verification executor ROUTES every
            # applicability decision through the owner (freeze_tested_world,
            # record_evidence, EvidenceLedger.applicable_to) — it never
            # re-derives binding/freshness itself; same reason as the twin.
            "forge.adaptive.verification_executor",
            # R37-19 (#300): the deterministic-reference twin (the scenario
            # machinery extracted from system_verification into the
            # labelled evaluation package) freezes its worlds and derives
            # its test bundles through the SAME owner entries — the
            # scenario composes the contracts, it never re-decides
            # applicability.
            "forge.adaptive.reference.system_twin",
            "forge.adaptive.workpackage",
            "forge.runs.azure_service",
            "forge.runs.github_service",
        ),
        enforcement=(
            "Import-registration plus the ADR-0027 core-import rule on "
            "runs.usecases (no forge.integrations.*, no forge.gateway.*)."
        ),
        negative_contract=(
            "No provider may count a green harness job as independent "
            "verification, apply a verdict to a candidate it does not "
            "bind, or re-derive the required-checks proof locally — "
            "WAIT is never a verdict and freshness is decided here."
        ),
    ),
    AuthorityBoundary(
        name="operator_projection",
        decision=(
            "WHAT an operator sees and may safely do: the derived state "
            "vocabulary, the CAS-guarded projection and the one "
            "subject-scoped authorized reader over the durable rows."
        ),
        owner_modules=(
            "forge.adaptive.operator_view",
            "forge.adaptive.operator_snapshot",
        ),
        allowed_dependents=(
            # NEXT-19 (#207): the credential binding/broker substrate
            # composes the owner's CanonicalSubject — the identity axis
            # binding keys are keyed by (family + connection + native
            # id). It consumes the subject TYPE and subject_of_run, it
            # never reads rows around the authorized snapshot reader.
            "forge.adaptive.credential_broker",
            "forge.adaptive.project_credentials",
            "forge.adaptive.support_bundle",
            "forge.api_operator",
        ),
        enforcement=(
            "Import-registration only: the projection composes the "
            "existing pure projections (never forks their parsing) and "
            "the snapshot reader is the only live-surface assembler."
        ),
        negative_contract=(
            "No surface may assert a state workers did not derive, "
            "offer an action outside the state × actor matrix, or read "
            "durable rows around the authorized subject-scoped reader."
        ),
    ),
    AuthorityBoundary(
        name="execution_delivery_spec",
        decision=(
            "WHICH execution and delivery choices a lane template "
            "consumes as PINS — the run/attempt identity, the driver + "
            "model route, the resume mode + pinned checkpoint ref, the "
            "credential delivery mode + ref, the profile digest and the "
            "artifact contract (collector entry, output root) — plus "
            "the supported composition matrix and its preflight "
            "refusals."
        ),
        owner_modules=("forge.adaptive.execution_spec",),
        allowed_dependents=(),
        enforcement=(
            "Import-registration only (the contract travels as template "
            "VARIABLES, not imports — the shipped templates render the "
            "pins at their variable-resolution headers; a production "
            "module composing the spec contract registers here like "
            "every other caller)."
        ),
        negative_contract=(
            "No dispatch, service or template may re-derive a pinned "
            "member from ambient variables (the model route, the resume "
            "contract, the credential route, the artifact contract), "
            "nor compose an unsupported provider/recipe/harness/"
            "credential combination — an unlisted composition is a "
            "preflight refusal, never an ambient fallback."
        ),
        honest_gaps=(
            "The dispatch services adopt the spec's rendering seam "
            "composition-by-composition (ADR-0029's ladder); the "
            "templates already consume the pins as documented "
            "(ADR-0032 §2). Azure carries no resume-mode surface and "
            "the GitLab batch lanes restore no checkpoint — the matrix "
            "records both as resume-incapable recipes rather than "
            "fabricating parity."
        ),
    ),
)


# ---------------------------------------------------------------------------
# The confinement allow-sets the architectural test iterates. Each set is
# EXHAUSTIVE: adding a module is a reviewed registry edit, and a reference
# from anywhere else is the `architecture.legacy_call_sites` finding.
# ---------------------------------------------------------------------------

#: Modules that may touch the RETIRED legacy lookup chain at all: the
#: store itself, the repository that wraps it, the versioned migration
#: adapter, and the one explicitly opt-in adapter (`FORGE_RETRY_LEGACY_
#: LOOKUP=1`, deleted at R38 — see revival._legacy_http_lookup).
LEGACY_LOOKUP_CHAIN_MODULES: tuple[str, ...] = (
    "forge.adaptive.checkpoint_migration",
    "forge.adaptive.checkpoint_repository",
    "forge.api_checkpoint_channel",
    "forge.runs.revival",
)

#: Inside ``forge.runs.revival`` the chain is reachable ONLY from this
#: function — the confined escape hatch itself.
LEGACY_LOOKUP_CONFINED_FUNCTION: str = "_legacy_http_lookup"

#: The ONLY modules that may CALL ``resolve_repository`` — the one
#: composition point (the store's own module composes itself; the HTTP
#: channel, the control service, the revision proof and the versioned
#: migration adapter are the registered composition sites).
RESOLVE_REPOSITORY_CALLERS: tuple[str, ...] = (
    "forge.adaptive.checkpoint_migration",
    "forge.adaptive.checkpoint_repository",
    "forge.adaptive.revisions",
    "forge.adaptive.wiring",
    "forge.api_checkpoint_channel",
)

#: The only modules that may CONSTRUCT ``AttemptStartSpec`` directly:
#: the type's home and the one adopted composition adapter. Production
#: services compose envelopes through
#: ``forge.adaptive.composition_adoption.compose_attempt_start``.
ATTEMPT_START_CONSTRUCTORS: tuple[str, ...] = (
    "forge.adaptive.composition_adoption",
    "forge.runs.composition",
)

#: The production dispatch entries that compose envelopes through the
#: adapter (the ADR-0027 ladder: one provider first; others adopt after
#: the composed suite passes — the registry records who has).
COMPOSED_DISPATCH_ENTRIES: tuple[str, ...] = ("forge.runs.github_service",)

#: The only modules that may CONSTRUCT a ``ContinuationMode``: the
#: owner (its decision table and ``matching_decision`` re-materializer)
#: and the compat-fixture reader (re-materializing a PERSISTED mode for
#: the versioned compat view — the same read ``matching_decision``
#: performs, never a re-derivation). The modules that may IMPORT the
#: continuation owner are the boundary's ``allowed_dependents`` above
#: (GitHub's dispatch leg plus the compat reader).
CONTINUATION_MODE_CONSTRUCTION_MODULES: tuple[str, ...] = (
    "forge.adaptive.compat_fixtures",
    "forge.adaptive.continuation",
)

#: The module that owns the lane-resume mode VOCABULARY (the constants
#: are the workflow-input contract the shipped GitHub template declares;
#: the lane-side reader is lane_driver.resume_mode over the env).
MODE_VOCABULARY_HOME: str = "forge.runs.github_service"

#: The sweep entry points: blob unlinks are legal ONLY inside these
#: function bodies (both hold the volume-wide GC reference/delete lock
#: from their final reference scan through the last unlink — R36-04).
GC_SWEEP_ENTRYPOINTS: tuple[str, ...] = (
    "_asweep_locked",
    "_sweep_locked",
)

#: Unlink receivers that are NOT CAS blobs and therefore exempt from the
#: sweep confinement: the atomic-write temp files (``tmp_name``), the
#: pins overlay file (``self._pins_path(...)``) and the pending-GC
#: journal file (the ``path`` local of CheckpointGcJournal.clear) —
#: enumerated EXHAUSTIVELY; any other receiver is a violation.
GC_UNLINK_EXEMPT_RECEIVERS: tuple[str, ...] = (
    "os.unlink(tmp_name)",
    "self._pins_path(...).unlink()",
    "CheckpointGcJournal.clear path.unlink()",
)


def boundary_by_name(name: str) -> AuthorityBoundary:
    """The boundary with this name — the registry's lookup helper."""
    for boundary in BOUNDARIES:
        if boundary.name == name:
            return boundary
    raise KeyError(f"no authority boundary named {name!r} — known: {[b.name for b in BOUNDARIES]}")


def owner_modules() -> tuple[str, ...]:
    """Every owner module across the boundaries (deduplicated, sorted)."""
    seen: dict[str, None] = {}
    for boundary in BOUNDARIES:
        for module in boundary.owner_modules:
            seen.setdefault(module, None)
    return tuple(sorted(seen))
