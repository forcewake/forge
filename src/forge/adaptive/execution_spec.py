"""R38-17 (issue #318): the VERSIONED execution/delivery specification.

The consolidation defect this module closes (#318 basis): one contract —
which model route a lane runs, which resume contract it executes, how its
credential is delivered, where its candidate lands — was resolved
independently at several layers, and the shipped lane templates still
re-derived some of those choices from AMBIENT template variables or
hard-coded defaults (the GitLab batch lanes ignored the dispatched model
pin entirely; the opencode SDK lane let an ambient project variable
OUTRANK the dispatched pin). The fix is not another framework: one small
frozen value, constructed ONCE at dispatch beside the attempt-start
envelope (ADR-0029's composition ladder — :mod:`forge.adaptive.
composition_adoption` is the sibling pattern), rendered into a SMALL set
of pinned template variables the shipped templates consume verbatim.

Three pieces:

- :class:`ExecutionSpec` (``forge.execution.spec/1``) — the frozen
  pin-set: run/attempt identity, driver + model route, resume mode +
  the pinned checkpoint ref, credential delivery mode + ref, the frozen
  execution-profile digest, and the artifact contract (collector entry
  + output root). Construction is fail-closed on every
  authority-bearing member (empty identity, a non-sha256 profile
  digest, a ``required`` resume without its pinned checkpoint ref, an
  unknown resume word) and runs the composition preflight below — an
  impossible provider/recipe/harness/credential combination is refused
  BEFORE any template variable renders. ``spec_digest()`` is sha256
  over the canonical document: a field change is a different digest,
  so a rendered template set is reconcilable against the persisted
  spec the dispatch approved.

- :func:`render_template_variables` — the pinned template-variable
  mapping (``FORGE_RUN_ID`` / ``FORGE_DRIVER`` / ``FORGE_MODEL`` /
  ``FORGE_LANE_RESUME_MODE`` / ``FORGE_RESUME_CHECKPOINT`` /
  ``FORGE_CREDENTIAL_REF`` / ``FORGE_CREDENTIAL_REDEEM`` / the spec
  version + digest). The AMBIENT fallbacks the shipped templates still
  carry are enumerated in :data:`AMBIENT_FALLBACK_VARIABLES` with the
  reason each is not authority-bearing; everything the spec pins is a
  pin the template consumes, never re-derives.

- the supported composition matrix — :func:`supported_compositions`
  (the small tested list of provider × runtime recipe × harness ×
  credential-route combinations with their caller/template/consumer
  contract versions) and :func:`preflight_composition` (the dispatch
  pre-check: an impossible combination refuses with a PRECISE
  incompatibility naming the axis and the supported alternatives —
  "driver X on provider Y lacks resume support; supported: ...").
  This is the seam the existing preflight surface composes: spec
  construction calls it, so the refusal fires at the same place the
  other dispatch pre-checks do (before any provider call).

Compatibility rule (ADR-0032 §4): a persisted spec document is read by
:func:`read_execution_spec`. A ``forge.execution.spec/1`` document
round-trips; a document claiming an unknown or NEWER schema version
refuses explicitly (naming the reader's supported version and the
instruction to upgrade the control plane first) — a v1 reader never
guesses at a v2 field, and an unknown top-level key is a refusal, not a
silent drop: additive evolution bumps the schema version.

Import boundary (ADR-0027 §3): core, import-light — only
:func:`forge.runs.spec.canonical_json_digest`. It imports no
``forge.integrations.*`` and no ``forge.gateway.*``.

Registered ownership: ``execution_delivery_spec`` in
:mod:`forge.adaptive.boundary_registry` (the registry's seventh entry).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from forge.runs.spec import canonical_json_digest

__all__ = [
    "AMBIENT_FALLBACK_VARIABLES",
    "CREDENTIAL_MODES",
    "CompositionRefusal",
    "ExecutionSpec",
    "EXECUTION_SPEC_SCHEMA",
    "EXECUTION_SPEC_VERSION",
    "RESUME_MODE_WORDS",
    "RUNTIME_RECIPES",
    "SUPPORTED_PROVIDERS",
    "SupportedComposition",
    "compose_execution_spec",
    "preflight_composition",
    "read_execution_spec",
    "render_template_variables",
    "supported_compositions",
]

#: The schema discriminator every execution spec carries (persisted and
#: rendered). Additive field changes bump this word; readers refuse
#: unknown versions explicitly (the compatibility rule above).
EXECUTION_SPEC_SCHEMA = "forge.execution.spec/1"

#: The numeric reader version :func:`read_execution_spec` supports.
EXECUTION_SPEC_VERSION = 1

#: The resume words the spec may PIN — the continuation decision's own
#: vocabulary (:mod:`forge.adaptive.continuation`), validated here as a
#: pass-through ONLY (the spec pins the selected word; it never derives
#: one). ``required`` additionally demands the pinned checkpoint ref.
RESUME_MODE_WORDS = frozenset({"fresh", "required", "restart"})

#: The credential delivery modes (#303's vocabulary, pinned by value —
#: importing :mod:`forge.adaptive.credential_broker` here would pull the
#: broker's surface into the spec's import graph for four words).
CREDENTIAL_MODES = frozenset(
    {
        "github-native-secret",
        "azure-variable-group",
        "gitlab-protected-variable",
        "runner-redemption",
        "ambient-legacy",
    }
)

#: The provider legs the matrix knows (the dispatch profiles the
#: credential broker and the shipped templates name).
SUPPORTED_PROVIDERS = frozenset({"github", "gitlab", "azure"})

#: The runtime recipes the shipped lane templates implement. The recipe
#: decides resume capability: the scripted batch lanes restore no
#: checkpoint (a ``required`` resume refuses in the template itself),
#: the SDK lane and the GitHub harness restore exactly; the Azure lane
#: carries no resume-mode surface at all (the registry's honest gap).
RUNTIME_RECIPES = frozenset(
    {
        "github-harness-entry",
        "gitlab-sdk-lane",
        "gitlab-batch-script",
        "azure-harness-entry",
    }
)

#: The recipes that CAN restore a held checkpoint (a ``required``
#: resume is executable). Everything else in :data:`RUNTIME_RECIPES`
#: refuses a required resume — at preflight here, and again in the
#: template before any vendor call.
RESUME_CAPABLE_RECIPES = frozenset({"github-harness-entry", "gitlab-sdk-lane"})


class CompositionRefusal(ValueError):
    """An impossible composition (or a malformed spec document) refused.

    The message names the axis and the supported alternatives — the
    precise incompatibility #318's negative test demands. Raised by
    :func:`preflight_composition`, by :func:`compose_execution_spec`
    (field validation), and by :func:`read_execution_spec` (the
    compatibility rule).
    """


@dataclass(frozen=True)
class SupportedComposition:
    """One supported row of the composition matrix.

    The combination is (provider × runtime recipe × harness ×
    credential route); the contract versions are what the row's caller
    (the dispatch envelope), template (the variable-resolution header)
    and consumer (the lane-side collector/meta contract) must speak for
    the combination to compose. ``resume_supported`` is derived from
    the recipe (see :data:`RESUME_CAPABLE_RECIPES`) and recorded on the
    row so a preflight refusal can name the capability, not just the
    spelling.
    """

    provider: str
    runtime_recipe: str
    harness: str
    credential_route: str
    #: The dispatch-envelope contract version the row's caller speaks.
    caller_contract: str
    #: The execution-spec contract version the row's template consumes.
    template_contract: str
    #: The lane-side consumer contract (the candidate meta schema the
    #: collector emits and the control plane ingests).
    consumer_contract: str
    resume_supported: bool = field(default=False)

    def combination_key(self) -> str:
        """The row's display key (refusal messages quote it)."""
        return (
            f"provider {self.provider!r} x recipe {self.runtime_recipe!r} "
            f"x harness {self.harness!r} x credential route {self.credential_route!r}"
        )


