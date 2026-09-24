# Verification binding — the exact candidate and the tested world

Reference note for the R36-14 verification binding
([`forge.adaptive.verification_binding`](../../src/forge/adaptive/verification_binding.py),
issue #273). A green pipeline and a verified run are different claims:
independent verification is positive proof that the required checks ran and
succeeded **for the exact candidate, after restore/revision/repair, in the
world that was actually tested**.

## The subject a verdict names

Every verdict the GitHub gate records carries a `verification.subject_identity`
fragment (`forge.verification.subject/1`) built from durable run facts:

| field | meaning |
| --- | --- |
| `candidate_digest` | the candidate's content binding — the collector's diff digest (sha256 over the exact `candidate.diff` bytes) when the publication recorded one, else a digest derived over (published oid, frozen base, plan digest) |
| `source_oid` | the published candidate commit — the review head |
| `tested_oid` | the sha the **provider** tested; recorded separately because a synthetic merge legitimately tests a different sha than the review head |
| `generation` | the collector's binding: work id, checkpoint, `exact`/`unbound`/`fresh`, generation vs checkout |
| `plan_revision_digest` | the plan the candidate answers to |
| `environment_profile_digest` | the spec-frozen execution profile — the tested world |
| `subject_digest` | sha256 over all of the above |

Any field moving is a different subject; a verdict approves its subject, never
a branch (ADR-0008 sharpened to content, not just sha spelling).

## Freshness

`verification.freshness_unknown` / the freshness gate
(`forge.runs.verification.verdict_freshness`):

- **current** — the recorded verdict's subject binds this exact candidate
  (and, when both sides record one, this environment profile);
- **stale** — the subject names another candidate/world: a passed record
  re-renders as `status="stale"` (`render_stale`) with the subject retained
  for audit, and it can never produce `verified_ready`. The resume path parks
  such runs `verification_stale` — fresh verification is required before the
  ready decision;
- **unknown** — the record predates the binding (no subject fragment): its
  legacy sha binding decides exactly as before.

## Expected-report inventory

The qualification `ExpectedReports` machinery freezes the expected report set
**with the work contract** at dispatch
(`forge.adaptive.qualification.freeze_report_inventory`) and it is recorded on
the run. At verdict time the observed reports reconcile against the **frozen**
rows (`verification.expected_report_coverage`): a missing report, a skipped
required check, or a report from an **older attempt** (its own claimed
candidate digest differs) keeps the run waiting — never `verified_ready`. The
file-based TRX arm reuses `reconcile_reports`, so a deliberately failing
secondary .NET project cannot disappear behind a passing first report.

## Repair classification

Infrastructure-**prerequisite** failures — runner unavailable, report/checks
transport error — classify as `repair.failure_class="infrastructure"`, never
as code defects: they consume no code-repair iteration. They ride a bounded
**distinct** budget (`repair_ledger.infrastructure_retries`, default 2,
`FORGE_INFRA_REPAIR_RETRIES`): within it the run keeps waiting; exhausted, it
blocks honestly with the `verification_infrastructure` vocabulary.

## Standing rules

- A green harness job **alone** never satisfies independent verification
  (`harness_green_verifies` is unconditionally false) — the required-checks
  positive proof over the frozen spec stays the only `verified_ready` source.
- A changed tested-environment/profile digest invalidates only the evidence
  that claimed the previous digest (`invalidate_for_environment_change`);
  invalidated rows are retained for audit, never deleted.
