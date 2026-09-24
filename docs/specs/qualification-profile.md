# Qualification profile spec (R32-13)

Status: implemented (2026-09-23) · Module: `src/forge/adaptive/qualification.py` ·
Tests: `tests/test_adaptive_qualification.py` (+ `tests/fixtures/qualification/`,
`tests/test_lane_closure.py` for the R36-10 closure legs) ·
Research: [research/2026-09-23-e2e-qualification/](../research/2026-09-23-e2e-qualification/) ·
Builds on: `forge.runs.execution_profile` (recipes/harnesses, `profile_digest`,
`verify_network_egress`) and `forge.adaptive.capability_profiles` (CAPABILITIES).

## What a QualificationProfile is

One qualified (runtime recipe, harness) combination as a single frozen record —
the thing a clean runner installs and executes, with no developer environment:

```
QualificationProfile
  lane_code_ref                        # the lane wiring's source ref
  recipe_id, recipe_digest             # the runtime axis, by name AND content
  harness_id                           # the agent axis
  model_route, credential_mode         # the route the qualification ran with
  test_invocation                      # the argv the runner executes
  bootstrap_closure                    # resolved pins: name → exact version
  feature_support                      # interrupt/steer/restore, ALL explicit
  layer_bindings                       # smoke → contract → … evidence bindings
  dependency_closure_digest            # R36-10 (optional): the wheelhouse identity
```

`recipe_digest` is the canonical-JSON sha256 over the recipe's `to_document()`
(`recipe_document_digest`) — the same pattern as `ExecutionProfile.profile_digest`.
`feature_support` states every harness feature explicitly (`False` is a
statement, never an absence) over the CAPABILITIES vocabulary (steer =
`live_input`, restore = `checkpoint_export`); nothing is inferred from an SDK
name. `profile_staleness` compares the pinned ids/digests against the shipped
vocabularies: a recipe that changed since qualification (or a harness that no
longer ships) is a staleness problem — re-qualify, never drift silently.

`dependency_closure_digest` (R36-10, additive — `""` keeps the pre-R36-10
meaning exactly, and archived documents without the field thaw to `""`) pins
the hash-locked wheelhouse the qualification installed from: the
`LaneClosureManifest.closure_digest` of a lane closure built by
`scripts/build_lane_closure.py`. A profile that pins it demands the
`closure-wheel` install route; one that leaves it empty is wheel-pinned, not
closure-pinned — pip resolved the runtime dependencies at install time — and
the runtime boundary report says so honestly.

## The digest contract

`qualification_digest` = sha256 over the sorted-key JSON of the whole record's
`to_document()`. Deterministic over identical records; ANY field mutation —
any — is a different digest. The freeze round-trip
(`from_document(json.loads(json.dumps(to_document())))`) reproduces the record
and the digest byte-for-byte; a document that fails the round-trip was never
freezable. The closure is canonicalized to sorted name→version pairs, so pin
ORDER is not a digest axis; pin CONTENT is.

## Fingerprint refusal semantics

`verify_installed_fingerprints(declared, installed)` compares the profile's
bootstrap closure against what the runner ACTUALLY installed. Exact-set
semantics, no permissive partial match: every declared pin must be installed at
that exact version (else `version_mismatch` / `missing_installed`), and the
installation must carry nothing undeclared (`undeclared_installed`). ANY
divergence raises `FingerprintMismatch` listing EACH one — a 99 %-matching
installation is a refusal, not a "close enough". The caught mismatch is still
recordable evidence for the trace: refusal is a verdict, not an erasure.

## Report reconciliation verdicts

`reconcile_reports(expected, found_dir)` binds every expected report to the
(candidate identity, test bundle digest) it must belong to. The runner writes
each report beside an identity sidecar `<report>.identity.json`
(`{"candidate_id": …, "bundle_digest": …}`). Per test project:

- `missing_report` — no report file. NEVER zero failures: counts stay `None`,
  unknown is never green.
- `stale_report` — file present but the sidecar is absent/unreadable or names
  another candidate/bundle: a leftover answering in this run's place.
- `unparseable_report` — present and identity-bound but not a parseable TRX
  with `ResultSummary/Counters`; counts unknown.
- `passed` / `failed` — parsed counts; failures fold in error/timeout/aborted,
  and a report that executed ZERO tests is `failed` (it proves nothing).

The aggregate lists EVERY problem distinctly — a failing test project cannot
disappear behind another project's passing TRX file. Green requires every
expected report present, identity-bound, parsed and failure-free.

## Egress probe-pair semantics

