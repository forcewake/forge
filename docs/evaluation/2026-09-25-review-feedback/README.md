# Q39-13 (#332) — Post-MR review feedback as a bounded revision on the current candidate

**Date:** 2026-09-25 · **Issue:** [#332 / backlog Q39-13](https://github.com/forcewake/forge/issues/332)
**Wired:** [#337 / R40-01](https://github.com/forcewake/forge/issues/337) (2026-09-26 — the §8 ingress seams closed; §8 now records the wired path)
**Prior art:** #321 / Q39-02 (the `ApprovedInput` rebind — the correction becomes the active
revision TEXT through that machinery), #288 (the GitLab lane-resume envelope), the #313
combined-steering trace (the live counterexample that motivated the revision-content join)
**Offline pins:** `tests/test_review_feedback.py` (the classification/record rules + the
service lane + the R40-01 gateway-parse arms), `tests/test_adaptive_revisions.py` (the
revision machinery this extends), `tests/production_entry/test_review_feedback.py`
(RF-1 / RF-2 traces), `tests/production_entry/test_feedback_ingress.py`
(R40-01: the REAL ASGI ingress → inbox → restarted worker → installed reconciler trace FI-1..FI-5)

## 1. The scenario

The user workflow does not end at the first Draft MR. A reviewer reads the candidate, opens a
discussion, and names a specific edit. The customer outcome: the reviewer requests exactly
that correction without restating the issue and without losing accepted work — human edits
preserved, only the permitted thing fixed, the affected checks rerun, a new reviewable result
returned, and **the bot never merges and never marks a human decision complete**.

Review feedback needs the SAME exact-input + authority discipline as initial implementation:
a durable request bound to the note that raised it, the CURRENT MR head, an authorized actor,
and the approved work scope — persisted as an input revision BEFORE any dispatch.

## 2. The inventory (which handler was reused — and why no new controller)

The GitLab note ingress inventory, as the code has it:

- the gateway (`forge/gateway/router.py::_match_run_command`) parses note commands and hands
  the metadata to the durable run-command step (`_ingest_run_command` → the worker step →
  `forge.runs.service.execute_run_command` → `RunService.run_command`) — the SAME dispatch
  that already carries MR-note metadata (`mr_iid`) for MR-bound commands (`/security`
  triage). MR notes with other verbs stay unparsed upstream (a deliberate gateway gate).
- the **adaptive** verbs (`/pause`, `/steer`, `/answer`, `/approve-revision` …) route through
  `forge.adaptive.command_router.ControlCommandRouter` — issue-scoped by construction (its
  run resolution is issue-based), background-task dispatched.

The narrowest compatible entry for a reviewer comment **on the Draft MR** is therefore the
classic note-command dispatch: `RunService.run_command` gains a `review_feedback` command
branch (the metadata shape the gateway already produces for MR notes: `note_id`, `mr_iid`,
`discussion_id`, `author_username`, `note_text`) that calls the new
`RunService.handle_review_feedback_note`. That handler follows the established
`handle_*_note` discipline: the `approvers_for` authority gate (the same gate as `/go`),
the A11 one-reply-per-note-id journal, journaled MR notes, typed refusals.

**No new controller, and `command_router.py` was NOT touched** — the adaptive router is the
wrong seam for MR-bound feedback (it resolves runs by issue, not by MR), and the review
scenario's subject is the MR. The remaining upstream seam is disclosed in §8.

## 3. The request contract as landed

One immutable frozen record, schema **`forge.review.feedback/1`**
(`forge.adaptive.revisions.ReviewFeedbackRequest`), stored in the run's evidence under
`review_feedback_requests`, **keyed by the originating note id**:

| Field | Meaning |
|---|---|
| `note_id` | the idempotency key — one reviewer comment → ONE durable request despite webhook replay (`record_review_feedback_request`; a DIFFERENT request under a used note id refuses `note_id_conflict`) |
| `discussion_id` / `mr_iid` | the originating discussion and its Draft MR |
| `actor` | must be in `approvers_for("gitlab", settings)` — else the request records `refused_unauthorized` with a refusal note, nothing staged |
| `head_sha` | **the CURRENT MR head at request time** (the live source-branch head) |
| `classification` | `clarification` \| `in-scope_correction` \| `material_change` (below) |
| `text` / `referenced_paths` / `diff_context` | the named edit, its explicitly claimed paths (backticked tokens) and the diff-position context |
| `status` | the lifecycle: `recorded` → `staged` → `dispatched`, or the typed terminals (`refused_unauthorized`, `deleted_discussion`, `conflicting_correction`, `correction_window_closed`, `stale_head`, `material_proposal`, `clarification_open`) |
| `invalidation` | the precise evidence invalidation recorded at staging (§6) |

The classification (`classify_review_feedback`) is fail closed:

- **`clarification`** — `/ask <question>`: routed to the approvers with a reply; **NO
  staging, NO dispatch** (the no-dispatch semantic of reviewer-only recovery — no coder
  budget is spent unless code was requested; the budget wiring itself is #325's).
- **`in-scope_correction`** — `/fix …` where EVERY explicitly claimed path is inside the
  frozen spec's `allowed_paths` (the same fnmatch semantics the changeset validation
  enforces — the gate and the writer agree on what the scope means). A correction naming
  no path at all cannot PROVE its scope and is material.
- **`material_change`** — out-of-scope or unprovable scope: the request becomes a durable
  **material proposal** and the reply says how to raise it (the material-revision approval
  route / a new request). **Never a permission expansion** — the bounded lane refuses to
  widen the write surface a human approved.

## 4. The bounded correction and the head binding

`stage_review_correction` resolves the ACTIVE revision's durable content (typed refusals
`no_active_plan` / `content_unreadable` — never a re-derivation), folds the correction into
the revision's SUMMARY with full provenance (discussion, note, head, the permitted change,
the referenced paths + diff) — **steps byte-identical, so the held WIP stays compatible** —
and stages it through the EXISTING `stage_pending_revision` human gate with a decision id
derived from the note identity (`correction_decision_id` — a replay re-derives the same
decision). Nothing dispatches: a human `/approve-revision` (the EXISTING route, untouched)
activates it, the activation computes the WIP reuse decision, and the correction becomes the
active revision TEXT — the #321 `ApprovedInput` machinery briefs the next executor with it
(proven end to end in RF-1: the dispatched `FORGE_PLAN` begins with the corrected revision's
rendered text; the agent sees the referenced diff + the current head, never an obsolete
source).

