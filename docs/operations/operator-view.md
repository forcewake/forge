# The operator view (R32-23) — attempts, decisions and recovery

This document covers the persistent operator view: the versioned
projection over a run's durable rows (`forge.adaptive.operator_view`), the
state vocabulary and what proves each state, the replay protection on
updates, the recovery-action validity matrix, the exportable support
bundle (`forge.adaptive.support_bundle`), the authorized snapshot reader
(`forge.adaptive.operator_snapshot`) and the authenticated read API
(`forge.api_operator`) that surface them live — including the R37-02
canonical repository subjects that scope every operator read, the
R37-03 current-state projection rules (exact activation receipts,
per-attempt generations, current-candidate binding, the source-version
fence), the R37-16 bounded operator experience (bounded drill-down,
typed blocked-reason diagnostics, pending-command/occupancy surfaces,
bounded bundle export, time-to-diagnose observability) and the R38-15
recovery surface (the delivery outcome as a first-class field, the
five-milestone pause/resume ladder, advisory recovery hints and the
bounded, allowlisted, backup-free export diagnostics).

For the control commands themselves, see
[operator-commands.md](operator-commands.md) and
[adaptive-runbook.md](adaptive-runbook.md). The view does not add
commands — it LINKS to the ones those documents already define.

## 1. The read-only charter

The operator view is **read-only by charter**. It never launches agents,
edits code, merges PRs, mutates claims or writes rows. Every control it
offers routes through the existing guarded path — the command router's
authenticated ingress (`/pause`, `/resume`, `/steer`, `/answer`) or the
classic operator commands (`/retry`, `/cancel`, `/reconcile`) — and the
view only *names* that path (`via`) with the ticket the path will check.
The fast path is the read; the safe path is the write; they never merge.

## 2. The projection contract

`OperatorProjection` (`forge.operator.view/1`) is a **derived, versioned
value** over one run's durable rows — run, attempts, control commands and
deliveries, checkpoints, verifications, publication intents, approvals,
questions. It is not a second truth store:

- **It composes, it does not fork.** Command rows classify through
  `operator_timeline.timeline_from_journal`; the blocked/waiting/summary
  line is `ops.status_projection` verbatim — the SAME summary semantics
  the classic `/status` renders; the credential guard is audit_export's
  `_redact`, imported, not copied.
- **Freshness belongs in the projection.** Every projection carries
  `source_digest` (a digest over every row it was derived from),
  `computed_at`, and a per-source `rows_observed` freshness map — a
  source this projection never saw says so, rather than reading as
  empty.
- **Evidence is thin.** Each state carries `evidence` links of the shape
  `{of, id, ref}` — what the row is, which row, its digest. The link
  proves the evidence EXISTS and where to drill in; it never inlines
  payloads, prompts or logs.
- **Identity is exact.** `render()` shows the source sha, the candidate
  shas, the CURRENT candidate (`identity.active_candidate` — the run
  row's `active_candidate_sha` pointer when recorded, else the LAST
  `candidate_shas` member, R37-03), the checkpoint id and digest, the
  plan digest, the attempt id and the generation — never "the latest
  build". A `verification_history` section carries older green verdicts
  as `historical_pass` entries: visible as history, never as current
  readiness.

## 3. The state vocabulary

One state per projection, from a closed vocabulary. Health states
(`wedged`, `stale`, `dead`) overlay the semantic state — the projection
carries both (`state` is what the console shows, `underlying_state` is
what it overlaid).

