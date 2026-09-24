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
| Profile | Provider | Release | Verdict | Strongest evidence | Upgrade claim |
| --- | --- | --- | --- | --- | --- |
| release-artifact-canary | * | v0.33.0 | declared_only | release-artifact-canary: binary-smoke; release-canary/previous-release-upgrade: binary-smoke | — |
| release-artifact-canary | * | v0.34.0 | declared_only | release-artifact-canary: binary-smoke; release-canary/previous-release-upgrade: binary-smoke | same-head-preservation 026→026 |
| gitlab-ce-v1 | gitlab | v0.35.0 | declared_only | real-provider-e2e: model-fixture | — |
| release-artifact-canary | * | v0.35.0 | declared_only | release-artifact-canary: binary-smoke; release-canary/previous-release-upgrade: binary-smoke | same-head-preservation 027→027 |
| gitlab-ce-v1 | gitlab | v0.36.0 | declared_only | real-provider-e2e: model-fixture | — |
| release-artifact-canary | * | v0.36.0 | declared_only | release-artifact-canary: binary-smoke; release-canary/previous-release-upgrade: binary-smoke | same-head-preservation 027→027 |
<!-- generated by python -m forge.profile_qualification capabilities -- end -->

Read plainly today: no profile is `supported` or even `lab-qualified` yet.
The `gitlab-ce-v1` profile is declared-only — its live flow was refused at
preflight (the deployed control plane did not match the pinned wheel), so
only the authored offline trace and the install receipt exist. That is the
honest state, stated.