**Head binding:** the request records `head_sha` at request time; the correction's dispatch
entry (`RunService.evaluate_review_corrections` → `_begin_review_correction` — **registered in
the installed periodic reconciler since #337**, see §8) re-reads the live branch head and
runs `head_binding_guard` — an unexpected head move (a human edit between request and
publication) is the typed **`stale_head`** conflict: the request is durably marked, the
operator note explains, and NOTHING dispatches (human edits preserved, never
force-overwritten; proven in RF-2).

The re-dispatch itself is the repair edge (ADR-0004: `waiting_ci → evaluating_ci →
proposing`), the same production leg every repair/revival funnels through, with the bounded
correction context appended to the executor's brief. `ready_for_human` deliberately has no
outgoing edge — the human decision is final — so feedback arriving after readiness is
recorded (the audit stands) and refused `correction_window_closed`.

## 5. Idempotency + the negative matrix

- **duplicate delivery** — the note id is the idempotency key: one durable request, one
  journaled reply (the `/go` A11 pattern), a replay re-derives the same decision id, and a
  replay after a lifecycle step replays the outcome body without re-staging.
- **unauthorized actor** — recorded `refused_unauthorized`, refusal note, nothing staged.
- **deleted discussion** — the note's discussion must still exist when the discussions
  surface is readable; a vanished thread records `deleted_discussion` and refuses.
- **conflicting reviewer instructions** — one live staged correction per run: a second
  correction note records `conflicting_correction`, naming the pending decision.
- **stale SHA** — the typed `stale_head` conflict (§4).
- **out-of-scope** — `material_change`, never a permission expansion (§3).

## 6. Evidence invalidation + the required-discussion readiness gate

`correction_invalidation_set` partitions the run's evidence on the **applicability axis**
(the head/candidate each item was produced under): only the review + verification bound to
the corrected head are superseded (never deleted; an invalidated verification never
auto-returns to passed); everything else is preserved. The partition is recorded in the
request at staging time. The rerun is structural: the correction cycle commits a NEW
candidate, so the required checks re-run (a fresh pipeline on the new sha) and the review
re-runs (the sha-binding replay guard refuses to reuse the old review).

**The readiness gate** (`_review_feedback_unresolved`, checked at the green-pipeline →
review handoff and again at the review boundary): a candidate whose code-requesting
correction discussions are still unresolved is **NOT ready** — however green its pipeline.
The run parks (re-checkable each reconciler pass; the review replays without a second model
call), and ONE held note per candidate names exactly what blocks readiness. Resolution is
GitLab's own resolvable-discussion model — **the REVIEWER resolves the thread; forge never
resolves, never merges, never marks the decision complete** (asserted in the tests: zero
`resolve_discussion` / merge calls; the MR stays a Draft). The final evidence comment gains a
**Review feedback** section naming the resolved and still-open discussions and the TESTED
candidate (byte-identical legacy comment for runs without feedback).

## 7. The traces

- **RF-1** (`tests/production_entry/test_review_feedback.py`) — the full native arc: issue →
  `/go` → the lane's work + the shipped collector → native artifacts → the reconciler
  publishes the candidate and opens the Draft MR → the reviewer's `/fix` comment through the
  REAL `execute_run_command` dispatch → ONE request bound to the current head → the REAL
  `/approve-revision` router → the correction re-dispatch carries the reviewer's text: the
  `FORGE_PLAN` digest equals the corrected revision's canonical digest, the brief envelope
  verifies over the ledger's bytes, and the three-way executor digest holds (evidence == a
  recomputation from the native ledger's recorded variables == the consumed bytes). A
  replayed note records nothing new. The MR stays a Draft.
- **RF-2** — the typed negative: a human edit lands on the factory branch between request
  and publication → `stale_head`, the request durably marked, the conflict note posted, ZERO
  dispatches.

## 8. The wired path (R40-01 / #337) and the remaining disclosed seams

The two seams this document used to disclose as gaps are CLOSED (2026-09-26):

- **The gateway parse for MR-note verbs — WIRED.**
  `forge/gateway/router.py::_match_run_command` parses `/fix` and `/ask` on MR notes
  (MR-discussion-bound: an issue- or commit-bound verb is not a run command at all) and
  emits the `review_feedback` run command through the SAME durable ingress every note
  command travels (`_ingest_run_command` → inbox + scheduled step in ONE transaction, the
  202 acknowledged only after the commit). The surface sits behind ONE capability flag,
  `FORGE_REVIEW_FEEDBACK_ENABLED` (default OFF — zero routing when off, the
  `adaptive_command_set()` pattern; `forge/gateway/feedback.py`). The transport identity
  (`X-Gitlab-Event-UUID`) rides the metadata and is deduped at the ingress
  (`delivery:{uuid}`, Redis SET-NX); the LOGICAL identity — the note id, unique inside a
  project, so `(connection, project.id, mr.iid, note.id)` is collision-free — is deduped
  by the inbox unique index and keys the request, the reply journal and the revision
  decision: a manual redelivery (new uuid, same note) collapses onto ONE request, never a
  second paid correction run. A malformed verb (`/fix` with no description) is the TYPED
  ingress refusal — 2xx with `refusal_reason: malformed_feedback_command`, no run
  command — because a 4xx spike counts toward GitLab's hook auto-disable
  (4 failures → 24 h backoff project-wide). Proven end to end by FI-1..FI-5
  (`tests/production_entry/test_feedback_ingress.py`): the real ASGI ingress → durable
  inbox → the wake LOST (worker died after the inbox commit) → the INSTALLED
  `run_step_worker` resuming over a fresh engine → the existing handler classifies → the
  INSTALLED `run_reconciler` re-drives the approved correction — no direct
  handle/evaluate call anywhere in the positive arms.
- **The reconciler registration — WIRED.** `forge/runs/reconciler.py::run_reconciler`
  (the exact loop `worker/app.py` gathers) schedules `evaluate_review_corrections` in its
  pass list: a bounded scan over `waiting_ci`/`evaluating_ci` runs, one
  exception-isolated loop per run. It doubles as the recovery path when the approving
  delivery was lost. Discovery of requests stays in the ingress/step path; authorization
  to start an attempt stays with the human `/approve-revision` — the pass re-drives only
  what BOTH already decided. FI-5 proves both registrations load-bearing: with the parser
  registration reverted (flag off) nothing is ever ingested; with the reconciler pass
  neutralized the approved correction never re-drives.
- **The capability output.** `forge.capability_manifest` carries
  `operator-commands/review-feedback` (`production_wiring`, commands `/fix` `/ask`) —
  platform-scoped to GitLab MR notes and explicitly behind the default-OFF flag, so the
  operator view never reports feedback as wired for a platform/profile that did not pass
  the trace. Unbinding the verbs in the gateway breaks the manifest row (doctor fails),
  never a silent over-claim.

Still disclosed (unchanged):

- **The discussions surface.** The fake native server has no `/discussions` route; the
  auxiliary discussions reads degrade exactly as designed (a 404 marks the surface down
  for the process, logged, never a silent guess). FI-4 pins the distinction end to end: a
  transient failure (404-surface-down or a 5xx, which fails the step for retry) is NEVER
  recorded as a deletion; a confirmed deletion (the readable list without the id) is the
  typed `deleted_discussion` refusal. The resolution accounting and the readiness gate
  are proven at the service level (`tests/test_review_feedback.py`), where the fake
  serves the surface.
- **The revision world.** As in #321: the live GitLab planner does not emit plan revisions —
  revision 1 is staged through the app's own durable shape in the traces; the staging, the
  approval and the activation are fully native.
- **Platform scope.** ONE native platform is qualified (GitLab MR notes). GitHub and
  Azure DevOps do not parse the verbs — parity deliberately unclaimed (#337 out of
  scope) until one native path is qualified.

## 9. Observability (the backlog's names)

- `feedback.ingress_received` — a structured log line at the GitLab ingress for every
  parsed feedback note (note id, project, MR, delivery uuid — R40-01).
- `feedback.duplicate_delivery` — the dedup answers, naming the LAYER: `transport`
  (the delivery uuid, exact network replays) or `logical` (the note identity — manual
  redeliveries and worker-restart replays).
- `feedback.refusal_reason` — the typed ingress refusal (`malformed_feedback_command`);
  the service-side typed refusals (`refused_unauthorized`, `deleted_discussion`,
  `stale_head`, …) are the request's durable status + their existing log lines.
- `feedback.request_created` / `feedback.dispatch_count` — the durable surface already
  records them: the `review_feedback.request` outbox row carries every status change with
  its payload (creation → staging → dispatch), and the correction dispatch is the run's
  `commit_cycle` bump + the native dispatch ledger entry.
- `feedback.queue_age` — derivable from the persisted step (`due_at` at ingress) against
  the claim time; the step IS the queue (ADR-0017 — Postgres owns the work).
- `review_feedback.resolution_time` — derivable from the request's `created_at` and the
  lifecycle transitions (the outbox row `review_feedback.request` carries every status
  change with its payload).
- `review_feedback.stale_head` — the typed conflict: the request status `stale_head` + the
  outbox transition row (and the log line at the refusal).
- `delivery.review_rounds` — the correction cycle's `commit_cycle` bump (the run's own
  durable counter; each round is one request → approval → re-dispatch → re-review).