#: The caller contract every current row speaks: the v2 attempt-start
#: envelope (:data:`forge.adaptive.composition_adoption.ATTEMPT_START_VERSION`).
_CALLER_CONTRACT = "forge.attempt-start/2"

#: The consumer contract every current row speaks: the candidate meta
#: schema v2 (the collector's emit-meta contract the shipped GitHub
#: template documents as "Meta schema v2").
_CONSUMER_CONTRACT = "forge.candidate-meta/2"


def supported_compositions() -> tuple[SupportedComposition, ...]:
    """The small tested composition matrix (issue #318 §6).

    A recipe for the row set: each row names a combination the shipped
    templates implement and the dispatch legs can compose, with the
    contract versions the three sides must speak. Anything NOT listed
    refuses at :func:`preflight_composition` — an unlisted combination
    is unsupported (a refusal), never an ambient fallback. Adding a row
    is the reviewed way a new qualified harness joins (acceptance §1:
    no duplicated approval/publication/continuation logic — the row
    composes the SAME contracts).

    Honest boundaries carried by the set (never fabricated parity):

    - the GitLab BATCH scripted lanes (claude-code and siblings) have
      NO checkpoint restore — ``resume_supported=False`` — and the
      GitHub harness + GitLab SDK lanes do;
    - the Azure lane carries no resume-mode surface (the registry's
      documented gap) and only the variable-group + redemption routes;
    - ``ambient-legacy`` (an unbound dispatch) appears only where the
      shipped template documents it as the legacy window.
    """
    return (
        # GitHub Actions harness — the adopted dispatch leg (ADR-0029's
        # ladder): every driver the workflow input documents, the two
        # #303 routes, exact resume.
        SupportedComposition(
            provider="github",
            runtime_recipe="github-harness-entry",
            harness="claude-code",
            credential_route="github-native-secret",
            caller_contract=_CALLER_CONTRACT,
            template_contract=EXECUTION_SPEC_SCHEMA,
            consumer_contract=_CONSUMER_CONTRACT,
            resume_supported=True,
        ),
        SupportedComposition(
            provider="github",
            runtime_recipe="github-harness-entry",
            harness="claude-sdk-lane",
            credential_route="github-native-secret",
            caller_contract=_CALLER_CONTRACT,
            template_contract=EXECUTION_SPEC_SCHEMA,
            consumer_contract=_CONSUMER_CONTRACT,
            resume_supported=True,
        ),
        SupportedComposition(
            provider="github",
            runtime_recipe="github-harness-entry",
            harness="claude-sdk-lane",
            credential_route="runner-redemption",
            caller_contract=_CALLER_CONTRACT,
            template_contract=EXECUTION_SPEC_SCHEMA,
            consumer_contract=_CONSUMER_CONTRACT,
            resume_supported=True,
        ),
        # GitLab CE SDK lane (#288's pipeline-variable envelope): the
        # interactive driver with exact resume, both #303 routes.
        SupportedComposition(
            provider="gitlab",
            runtime_recipe="gitlab-sdk-lane",
            harness="claude-sdk-lane",
            credential_route="gitlab-protected-variable",
            caller_contract=_CALLER_CONTRACT,
            template_contract=EXECUTION_SPEC_SCHEMA,
            consumer_contract=_CONSUMER_CONTRACT,
            resume_supported=True,
        ),
        SupportedComposition(
            provider="gitlab",
            runtime_recipe="gitlab-sdk-lane",
            harness="claude-sdk-lane",
            credential_route="runner-redemption",
            caller_contract=_CALLER_CONTRACT,
            template_contract=EXECUTION_SPEC_SCHEMA,
            consumer_contract=_CONSUMER_CONTRACT,
            resume_supported=True,
        ),
        # GitLab CE batch scripted lane: NO restore (the template itself
        # refuses a required resume); the native route plus the
        # documented ambient-legacy window.
        SupportedComposition(
            provider="gitlab",
            runtime_recipe="gitlab-batch-script",
            harness="claude-code",
            credential_route="gitlab-protected-variable",
            caller_contract=_CALLER_CONTRACT,
            template_contract=EXECUTION_SPEC_SCHEMA,
            consumer_contract=_CONSUMER_CONTRACT,
            resume_supported=False,
        ),
        SupportedComposition(
            provider="gitlab",
            runtime_recipe="gitlab-batch-script",
            harness="claude-code",
            credential_route="ambient-legacy",
            caller_contract=_CALLER_CONTRACT,
            template_contract=EXECUTION_SPEC_SCHEMA,
            consumer_contract=_CONSUMER_CONTRACT,
            resume_supported=False,
        ),
        # The unbound (ambient-legacy) windows the shipped GitHub and
        # Azure templates document: a dispatch that predates the #303
        # ref inputs keeps receiving the unchanged legacy payload.
        SupportedComposition(
            provider="github",
            runtime_recipe="github-harness-entry",
            harness="claude-code",
            credential_route="ambient-legacy",
            caller_contract=_CALLER_CONTRACT,
            template_contract=EXECUTION_SPEC_SCHEMA,
            consumer_contract=_CONSUMER_CONTRACT,
            resume_supported=True,
        ),
        # Azure Pipelines (ADR-0024): the variable-group carrier and the
        # redemption route; no resume surface (the honest gap).
        SupportedComposition(
            provider="azure",
            runtime_recipe="azure-harness-entry",
            harness="claude-sdk-lane",
            credential_route="azure-variable-group",
            caller_contract=_CALLER_CONTRACT,
            template_contract=EXECUTION_SPEC_SCHEMA,
            consumer_contract=_CONSUMER_CONTRACT,
            resume_supported=False,
        ),
        SupportedComposition(
            provider="azure",
            runtime_recipe="azure-harness-entry",
            harness="claude-sdk-lane",
            credential_route="ambient-legacy",
            caller_contract=_CALLER_CONTRACT,
            template_contract=EXECUTION_SPEC_SCHEMA,
            consumer_contract=_CONSUMER_CONTRACT,
            resume_supported=False,
        ),
    )