| State | Meaning | The row that proves it |
|---|---|---|
| `requested` | The run exists; nothing more is proven | The run row alone |
| `authorized` | A human decision exists; nothing is executing | A gate-approval row, or a control command at `authorized` |
| `executing` | An attempt is actively working | The latest attempt at `executing`/`running` |
| `pause_pending` | A pause was COMMANDED; the checkpoint is not yet committed | The latest pause command at any rung `received`…`applied` |
| `safely_paused` | A checkpoint is committed and the fence is held | A committed checkpoint row (+ fence word `held`, or a checkpointed pause — the router raises the fence with the booking) |
| `resumed` | A resume was accepted AND the checkpoint bytes were ACTIVATED | A resume command (not refused) + a checkpoint row with an activation time |
| `unverified` | A candidate exists; independent verification has not passed | The run's `candidate_shas` with no `passed` verification row |
| `verified_ready` | Independent verification passed for the candidate | A verification row with result `passed` bound to the CURRENT candidate (its `candidate_sha` equals the active pointer or the last `candidate_shas` member — a pass naming a NON-current member is a `historical_pass`, and a row naming no candidate binds to whatever the run holds) |
| `wedged` | Executing but no SEMANTIC transition within the threshold — the looked-launched-but-stalled failure | Latest attempt `executing` + the newest transition timestamp (timeline rows, attempt updates, checkpoints, verifications) older than the threshold (default 30 minutes, a parameter of `derive_state`) |
| `stale` | The stored projection predates the source rows | `stored.source_digest` ≠ the current rows' digest |
| `dead` | Terminal-failed/cancelled with external effects never reconciled | A failed/cancelled terminal + publication intents in `requested`/`dispatched`/`probing`/`unknown` |
| `accepted` | The result was accepted | An acceptance marker in the run's evidence, or an attempt outcome `accepted` |
| `rejected` | The run failed | The run row at `failed`, or an attempt outcome `failed` |
| `cancelled` | The run was cancelled | `cancel_requested`, the run row at `cancelled`, or an attempt outcome `cancelled` |

Two distinctions the ladder enforces on purpose:

- **A resume request is not a restoration.** `/resume` recorded — even
  drained and `applied` — stays `safely_paused` until a checkpoint row
  carries an activation receipt: an applied resume whose recorded
  `checkpoint_ref` names THAT checkpoint (R37-03). An applied ACK that
  named a different checkpoint — or recorded no reference at all — is
  `activation: "unmatched-command"` with `activated_at: None`; command
  application alone is never filesystem-restoration proof.
- **A succeeded attempt is not an acceptance.** A succeeded attempt
  yields a candidate (`unverified`/`verified_ready`); acceptance is a
  decision, and only a decision row proves `accepted`.

Order of precedence (checked top-down): terminal → pause_pending →
safely_paused → resumed → executing/wedged → verified_ready → unverified
→ authorized → requested. Control outranks progress: a commanded pause
is the headline even when a verified candidate already exists.

## 4. Update semantics — CAS and replay protection

`apply_update(current, source_rows, expected_version)` is the only way a
projection version advances, and it only ever mints `current + 1`:

- **Delayed replay refused.** An update whose `expected_version` is
  OLDER than the stored projection was computed against rows the stored
  version has already superseded. Applying it would overwrite newer
  truth with older rows — exactly the delayed/replayed-update failure —
  so it raises `StaleProjectionRejected`, which names the current state
  and the safe next action. The newer projection is kept untouched.
- **Versions are monotonic.** An `expected_version` AHEAD of the stored
  projection claims a future truth; that is a caller bug, not a race,
  and fails loudly (`ValueError`). Versions are never re-used and never
  roll back.
- **Projections never mix runs.** An update whose run row names a
  different run than the stored projection is refused.

A reader holding an older projection can also *derive* the staleness
without updating: `derive_state(rows, now, stored=older)` reports
`stale` (with the underlying state preserved) whenever the rows have
moved past what `older` was computed from.

## 5. Recovery actions — the validity matrix

`RecoveryActions.valid_for(state, actor_role)` is the closed matrix. The
pinned constraints: **resume only in `safely_paused`** (a resume stands
on a confirmed checkpoint); **steer only while `executing`**; **retry
only in the failed terminals**; **a stale view allows only the probe** —
refresh before acting.

| State | Approver may | Read-only roles may |
|---|---|---|
| `requested` | cancel, probe | probe |
| `authorized` | pause, cancel, probe | probe |
| `executing` | pause, steer, cancel, probe | probe |
| `pause_pending` | cancel, probe | probe |
| `safely_paused` | resume, cancel, probe | probe |
| `resumed` | pause, cancel, probe | probe |
| `unverified` | cancel, probe | probe |
| `verified_ready` | probe | probe |
| `wedged` | pause, cancel, probe | probe |
| `stale` | probe (only — refresh first) | probe |
| `dead` | retry, reconcile, probe | probe |
| `accepted` | — (done is done) | — |
| `rejected` | retry, probe | probe |
| `cancelled` | probe | probe |

