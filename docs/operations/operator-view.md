# Read-only operator API

These routes inspect durable state; they perform no model calls, provider
writes or lifecycle transitions. Reading a hint does not authorize it.

| Route | Result |
| --- | --- |
| `GET /operator/runs?subject=<provider>/<connection>/<native-id>&limit=&cursor=` | Scoped summaries, state/reason, age, consistency and unresolved-effect count |
| `GET /operator/runs/{run_id}?subject=…&limit=&sections=` | Projection, source coverage, occupancy, checkpoint, candidate/verification, recovery, lineage, command progress and advisory actions |

## Scope and authentication

A bearer signs the exact canonical subject set using
`operator_subject_scope_token(secret, subjects)` under
`FORGE_LANE_CONTROL_SECRET`. Provider, connection and native identity all
matter; a display name grants no authority. This operator credential is
separate from lane v1 OIDC sessions and cannot authorize lane mutations.
Legacy name grants work only when each configured name resolves to exactly
one canonical subject; unknown or ambiguous names refuse and need reissue.

No secret or session factory returns 503; no bearer 401; malformed, absent
or mixed scope declarations 400; incorrect scope signature or unresolved
legacy grant 403. Out-of-scope and unknown runs both return 404. Cursors
are scope-bound. There is no wildcard scope.

## Bounds and honest coverage

List pages default to 50 and cap at 200. Detail history windows default
to 20 and cap at 100. Pending commands and unresolved intents have a hard
200-row bound; checkpoint reads expose the active identity, never artifact
bytes. Each section reports total, returned, truncation and applied limit.
`sections` selects `run,attempts,commands,deliveries,checkpoints,
verifications,publications,approvals,occupancy`; unselected authorities are
not queried and render `unknown`. Invalid limits refuse with 422, invalid
section selections with 400. Missing checkpoint authority is `unknown`,
not an empty history. Credential redaction applies recursively.

State is derived from source rows. An approval is not execution; pause
requested is not safely paused; resume accepted is not checkpoint activated;
green historical verification is not verification of the current candidate.
`projection_age_seconds`, `source_version`, `projection_inconsistent` and
`source_coverage` expose stale or incomplete reads. Refresh an inconsistent
view before acting. An unavailable authority never becomes a positive fact.

## Recovery

| Evidence | Safe action |
| --- | --- |
| `revoked_authority` | Rotate or rebind credentials; retry cannot restore authority |
| `uncertain_native_effect` | Guarded `/reconcile`; determine whether the effect landed before retry |
| `required_checkpoint_loss` | Restore the exact checkpoint or retire the run; never silently restart |
| `capacity_wait` | Wait for native observation; unknown/draining occupancy holds capacity |
| `verification_stale` | Verify the current candidate SHA |
| `empty_diff_no_effect` | No useful delivery is proven; inspect the driver and approved guidance |

Recovery distinguishes pause requested, checkpoint committed, runner
observed stopped, resume authorized and exact resume applied. Acceptance,
current candidate, review round and verification remain separate fields.
Unknown native occupancy is never stopped; stale-head correction preserves
human commits and needs a new correction against the current head.

Commands distinguish received, durably accepted, dispatched, applied,
checkpointed, refused and unknown. Feedback outcomes distinguish accepted,
pending, applied, refused and exhausted. A provider outage with bounded
backoff is pending; exhausted observation needs an operator decision.
Unresolved discussions require human resolution; writing code does not
resolve them. Budget exhaustion needs an explicit amendment of its limiting
axis. An acknowledgement is neither execution nor delivery.

Actions name the full run and round identity. Guarded command submission
rechecks projection version, candidate SHA, round reference/status and
command progress; stale advice refuses rather than applying to a new world.
Only observed evidence releases native occupancy. Admission exposes actual
counters when a policy is mounted; missing capacity authority is `null`.
The optional limits grades and support-bundle exporter are removed.

Use [commands](operator-commands.md), [backup/restore](backup-restore.md),
[token rotation](token-rotation.md) and [closing budget](closing-budget.md).
The longer historical projection narrative remains at original path
`docs/operations/operator-view.md` in the [archive](../archive.md).
