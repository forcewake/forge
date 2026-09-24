# Lane closure: the hash-locked wheelhouse (R36-10 / #269)

Status: implemented (2026-09-23) · Builder: `scripts/build_lane_closure.py` ·
Contract: `forge.adaptive.qualification` (`LaneClosureManifest`, `verify_closure_dir`,
`verify_artifact_supply_chain`, `verify_credential_isolation`,
`resolve_closure_install_route`, `enforce_closure_install`,
`runtime_boundary_report`) · Tests: `tests/test_lane_closure.py`,
`tests/test_adaptive_qualification.py` · Spec:
[qualification-profile.md](../specs/qualification-profile.md).

## The problem this closes

The Forge wheel is hash-pinned (the R36-07 ladder downloads the promoted
wheel and refuses any byte that does not reproduce the pinned sha256),
but `pip install <wheel>` still resolves the wheel's RUNTIME
dependencies at install time — against whatever index the runner sees,
at whatever versions are current that minute. That is a
reproducible-FILE guarantee, not a reproducible-ENVIRONMENT one: two
clean installs of the same wheel can carry different dependency sets,
and a tampered or substituted dependency never touches the wheel hash
that guards the gate.

The lane closure pins the whole environment: forge plus its entire
transitive runtime dependency set, as exact wheels, each sha256-pinned,
in ONE wheelhouse directory described by ONE manifest
(`closure-manifest.json`). The manifest's canonical-JSON sha256 — the
`closure_digest` — IS the closure identity: pin the digest and the
environment it names cannot drift apart from the pin silently.

## Building a closure

```bash
# from the PROMOTED release (production): the wheel the latest archived
# promotion record vouches for (docs/releases/evidence/v*/promotion.json)
uv run python scripts/build_lane_closure.py

# from the LOCAL TREE (development; honestly less qualified — a moving
# source ref is never a pinned identity, exactly like dev-source)
uv run python scripts/build_lane_closure.py --local

# from an EXPLICIT pinned wheel URL
uv run python scripts/build_lane_closure.py \
    --wheel-url https://github.com/forcewake/forge/releases/download/v0.35.0/forge-0.35.0-py3-none-any.whl \
    --wheel-sha256 1e365612473426a2130000784a6cd8c707ffcedae7f8f6bcb34c3f75791700d9
```

The build refuses a non-empty target directory (default
`dist/lane-closure`) — a closure is never assembled over leftovers.

What the builder does, and why each step is shaped this way:

1. **The forge wheel** — `uv build` (local), a sha256-verified download
   (pinned URL), or the promoted wheel from the archived promotion
   record (an image-only record refuses honestly: it vouched for no
   wheel).
2. **The dependency set comes from the LOCKFILE, never a live index**:
   `uv export --frozen --no-dev --no-emit-project` renders `uv.lock` —
   the committed, hash-pinned resolution — as a requirements file where
   every pin carries `--hash=sha256:…`.
3. **The download is hash-checked**: a seeded builder venv runs
   `pip download --only-binary :all: --require-hashes -r …` — pip
   refuses any artifact whose bytes do not reproduce a lock hash, and
   the wheelhouse stays wheels-only.
4. **The manifest** (`closure-manifest.json`, schema
   `forge.lane.closure/1`) records every artifact name + sha256, the
   forge wheel identity (name, version, source), and the canonical
   resolution command (path-free, so it is a digest axis that two clean
   builds reproduce). `closure_digest` is the sha256 over the manifest's
   canonical JSON.

Two clean builds of the same lock produce the same `closure_digest`
(the suite pins this: same inputs → same artifacts → same manifest →
same digest). `uv build` is byte-deterministic, so the local route is
reproducible too — but its wheel is still a moving source ref until
promoted; production pins the promoted record's wheel.

## Verifying a closure

```bash
uv run python scripts/build_lane_closure.py --verify dist/lane-closure
```

`verify_closure_dir` (the library function; the CLI is a thin wrapper)
checks, with no network and no imports from the directory:

- the manifest is present, parses, carries the right schema tag, and
  its stored `closure_digest` reproduces from its own body (a tampered
  manifest row is detectable against the identity recorded beside it);
- EVERY declared artifact is present with the exact pinned sha256 —
  a missing wheel and a tampered wheel are each a named problem;
- the directory contains NOTHING besides the manifest and the declared
  artifacts — an undeclared file (the poisoned-cache fixture: a foreign
  wheel planted beside the closure) is a typed refusal, not a warning.

Any problem → `ClosureVerificationError` listing each one, exit 1,
before anything executes. A return value means: this wheelhouse IS the
closure its digest names.

## Enforcing the closure at install time (the `closure-wheel` route)

