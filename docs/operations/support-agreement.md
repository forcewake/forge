# Support agreement — the supported profile (Q39-15 / #334)

The supported profile is `supported-gitlab-ce-v1` (the frozen composition,
manifest digest `0a49ffe7…`, see the [supported-profile runbook](
supported-profile-runbook.md) and the [profile records](
../releases/profile-records.md)). This document states what operating
limits are MEASURED on that profile, who owns the response to each failure
domain, which failure domains are EXCLUDED, and which items are
**pending-human** — stated as pending, never as done.

Every number below names its evidence class: **live** (measured on the
actual deployment, `qualification/deployment-ops-2026-09-25.json`, the 9/9
deployment-ops drills of 2026-09-25 and the #326 supported-composition v2
trace), **code** (pinned by `tests/test_ops_limits.py` on disposable
infrastructure — no network, no lab containers), or **pending-human**.

## The measured limits (and where each number comes from)

| Limit | Value | Class | Evidence |
| --- | --- | --- | --- |
| Concurrent native execution per project | 3 slots; peak occupied 3 with REAL native jobs; a 4th dispatch parks `blocked(execution_capacity)` — never overbooks | live | `deployment_remote_occupancy` + `deployment_lost_response_at_cap` (9/9 drills) |
| Control command latency under contention | received→applied 9.684 s in the slow-ACK window; pause/cancel p95 0.0288 s (control) / 0.0521 s (cancel under a slow provider), n=12 | live | `measured_limits.command_latency_under_contention`, `deployment_pause_cancel_percentiles` |
| Backup / restore at the current store size | backup 0.211 s, restore 0.177 s — remeasure as the store grows; the number does not extrapolate | live | `deployment_backup_restore` |
| Reviewer WIP bound | 5 concurrent reviewable WIP (a STATED POLICY FIELD, not a measurement); the admission bound (3) stays within it | live | `reviewer_wip` block, same record |
| Operator wait per accepted task | 347.05 s exact; setup effort 58.20 s exact; CI runtime lower bound 254.70 s on real captures | live | #330 delivery economics, `evaluation/economics/accepted-task-ledger-v1.json` |
| Reviewer-leg protection | the #325 closing reserve (FORGE_CLOSING_RESERVE_USD=0.50 under a 2.50 cap) covered the closing review on the v2 trace — verdict `ok`, run terminal `ready_for_human` | live | `closing-review-within-reserve`, `qualification/records/supported-composition-v2-2026-09-25.json` |

None of these generalize to larger fleets without re-measurement — each
drill's `tested_limits` carries the exact N, budgets and waits it tested.

## The operator surface (what a supported operator reads)

ONE authorized surface answers "is this queued / progressing / paused /
unverified / verified / blocked, and what is the next SAFE action":
`GET /operator/runs/{run_id}` (the operator read API, lane-control
credential family). Its `ops_limits` block carries the six quantities —
current attempt, native occupancy, exact checkpoint, unresolved effects,
required checks, accounting coverage — plus:

- the **four observability quantities, always separate records**:
  `ops.command_application_latency` (per command, received→applied),
  `ops.native_occupancy` (the lease gauge by occupancy state),
  `ops.recovery_duration` (pause applied → the matched checkpoint
  activation — the exact restore observed),
  `ops.manual_intervention_minutes` (WAIT-for-human windows only; active
  human working time is recorded nowhere and is never invented). They are
  never summed, averaged or divided into one another; an unmatched window
  is counted, never zero-filled;
- the **admission accounting**: intake counters (refused requests /
  admitted-to-queue) and execution counters (slots held / draining /
  completed) side by side, with the capped-admission verdict derived from
  EXECUTION OCCUPANCY ONLY — the intake count is never a claim about
  execution slots (pinned: a forged intake-derived verdict raises);
- the **review-budget distinction**: a FAILED REVIEW BUDGET renders as
  exactly that — never lost code, never failed independent checks — with
  the #325 review-only continuation and the auditable top-up named beside
  it;
- **history separation**: current fields derive from the LATEST row of
  each section; an applied resume that named a different checkpoint never
  activates the current one (`activation: unmatched-command`), and every
  historical sample carries its own `from`/`to` moments.

Repositories outside the operator token's canonical-subject scope never
enter any of it: out-of-scope is a 404 indistinguishable from unknown, on
the list, the detail and the bundle paths (pinned in code).

## The recovery drills (code-proven, disposable infrastructure)

Pinned in `tests/test_ops_limits.py`, each driving the SHIPPED path on
disposable SQLite/tmp-dir stores — zero network, zero lab containers:

