# The operator view (R32-23) — attempts, decisions and recovery

This document covers the persistent operator view: the versioned
projection over a run's durable rows (`forge.adaptive.operator_view`), the
state vocabulary and what proves each state, the replay protection on
updates, the recovery-action validity matrix, the exportable support
bundle (`forge.adaptive.support_bundle`), and — since R36-15 — the
authorized snapshot reader (`forge.adaptive.operator_snapshot`) and the
authenticated read API (`forge.api_operator`) that surface them live.

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
  shas, the checkpoint id and digest, the plan digest, the attempt id and
  the generation — never "the latest build".

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
| `verified_ready` | Independent verification passed for the candidate | A verification row with result `passed` bound to the CURRENT candidate (its `candidate_sha` is one of the run's `candidate_shas`; a passed row naming no candidate binds to whatever the run holds) |
| `wedged` | Executing but no SEMANTIC transition within the threshold — the looked-launched-but-stalled failure | Latest attempt `executing` + the newest transition timestamp (timeline rows, attempt updates, checkpoints, verifications) older than the threshold (default 30 minutes, a parameter of `derive_state`) |
| `stale` | The stored projection predates the source rows | `stored.source_digest` ≠ the current rows' digest |
| `dead` | Terminal-failed/cancelled with external effects never reconciled | A failed/cancelled terminal + publication intents in `requested`/`dispatched`/`probing`/`unknown` |
| `accepted` | The result was accepted | An acceptance marker in the run's evidence, or an attempt outcome `accepted` |
| `rejected` | The run failed | The run row at `failed`, or an attempt outcome `failed` |
| `cancelled` | The run was cancelled | `cancel_requested`, the run row at `cancelled`, or an attempt outcome `cancelled` |

Two distinctions the ladder enforces on purpose:

- **A resume request is not a restoration.** `/resume` recorded — even
  drained and `applied` — stays `safely_paused` until the checkpoint row
  carries an activation. The displayed state never gets ahead of the
  bytes.
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

## 8. The authorized snapshot reader (R36-15)

`OperatorSnapshotReader` (`forge.adaptive.operator_snapshot`) is the ONE
subject-scoped async reader that assembles the projection inputs from
the durable authorities: the run row (`FlowRun`, with `status_reason` as
`blocked_reason`), the revival-attempt records (`ActionLog` rows with a
`retryability`), the control commands and deliveries (`control_commands`
/ `control_command_deliveries`), the ACTIVE checkpoint (through the
injected `CheckpointRepository`), the pause fence (`pause_fences`), the
verification verdict (`evidence["verification"]`, the unified R02 shape),
the publication intents, the gate approvals and the admission leases.

Two rules pin its contract:

- **Subject-scope enforcement.** The reader takes an authorized subject
  scope (repo full names) and every run query filters by it; a run
  outside the scope is `None` — indistinguishable from unknown — on the
  list, detail and support-bundle paths.
- **Coverage honesty.** The snapshot carries `source_coverage` per
  section (`present | missing | unknown`) and `projection_age` (how old
  the newest observed row is). A source never queried is `unknown` —
  `questions` (no durable authority yet), checkpoints without an
  injected repository, or any section whose authority was unreachable
  (a checkpoint outage is `unknown`, never "no checkpoint"). An observed
  empty section is `missing`. Neither is ever filled or assumed.

The derived-state bindings the reader feeds (and tests pin): a `passed`
verification decorates only the candidate it tested (`tested_oid` must
be a current `candidate_shas` entry — an old green verdict about another
candidate reads `unverified`); `safely_paused` stands on the
repository's real ACTIVE entry plus the durable fence word; a resume's
activation proof is the resume command that reached
`applied`/`checkpointed`.

## 9. The read API (R36-15)

`forge.api_operator` mounts three GET routes, read-only by construction
(zero write endpoints; rendering performs no provider writes, no model
calls, no state transitions):

| Route | Returns |
|---|---|
| `GET /operator/runs?repo=owner/a&repo=owner/b` | Thin summaries per run: state, underlying state, blocked/waiting line, `updated_at`, `projection_age_seconds`, unresolved-effect count |
| `GET /operator/runs/{run_id}?repo=owner/a` | The full `render()` document plus `subject`, `source_coverage`, `projection_age_seconds`, `occupancy` (the admission/lease slice) and `actions` — the ADVISORY hints |
| `GET /operator/runs/{run_id}/support-bundle?repo=owner/a` | The `forge.support.bundle/1` document (coverage, digest, every attempt — failed included), redaction on |

**Authentication** is the lane-control credential family, reused: the
same `HMAC-SHA256` under `FORGE_LANE_CONTROL_SECRET`, presented as a
bearer token, where the signed material is the SUBJECT SCOPE —
`operator:` + the declared repo full names, comma-joined sorted
(`operator_scope_token(secret, repos)` mints it). The caller declares
the scope it asks about (exactly as a lane declares its `work_id`) and
the server verifies the token signs exactly that scope. Fail-closed
ladder: no secret / no session factory → **503**; no bearer → **401**;
blank declared scope → **400** (no wildcard — least authority); token
that does not sign the declared scope → **403**; a run outside the
verified scope → **404**, indistinguishable from unknown.

**Actions are advisory.** The API renders hints for the read-only
`observer` role (probe only). Execution goes through the EXISTING
guarded command routes — the command router's authenticated ingress, the
lane-control ack surface — which revalidate authority and the current
world: a stale action replayed after the rows moved is refused there
(the lane ack's CTL-04 CAS expires commands written against an old plan
revision / execution epoch; `RecoveryActions.decide` names the current
state and the safe next action).

The composition may inject the checkpoint authority by setting
`app.state.operator_checkpoint_repository` to a `CheckpointRepository`;
without it the checkpoints section reads `unknown` — never an invented
empty history.