The closure route is an OPTIONAL, additive lane on the R36-07 install
ladder (`ci/templates/forge-harness.github.yml` and its dogfood mirror;
the fragment's route resolution is owned by that ladder). The library
contract (`resolve_closure_install_route`) is what the fragment mirrors:

- **Selected** when `FORGE_LANE_CLOSURE_SHA256` (the expected
  `closure_digest`) is set non-empty and `FORGE_LANE_CLOSURE_DIR` names
  the staged wheelhouse. Set-but-empty behaves as unset (the R36-07
  normalization — an emptied Actions variable selects no route).
- **Conflicts refuse**: the closure pin beside the dev source flag, an
  explicit `FORGE_LANE_WHEEL`, or an explicit `FORGE_LANE_REF` raises a
  `LaneInstallRouteConflict` naming both variables, before any download
  or install. The closure IS the wheel route — the promoted wheel ships
  inside it — so there is never a lawful combination.
- **The install is offline**: `pip install --no-index --find-links
  <closure-dir> forge==<version>`. Under `--no-index` pip never
  consults a registry, so the TARGET repository's own lockfile cannot
  replace the collector runtime — the only forge pip can see is the
  hashed wheel inside the verified closure.
- **The identity gate** (`enforce_closure_install`) then binds the
  result: the verified manifest's digest must equal the pinned
  `dependency_closure_digest` (a verified wheelhouse that is not the
  PINNED closure is a different runtime), and the installed set
  (name → version, e.g. from `importlib.metadata`) must match the
  manifest's pins EXACTLY (`verify_installed_fingerprints` semantics:
  version mismatch / missing / undeclared each listed, no partial
  pass). Refusal happens before the first model call.

A qualification profile carries the pin as
`QualificationProfile.dependency_closure_digest` (additive; empty keeps
the wheel-pinned meaning). The profile digest covers it, so changing
the pinned closure is a different qualification — the controlled
update/requalification path the backlog demands.

## Supply-chain binding to the release record

`verify_artifact_supply_chain(closure_manifest, release_record)` binds
the closure's forge wheel to the archived promotion record: the sha256
inside the closure must be the sha256 the record vouches for. The
refusals are typed and honest:

- `not_built` — an image-only record (no wheel was built). The record
  cannot vouch for a wheel claim, so none is verified — never a
  fabricated green.
- `digest_mismatch` — the closure carries bytes the release never
  qualified.
- `identity_mismatch` — the digests agree but the file identity does
  not.

This composes the manifest hash check (which already refuses tampered
bytes) with the RECORD binding: the right bytes from the wrong release
are still wrong.

## The security boundary — what it IS and who enforces it

The backlog's demand is honesty here: a command allowlist is not a
sandbox. The division of labor:

- **The runner/container platform enforces the boundaries** — network
  egress allowlists, filesystem scoping, credential injection. Forge
  does not implement a sandbox and does not claim one.
- **Forge verifies and probes**:
  - *Install identity* — the closure manifest and the fingerprint gate
    prove WHAT the runtime is (the `runtime.installed_fingerprint`
    evidence).
  - *Egress* — `probe_egress_pair` dials BOTH a permitted destination
    (must succeed — the producer evidence) and a denied one. The honest
    statuses: `denied_by_policy` (consistent with enforcement; a
    refused probe proves the control MAY exist, never that it does),
    `denied_unexpected` (the denial is real, this policy cannot claim
    it), `policy_declared_but_not_enforced` (the hook is present and
    the denied destination ANSWERED — the one shape the probe proves),
    `policy_absent`, `probe_indeterminate`. DNS failure alone is never
    policy proof — the pair distinguishes them.
  - *Credentials* — `verify_credential_isolation(staged_names,
    forbidden_names)` checks that NO publisher credential name is among
    the names the lane stages. Names only, never values: a refusal that
    quoted a secret would leak the thing it guards. The receipt
    (`credential.staged_scope_receipt`) freezes WHAT the lane stages
    and the isolated verdict; only selected model/read credentials
    reach the lane process — publisher credentials stay outside every
    agent workspace (the lane template's per-driver credential gating).

`runtime_boundary_report(profile, egress_pair, closure_manifest,
installed=…, credential_receipt=…)` composes the legs into ONE document
(schema `forge.qualification.boundary/1`) carrying the R36-10
observability keys — `runtime.installed_fingerprint`,
`security.egress_probe_results`, `credential.staged_scope_receipt` —
plus the closure binding. Honest by construction: a leg that never ran
keeps its `not_*` status, lands in `problems`, and holds the verdict at
`outside_boundary`. A partial boundary report is an honest report,
never a green one.

## Operational notes

- The closure is platform-shaped: the builder downloads the wheels the
  current interpreter/platform resolves from the lock. Build the
  wheelhouse on the lane's platform class (or pin per-platform
  closures per digest).
- The builder venv (`uv venv --seed`) is staging-only: the closure
  contains wheels, never the builder's tooling.
- `--requirements` overrides the lock export with an explicit
  hash-pinned requirements file (tests, tiny closures); the download
  stays `--require-hashes`.
- Requalification: any change to the closure inputs (a new lock, a new
  forge wheel) changes the `closure_digest`, which changes the profile
  digest — the qualification record that pinned the old closure is
  invalidated and names its upgrade; nothing drifts silently.