- **Slow runner at the cap** — three slots held under realistic start
  latency; the fourth dispatch parks; releasing ONE slot from evidence
  flips the capped verdict while the queued intake count GREW.
- **Vendor 429 on a start** — ambiguous acceptance: occupancy stays
  `dispatched_unknown`, the slot stays held until a terminal probe
  observation (a definite 404 refusal, by contrast, returns capacity
  immediately — AT-06).
- **Control-plane restart** — engine disposed and recreated over the same
  durable store: occupancy survives, an unknown native start parks
  draining, and the reconciler resolves it by its durable
  `native_intent_ref` through the fresh engine.
- **Unknown native start** — intent without handle parks draining; the
  local terminal verdict never frees the slot by itself.
- **Concurrent operator commands** — one logical command per sequence
  race; exactly the command matching the CTL-04 world dispatches, the
  stale twin EXPIRES with the reason; a redelivered duplicate is adopted,
  never duplicated.
- **Retention under a paused active checkpoint + an investigation hold** —
  the pinned checkpoint and its blobs survive the retention pass; a
  non-terminal run holds its credential receipts; an investigation hold
  holds them regardless of status; only unheld terminal work prunes.
- **Out-of-scope private bundle request** — a principal whose token does
  not scope the repository gets 404 on the detail AND the bundle, and the
  run never appears in their list.

## Response ownership

| Failure domain | Owner | Basis |
| --- | --- | --- |
| forge control plane (forge-app containers, the durable database, the CAS volume) | the forge operator (this repo's runbooks; [deployment boundaries](deployment-boundaries.md)) | restart/reconcile drills are code-proven; backup/restore is live-measured |
| The supported GitLab CE instance and its runner (the lab) | the lab owner (home infra operator) | the 9/9 live drills ran against it; degraded-native behavior (blocked classification, no lost code) is code-proven |
| Model route / vendor throttling (LiteLLM, the model vendors) | the forge operator tunes the revival ladder; vendor-side 429/529 windows are the VENDOR's domain | `deployment_degraded_modes` (live, the fake-429 lane through the app's own revival budget); the 429 occupancy semantics are code-proven |
| Reviewer-leg budget decisions | the requesting human + the operator (top-up is an audited command, never a silent re-plan) | #325 closing budget; the v2 trace's closing review passed within the reserve |

## Excluded failure domains (honest, with today's worked example)

EXCLUDED from this agreement — the forge operator does not own them and
the supported profile's guarantees do not cover them:

- **The home network, NAT and DNS path in front of the lab** (dynamic-DNS
  providers, port forwarding, TLS termination on consumer infrastructure).
  Worked example, recorded as a measured ops event: on 2026-09-25 the lab
  GitLab became unreachable ~10:30Z (TLS resets; the last live contact of
  the 9/9 drills was their 10:20:04Z completion). The in-flight work
  degraded gracefully — blocked classification, no lost code, ZERO paid
  dispatches burned on the dead native. Recovery came ~11:25Z by OPERATOR
  ACTION (the human restored the network — intervention, not self-healing):
  a ≈55-minute outage window whose duration was bounded by human response,
  which is itself the worked example for `ops.manual_intervention_minutes`.
  Detection→safe-state timing is NOT derivable from committed artifacts —
  stated as unmeasured, never estimated. Ops observation, same class:
  `gitlab.forcewake.me` is NXDOMAIN (a Cloudflare zone record gap); the
  lab's canonical URL is the duckdns hostname.
- **The customer's own repositories' CI beyond the contract** (runner
  fleets we do not operate, vendor-side quota of third-party actions).
- **Physically lost durable state without backups** — the agreement covers
  recovery THROUGH the backup/restore path (live-measured at the current
  store size), not recovery from nothing.

## Pending-human (stated as pending, never as done)

1. **Second-operator repetition of the recovery exercise.** The acceptance
   criterion asks that a SECOND operator repeats the recovery drill; the
   drills here were driven by one author. Pending a second operator's run
   (a comment on #334 with the drill output suffices as evidence).
2. **Dumps-history cleanup: maintainer approval.** The plan exists at
   `~/forge-private/backups/2026-09-24/HISTORY-CLEANUP-PLAN.md` (outside
   the repository, access-controlled) and is NOT executed. Executing it
   needs explicit maintainer approval; until then the historical dumps are
   retained untouched.

Both items are honest gaps: the code, the drills and the live evidence
above stand without them, and nothing in this document quietly claims
them as done.