@dataclass(frozen=True)
class CompositionRequest:
    """What a dispatch wants to compose (preflight's input)."""

    provider: str
    runtime_recipe: str
    harness: str
    credential_route: str
    #: The resume word the continuation decision selected ("" = do not
    #: check the resume axis — used by callers composing a spec without
    #: a resume contract).
    resume_mode: str = ""


def preflight_composition(
    matrix: tuple[SupportedComposition, ...] | None,
    requested: CompositionRequest,
) -> SupportedComposition:
    """Refuse an impossible combination PRECISELY, or return its row.

    The checks run narrowest-axis-first, and every refusal names the
    axis and the supported alternatives — the #318 acceptance shape
    ("preflight gives a precise incompatibility"):

    - an unknown provider (not a matrix provider at all);
    - a recipe that does not run on the provider;
    - a harness that does not run on that provider+recipe;
    - a credential route the provider does not support;
    - a ``required`` resume on a recipe that cannot restore (the
      issue's exemplar: "driver X on provider Y lacks resume support;
      supported: ...").

    *matrix* may be ``None`` — :func:`supported_compositions` then
    supplies the shipped matrix (the convenience every caller in this
    module uses).
    """
    rows = supported_compositions() if matrix is None else tuple(matrix)
    if not rows:
        raise CompositionRefusal(
            "the supplied composition matrix is empty — a composition preflight "
            "without any supported combination refuses (never an ambient fallback)"
        )
    provider_rows = [row for row in rows if row.provider == requested.provider]
    if not provider_rows:
        supported = sorted({row.provider for row in rows})
        raise CompositionRefusal(
            f"provider {requested.provider!r} is not in the supported composition "
            f"matrix — supported providers: {supported}"
        )
    recipe_rows = [row for row in provider_rows if row.runtime_recipe == requested.runtime_recipe]
    if not recipe_rows:
        supported = sorted({row.runtime_recipe for row in provider_rows})
        raise CompositionRefusal(
            f"runtime recipe {requested.runtime_recipe!r} does not run on provider "
            f"{requested.provider!r} — supported recipes on {requested.provider!r}: "
            f"{supported}"
        )
    harness_rows = [row for row in recipe_rows if row.harness == requested.harness]
    if not harness_rows:
        supported = sorted({row.harness for row in recipe_rows})
        raise CompositionRefusal(
            f"harness {requested.harness!r} does not run on provider "
            f"{requested.provider!r} under recipe {requested.runtime_recipe!r} — "
            f"supported harnesses: {supported}"
        )
    matched = [row for row in harness_rows if row.credential_route == requested.credential_route]
    if not matched:
        supported = sorted({row.credential_route for row in harness_rows})
        raise CompositionRefusal(
            f"credential route {requested.credential_route!r} is not supported for "
            f"harness {requested.harness!r} on provider {requested.provider!r} "
            f"(recipe {requested.runtime_recipe!r}) — supported routes: {supported}"
        )
    row = matched[0]
    if requested.resume_mode == "required" and not row.resume_supported:
        capable = sorted(
            {
                f"{row_candidate.harness} on {row_candidate.provider} "
                f"({row_candidate.runtime_recipe})"
                for row_candidate in rows
                if row_candidate.resume_supported
            }
        )
        raise CompositionRefusal(
            f"driver {requested.harness!r} on provider {requested.provider!r} lacks "
            f"resume support (recipe {requested.runtime_recipe!r} restores no held "
            f"checkpoint — a required resume would silently discard authorized "
            f"WIP); supported resume-capable compositions: {capable}"
        )
    return row