Actor roles: `approver` (the configured approver set — the SAME authority
as `/go` and the command router), `observer`, `automation` (read-only
roles may only probe). Unknown states and roles fail visibly.

Every offered action (`RecoveryActions.plan`) carries the **audit four
facts** — who (the actor), what exactly (`action_digest`: the candidate
sha, else the checkpoint digest, else the plan digest — never "the
latest"), when (a server-side timestamp), why/linkage (the run and the
projection version the decision traces to) — plus its guarded route
(`via`) and the **CAS ticket**: the `expected_version` it must still
match at execution time.

`RecoveryActions.decide(action, current)` checks a command against the
CURRENT projection. A command computed against an old status comment
(`expected_version` older than current) is refused **with the current
state and the safe next action named** — the operator re-decides against
the world as it now is, never against the comment they are staring at.
The same refusal answers a version ahead of the stored truth and an
action whose state has since changed (or whose role never allowed it).

## 6. The support bundle

`SupportBundle.build(run_id, rows)` (`forge.support.bundle/1`) is the
exportable evidence pack for handoff and audit:

- **attempts** — ALL attempts, failed ones included; recovery never
  resets history. An outcome forge cannot prove exports as `unknown`,
  never a guess.
- **commands** and **deliveries** — the control history with actors and
  ladder statuses.
- **checkpoints** — ids and digests only; the bundle points at the
  content-addressed store, it never carries workspace blobs.
- **verifications** and **publications** — the independent-check records
  and the publication intents (unknown stays unknown).
- **projection** — the `render()` document the bundle was built under.
- **coverage** — every section is marked `present | missing | unknown`:
  a section the collector never looked at is `unknown`, an observed
  empty one is `missing`; missing data stays EXPLICIT, never filled or
  assumed.
- **digest + timestamp** — the digest covers the CONTENT (sections,
  projection state, source digest) and never the generation timestamp,
  so the same rows always digest identically whatever clock built the
  bundle.

**Redaction policy** (both the projection and the bundle): identity and
evidence are ids and digests by construction — no payloads, no prompts —
and the finished document passes the audit-export credential guard
regardless: token/secret/password/key-looking VALUES are replaced with
`[redacted]` before an operator or an export ever sees them. See
[audit-retention.md](audit-retention.md) for the retention side.

## 7. One state, every surface

The projection is derived from the SAME durable rows every other surface
reads, by the SAME helpers where they exist (`timeline_from_journal`,
`status_projection`, `_redact`). A native comment reply (`/status`), a
management API response and this view can therefore never disagree about
what a run is doing — they disagree only about which slice they render.
When the rows move, every surface moves; when they have not, every
surface says so (`rows_observed`, `stale`).

`status_note_lines(projection)` is the parity hook for the native side:
the compact lines a `/status` reply renders (state, short identities,
blocked/waiting, unresolved effects, last transition) are derived from
the SAME projection the API renders — a test pins that both surfaces
agree on state and identity for the same snapshot.

## 8. The authorized snapshot reader (R36-15, R37-02/R37-03)

`OperatorSnapshotReader` (`forge.adaptive.operator_snapshot`) is the ONE
subject-scoped async reader that assembles the projection inputs from
the durable authorities: the run row (`FlowRun`, with `status_reason` as
`blocked_reason`), the INITIAL execution synthesized from the run row
plus the revival-attempt records (`ActionLog` rows with a
`retryability`), the control commands and deliveries (`control_commands`
/ `control_command_deliveries`, a resume carrying its recorded
`checkpoint_ref`), the ACTIVE checkpoint (through the injected
`CheckpointRepository`), the pause fence (`pause_fences`), the
verification verdict (`evidence["verification"]`, the unified R02
shape), the publication intents, the gate approvals and the admission
leases.

Four rules pin its contract:

