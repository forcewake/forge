# Q39-02 (#321) — Rebind GitLab continuation to the approved active revision text

**Date:** 2026-09-24 · **Issue:** [#321 / backlog Q39-02](https://github.com/forcewake/forge/issues/321)
**Prior art:** #251/#272 (the GitHub `dispatch_plan_binding` + executor-digest discipline), #288 (the
GitLab lane-resume envelope), #313 (the combined steering trace — the live counterexample),
`docs/evaluation/2026-09-25-combined-steering/` (the counterexample's record)
**Offline pins:** `tests/test_adaptive_revisions.py` (rules), `tests/test_runs_service.py`
(dispatch boundary), `tests/production_entry/test_gitlab_revision_rebind.py` (RB-1..RB-3 traces)

## 1. The defect and the live counterexample

At `6df4020`, the GitLab dispatch (`RunService._advance_harness`) built the lane brief from
`spec.plan_summary` — the ORIGINAL executable spec frozen at `/go`. The native
`/approve-revision` activation transaction switched `active_plan` identity and digest (proven
live in #313), but the durable pointer carried no revision CONTENT, so the executor kept
receiving the superseded bytes.

The author-recorded counterexample (`2026-09-25-combined-steering`, cycle 1): the operator's
approved revision renamed the public entrypoint `check` → `validate_email`; the activation
routed the checkpoint `preserve`; the first continuation restored the renamed WIP **and then
reverted it**, because the brief still described approach X. The run needed a **second manual
standing steer** to succeed. Identity → revision CONTENT was the missing join.

## 2. The ApprovedInput contract as landed

One immutable record, schema **`forge.revision.approved-input/1`**
(`forge.adaptive.revisions.ApprovedInput`), resolved by `resolve_approved_input(...)` at EVERY
GitLab dispatch entry (start, retry, revival, repair re-dispatch — all funnel through
`_advance_harness`):

| Field | Source |
|---|---|
| `task_title` / `task_description` | the frozen, digest-verified RunSpec (R04 — never a live issue re-read) |
| `plan_text` | **the ACTIVE revision's rendered TEXT** (`render_revision_brief` — summary, step objectives, the digest, the reuse route, the preserved evidence) |
| `source` | `revision` \| `spec` \| `spec-legacy` (below) |
| `plan_id` / `work_id` / `active_revision` / `plan_digest` / `revised_from_digest` / `work_contract_digest` / `activated_by_decision` | the durable `active_plan` pointer the activation CAS switched |
| `evidence_refs` | the artifacts the activation's WIP reuse decision preserved |
| `allowed_writes` | the spec's frozen `allowed_paths` |
| `wip_reuse` | the persisted `checkpoint_reuse_decision` document, verbatim |

Resolution rules (fail closed):

- **active revision with content** — the content must re-parse AND its canonical digest must
  equal the pointer's `plan_digest` (the digest the CAS switched). Mismatch → typed
  `RevisionRebindRefused("content_digest_mismatch")` → the run parks
  `blocked(rebind_refused…)` with an operator note; ZERO provider I/O. Never a fallback.
- **no active revision** — today's behavior: the spec's plan text, labeled `source: spec`.
- **prior-version pointer without content** (persisted before this change) — the EXPLICIT
  legacy adapter: the spec brief under the label `source: spec-legacy`. The superseded
  revision's bytes are never re-derived.

The activation transaction (`activate_pending_revision`) now persists the switched revision's
own `model_dump()` beside its digest (`active_plan.revision_content` — additive; pre-existing
pointers parse unchanged). The old approved RunSpec stays history — nothing mutates it to
simulate activation.

## 3. The brief envelope and the three-way digest proof

The dispatch brief (the GitLab `FORGE_PLAN` variable) is generated FROM the resolved record —
a pure function of durable state. On a revision-bound dispatch the envelope gains three
variables (conditional, mirroring the GitHub dispatch's conditional `plan_digest` input;
no-revision dispatches stay byte-identical to the pre-rebind envelope):

- `FORGE_PLAN_DIGEST` — the ACTIVE revision's canonical digest;
- `FORGE_SPEC_DIGEST` — the frozen RunSpec digest;
- `FORGE_BRIEF_ENVELOPE_DIGEST` — the A03 brief envelope built from the record
  (run id + task bytes + the revision's brief TEXT + the spec digest).

The resolved record (`evidence.approved_input`) and `evidence.revision.executor_input_digest`
persist **beside the native-start intent** — before the provider call — so a lost start
response or worker death still leaves the exact identity this dispatch briefed under.

The PE-7 three-way discipline, proven in RB-1 (`tests/production_entry/
test_gitlab_revision_rebind.py`):

1. **evidence** — `revision.executor_input_digest.executor_input_digest`, computed at dispatch
   over `(run_id, plan_digest, envelope_digest, spec_digest, lane_resume_mode)`;
2. **server** — the same digest recomputed from the native ledger's RECORDED pipeline
   variables (what the production client actually sent), via
   `forge.adaptive.revisions.executor_input_digest`;
3. **consumed** — the recorded `FORGE_PLAN` bytes verify against the dispatched
   `FORGE_BRIEF_ENVELOPE_DIGEST` (`forge.harnesses.brief_envelope.verify_brief_envelope`),
   and their leading span is exactly the persisted `approved_input.plan_text` (the repair
   context follows, bounded, as before).

## 4. The live counterexample's shape — outcome

RB-1 drives the counterexample end to end on the production entry (real RunService + real
GitLab client over HTTP, real durable DB, real worker restart, real `/approve-revision`
ingress, real activation transaction, real checkpoint channel):

issue → `/go` (the spec brief) → the first runner's lane applies the rename and uploads a real
checkpoint → the CI job cancelled natively → a restarted worker classifies the loss → revision
2 (the rename, steps byte-identical so the WIP stays compatible) staged through the app's own
`stage_pending_revision` and approved natively → `/retry`:

- the re-dispatched `FORGE_PLAN` carries revision 2's TEXT (`validate_email`); the spec brief
  is history (AC-01);
- `FORGE_PLAN_DIGEST` == `plan_digest(revision 2)`; the resume is `required` over the exact
  pinned checkpoint; the reuse route is `preserve`;
- the resumed lane (a real subprocess, required restore) restores the checkpoint and the
  rename **survives the continuation** — no revert;
- **zero steer rows exist for the run**: ONE approved direction change, no rescue steer
  (AC-03). The #313 cycle-2 workaround (a standing-direction steer posted while blocked) is
  no longer needed on this path.

## 5. The standing-guidance promotion (the #313 cycle-2 remedy, formalized)

`forge.adaptive.revisions` now owns the classification and the promotion:

- `is_standing_guidance(text)` — a CLOSED marker vocabulary (`standing direction`, `from now
  on`, `going forward`, `henceforth`, …). An unmarked instruction is a next-turn hint and
  promotes to NOTHING — nothing that was not explicitly standing can expand authority.
- `standing_guidance_revision(active, text, command_id)` — the PENDING proposal: steps stay
  byte-identical (WIP compatibility survives by construction), the standing text folds into
  the summary with its command provenance, the revision number follows the active one.
- `stage_standing_guidance_promotion(...)` — stages that proposal through the EXISTING
  `stage_pending_revision` route; a human `/approve-revision` (the activation CAS) still owns
  the switch. Pinned by unit tests: an unapproved promotion leaves the resolved approved
  input byte-unchanged; a next-turn hint stages nothing.

## 6. Old-revision callbacks cannot finalize the new attempt (verified)

- The activation CAS: a late approval minted against the revision-1 world refuses
  `parent_mismatch` and consumes nothing; a redelivered approval of a consumed decision is
  `already_active` (one switch, one continuation). Proven in RB-2.
- The publication/verification guards key on the identities the CAS switched: `run.plan_digest`
  (switched by the activation), the re-bound human gate (R32-11), and the publisher's own
  spec-digest + attempt-base + generation fences (F14/R10 — the SPEC is the write authority
  and is legitimately unchanged by a revision). `stale_callback_guard` fences a late
  native callback whose digest was minted against the superseded plan — proven in RB-2
  against the live row digest. The reconciler adopts outcomes only through the journaled
  (latest-attempt) handle, so an old runner's late pipeline cannot reach the run — the
  CE-3 supersession discipline, unchanged.
- The more-restrictive revision: `refused_wip_reuse` (the #272 fence, ported to the GitLab
  dispatch) refuses a `required` resume whose checkpoint the activation routed to
  `fresh_attempt` BEFORE any provider I/O, with the recorded reason explaining the rejected
  reuse (AC-04, proven in RB-3 and at unit level).

## 7. Observability

- `revision.executor_input_digest` — the evidence document (§3).
- `revision.rebind_refused{reason}` — the typed refusal codes (`content_digest_mismatch`,
  `content_unreadable`, `run_not_found`), logged and parked into the run's status reason and
  an operator note.
- `continuation.manual_guidance_repeated` — remains ABSENT on this path: RB-1 asserts the
  continuation needed zero manual guidance beyond the one approved direction change.

## 8. Honest seams

- The fake native server has no real runner that materializes the job env, so the resumed
  lane subprocess receives the dispatched envelope variables directly, and the shared
  `run_lane` helper writes its own canned brief file — the runner-consumed brief bytes are
  proven at the envelope/ledger level (§3), not through the lane's brief file.
- The revision WORLD (revision 1's pointer) is staged through the app's own durable shape
  (the #313 disclosure): the live GitLab planner does not yet emit plan revisions. The
  approval, the activation CAS, the reuse decision, the dispatch and the resume are fully
  native.
- The module is env-clean once: the PE module scrubs every `FORGE_*`/`GITLAB_*`/`GITHUB_*`
  variable for its duration and sets explicitly what it needs.

## 9. Reproduction

```bash
uv run pytest tests/test_adaptive_revisions.py tests/test_runs_service.py -q   # the rules
uv run pytest tests/production_entry/test_gitlab_revision_rebind.py -q        # RB-1..RB-3
```