def _require_non_empty(value: str, name: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise CompositionRefusal(
            f"ExecutionSpec field {name!r} is empty — an authority-bearing pin is "
            "fail-closed: the dispatch resolves it once, durably, or refuses"
        )
    return cleaned


def _require_sha256(value: str, name: str) -> str:
    cleaned = str(value or "").strip().lower()
    if len(cleaned) != 64 or any(char not in "0123456789abcdef" for char in cleaned):
        raise CompositionRefusal(
            f"ExecutionSpec field {name!r} must be a sha256 digest (64 hex chars), "
            f"got {cleaned!r} — the digest axes are pinned by content address, "
            "never by an ambient guess"
        )
    return cleaned


@dataclass(frozen=True)
class ExecutionSpec:
    """The versioned pin-set a lane template consumes (frozen).

    Constructed ONCE at dispatch by :func:`compose_execution_spec`
    (never directly assembled by a caller) and rendered by
    :func:`render_template_variables`. Authority-bearing members
    (identity, driver, resume mode, profile digest, the artifact
    contract) are non-empty by construction; the model route may be
    empty ONLY as the recorded "no route pinned" case — the driver's
    documented vendor default then applies and the candidate meta
    records what actually ran.
    """

    #: The logical intent id (``AttemptStartSpec.run_id``'s sibling).
    run_id: str
    #: The durable execution identity (hex64 — the envelope's
    #: ``execution_attempt_id``, derived never guessed).
    execution_attempt_id: str
    #: The harness driver id (the matrix's harness axis).
    driver: str
    #: The model route the approved RunSpec froze ("" = unpinned).
    model: str
    #: The resume word the continuation decision selected.
    resume_mode: str
    #: The pinned checkpoint content address (hex64; REQUIRED when
    #: resume_mode is ``required``, empty otherwise).
    continuation_ref: str
    #: The #303 credential delivery mode (one of :data:`CREDENTIAL_MODES`).
    credential_mode: str
    #: The non-secret credential ref (empty under ambient-legacy).
    credential_ref: str
    #: The frozen execution-profile digest (sha256 — the envelope's
    #: profile axis).
    profile_digest: str
    #: The artifact contract's collector entry (the pinned command the
    #: lane's finalization runs, e.g. ``python -m forge.harness_entry
    #: --collect-candidate``).
    collector_entry: str
    #: The artifact contract's output root (the non-hidden staging dir).
    output_root: str
    #: The matched matrix row (provider/recipe/credential provenance).
    composition: SupportedComposition
    #: The schema discriminator (always :data:`EXECUTION_SPEC_SCHEMA`).
    schema_version: str = EXECUTION_SPEC_SCHEMA

    def spec_digest(self) -> str:
        """sha256 over the canonical pinned document — a field change
        is a different digest (the reconciliation axis: the rendered
        template variables and the persisted document name the same
        spec iff the digests match)."""
        return canonical_json_digest(self.to_document())

    def to_document(self) -> dict[str, Any]:
        """The persisted form (the document :func:`read_execution_spec`
        reads back). Exactly the pinned fields — no ambient state, no
        timestamps."""
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "execution_attempt_id": self.execution_attempt_id,
            "driver": self.driver,
            "model": self.model,
            "resume_mode": self.resume_mode,
            "continuation_ref": self.continuation_ref,
            "credential_mode": self.credential_mode,
            "credential_ref": self.credential_ref,
            "profile_digest": self.profile_digest,
            "artifact_contract": {
                "collector_entry": self.collector_entry,
                "output_root": self.output_root,
            },
            "composition": {
                "provider": self.composition.provider,
                "runtime_recipe": self.composition.runtime_recipe,
                "credential_route": self.composition.credential_route,
                "caller_contract": self.composition.caller_contract,
                "template_contract": self.composition.template_contract,
                "consumer_contract": self.composition.consumer_contract,
            },
        }