- **Canonical-subject scoping (R37-02).** The reader takes an authorized
  scope of `CanonicalSubject` values — provider family + connection
  identity + native repository identity, derived from each family's OWN
  columns (GitHub rows by full name, GitLab/Azure rows by `project_id`;
  the connection from the run's recorded `evidence["connection"]`, else
  the single-connection default `-`). Display names are
  presentation-only: the same name on two connections or across families
  is two different subjects, and a grant for one never selects the
  other's runs. Listing runs through per-provider SQL predicates plus
  the exact canonical match BEFORE any per-run checkpoint/artifact read,
  in bounded pages (`limit` + a scope-bound cursor; default 50, max
  200). A run outside the scope is `None` — indistinguishable from
  unknown — on the list, detail and support-bundle paths.
- **Coverage honesty.** The snapshot carries `source_coverage` per
  section (`present | missing | unknown`) and `projection_age` (how old
  the newest observed row is). A source never queried is `unknown` —
  `questions` (no durable authority yet), checkpoints without an
  injected repository, or any section whose authority was unreachable
  (a checkpoint outage is `unknown`, never "no checkpoint"). An observed
  empty section is `missing`. Neither is ever filled or assumed. The
  `attempts` mark describes the revival-record authority; the initial
  execution row is derived from the run row (whose coverage is `run`).
- **History is never relabelled (R37-03).** Each revival attempt reads
  ITS OWN generation from its record (`remote_result`), the initial
  execution reads `generation: "unknown"` when no creation-time
  generation is recorded — never the run's CURRENT
  `cancellation_generation` copied onto history. A checkpoint's
  `activated_at` attaches only when an applied resume's recorded
  `checkpoint_ref` names THAT checkpoint; otherwise
  `activation: "unmatched-command"` with `activated_at: None`.
- **A consistent read or an honest stale mark.** Every SQL section of
  one run's assembly runs inside ONE session (a single snapshot on
  sqlite), fenced by a `source_version` — the per-authority max row
  versions observed before and after the sections. When the fence moved
  (a repair committed mid-assembly under READ COMMITTED), the snapshot
  is `projection_inconsistent` and every surface renders that flag
  instead of presenting a confident state assembled from mixed
  versions. The rendered projection is EPHEMERAL — a fresh version-1
  value over rows just read, never a durable CAS ticket; guarded actions
  re-read authoritative state.

The derived-state bindings the reader feeds (and tests pin): a `passed`
verification decorates only the CURRENT candidate (`tested_oid` equals
the active pointer or the last `candidate_shas` member — an old green
verdict about an earlier member renders `historical_pass` and reads
`unverified`; a verdict about something outside the run's candidates
decorates nothing); `safely_paused` stands on the repository's real
ACTIVE entry plus the durable fence word; a resume's activation proof
is the applied resume command that named THAT checkpoint.

## 9. The read API (R36-15, R37-02/R37-03)

`forge.api_operator` mounts three GET routes, read-only by construction
(zero write endpoints; rendering performs no provider writes, no model
calls, no state transitions):

| Route | Returns |
|---|---|
| `GET /operator/runs?subject=<family>/<connection>/<native>&limit=&cursor=` | A bounded page of thin summaries per run: state, underlying state, blocked/waiting line, `updated_at`, `projection_age_seconds`, `projection_inconsistent`, unresolved-effect count, plus the page bound and the scope-bound continuation cursor |
| `GET /operator/runs/{run_id}?subject=…&limit=&sections=` | The full `render()` document plus `subject`, `subject_id`, `source_coverage`, `projection_age_seconds`, `occupancy` (the admission/lease slice), `source_version` / `projection_inconsistent` (the consistency fence), `recovery` (the R38-15 recovery section, §11), `actions` — the ADVISORY hints — and the R37-16 diagnostics sections: `sections` (the read bounds), `pending_commands`, `occupancy_summary` and `blocked_reasons` |
| `GET /operator/runs/{run_id}/support-bundle?subject=…&max_bytes=&sections=` | The `forge.support.bundle/1` document (coverage, digest, every attempt — failed included), redaction on, with the same `subject_id` and fence marks, plus the `export` block stating its scope and size (see §10) and the bounded `diagnostics` slice (§11.4) |

