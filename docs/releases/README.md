# Release evidence (R32-19)

How a forge release artifact earns its mutable tags, and what evidence is
kept to prove it afterwards. The implementing module is
`src/forge/release_promotion.py`; the research basis is
`docs/research/2026-09-23-e2e-qualification/03-evidence-based-capability-qualification.md`.

The one-sentence contract: **a digest is promoted only from evidence that
was recorded before promotion — the gate queries evidence, it never re-runs
anything.**

## The promotion gate

The release workflow (`.github/workflows/release.yml`) runs in three jobs:

1. `publish-ghcr` — guards (release token, tag == `forge.__version__`),
   build once, push **by digest**, then the canary
   (`scripts/canary_smoke.py`) against that exact digest: the fresh-DB
   recipe (real alembic chain → boot gate → `/health` version → `/mcp`
   401 → doctor) and the previous-release upgrade, with real-shaped seeded
   data that the upgrade must preserve (see below). Stage outcomes are
   written as capability-tagged machine records (`--results-json`).
2. `promotion-gate` — `python -m forge.release_promotion gate` evaluates
   the **qualification profile**:
   - the required CI checks for the **tagged sha** — explicit,
     provenance-bound names, never wildcards: `lint`, `typecheck`,
     `test (3.13)`, `test (3.14)`, `integration`
     (`.github/workflows/ci.yml`), collected from the GitHub check-runs
     API;
   - the canary stage outcomes from job 1.

   Verdicts: `promote`, `conditional_promote`, `block`.
   - a **failed** required check blocks promotion **even when the canary
     passed**;
   - a required check with **no recorded result** (never executed,
     cancelled, still pending) blocks **fail-closed**, with the check's
     provenance named in the reason;
   - a check that **failed and passed on retry** records BOTH attempts and
     is a `conditional_pass` — never silently green;
   - a canary stage that self-skipped is recorded as `skip` and never
     counted as a pass.
3. `attach-tags` — only runs when the gate qualified the digest: the
   mutable tags (`:vX`, `:latest`) move onto it, then keyless signing and
   the provenance attestation.

## What is archived per release

`docs/releases/evidence/v<version>/` is the immutable, committed evidence
store (deterministic paths; the storage IS the path):

- `manifest.json` — the release-evidence manifest snapshot
  (`forge.release_manifest`) as of the release, generated from the tagged
  tree;
- `promotion.json` — the promotion record, stamp
  `forge.release.promotion/1`: exact image digest, wheel/sdist identity
  (or an explicit note when the pipeline builds none), version, qualifying
  CI run id + head sha, per-check results with provenance and full attempt
  history, canary stage outcomes with capability tags, the derived
  decision and its timestamp.

Files are written once (`archive_release_evidence`); byte-identical
rewrites are no-ops, different content over an existing artifact is
refused unless superseding a re-tag deliberately. The workflow also
uploads the record as a run artifact and prints it into the step summary.

`v0.33.0` is a **retrospective** record, reconstructed from the GitHub
check-runs API and the release run logs: it shows the gate's verdict for a
release that shipped **without** one — `blocked`, because the `typecheck`
required check failed on the release sha (the mypy error shipped in the
image) while the canary passed. That record is the standing demonstration
of the policy: a green canary never outweighs a red required check.

## The gaps query

`python -m forge.release_promotion gaps [--fail-on-gap]` — the
doctor-style export: every capability whose `evidence_class` requires live
qualification (`boot_canary`, `real_provider_e2e`) with **no fresh
promotion evidence** — no record, no canary stage tagged with the
capability, a blocked record, or evidence only for an older version. Each
gap carries its reason. Fresh means: a qualified promotion record at the
version being checked tags canary evidence for that capability.

R36-22 extends the query with the **profile-qualification channel**: the
live-provider capabilities (`real_provider_e2e`) are judged on the
separate committed record store (`qualification/records/`, see
[profile-records.md](profile-records.md)) — a provider-matched record at
the version being checked whose DERIVED verdict is `supported` or
`lab-qualified` clears the gap; a `declared_only` record keeps it, named.

## Profile qualification — a separate evidence record (R36-22)

A promoted digest still does not qualify every provider/recipe/harness
combination. That lives in **another** record: `qualification/records/`
(stamp `forge.profile.qualification/1`), documented in
[profile-records.md](profile-records.md) — evidence classes with
applicability that never substitute for one another, verdicts derived
(`supported | lab-qualified | declared_only | unqualified`), named
requalification triggers, upgrade claims that distinguish same-head
preservation from real schema transitions, and a promotion-refusal hook
(a required profile evidence entry marked `skip` refuses that profile's
promotion even with core CI green — wired into the release workflow's
`promotion-gate` job).

## Generated version pins

`scripts/generate_template_pins.py` renders the README image pins **from
the archived promotion record** — the pins can only cite digests whose
evidence is committed. Everything it owns sits between
`<!-- generated by scripts/generate_template_pins.py -->` fences, the
block carries the promotion verdict, and `--check` (exit non-zero on
drift) is the CI guard. Hand-editing a pin line is the drift this removes.

## The seeded-upgrade canary and its honest limitation

`scripts/canary_smoke.py --seed-real-data`: after the previous image
migrates a fresh DB to the N-1 head, the canary seeds real-shaped rows
with raw SQL against that exact schema — a `flow_runs` row mid-flight with
its frozen `run_specs` spec, bounded `step_runs`, a `control_commands`
row, a `publication_intents` row — fingerprints them (per-table row counts
plus sha256 over ordered row identity), and the upgraded schema must
reproduce the fingerprint exactly.

Limitations, stated plainly:

- the rows are **real-shaped, not app-minted**: driving the previous
  image's application code to produce them would need a provider fixture
  the canary deliberately does not have, so the inserts are
  schema-accurate SQL at the N-1 head instead;
- tables that do not exist at the previous head (e.g. `checkpoint_metadata`,
  new in 026) cannot be seeded — the set covers what a real deployment of
  the previous version could actually hold;
- fresh DBs only — the canary never touches real data.