#: The collector entry every supported recipe runs today (the packaged
#: generation-aware collector — Q35-01; pinned per spec so a template
#: never re-derives the artifact path from ambient layout).
DEFAULT_COLLECTOR_ENTRY = "python -m forge.harness_entry --collect-candidate"

#: The output root every supported recipe stages into (the non-hidden
#: staging dir — R16/A08; pinned per spec for the same reason).
DEFAULT_OUTPUT_ROOT = "forge-output"

#: The template variables that remain AMBIENT by design, with the
#: documented reason each is NOT authority-bearing (ADR-0032 §3). A
#: template may consult these; it may never consult anything ambient
#: for a member the spec pins.
AMBIENT_FALLBACK_VARIABLES: tuple[tuple[str, str], ...] = (
    (
        "FORGE_HARNESS_MCP",
        "tooling surface (which MCP servers load) — a lane capability, "
        "never an authorization or artifact choice",
    ),
    (
        "FORGE_STEERING_ENABLED",
        "an opt-in lane consumer switch — OFF is fail-closed (the lane stays on its local mailbox)",
    ),
    (
        "FORGE_LANE_REF / FORGE_LANE_WHEEL(_SHA256)",
        "the lane install route — the immutable-resource pin ladder "
        "(Q35-08/R36-07), verified before any model call",
    ),
    (
        "FORGE_LANE_CONTROL_URL",
        "where the lane dials OUT — empty means the steering channel "
        "stays off (fail-closed, never a guessed control plane)",
    ),
    (
        "FORGE_CLAUDE_VERSION",
        "the vendor CLI install pin — tooling identity, recorded in the "
        "trace, never an execution-contract choice",
    ),
    (
        "the per-driver template model default",
        "the DOCUMENTED legacy fallback when the spec pins no model "
        "route (model == ''): the vendor default then runs and the "
        "candidate meta records what actually ran",
    ),
)