**Authentication** is the lane-control credential family, reused: the
same `HMAC-SHA256` under `FORGE_LANE_CONTROL_SECRET`, presented as a
bearer token. A **v2 grant** signs the CANONICAL SERIALIZED SUBJECT SET
— `operator-scope-v2:` + canonical JSON of the sorted subject ids
(`operator_subject_scope_token(secret, subjects)` mints it). The caller
declares the scope it asks about (exactly as a lane declares its
`work_id`) and the server verifies the token signs exactly that set.

**Legacy grants (v1, name-only)** — `operator_scope_token(secret,
repos)` — keep working ONLY through resolution: every declared name must
match EXACTLY ONE canonical subject among the CONFIGURED repositories
(`app.state.operator_subjects`, the deployment's mounted subject list);
an ambiguous name, an unknown name, or nothing configured fails closed
with a 403 carrying `operator.legacy_grant_refused` and the reissue
instruction (reissue as canonical subjects). Never a silent fan-out
across connections.

Fail-closed ladder: no secret / no session factory → **503**; no bearer
→ **401**; no declared scope, both grant families declared, a malformed
subject reference or a cursor from another scope → **400** (no wildcard —
least authority); a token that does not sign the declared scope, or a
legacy grant that cannot resolve → **403**; a run outside the verified
scope → **404**, indistinguishable from unknown. Observability:
`scope` / `scope_version` / `page_size` in the list document,
`subject_id` everywhere, `source_version` / `projection_inconsistent`
on detail and bundle, and the `operator.legacy_grant_refused` refusal
code.

**Actions are advisory.** The API renders hints for the read-only
`observer` role (probe only). Execution goes through the EXISTING
guarded command routes — the command router's authenticated ingress, the
lane-control ack surface — which revalidate authority and the current
world: a stale action replayed after the rows moved is refused there
(the lane ack's CTL-04 CAS expires commands written against an old plan
revision / execution epoch; `RecoveryActions.decide` names the current
state and the safe next action). An operator bearer is not a lane token
and cannot drive the guarded routes at all.

The composition may inject the checkpoint authority by setting
`app.state.operator_checkpoint_repository` to a `CheckpointRepository`;
without it the checkpoints section reads `unknown` — never an invented
empty history. Likewise the configured subject registry
(`app.state.operator_subjects`) is what legacy name resolution consults.

## 10. The bounded operator experience (R37-16)

The list already pages (R37-02); R37-16 bounds the DRILL-DOWN and the
EXPORT the same way, and adds the diagnostics an operator actually acts
on. Nothing here is a second mutation authority — execution stays in the
guarded command routes.

### Bounded drill-down (`?limit=` / `?sections=`)

The detail endpoint's per-run section reads carry explicit windows:

- **attempts** — the LAST N revival records (default 20, max 100) beside
  the synthesized initial execution, with the authority `total_count`;
- **commands** — the PENDING commands whole (up to a hard 200-row cap)
  plus the LAST N settled ones;
- **effects (publications)** — the UNRESOLVED intents whole (same hard
  cap) plus the LAST N resolved ones;
- **checkpoints** — exactly the ACTIVE entry the authority exposes
  (there is no unbounded checkpoint history read); `run` and
  `verifications` are single rows by construction; occupancy reads the
  open leases whole plus the LAST N released ones.

Every windowed section reports `total_count`, `returned`, `truncated`
and the served `limit` in the detail's `sections` block — a window is
never mistaken for the whole journal. `?sections=` (a comma-separated
subset of `run,attempts,commands,deliveries,checkpoints,verifications,
publications,approvals,occupancy`) bounds the QUERY itself: unselected
sections are never read and render coverage `unknown`. No per-run
artifact BYTES are ever read — checkpoints surface their
content-addressed digest only. Out-of-range `?limit=` values are
refused (422), and unknown or empty `?sections=` selections are refused
with 400.

### Typed blocked reasons (`blocked_reasons`)

`explain_blocked(projection)` renders why a run waits as a closed
vocabulary of outcome codes — each with a one-line explanation, a THIN
link to the exact proving row (its id/digest), and the SAFE next action
naming where it executes (a guarded route or a runbook):

| Code | Meaning | Evidence row | Safe next action | Retry? |
|---|---|---|---|---|
| `revoked_authority` | The credential/permission the run depends on was withdrawn (401/403, revoked/expired/suspended) | the run row | `rotate_or_rebind_credential` via [token-rotation.md](token-rotation.md) | **no** |
| `uncertain_native_effect` | An external effect's landing is unproven (`requested`/`dispatched`/`probing`/`unknown`) | the publication-intent row | `reconcile` via the guarded `/reconcile` | **no** |
| `required_checkpoint_loss` | The run stands paused (held fence) on a checkpoint the authority reads `missing`/`unknown` | the checkpoint row | `restore_checkpoint_or_retire` via [backup-restore.md](backup-restore.md) | **no** |
| `capacity_wait` | A lease holds capacity with UNCERTAIN occupancy (`dispatched_unknown`/`draining`), or admission worded a capacity wait | the lease row | `wait_for_reconciler` (probe) | yes |
| `verification_stale` | The only green verdict names an earlier candidate; the CURRENT one is unverified | the verification row | `verify_current_candidate` | yes |

The non-retryable codes NEVER suggest retry — a revoked credential
cannot be retried into validity, an uncertain effect cannot be retried
around, a lost checkpoint cannot be retried into existence (pinned by
tests). A healthy projection carries no reasons at all.

### Pending commands and native occupancy

The detail renders the actionable slices as their own sections:

- **`pending_commands`** — every command not yet spent (kind, age, the
  CTL-04 world it must still match: `expected_plan_revision` /
  `expected_execution_epoch`). A stale hint cannot execute after the
  world moves — the guarded route expires it.
- **`occupancy_summary`** — `occupied_vs_limit` (the open-lease count
  beside the deployment's `max_active_per_project`, mounted as
  `app.state.operator_admission_policy`; `null` when unmounted — an
  honest unknown), the per-occupancy-word counts, and the AGES of the
  leases whose occupancy is uncertain.

### Bounded support-bundle export

The bundle reads FULL history (evidence completeness — windows live on
the detail route) and is bounded at the EXPORT: the `export` block
states the scope, the byte size, the cap and the hard maximum
(`?max_bytes=`, default 1 MiB, hard max 8 MiB). A serialized bundle
over the cap is REFUSED with the typed `operator.bundle_too_large`
(413) naming the actual size — never silently truncated; narrow with
`?sections=` (unselected sections read coverage `unknown`) or raise the
cap within the hard maximum. Redaction is unchanged: no bearer tokens,
model keys or raw payloads (pinned by tests on every export shape).

### Time-to-diagnose observability

Every operator response carries two headers — `operator.query_duration`
(seconds) and `operator.page_payload_bytes` (the serialized body) — and
a matching structured log line. These are the numbers the pilot reads
to confirm list latency and payload stay bounded as history
accumulates; thousands-of-runs paging is pinned by tests (the
checkpoint authority is consulted once per PAGE MEMBER, never per run
in scope).

## 11. The recovery surface (R38-15)

The live single-writer run's exact pain: an empty resumed diff displayed
as a "successful resume" shape, and operators could not tell a requested
pause from a verified checkpoint, a stopped runner, an authorized resume
or an applied exact resume. The recovery surface (in
`forge.adaptive.operator_view`, rendered by the detail route's
`recovery` document, `forge.operator.recovery/1`) separates exactly
those facts. It is still read-only by charter: every hint names an
EXISTING guarded route; nothing here is a second authorization surface.

### 11.1 The delivery outcome — a first-class field

`delivery_outcome_of(rows)` derives what the CURRENT attempt actually
delivered from the markers already recorded — never from a successful
SDK turn:

| Outcome | Meaning | The evidence that derives it |
|---|---|---|
| `delivered` | A candidate was collected | `candidate_state=candidate` (#302 marker), or collected `candidate_shas` |
| `empty_diff_no_effect` | A zero-change candidate — a FAILED/no-effect delivery, never a successful resume | `candidate_state=zero_change`, or the run row's `repair_no_effect` / `harness_no_changes` |
| `collection_failed` | The collector failed; no candidate exists | `candidate_state=collection_failed`, or `harness_artifact_missing` / `harness_candidate_invalid` |
| `driver_failed` | The lane driver failed before a candidate existed | `candidate_state=driver_failed`, `driver_exit` ≠ `completed`, or `harness_driver_failed` |
| `not_collected_yet` | No delivery recorded — an honest unknown | nothing recorded |

The #302 finalization markers
(`FORGE_LANE_OUTCOME:{driver_exit, collector_exit, candidate_state}`)
land on the run's evidence (the harness fragment, or the top-level
spelling); the reader maps them onto the run row's `lane_outcome` slice
and the view derives from that. Priority: the recorded `candidate_state`
wins (the lane already reconciled driver and collector), then the driver
exit, then the run row's typed blocked reason, then collected
candidates, then the honest nothing-recorded.

**The display rule:** the three failure outcomes
(`empty_diff_no_effect`, `collection_failed`, `driver_failed`) render
`delivery.failed: true` with a headline that says FAILED — the state
ladder may honestly say `resumed` (the activation receipt holds), and
the recovery document says the resume delivered no changes. Never a
successful resume of useful work.

### 11.2 The five-milestone ladder

`recovery_ladder(rows, coverage, occupancy)` renders each milestone
independently — `present` (with its thin evidence link and moment),
`absent` (the authority was observed and holds no such row) or
`unknown` (never queried, or the authority was unreachable):

| Milestone | The row that proves it |
|---|---|
| `pause_requested` | the pause COMMAND row (any rung — the request itself is the milestone) |
| `checkpoint_committed` | the verified checkpoint (its id + digest) |
| `runner_stopped` | the native job's terminal observation — a RELEASED execution lease (the reconciler's probe, never a timer) |
| `resume_authorized` | the resume DECISION row — a resume command not refused on the ladder (`rejected`/`expired` was never authorized) |
| `exact_resume_applied` | the R37-03 activation receipt: an applied resume whose recorded `checkpoint_ref` named THIS checkpoint (`activation: "matched"`); an unmatched one activates nothing |

