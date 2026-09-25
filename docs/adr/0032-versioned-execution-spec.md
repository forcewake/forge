# ADR-0032: The versioned execution spec — one pinned contract for every lane template

Status: accepted (2026-09-24)

Context: review `59ba869`, item R38-17 (issue #318). Previous:
ADR-0029 (composition boundaries), ADR-0030 (authority ownership),
ADR-0031 (contracts vs reference). Dependencies: #303 (the credential
delivery contract), #307 (the release-composition manifest).

Forge's mature contracts — durable runs (ADR-0017), the frozen RunSpec
(ADR-0018), the typed continuation, scoped credentials (#303),
verification binding — were each resolved correctly, but the LANE
TEMPLATES still re-derived parts of the execution from their own
ambient state:

- the GitLab `claude-code` batch lane ran the template's hard-coded
  `ANTHROPIC_MODEL` default and never consumed the dispatched
  `FORGE_HARNESS_MODEL` — the model the gate approved and the model
  the lane ran were two different decisions;
- the `opencode-sdk-lane` resolution chain let the ambient
  `OPENCODE_MODEL_ID` project variable OUTRANK the dispatched pin;
- the `copilot` batch lane consulted only the ambient `COPILOT_MODEL`;
- every template carried its own prose about which variables were
  dispatch products and which were repo setup, so "which layer owns
  this choice?" was a per-template reading assignment — the same
  defect class ADR-0030 closed for module boundaries, one layer out.

The same review cycle exposed the release-side symptom: the v0.37.0
wheel shipped no console entry-points and one version string identified
two compositions — coordinated contract surfaces that no single file
pinned.

## Decision

### 1. One spec, not per-template derivations

`forge.adaptive.execution_spec` owns
`ExecutionSpec` (`forge.execution.spec/1`): the frozen pin-set a
template consumes — run/attempt identity, driver + model route, resume
mode + the pinned checkpoint ref, credential delivery mode + ref, the
frozen execution-profile digest, and the artifact contract (collector
entry, output root). It is the attempt-start envelope's sibling: both
are constructed ONCE at dispatch from inputs that were resolved once
and are durable (the `composition_adoption` pattern), nothing ambient
enters, and a repeat delivery reconstructs the identical value.

Every authority-bearing member fails closed at construction: empty
identity, a non-sha256 profile digest, an unknown resume word, a
`required` resume without its pinned checkpoint content address, a
bound credential mode without its non-secret ref. The model route may
be empty ONLY as the recorded "no route pinned" case — the driver's
documented vendor default then applies and the candidate meta records
what actually ran. A template never re-derives a pinned choice from
ambient variables.

### 2. The rendering contract

`render_template_variables` renders the small pinned set —
`FORGE_RUN_ID`, `FORGE_DRIVER`, `FORGE_MODEL`, `FORGE_LANE_RESUME_MODE`,
`FORGE_RESUME_CHECKPOINT`, `FORGE_CREDENTIAL_REF`,
`FORGE_CREDENTIAL_REDEEM`, plus the spec's own version word and digest
(`FORGE_EXECUTION_SPEC`, `FORGE_EXECUTION_SPEC_DIGEST`). `spec_digest()`
is sha256 over the canonical document, so the rendered set and the
persisted document name the same spec iff the digests match.

The ambient fallbacks that remain are enumerated in
`AMBIENT_FALLBACK_VARIABLES` with the reason each is not
authority-bearing (MCP tooling, the steering opt-in, the lane install
pin ladder, the outbound control URL, the vendor CLI pin, the template
model default of the unpinned legacy window). A variable is either a
spec pin or a documented ambient fallback — never both.

The shipped templates consume the pins at their variable-resolution
headers: the GitLab batch lanes resolve the model from the dispatched
pin first (the template default is the documented legacy fallback),
the SDK lanes keep their dispatched-first chains, and the GitHub/Azure
headers state which variables are pins. The credential blocks stay
#317's gate; the finalization blocks are untouched.

### 3. The supported composition matrix

`supported_compositions()` is the small tested list of
(provider × runtime recipe × harness × credential-route) combinations
with their caller (`forge.attempt-start/2`), template
(`forge.execution.spec/1`) and consumer (`forge.candidate-meta/2`)
contract versions. `preflight_composition(matrix, requested)` is the
dispatch pre-check: an impossible combination refuses with a precise
incompatibility naming the axis and the supported alternatives —
"driver 'claude-code' on provider 'gitlab' lacks resume support …
supported resume-capable compositions: …". An unlisted combination is
a refusal, never an ambient fallback.

The matrix carries the honest boundaries rather than fabricated
parity: the GitLab batch scripted lanes restore no checkpoint
(`resume_supported=False`); the GitHub harness and the GitLab SDK lane
restore exactly; the Azure lane carries no resume-mode surface (the
ADR-0030 registry's documented gap). Adding a qualified harness is
adding a row — it composes the same caller/template/consumer
contracts, no duplicated approval/publication/continuation logic.

Spec construction runs the preflight (the natural dispatch pre-check
seam: the refusal fires where the other pre-flight refusals do,
before any provider call). The doctor's offline matrix modes are the
future operator surface for printing the matrix.

### 4. The compatibility rule

A persisted spec document is read by `read_execution_spec`:

- a `forge.execution.spec/1` document round-trips — the revived spec
  re-runs the same construction validation and the CURRENT matrix
  preflight, so a previous-version run loads and its next safe
  recovery step renders through the current composition;
- a document claiming an unknown or newer schema version refuses
  explicitly, naming the reader's supported version and the upgrade
  instruction — a v1 reader never guesses at a newer field;
- an unknown top-level key refuses (additive evolution bumps the
  schema word; a silent drop would forge compatibility);
- a persisted row that left the supported set refuses explicitly —
  the declared predecessor continues, everything else says so.

### 5. The console entry-points

The wheel declares `[project.scripts]`: `forge-doctor = forge.doctor:main`
(the module main existed; the v0.37.0 wheel simply never exposed it)
and `forge = forge.cli:main` — a thin dispatcher mapping `forge doctor`,
`forge gate` (the release-promotion gate) and `forge migrate` onto the
existing module mains, no new logic. This satisfies the #287
inventory's note about the missing entry-points and gives the
operators one executable surface per contract.

### 6. Ownership

The boundary registry gains its seventh entry,
`execution_delivery_spec`: `forge.adaptive.execution_spec` owns WHAT a
template may consume as a pin; no service or template re-derives a
pinned member ambiently, and the import-registration rule guards the
module like every other owner.

## Consequences

- A second qualified harness joins by adding a matrix row and a
  template that consumes the same pins — not by copying decision
  logic (acceptance §1).
- The ops isolation drill no longer flags the #303 non-secret ref
  names (`FORGE_CREDENTIAL_REF` / `FORGE_CREDENTIAL_REDEEM`) as
  credential-shaped: they are references, not values; a token-shaped
  name still flags (#303's live re-run note).
- Model drift between the approved RunSpec and the batch lanes' actual
  route is closed by construction (the pin outranks the template
  default everywhere).
- The unpinned-model case is honest, not permissive: empty pin → the
  documented vendor default → recorded in the candidate meta.
- Evolution rule: additive field changes bump
  `forge.execution.spec/N`; readers refuse mismatched versions
  explicitly until the control plane upgrades.

## Amendment (Q39-17 / #336): the two authority members

ADR-0033's consolidation adds two members to the spec, additively:
`approved_input_digest` (the resolved `ApprovedInput`'s
`plan_text_digest` — #321's executor-input identity) and `grant_id`
(the `CredentialOperationGrant` a redemption-mode dispatch minted —
#320's credential authorization). Both ride the document and therefore
`spec_digest()`; both fail closed when present but malformed, and a
`runner-redemption` spec without its `grant_id` refuses (a redemption
the endpoint could never authorize). Empty values are the recorded
pre-#321/#320 window, labeled by the same discipline as the unpinned
model route.

The schema word stays `forge.execution.spec/1` —
**additive-with-version-note** rather than a `/2` bump: the members
are optional with empty legacy values, no existing member's digest
semantics changed, and a pre-amendment reader still refuses the new
keys as unknown (the compatibility rule working as designed — the
upgrade instruction names the control plane). A `/2` bump is reserved
for a change that would alter the digest MEANING of an existing
member.