def compose_execution_spec(
    *,
    run_id: str,
    execution_attempt_id: str,
    driver: str,
    provider: str,
    runtime_recipe: str,
    credential_mode: str,
    resume_mode: str = "fresh",
    model: str = "",
    continuation_ref: str = "",
    credential_ref: str = "",
    profile_digest: str = "",
    collector_entry: str = DEFAULT_COLLECTOR_ENTRY,
    output_root: str = DEFAULT_OUTPUT_ROOT,
    matrix: tuple[SupportedComposition, ...] | None = None,
) -> ExecutionSpec:
    """Compose the spec at dispatch — validate, preflight, freeze.

    Every authority-bearing input is resolved ONCE upstream (the run
    row, the continuation decision, the broker's delivery plan, the
    frozen RunSpec) — nothing here re-derives a decision another owner
    owns; this function refuses, it never guesses:

    - :func:`preflight_composition` runs FIRST over the (provider,
      recipe, harness=driver, credential route) the dispatch assembled
      — an impossible combination refuses with the precise message;
    - identity, driver, resume word and profile digest are fail-closed
      on emptiness; the profile digest must be sha256;
    - ``required`` demands its pinned checkpoint ref (sha256) — the
      WIP an operator authorized must arrive by content address or the
      dispatch refuses;
    - a bound credential mode demands its non-secret ref; only
      ``ambient-legacy`` may carry none (the documented window);
    - ``model`` may be empty (the recorded unpinned case) — never a
      permissive guess of a route.
    """
    mode = _require_non_empty(resume_mode, "resume_mode")
    if mode not in RESUME_MODE_WORDS:
        raise CompositionRefusal(
            f"ExecutionSpec field 'resume_mode' carries {mode!r} — the spec PINS the "
            f"continuation decision's word (one of {sorted(RESUME_MODE_WORDS)}); it "
            "never derives or remaps one"
        )
    credential_mode_cleaned = _require_non_empty(credential_mode, "credential_mode")
    if credential_mode_cleaned not in CREDENTIAL_MODES:
        raise CompositionRefusal(
            f"ExecutionSpec field 'credential_mode' carries {credential_mode_cleaned!r} "
            f"— the #303 delivery vocabulary is {sorted(CREDENTIAL_MODES)}"
        )
    composition = preflight_composition(
        matrix,
        CompositionRequest(
            provider=str(provider or "").strip(),
            runtime_recipe=str(runtime_recipe or "").strip(),
            harness=str(driver or "").strip(),
            credential_route=credential_mode_cleaned,
            resume_mode=mode,
        ),
    )
    continuation = str(continuation_ref or "").strip().lower()
    if mode == "required":
        # The WIP an operator authorized arrives by content address or
        # the dispatch refuses — never a guessed checkpoint.
        continuation = _require_sha256(continuation, "continuation_ref")
    elif continuation:
        continuation = _require_sha256(continuation, "continuation_ref")
    credential_ref_cleaned = str(credential_ref or "").strip()
    if credential_mode_cleaned != "ambient-legacy":
        credential_ref_cleaned = _require_non_empty(credential_ref_cleaned, "credential_ref")
    return ExecutionSpec(
        run_id=_require_non_empty(run_id, "run_id"),
        execution_attempt_id=_require_sha256(execution_attempt_id, "execution_attempt_id"),
        driver=str(driver or "").strip(),
        model=str(model or "").strip(),
        resume_mode=mode,
        continuation_ref=continuation,
        credential_mode=credential_mode_cleaned,
        credential_ref=credential_ref_cleaned,
        profile_digest=_require_sha256(profile_digest, "profile_digest"),
        collector_entry=_require_non_empty(collector_entry, "collector_entry"),
        output_root=_require_non_empty(output_root, "output_root"),
        composition=composition,
    )