A checkpoint authority that is unavailable while the DB stays healthy
makes both checkpoint milestones `unknown` — never "no checkpoint". A
snapshot whose source fence moved (§8) renders the whole recovery
document with `consistency: "inconsistent"` plus an explicit uncertainty
note instead of a confident ladder assembled from mixed versions.

### 11.3 Recovery hints — advisory, guarded routes named

`recovery_hint(state, delivery_outcome, blocked_reason)` answers every
state × outcome with an advisory whose commands each carry the EXACT
command shape and the guarded route that executes it
(`/steer <run-id> <text>` via `command_router:/steer`,
`/resume <run-id>` via `command_router:/resume`, `/retry <run-id>` via
`operator-commands:/retry`, `/reconcile <run-id>` via
`operator-commands:/reconcile`, the probe via `read-only:/status`, the
credential rotation via `runbook:token-rotation`). The pinned rules:

- a revoked/stale authority (the R37-16/#297 non-retryable wording)
  outranks the delivery outcome — rotate or reconcile, `retryable:
  false`, and NEVER a retry verb;
- `empty_diff_no_effect` in `resumed` names BOTH escapes (re-issue the
  guidance, or restart from the verified checkpoint);
- `collection_failed` carries the collector's typed error and the rerun
  path; `driver_failed` the guarded rerun; `not_collected_yet` the probe;
- `None` only for the healthy ends (a delivered candidate that is
  verified or accepted);
- unknown states and outcomes fail visibly.

**Stale action hints.** The detail route's action block
(`action_hint_block`) marks every hint `stale: true` with the current
state's safe alternative (`probe`) when the snapshot's consistency fence
moved — the display half of the refusal the guarded routes already
enforce (CTL-04 CAS); the block also states why it is stale.

### 11.4 The bounded export diagnostics

The support-bundle export carries a `diagnostics` slice
(`forge.operator.diagnostics/1`), structurally bounded three ways:

- **SIZE** — at most 10 entries per list section (`truncated` names what
  was cut) on top of the export's byte cap;
- **ALLOWLIST** — every section serializes only its declared
  `DIAGNOSTIC_SECTION_FIELDS` (the audit-export `CREDENTIAL_DOCUMENT_FIELDS`
  pattern); anything else a source row carried is dropped;
- **BACKUP EXCLUSION** — raw operational backup names (the R38-03/#304
  world: `*.dump`, `*.pgdump`, `*.sql`, `backups/…`) are scrubbed from
  every string the slice carries, counted as
  `export.raw_backups_excluded`; a sanitized RECEIPT reference (a value
  naming a receipt) is referenced at most.

## 12. The operating-limits read-model (Q39-15)

The detail route's `ops_limits` block (`forge.ops.limits/1`, folded by
`forge.adaptive.ops_limits`) is the supported customer profile's one
look at the operating limits: the six quantities (current attempt,
native occupancy, exact checkpoint, unresolved effects, required
checks, accounting coverage), the customer state on the six-word
vocabulary (queued / progressing / paused / unverified / verified /
blocked — the `wedged`/`dead` overlays re-classify to blocked), the
four `ops.*` observability quantities as SEPARATE records
(`ops.command_application_latency`, `ops.native_occupancy`,
`ops.recovery_duration`, `ops.manual_intervention_minutes` — never
blended; an unmatched window is counted, never zero-filled; an
unselected section's measure reads `unknown`), the admission accounting
(intake counters and execution slots as different populations, the
capped verdict derived from occupancy only), and the review-budget
distinction (a FAILED REVIEW BUDGET renders as exactly that — never
lost code, never failed independent checks). The measured limits
themselves, response ownership and the excluded failure domains live in
the [support agreement](support-agreement.md).

## 13. Diagnosing the pilot failure cases

The runbook slice for the agreed pilot failure cases — each typed
blocked reason mapped to where the operator looks and the safe next
action. The pilot records actual time-to-diagnose and manual
escalations; that second-operator validation is the REMAINING step
noted here (API availability is not usability proof).

| The failure you see | Where to look first | Safe next action |
|---|---|---|
| A resume "succeeded" but nothing changed (`empty_diff_no_effect`) | Detail → `recovery.delivery` (outcome + headline: a FAILED/no-effect delivery, never a successful resume), `recovery.hint` | Re-issue the guidance (`/steer <run-id> <text>`) or restart from the verified checkpoint (`/resume <run-id>`) — both through the command router |
| A run waits with `revoked_authority` | Detail → `blocked_reasons[0].evidence` (the run row), `source_coverage` for which sections were even queried | Rotate/rebind the credential per [token-rotation.md](token-rotation.md), then re-probe — do NOT retry |
| A terminal run still holds external effects (`uncertain_native_effect`) | Detail → `unresolved_effects` (operation keys, target refs) | Run the guarded `/reconcile` to determine each landing before anything else |
| A paused run cannot resume (`required_checkpoint_loss`) | Detail → `source_coverage.checkpoints` (`missing`/`unknown`), the checkpoint identity | Restore from backup per [backup-restore.md](backup-restore.md) or retire the run — do NOT retry |
| Nothing progresses, capacity suspected (`capacity_wait`) | Detail → `occupancy_summary.unknown_ages` (the uncertain leases' ages) and `occupied_vs_limit` | Wait for the reconciler's probe; escalate only when the age grows past the agreed threshold |
| Green checks but the run reads `unverified` (`verification_stale`) | Detail → `identity.active_candidate` vs `verification_history` | Run independent verification for the CURRENT candidate |
| The view itself looks wrong | Detail → `projection_age_seconds`, `source_version`, `projection_inconsistent`, `rows_observed` | Refresh (probe); an inconsistent fence means a repair landed mid-read — re-read before acting |
| You need everything for handoff | Bundle → `export` block (scope + size), `coverage` map | Export whole, or narrow with `?sections=` when the byte cap refuses |

All of this is diagnosable from the supported surfaces without database
edits; the action verbs route through the guarded command routes and
runbooks named above.