`probe_egress_pair(policy, permitted_dest, denied_dest)` proves BOTH legs of
the declared egress control. The PERMITTED leg dials a policy-granted
destination (the producer surface: model route, package registries) and must
SUCCEED — that success is the producer evidence. The DENIED leg dials a
policy-denied destination and answers with the honest distinction
(`verify_network_egress`'s three-value style, doubled):

- `denied_by_policy` — hook present, destination denied by the declared
  patterns, connection refused. Consistent with enforcement; a refused probe
  proves the control MAY exist, never that it does.
- `denied_unexpected` — connection refused but the policy is not in force
  (hook absent / destination not denied): the denial is real, this policy
  cannot claim it — something else is wrong and is unqualified.
- `policy_declared_but_not_enforced` — hook present, destination denied by the
  patterns, connection SUCCEEDED. The one shape the probe PROVES: enforcement
  is missing. The research case — "disable the network policy while retaining
  its env variable" — looks exactly like this and is detected exactly here.
- `policy_absent` — no declaration in the runtime env and the destination
  answered: egress uncontrolled, not enforced.
- `probe_indeterminate` — the "denied" destination is actually allowlisted; a
  permitted destination cannot falsify enforcement (said loudly).

The pair is `consistent` only when the permitted leg reached a
policy-GRANTED destination and the denied leg was denied by policy; a
reachable-but-never-granted destination is a mis-specified pair, not a pass.

## Layer vocabulary

`QualificationLayer`: `smoke → contract → integration → acceptance` (the
canonical stack, research topic 1). Each `LayerBinding` carries the evidence
pointers that promote the layer (non-empty — a layer promoted by nothing is a
declaration, and this record is not one). Bindings stack bottom-up as a
PREFIX of the canonical order with no gaps: acceptance evidence without the
layers beneath it is the ice-cream cone, not a qualification. The acceptance
layer is CURATED: at most `ACCEPTANCE_MAX_SHARE` (5 %) of the suite —
`acceptance_within_budget` checks the cap at curation time; beyond it the
layer stops adding signal and starts adding E2E flake.

## The trace, and the clean-runner flow

`assemble_qualification_trace(profile, fingerprint=…, reports=…, egress=…,
started_at=…, finished_at=…)` produces the versioned
(`forge.qualification.trace/1`) evidence record: profile digest, fingerprint
verdict (a caught `FingerprintMismatch` included), report reconciliation, the
egress pair, the layer bindings, timestamps. The verdict is DERIVED, never
passed in: `qualified` only when every leg is complete and passing; any leg
unknown or partial stays `not_qualified` with a problem line saying why — a
partial trace is an honest trace, never a green one.

How a clean runner installs and executes a profile without a developer
environment:

1. Resolve the profile (by digest) and check `profile_staleness` — refuse a
   stale record before spending anything.
2. Install the `bootstrap_closure` pins (the recipe's locked install tail —
   `uv sync --frozen` / `dotnet restore --locked-mode` / `npm ci`), then
   `verify_installed_fingerprints` against what landed; any divergence aborts.
3. Run `test_invocation` inside the declared egress policy; `probe_egress_pair`
   both legs (env hook present AND enforcement observed).
4. Reconcile the reports (identity sidecars bound to this candidate/bundle).
5. Assemble the trace; the promotion reads the trace, it never re-runs suites.

## The runtime dependency closure and the boundary (R36-10)

The wheel pin alone is a reproducible-FILE guarantee — pip still resolves
the runtime dependencies at install. The closure legs close that, and state
the security boundary outside the model (operations runbook:
[lane-closure.md](../operations/lane-closure.md)):

- **`LaneClosureManifest`** (`forge.lane.closure/1`) — the wheelhouse
  contract: every artifact name + sha256 (wheels only, sorted — order is
  not a digest axis), the forge wheel identity (name/version/source:
  `local-uv-build` | `pinned-url` | `promotion-record`), and the canonical
  path-free resolution command. `closure_digest` = canonical-JSON sha256
  over `to_document()`; deterministic over identical closures, different
  on ANY change. The forge wheel must be a member of its own closure and
  the declared version must agree with the wheel name.
- **`verify_closure_dir`** — a staged wheelhouse against its manifest:
  every artifact present and hash-matching, NOTHING undeclared beside
  them (a poisoned-cache file is a typed `ClosureVerificationError`), and
  the stored digest must reproduce from the manifest body. Pre-execution,
  pure filesystem + hashlib.
- **`verify_artifact_supply_chain(manifest, release_record)`** — the
  forge wheel in the closure must be the wheel the promotion record
  vouches for (by sha256, and by name when the record names it). An
  image-only record refuses with `not_built` — honestly, never a
  fabricated green; different bytes refuse with `digest_mismatch`.
- **Credential scope receipts** — `verify_credential_isolation(staged,
  forbidden)` is pure name-set arithmetic: any forbidden (publisher)
  name among the staged ones raises
  `CredentialIsolationViolation` carrying NAMES ONLY, never values. The
  `CredentialScopeReceipt` freezes what the lane stages plus the
  isolated verdict — and is only constructible through the check, so a
  self-declared "isolated" receipt cannot exist.
- **The `closure-wheel` install route** (`resolve_closure_install_route`,
  additive to the R36-07 ladder) — `FORGE_LANE_CLOSURE_SHA256` (the
  expected digest) + `FORGE_LANE_CLOSURE_DIR` select an offline install
  (`pip install --no-index --find-links <dir> forge==<version>`); the pin
  beside any other explicit route input (dev flag / explicit wheel /
  explicit ref) refuses naming both variables; set-but-empty is unset.
  `enforce_closure_install(expected_digest, manifest, installed)` is the
  identity gate: the verified manifest must BE the pinned closure, and
  the installed set must match the manifest pins exactly — under
  `--no-index` a target-repo lockfile cannot replace the collector
  runtime.
- **`runtime_boundary_report(profile, egress_pair, closure_manifest,
  installed=…, credential_receipt=…)`** — the one document (schema
  `forge.qualification.boundary/1`) with the R36-10 observability keys:
  `runtime.installed_fingerprint`, `security.egress_probe_results`,
  `credential.staged_scope_receipt`, plus the closure binding
  (`bound`/`unpinned`/`mismatch`). The verdict is derived:
  `within_boundary` only when every leg is complete and passing; any leg
  unknown or partial keeps `outside_boundary` with a problem line saying
  why — the trace's honesty discipline, boundary edition.
