# ADR-0031: Contracts vs reference — separating reusable product contracts from reference scenarios

Status: accepted (2026-09-24)

Context: review `4af6b33`, item R37-19 (issue #300). The R36 session
(ADR-0030) gave every authority decision one owning module and a
mechanical registry — but three of those modules still combined
product contracts with the scenario machinery that exercises them:

- `adaptive/system_verification.py` carried the PYTHON-shaped twin
  itself: the synthetic service/image identities, the scenario
  builders, a REAL sqlite schema-upgrade path and a REDIS-less
  double-delivery harness, beside the runtime contracts (tested-world
  binding, applicability joins, the readiness query);
- `adaptive/saga_durable.py` carried `NativeShapedRemote` — an
  in-process GitLab/GitHub-shaped remote with provider-realistic
  duplicate behavior — beside the durable store and the publication
  entry;
- `adaptive/steering_causality.py` carried the reactive scripted
  vendor EXECUTABLE (spawned as the lane subprocess's `CODEX_BINARY`)
  beside the pure three-arm causality grader.

The fixtures are valuable; the review's point is that importing them
BESIDE runtime contracts must not turn their assumptions into deployed
guarantees. A maintainer could not tell product code from fake-native
integration from layout alone, and a runtime import graph that
transitively included a scenario made "which behavior is real?" a
reading assignment.

## Decision

### 1. The labelled reference package

`src/forge/adaptive/reference/` is the evaluation/testing package
(label `REFERENCE_PACKAGE_LABEL = "forge.reference/1"`, documented in
its `__init__`). Three kinds of code live in this repository and are
now distinguishable from layout + metadata:

| Kind | Where | Examples |
|---|---|---|
| product code | `src/forge` minus the reference package | the six boundary owners, the runtime contracts |
| fake-native integration | `forge.adaptive.reference` | the SQLite twin, `NativeShapedRemote`, the reactive scripted vendor |
| customer-executed evidence | `qualification/records`, `docs/releases/evidence` | measured deployment records |

The three extractions (behavior unchanged, moved verbatim):

- `reference/system_twin.py` ← `system_verification.py`: the synthetic
  identity helpers, `TwinContract`/`TwinService`/`TwinScenario` +
  `default_twin_scenario` (with the negative-combination and
  stale-evidence mutations), `SystemEdge` (the edge DECLARATIONS a
  scenario judges), the sqlite schema-upgrade machinery
  (`TwinMigration`, `execute_schema_upgrade`, preservation
  fingerprints), the `DoubleDeliveryHarness`, the synthetic replay
  generators and the twin's bundle/schema/dialect constants. The
  runtime module KEEPS the contracts and decisions: the verifier
  environment/authority refusals, `EdgeResult`/`ProjectionOutcome`
  (executed-outcome vocabulary), the edge executions,
  `run_system_verification`, selective invalidation and the readiness
  query.
- `reference/native_shaped_remote.py` ← `saga_durable.py`:
  `NativeShapedRemote` (the in-process provider-shaped remote).
  `PostgresSagaStore`, `DurablePublicationEntry`, the saga
  serialization and `NativeCommit` (the native-identity record the
  REAL adapters also produce) stay runtime.
- `reference/reactive_vendor.py` ← `steering_causality.py`: the frozen
  demonstration task, the checkable-instruction grammar
  (`parse_instruction`/`apply_rename`/`apply_instruction`) and the
  executable (`run_app_server`, `run_once`, `vendor_main`, the env
  knobs). The PURE GRADER stays runtime. The grammar moved WITH the
  scenario so the vendor's application and the grader's arm 3 share
  ONE parse — one decision, one module.

### 2. The dependency-direction rules (mechanical)

Enforced by `tests/test_reference_separation.py` and by two new rules
(`reference separation`, `reference purity`) in
`tests/test_architecture_boundaries.py`, both with negative trap
tests:

1. **Runtime entry points never import the reference package.** The
   entry points: the three provider services, `main`, the wiring, the
   lane entry. An evaluation scenario beside a runtime contract must
   never become a deployed guarantee by import.
2. **Outside the reference package, only the registered compat homes
   import it** — exactly the three modules the scenarios were
   extracted from. A NEW runtime module pulling a scenario in fails
   the check with an instruction to compose the runtime contract
   instead (or register an extraction home with a reviewed reason). A
   drained compat home (importing nothing from the package) fails as
   stale, mirroring the boundary registry's two-way honesty.
3. **The reference package composes runtime contracts only**: it never
   imports a provider service, the wiring, `main` or the lane entry,
   and it owns no orchestration. Its scenario modules are registered
   in the boundary registry's `allowed_dependents` like any other
   caller of an owner (`reference.system_twin` → the
   verification-applicability owners; `reference.native_shaped_remote`
   → the candidate-publication owners), so the R36-20 import
   registration fence covers them automatically.
4. **No implicit selection**: importing the production composition
   loads NO reference module for the lazily-compatible homes — the
   issue's recovery test ("import the production service composition
   in a clean environment and assert no reference-native
   implementation is selected implicitly") runs as a subprocess probe
   over `saga_durable` + `saga_native` + the pure grader. The one
   runtime module that DOES import the reference package at module
   level is `system_verification` — by design: it EXECUTES the twin;
   its docstring names the reference module it pulls in.

### 3. The compatibility surface and the migration policy

Old import paths survive through tested compat re-exports pinned by
`tests/test_reference_separation.py` (every moved symbol resolves on
its old home AND is the very object the reference module defines —
identity, not a copy):

- `system_verification`: a plain top-level re-import from
  `reference.system_twin` (the runner composes the scenario it
  executes);
- `saga_durable` and `steering_causality`: LAZY `__getattr__`
  re-exports. Two hard constraints force the laziness:
  `reference.native_shaped_remote` imports `NativeCommit` (runtime),
  so an eager re-export would be an import cycle; and
  `steering_causality.py` doubles as the SPAWNED vendor file
  (`CODEX_BINARY`, executed under `/usr/bin/env python3` where no
  forge import is guaranteed to resolve), so its module top level must
  stay stdlib-only. The spawn contract is unchanged: the file's
  `__main__` block loads `reference/reactive_vendor.py` BY PATH
  (stdlib `importlib`, registered in `sys.modules` before exec for the
  dataclass machinery) and hands it argv.
- **Removal policy**: a compat re-export is removed only when its
  callers are drained to the reference path — at that point the pinned
  inventory in `tests/test_reference_separation.py` shrinks WITH the
  re-export, and the drained registration fails the architecture check
  until removed (the same expand-contract discipline ADR-0029 §3
  prescribes for document versions).

### 4. The owner map is unchanged

ADR-0030's six boundaries, owners and negative contracts stand exactly
as recorded there. This ADR adds NO new owner, no new orchestration
layer and no new DTO: it moves scenario code across a labelled
boundary and registers the moved modules as ordinary dependents of the
existing owners. The observability spellings the issue names map onto
the existing surfaces: `architecture.owner_violations` gains the
reference-separation axis (the two new architecture rules),
`compatibility.fixture_failures` is the pinned compat inventory
failing, and `production.entry_coverage` is the entry-point list the
import rules iterate.

## Consequences

- A feature fix to a runtime contract changes one owning module; the
  scenarios that prove it are beside it in name only — importing the
  contract never imports the scenario (except the twin's runner, which
  says so).
- A maintainer distinguishes product code / fake-native integration /
  customer-executed evidence from layout + metadata: the package path,
  the `REFERENCE_PACKAGE_LABEL`, and the labelling docstring every
  reference module carries.
- The monorepo, one release train and the modular monolith are
  untouched — the reference package rides the same wheel
  (`hatch` packages `src/forge` whole); no multi-repo coordination.
- The pre-existing acceptance traces (`test_system_verification`,
  `test_saga_durable`, `test_saga_native`, `test_causal_steering`, the
  production-entry two-writer/steering/executor traces) ran green
  BEFORE and AFTER the move with unchanged assertions — the moved code
  is verbatim.

## Appendix — residual duplicated decision points (listed, not fixed)

The honest audit the extraction surfaced. These are NOT addressed by
this ADR (fixing them is follow-up work; each needs its own reviewed
owner decision):

1. **Boolean env-knob parsing** — "what counts as truthy" is decided
   independently at five sites: `lane_driver._STEERING_TRUTHY`,
   `lane_driver._RESUME_TRUTHY`, `discovery_stage._TRUTHY`,
   `command_router._TRUTHY` and (verbatim from the move) the inline
   tuple in `reference/reactive_vendor.py`. The reference copy must
   stay stdlib-local for the spawn arm, so consolidation would be
   four-sites-into-one plus one documented spawn-local exception.
2. **ISO-8601 parsing/normalization** — the
   `value.replace("Z", "+00:00")` + `datetime.fromisoformat` dance
   appears in ~10 modules (`pilot`, `pilot_ladder`,
   `delivery_metrics`, `delivery_measurement`, `verification_sets`,
   `integrations/github`, `profile_qualification`, …) beside the
   grader's `_iso_le` (which relies on 3.11+ native `Z` handling).
   A single parsing owner would remove a class of subtle comparison
   bugs (aware-vs-naive, `Z`-spelling).
3. **Marker-correlation scanning** — "correlate a publication effect
   by scanning the LISTED branch commits' messages for the marker" is
   implemented both in the reference remote
   (`NativeShapedRemote.commits_carrying`) and in the native adapters'
   shared base (`saga_native`, `marker in commit.message`). The
   Protocol is the contract; the scan is a parallel implementation on
   each side of the reference/runtime line. Consolidating the scan
   into the runtime seam the reference composes would need the
   reference package to import it — allowed by the rules here, worth
   doing only with the next touch of that seam.
4. **The lazy `__getattr__` compat idiom** — now exists twice
   (`saga_durable`, `steering_causality`). Two ~15-line copies of the
   same PEP 562 mechanism; a shared helper would couple two
   extractions' removal schedules. Acceptable while both live; revisit
   if a third extraction needs it.
5. **Preservation-fingerprint discipline** — the twin's
   `_preservation_fingerprint` (counts + per-table sha256 over ordered
   row identities) mirrors the release canary's product-side
   discipline in-process. Deliberate: a reference scenario PROVES a
   mechanism by re-implementing its discipline against synthetic data.
   Listed so the next canary change knows it has a twin to keep
   honest.