def render_template_variables(spec: ExecutionSpec) -> dict[str, str]:
    """The SMALL pinned set the lane templates consume.

    Exactly the members ADR-0032 §2 names; every value is the spec's
    pin (the template maps it into its variable-resolution header and
    consumes it — it never re-derives a pinned choice from ambient
    variables). The ambient fallbacks that remain are the documented
    :data:`AMBIENT_FALLBACK_VARIABLES`, none authority-bearing.
    """
    return {
        "FORGE_RUN_ID": spec.run_id,
        "FORGE_DRIVER": spec.driver,
        # The approved RunSpec's frozen route — the pin the GitLab
        # batch lanes previously IGNORED (the ambient template default
        # outranked it; ADR-0032 §1's consolidation defect).
        "FORGE_MODEL": spec.model,
        "FORGE_LANE_RESUME_MODE": spec.resume_mode,
        "FORGE_RESUME_CHECKPOINT": spec.continuation_ref,
        "FORGE_CREDENTIAL_REF": spec.credential_ref,
        "FORGE_CREDENTIAL_REDEEM": "1" if spec.credential_mode == "runner-redemption" else "",
        # The spec identity itself: the template's header records which
        # spec version pinned this variable set, and the digest the
        # control plane persisted for reconciliation.
        "FORGE_EXECUTION_SPEC": spec.schema_version,
        "FORGE_EXECUTION_SPEC_DIGEST": spec.spec_digest(),
    }


