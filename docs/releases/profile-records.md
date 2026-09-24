# Profile-qualification records (R36-22 / #281)

A promoted release artifact proves the DIGEST qualified — the required CI
checks passed on the tagged sha and the canary stages passed on the exact
image ([release_promotion](../../src/forge/release_promotion.py)). That
says nothing about whether a given provider/recipe/harness combination
works on that artifact. The profile-qualification record is the OTHER
evidence: a SEPARATE, committed record store —
`qualification/records/*.json`, stamp `forge.profile.qualification/1` —
implementing `src/forge/profile_qualification.py`.

The one-sentence contract: **a profile is as qualified as the weakest
evidence class its required capabilities carry — verdicts are derived from
the records on every read, never asserted by them.**

## The record

One record binds ONE provider/recipe/harness combination on ONE release:

| Field | Meaning |
| --- | --- |
| `provider` + `provider_version` | e.g. `gitlab` + `GitLab CE 19.3.2 (revision 34042bf7d00)` |
| `runtime_recipe` | e.g. `python-3.13 (uv standalone, lane venv)` |
| `harness_binary` + `harness_version` | the exact harness and its pinned version |
| `credential_route` | which credentials reach which process (bot PAT / read-only clone PAT / BYOK) |
| `control_capabilities` | the control verbs the profile exercises (`interrupt`, `steer`, …) |
| `verification_contract` | the independent check the profile demands (e.g. `required-jobs=smoke`) |
| `closure_digest` | the R36-10/#269 field: the sha256 identity of the hash-locked wheelhouse installed from; empty = wheel-pinned, stated |
| `image_digest` / `wheel_sha256` | the release artifact identity (`release.artifact_identity`) |
| `capabilities` | the REQUIRED capability set the record claims to qualify |
| `evidence` | the executed entries, each in its class (below) |
| `evidence_refs` | repo-relative pointers to the release-evidence archive / profile evidence bundles — referenced, never copied |
| `upgrade` | the executed upgrade facts (see below) |

There is deliberately **no verdict field**: `derive_verdict()` computes the
verdict from the evidence at every read, so an edited record cannot assert
more than its evidence supports.

## Evidence classes and the verdict lattice

Six classes, each a different applicability — **no class substitutes for
another**:

| Class | Applicability |
| --- | --- |
| `binary-smoke` | the artifact itself boots and passes its smoke recipe — proves the binary, never a provider workflow |
| `model-fixture` | authored cohort/model fixtures — proves the contract, never the provider or model |
| `pg-integration` | real PostgreSQL with authored fixtures — proves the database contract, never provider behavior |
| `offline-operational` | process-level operational scenarios in the lab (recovery, resume, restarts) without the live provider |
| `live-provider` | real task or recovery results against the live provider and model route the record names |
| `customer-acceptance` | a REAL design partner accepted the work — recorded once from the partner's decision, never regenerated from fixtures (AT-12) |

Verdicts, weakest first, and how evidence maps onto them:

- per required capability, the strongest **passing** entry's class decides
  the contribution: `binary-smoke`/`model-fixture` → `declared_only`;
  `pg-integration`/`offline-operational` → `lab-qualified`;
  `live-provider`/`customer-acceptance` → `supported`;
- the record's verdict is the **weakest** contribution — a profile is
  never auto-supported by a sibling capability;
- no entry, or nothing passing, for a required capability →
  `unqualified` (a `skip` is recorded as a skip, never counted as a pass).

So: a record claiming `supported` must carry at least one
live-provider-or-stronger evidence entry for EVERY required capability;
model-fixture evidence alone caps at `declared_only`; offline-operational
caps at `lab-qualified`. Runtime proof is never inferred from a test
filename or a self-declared label.

## Requalification triggers

`requalification_triggers(record, changes)` names the axis for every
change the record must answer to:

- **changed runtime dependency fingerprint** (`runtime_dependency_fingerprint`),
- **changed template defaults** (`template_defaults_digest`),
- **changed authority contract** — the checkpoint repository protocol
  version (`authority_contract_version`),
- **changed provider behavior** (`provider_behavior_fingerprint`).

Any trigger degrades the affected record's derived verdict to
`unqualified` with the trigger named. A change naming an axis the record
left UNPINNED is also a trigger (fail-closed — an unpinned axis never
silently absorbs a change). Degradation is a view: records are frozen
data, the store is append-only, and replaying an older record never
resurrects a verdict (ordering comes from the release version).

## Upgrade-claim honesty

`upgrade_claim(record)` DERIVES the claim from the executed facts:

- **`same-head-preservation`** — the canary ran N→N (the v0.35.0
  `027 -> 027` run): the seeded rows were preserved, and the claim says
  exactly that. A same-head test **never** labels itself a schema upgrade.