#: The exact top-level keys a persisted v1 document carries (the
#: compatibility rule's reader contract — unknown keys refuse).
_DOCUMENT_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "execution_attempt_id",
        "driver",
        "model",
        "resume_mode",
        "continuation_ref",
        "credential_mode",
        "credential_ref",
        "profile_digest",
        "artifact_contract",
        "composition",
    }
)


def read_execution_spec(document: Mapping[str, Any]) -> ExecutionSpec:
    """The compat reader: a persisted spec document → the frozen spec.

    The compatibility rule (ADR-0032 §4):

    - a ``forge.execution.spec/1`` document round-trips (the digest of
      the read spec equals the digest of the document the dispatch
      persisted — same fields, same values);
    - a document claiming an unknown or NEWER schema version refuses
      explicitly — the reader names its supported version and the
      upgrade instruction, never guessing at a newer field;
    - an unknown top-level key refuses (an additive evolution bumps
      the schema word; a silent drop would forge compatibility);
    - the read fields re-run the SAME construction validation (and the
      composition preflight against the CURRENT matrix): a persisted
      row that left the supported set refuses explicitly — the
      documented predecessor continues, everything else says so.

    Raises :class:`CompositionRefusal` on every refusal.
    """
    if not isinstance(document, Mapping):
        raise CompositionRefusal(
            "an execution spec document must be a mapping — the persisted form is "
            "the document compose_execution_spec().to_document() wrote"
        )
    keys = set(map(str, document.keys()))
    unknown = sorted(keys - _DOCUMENT_KEYS)
    if unknown:
        raise CompositionRefusal(
            f"the execution spec document carries unknown keys {unknown} — this "
            f"reader supports {EXECUTION_SPEC_SCHEMA}; an additive field change "
            "bumps the schema version (refuse explicitly, never drop silently)"
        )
    schema = str(document.get("schema_version") or "")
    if schema != EXECUTION_SPEC_SCHEMA:
        raise CompositionRefusal(
            f"the persisted execution spec declares schema {schema!r} but this "
            f"reader supports {EXECUTION_SPEC_SCHEMA!r} (v{EXECUTION_SPEC_VERSION}) — "
            "upgrade the control plane before composing older records; a reader "
            "never guesses at a newer spec's fields"
        )
    artifact = document.get("artifact_contract")
    if not isinstance(artifact, Mapping):
        raise CompositionRefusal(
            "the execution spec document's 'artifact_contract' must be a mapping "
            "(collector_entry, output_root) — the artifact contract is pinned, "
            "never re-derived"
        )
    composition = document.get("composition")
    if not isinstance(composition, Mapping):
        raise CompositionRefusal(
            "the execution spec document's 'composition' must be a mapping — the "
            "persisted matrix row the dispatch preflighted"
        )
    return compose_execution_spec(
        run_id=str(document.get("run_id") or ""),
        execution_attempt_id=str(document.get("execution_attempt_id") or ""),
        driver=str(document.get("driver") or ""),
        provider=str(composition.get("provider") or ""),
        runtime_recipe=str(composition.get("runtime_recipe") or ""),
        credential_mode=str(document.get("credential_mode") or ""),
        resume_mode=str(document.get("resume_mode") or ""),
        model=str(document.get("model") or ""),
        continuation_ref=str(document.get("continuation_ref") or ""),
        credential_ref=str(document.get("credential_ref") or ""),
        profile_digest=str(document.get("profile_digest") or ""),
        collector_entry=str(artifact.get("collector_entry") or ""),
        output_root=str(artifact.get("output_root") or ""),
    )