- **`schema-transition`** — an actual N-1→N migration, naming the source
  schema, the target schema, the representative seeded records and the
  preservation checks actually executed. A transition that seeded nothing
  and checked nothing (the v0.33.0 `024 → 026` canary, pre-seed-real-data)
  makes **no claim at all** — an upgrade claim must name what a real
  deployment held and what was checked.

## The gaps join and the promotion-refusal hook

- `python -m forge.release_promotion gaps` now joins the record store: a
  manifest capability whose `evidence_class` requires profile
  qualification (`real_provider_e2e`) stays a NAMED gap until a
  provider-matched record at the release being checked covers it with a
  derived verdict of `supported` or `lab-qualified`. A `declared_only`
  record does not clear the gap — it IS the gap, named: fixtures executed,
  live evidence absent. A release can stay promoted while an optional
  profile is withheld; both facts are stated.
- `python -m forge.profile_qualification gate` is the enforcement hook
  (wired into the release workflow's `promotion-gate` job): a record whose
  required evidence is marked `skip` — or failed, or missing — refuses
  that profile's promotion **even with core CI green**. Only each
  profile's latest record is judged; fixing a refusal means landing a NEW
  record, never editing history.

The store is immutable like the release archive: byte-identical rewrites
are no-ops, different content over an existing record is refused
(`write_profile_record`, `replace=True` only for an explicit rewrite).
A referenced evidence artifact deleted from the tree degrades the verdict
(`qualification.missing_evidence`) — it never crashes the loader and never
mutates history.

## Current profile verdicts

Regenerate with `python -m forge.profile_qualification capabilities` — the
table below is derived from the committed records (a drift test holds it
in sync):

<!-- generated by python -m forge.profile_qualification capabilities -- begin -->
| Profile | Provider | Release | Verdict | Strongest evidence | Tiers (executed traces) | Upgrade claim |
| --- | --- | --- | --- | --- | --- | --- |
| release-artifact-canary | * | v0.33.0 | declared_only | release-artifact-canary: binary-smoke; release-canary/previous-release-upgrade: binary-smoke | release-artifact-canary: none; release-canary/previous-release-upgrade: none | — |
| release-artifact-canary | * | v0.34.0 | declared_only | release-artifact-canary: binary-smoke; release-canary/previous-release-upgrade: binary-smoke | release-artifact-canary: none; release-canary/previous-release-upgrade: none | same-head-preservation 026→026 |
| gitlab-ce-v1 | gitlab | v0.35.0 | declared_only | real-provider-e2e: model-fixture | real-provider-e2e: none | — |
| release-artifact-canary | * | v0.35.0 | declared_only | release-artifact-canary: binary-smoke; release-canary/previous-release-upgrade: binary-smoke | release-artifact-canary: none; release-canary/previous-release-upgrade: none | same-head-preservation 027→027 |
| gitlab-ce-v1 | gitlab | v0.36.0 | declared_only | real-provider-e2e: model-fixture | real-provider-e2e: none | — |
| gitlab-ce-v1 | gitlab | v0.36.0 | declared_only | real-provider-e2e: model-fixture | real-provider-e2e: scripted | — |
| gitlab-ce-v1 | gitlab | v0.36.0 | supported | real-provider-e2e: live-provider | real-provider-e2e: none | — |
| release-artifact-canary | * | v0.36.0 | declared_only | release-artifact-canary: binary-smoke; release-canary/previous-release-upgrade: binary-smoke | release-artifact-canary: none; release-canary/previous-release-upgrade: none | same-head-preservation 027→027 |
| gitlab-ce-v1 | gitlab | v0.37.0 | supported | real-provider-e2e: live-provider | real-provider-e2e: none | — |
| release-artifact-canary | * | v0.37.0 | declared_only | release-artifact-canary: binary-smoke; release-canary/previous-release-upgrade: binary-smoke | release-artifact-canary: none; release-canary/previous-release-upgrade: none | same-head-preservation 027→027 |
<!-- generated by python -m forge.profile_qualification capabilities -- end -->

Read plainly today (updated by R37-08 / #289): the `gitlab-ce-v1` profile
derives **supported** over its claimed `real-provider-e2e` capability —
`gitlab-ce-v1@live` carries the first live-provider evidence (the 2026-09-24
live single-writer flow: real model through the gateway, claude-code 2.1.273
on the real runner, the six-case oracle green on the exact candidate sha).
It is NOT promotable yet: the #298 machinery holds it — no committed live
`TraceRecord` exists under `qualification/traces/` for the capability (the
Tiers column honestly says `none`), and the manifest's human gate lists the
profile **pending-approval** (no human approver has signed the promotion
decision). The R37-08 interruption arm is recorded in the same record as
INFORMATIONAL failing evidence — mechanics live-proven, delivery failed
(two resumed turns with empty candidates) — so the cross-runner
FILE-preservation claim stays open, and the refusal-resolution matrix names
the follow-ups (including the LIVE-found v0.36.0 SDK-lane template exit
bug). A scripted tier still never supports `real-provider-e2e`; a live
trace file and a human approval are what stand between the derived verdict
and a supported manifest status.

## R37-06: the strict schema and the legacy store

`validate_record()` (R37-06 / #287, the validation half of the #298
overlap) type-checks every record: `executed_at` must be an ISO-8601
timestamp (a digest in a timestamp field is a finding naming the digest),
hash fields hex64, version fields semantic, and a `supported` verdict
must pin every requalification fingerprint. Records opt in with
`legacy: false`; the v1-era records are `legacy: true` — their findings
are REPORTED under `qualification.record_validation` and they load as
history, because history is not rewritten to satisfy a newer schema. A
strict record with any finding refuses the whole load (fail-closed).
`gitlab-ce-v1@0.37.0-preflight` is the first strict record: distinct
timestamp/hash fields, observed fingerprints, the typed
`refusal_resolution` matrix, and the read-only lab inventory
(`qualification/inventory-2026-09-24.json`) behind its observations.

## R37-17: tiers from executed traces, the human-gated manifest

R37-17 (#298) promotes profiles from executed evidence WITHOUT upgrading
the evidence class — three new derived views over the same frozen store:

**Executed traces and evidence tiers.** An evidence entry CLAIMS; an
executed trace record (stamp `forge.trace/1`, committed under
`qualification/traces/`) is the artifact behind the claim, carrying its
own provenance stamp: `scripted`, `live` or `refused`.
`derive_evidence_tier(record, traces)` joins each required capability onto
those traces: a scripted capture **stays scripted even when it invokes
real protocol code** (the committed CE-2 trace drives the real
production-entry code against authored fakes — a stronger scripted trace,
never live provenance); a `live` trace (real provider AND model route
provenance) upgrades only its own capability, and only in its own
provider's records; a `refused` trace (an executed observation of a
refusal) never upgrades anything. No trace at all → tier `none`
(fail-closed). Two HOLDs (`qualification.unmet_capabilities`): a
capability named `real-provider-e2e` whose tier is not live — the
fixture-only-record-with-a-real-provider-label negative arm — and an entry
claiming `live-provider` evidence whose traces cap the tier below live
(the trace is the source of truth; a label never upgrades a scripted
trace).

**The supported-profiles manifest.** `build_supported_profiles(store)`
(stamp `forge.profile.manifest/1`, CLI `python -m forge.profile_qualification
manifest`) renders per profile — only the LATEST record is judged — the
derived verdict, the trace-derived tiers per capability, the limitations
(the typed `refusal_resolution` matrix, unpinned axes, holds), the
requalification triggers (every withdrawal axis with its pinned value,
`profile.installed_fingerprint`), and a `human_approved_by` field that is
**required for any `supported` verdict**: a derived-supported profile
without a matching human approval (`qualification/profile-approvals.json`,
stamp `forge.profile.approvals/1`) is listed `pending-approval`, never
supported. The withdrawal axes add the release-artifact identity to the
four R36-22 fingerprints: a changed wheel or image identity — including a
different wheel under a CONSTANT version string — withdraws the profile's
claim (`qualification.withdrawn`). The manifest itself is never committed:
it is a view, re-derived on every read like the verdict.

**Install-instructions coherence.** `scripts/generate_template_pins.py
--capabilities` renders the qualified-pin view: a wheel pin may come only
from an artifact MATCHING a qualified profile (a record whose derived
verdict is `supported`/`lab-qualified` and whose `wheel_sha256` IS the
promoted wheel, `release.tested_sha`). An image-only record — or a
promoted wheel no qualified profile pins — gets a TYPED refusal (exit 2):
there is no fallback to the newest wheel elsewhere.

**Enforced upgrade-claim honesty.** A record may assert an
`upgrade.claim` label; `validate_record()` checks it against the derived
claim (`upgrade_claim()`). A 027→027 canary labelled `schema-upgrade`, a
seeded 026→027 transition labelled preservation, or ANY label on facts
that seeded/checked nothing is a typed `upgrade-claim-mislabelled`
finding (and refuses a strict record at load).

**Field-shape conflation, named.** Two new finding kinds extend R37-06's
validators: `sha-in-version-field` (a sha256 in `release_version`,
`harness_version`, `provider_version` or `authority_contract_version`) and
`version-in-hash-field` (a version string in `closure_digest`,
`wheel_sha256`, `image_digest` or an entry's `artifact_sha256`). The
tested sha and the deployed artifact are distinct identities, assumed
nowhere conflated — each binding axis (image, wheel, closure, template,
harness, provider behavior, model route, verification contract) carries
its OWN field with its OWN hash.

**The typed store.** `ProfileRecordStore` is the append-only view over
`qualification/`: overwriting an existing record file with different
bytes raises `ProfileRecordImmutableError` naming the versioned filename
(land a NEW record to supersede), and history stays separately queryable
(`history(profile)` / `latest(profile)` / `record(record_id)`) after new
records land.
