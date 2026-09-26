# Deployment boundaries — the supported topology, its limits and the operator runbook (R37-20, R38-18)

Issue #301 (external review `4af6b33`, item R37-20) declared what the
ACTUAL deployment supports; issue #319 (review `59ba869`, item R38-18)
bound every measurement to the FROZEN supported profile (#307) and added
the deployment-only failure arms. The executed evidence:

- `qualification/deployment-ops-2026-09-24.json` — the R37-20 run (the
  full report of that era, still public history);
- `qualification/deployment-ops-2026-09-25.json` — the R38-18 run: the
  **sanitized published summary** (schema `forge.deployment.ops.sanitized/1`).
  The full diagnostics report of that run (identifiers, per-cycle
  details, host paths) is retained OUTSIDE the repository
  (`~/.forge-private/deployment-ops/`, access-controlled) per the #304
  discipline — the public tree carries receipts, never operational
  payloads.

Both reports are produced by `scripts/run_deployment_ops.py` — read-only
probes plus explicitly disposable resources; no lab container is ever
started, stopped or recreated by a run.

## 0. The frozen-profile binding (R38-18 — what a measurement is evidence FOR)

The supported profile is FROZEN:
`qualification/profiles/supported-gitlab-ce-v1.json`, manifest digest
`7c292dd89bb9a1f8…` (self-vouching sha256 — a drifted file does not
vouch for itself and refuses the bind). Every drill in the 2026-09-25
run records that digest, and the runner OBSERVED the deployment
read-only against the manifest's `control_plane.executed_lab` bind
before any measurement:

| Bind axis | Manifest (executed-lab) | Observed at run time |
| --- | --- | --- |
| image name | `localhost/forge:dev` | `localhost/forge:dev` |
| image id | `58e0bd3e…b296` | `58e0bd3e…b296` |
| image digest | `sha256:20e9cdcd…bbce1` | `sha256:20e9cdcd…bbce1` |
| deployed schema head | `027` | `027` (read-only `alembic_version`) |
| reported version | `0.37.0` | `0.37.0` (`GET /health`) |

**A mismatch on ANY axis (or an axis the probes could not observe)
renders the whole run `unqualified-for-profile`** — the report names
every difference, the profile-bound arms FAIL with that violation, and
the runner exits non-zero. A measurement is evidence only for the
deployment the profile's executed-lab bind names; it never silently
passes against a different deployment. The 2026-09-25 run bound
**matched** → `qualified-for-profile`.

## 1. The supported topology (declared, and demonstrated — not inferred)

| Element | Supported shape | Observed in the executed run |
| --- | --- | --- |
| API replicas | **1** (`forge-app`) | 1 container, image `localhost/forge:dev`, `/health` ok |
| Worker replicas | **1** (`forge-worker`) | 1 container, same image |
| CAS storage | **one shared volume** bind-mounted at `/app/data` into BOTH consumers | both containers carry the mount (the run's topology section) |
| Database | one PostgreSQL (`forge-postgres`, host port 5433, database `forge`) | schema head `027`, version `0.37.0` |
| Queue | one Redis (`forge-redis`) | `/health` redis ok |
| Model route | one LiteLLM proxy (`forge-litellm`, host port 4000) | `/health` litellm ok |
| Runner | the lab's own runner (id 4, `unraid`, instance-type) — no public runner fleet | runner inventory recorded read-only |
| Admission bound | `FORGE_ADMISSION_MAX_ACTIVE_PER_PROJECT` (default 3 — no override observed) | limit 3 held under the controlled failures (§2) |

### CAS semantics — the boundary, stated rather than assumed

The checkpoint CAS bytes are node-local files on ONE volume. What makes
them visible to both processes is the **shared volume mount**, not the
shared database:

- every consumer that mounts `/app/data` sees the same
  content-addressed store, coordinated by the store's volume-wide lock
  (bounded `GCLockTimeout` waits) and the single-writer publication
  fence;
- **a second API/worker replica started WITHOUT the same volume would
  NOT see this node's bytes.** The shared database does not replicate
  content-addressed blobs. Scaling beyond one volume requires a
  shared/networked CAS root (or per-replica stores with an explicit
  replication contract) — neither is demonstrated by this deployment,
  and the topology section of the private report lists any consumer
  found without the mount as a DISCREPANCY, never a silent assumption.

### Model/provider quotas (budget uncertainty is never free capacity)

Numerical budgets are configured on both consumers:
`FORGE_BUDGET_PROFILES` (trivial 8 calls / 40k tokens / 900s; standard
40 / 200k / 3600s; heavy 120 / 600k / 10800s) plus
`FORGE_LANE_BUDGET_SECONDS=1800` per lane. A run without a budget row
never converts to "free capacity" or "zero spend" — refusals and
reconciliation are the only outs.

## 2. Capacity against real remote occupancy (executed, profile-bound)

Driven through the app's OWN dispatch entry (issue → `/implement` →
`/go`) on disposable GitLab projects, with REAL lane jobs on runner 4,
cancelled job-level immediately after the observation:

- **Open leases never exceeded the observed per-project bound** (peak
  3/3 — sampled for the whole load window, including across the cancel
  legs).
- **Overload is a typed park, never oversubscription**: the cycles
  beyond the bound park `blocked(execution_capacity)`.
- **Queue wait is measured separately from execution** (worst cycle
  3.277s in this run; per-cycle values in the private report).
- **Lost native responses never release capacity early**: the
  lost-cancel leg drops the answer at the client seam — the lease stays
  accounted until the deployment's own reconciler observes the native
  job terminal (drained 10.2s after job-level cancels in this run).
- **A failed cancel moves nothing**: cancelling an already-completed
  job is refused (or idempotently no-ops) by the provider and releases
  no capacity.

### The lost-response-at-the-cap arm (R38-18 negative test 1)

The 2026-09-25 run exercises the review's exact shape on the REAL
control plane, on the arm's OWN disposable project (the app's fair-use
gate counts a user's runs per project per hour — 6 — so the arm does
not share the occupancy section's budget):

1. every cycle is **planned first** (a plan is a model call and holds
   no lease — the probe window contains no model latency);
2. one `/go` **drops its native dispatch response at the client seam**
   (the pipeline/job identity is never read by the drill) while the
   fill `/go` cycles bring the project to the cap;
3. the dropped run's occupancy is proven **from the durable lease row
   alone** — still accounted (`dispatched_unknown` observed by the
   sampler in the dispatch window, `native_running` once the app's own
   dispatch completed) — never from the answer that was dropped;
4. the cycle immediately after the cap parks with the **typed verdict**
   (`parked_execution_capacity`) — the cap was held 3/3 with the
   dropped run's slot inside it;
5. after job-level cancels the reconciler resolved the occupancy **by
   observation** and drained the project to zero open leases (20.4s in
   this run).

## 3. Backup/restore across the deployment (executed)

The `checkpoint.reachability` signal is the contract:

- `backup_store` snapshots the REAL `/app/data` CAS root read-only (no
  container stop), the metadata half is exported with it, and a
  read-only `pg_dump` of `checkpoint_metadata` lands in the private
  report directory.
- The restore goes into a DISPOSABLE target (a temp root plus a
  disposable `forge_ops_restore` database, dropped afterwards). This
  run: 12/12 works verified, 6/6 pins resolved, restore 0.162s (the
  store's CURRENT size — remeasure as it grows).
- **Fidelity, not repair**: every checkpoint whose verified read works
  in the source also resolves after the restore; a checkpoint already
  unavailable in the source stays exactly that (reported as
  `source_unavailable` — never silently "resolved").
- **Mismatched halves are refused typed**: `verify_backup_consistency`
  detects metadata naming checkpoints the blob half lacks BEFORE any
  restore; `restore_store` refuses with `BackupMismatchError` listing
  the affected works and writes nothing.

### The mismatched-restore preflight arm (R38-18 negative test 3)

Restoring mismatched snapshots into a disposable installation refuses
at PREFLIGHT, before any new model turn. The arm runs three
installations in order, counting model turns (the resume dispatch that
would spend the first NEW model turn sits behind the gate):

- **mismatched halves** (t2 metadata + t1 blobs) → typed
  `RestorePreflightRefused("backup-halves")`, nothing written;
- **wrong schema head** (an installation declaring `026` against the
  frozen profile's `027`) → typed
  `RestorePreflightRefused("schema-head")` BEFORE any restore ran,
  nothing written;
- only the consistent snapshot at the profile's head restores — and
  only then did the model-turn gate open (0 turns through both
  refusals, 1 after the verified restore).

## 4. Credential isolation and egress (executed)

- The **model-facing variable set** of a dispatch is exactly the lane
  envelope (`FORGE_LANE_RESUME_MODE`, `FORGE_LANE_RESUME`,
  `FORGE_RESUME_CHECKPOINT`, `FORGE_ATTEMPT_GENERATION`,
  `FORGE_CONTINUATION_DECISION_ID`, `FORGE_LANE_CONTROL_URL`,
  `FORGE_LANE_CONTROL_TOKEN`) — asserted from the RECORDED dispatch
  envelope of the live run: no control-plane root credential name
  appears, and nothing credential-shaped beyond the work-scoped lane
  token and the non-secret delivery refs.
- **Deny probes observe actual denial** on the live surfaces: the
  lane-control endpoint refuses the MCP master key, the root model
  broker key and the GitLab root token; the operator surface refuses a
  lane token. A 2xx answer anywhere in that set is a `violated` probe
  and fails the run.

## 5. Degraded modes (executed; fences never disabled)

| Mode | Behavior (typed, bounded) |
| --- | --- |
| Storage pressure | an over-quota upload is refused `StorageQuotaExceededError` and leaves the store byte-identical (`storage.quota_refusal`) |
| Provider throttling (429) | classified transient by the app's own classifier; the revival budget bounds retries (`FORGE_RUN_AUTO_REVIVE_LIMIT`, default 2; backoff 60s → … capped at 900s) — an exhausted budget parks `blocked` with the reason, never an infinite retry |
| Slow control ACK | tolerated and MEASURED: `control.received_to_applied` 8.504s in this run, through the app's real lane-control endpoints (objective ≤ 60s) |
| Provider outage / lost dispatch | occupancy holds `dispatched_unknown`/`draining` and stays visible until the reconciler's probe decides |
| Fair-use exhaustion | a user past `FORGE_ADMISSION_USER_RUNS_PER_HOUR` (6, per project) parks the new run `blocked(fair_use_denied)` — a typed verdict, never a silent drop |

The publication and cancellation fences are never disabled by any
degraded mode; the app's health is asserted around every leg.

### The volume-fill-during-pause arm (R38-18 negative test 2)

The checkpoint volume filled to the configured safety threshold
(4096B in this run's quota-tmp-dir shape — **a real disk is never
filled; the typed-refusal path is what is measured**) while a run is
PAUSED with a pinned checkpoint (the pause-fence shape: a checkpoint
pinned by the persisted continuation decision):

- the pinned WIP **survived**: its verified read still resolves at the
  threshold (0.0002s), its bytes never deleted;
- new writes at the threshold refused **typed**
  (`StorageQuotaExceededError`; 5 refusals at 3241B on disk, 0 silent
  writes) — storage growth stopped at the configured boundary;
- **admission stops predictably**: every further write refused at the
  same typed boundary — the operator parks admission on a typed
  signal, never a silent queue.

## 6. Token rotation (the runbook)

Observed generation behavior (the executed rotation drill, on a
DISPOSABLE configuration — the deployment's env untouched):

- a token minted under the rotated-away secret fails verification
  outright (403) the moment the secret changes — rotation retires the
  whole old credential, no grace;
- within one secret, a token from a SUPERSEDED generation is refused
  with the actionable 403 naming both generations ("superseded runner
  generation (0; the work is at generation 1)");
- the rotated (v2) current-generation token authenticates normally.

Production procedure (`credential.rotation_generation`):

1. Generate the new `FORGE_LANE_CONTROL_SECRET` (v2).
2. Recreate the consumers with v2 (the standard
   [lab-alignment runbook](lab-alignment-runbook.md) recreate steps —
   pg_dump first, images never pruned). In-flight lanes holding v1
   tokens lose steering/auth access immediately — by design; their runs
   continue to completion through the native pipeline, and their WIP
   resumes under v2-dispatched attempts.
3. Re-dispatch any paused work (`/retry`): the re-dispatch bumps the
   durable attempt generation and mints a v2, generation-scoped token.
4. Verify: the deny probes in the executed report's credential section
   all answer denied; a v1 token anywhere is a 403.

## 7. The measured-limits table (R38-18 — the runbook's numbers, bound to the frozen profile)

Every number below is the 2026-09-25 run's measurement against the
profile whose digest opens this document. The scope sentence travels
with each — these are stated shapes, not extrapolations.

| Limit | Measured value | Scope / guarantee |
| --- | --- | --- |
| Concurrency (per project) | limit 3; peak occupied 3; cap held 3/3 under the dropped-dispatch failure; occupancy mix at the cap `{native_running: 3}` (with `dispatched_unknown` visible in the dispatch window); cycle after the cap → `parked_execution_capacity` | **guaranteed** (durable lease CAS; lost responses hold; overload parks typed) |
| Pause/cancel responsiveness | control commands received→applied: p50 0.0193s / p95 0.0258s / max 0.0258s (n=12); cancel-under-slow-provider: p50 0.0521s / p95 0.0521s (n=12, 0.05s provider latency) | n=12 pause/resume cycles through the REAL lane-control endpoints over a disposable database, contended by 3×8 checkpoint uploads; objective ≤ 60s — **best-effort, stated percentiles + scope, never one best-case latency, not a fleet claim** |
| Command latency under contention | slow-control-ACK received→applied 8.504s (with an 8s deliberately slow lane) | through the app's real lane-control endpoints during the occupancy load — **best-effort** |
| Restore time | 0.162s | the deployment's CURRENT store size — **measured, not extrapolated**; remeasure as the store grows |
| Queue wait (request → lease) | worst cycle 3.277s | reported per cycle, separately from execution — **best-effort** |

## 8. Human review capacity (a STATED POLICY FIELD — never a throughput claim)

Model throughput and human review capacity are never conflated: the
report carries a reviewer-WIP bound as **policy**, not measurement —
`reviewer_wip_bound: 5` concurrent reviewable WIP per reviewer (the
run's `--reviewer-wip-bound`, default 5), against the deployment's
admission bound of 3 active runs per project. Admission staying within
the reviewable volume is a **coherence check** (`coherent: true` in
this run): an admission bound BEYOND the stated reviewer-WIP bound is
recorded as a policy finding and fails the run — lower the admission
bound or grow review capacity BEFORE increasing load.

## 9. One incident, recovered from the runbook (no DB patching)

**Incident shape**: a lane job died mid-run (runner loss / job
cancelled) and the run sits `blocked` with the honest classification.

1. Read the state: `GET /runs/{run-id}` (blocked reason, evidence) and
   the operator occupancy view (`occupied_vs_limit`,
   `unknown_ages`) — uncertain occupancy stays visible until the
   reconciler observes the native job terminal; a `draining` lease is
   RESOLVED BY OBSERVATION, never by hand.
2. If WIP was checkpointed (the pause fence): the persisted
   continuation decision pins the exact checkpoint digest; `/retry`
   dispatches a fresh attempt whose envelope carries `required` resume
   + the pinned digest (the strict restore guard refuses a corrupt
   dispatch contract before any provider I/O).
3. If occupancy wedges with the provider provably terminal and the
   reconciler cannot observe it (provider outage): use the AUDITED
   override — `release_lease_with_evidence(..., override="approver=…;
   reason=…; ticket=…")` — the approver and ticket land in the release
   reason's audit trail (`override.audit`). Never a bare UPDATE.
4. Confirm recovery: capacity drains to zero open leases for the
   project, and the incident's diagnostics redact credentials by the
   evidence policy (`FORGE_EVIDENCE_DENY_PATTERNS`).
5. **Know when to escalate rather than retry**: a `fair_use_denied` or
   `execution_capacity` park is TYPED and self-healing once work drains
   — retry after the window; a `StorageQuotaExceededError` on the
   checkpoint volume needs an operator (raise the quota or clean
   superseded checkpoints — never delete PINNED WIP); a lease wedged
   past the reconciler's probe with the provider down needs the AUDITED
   override (step 3), not repeated cancels.

## 10. What is guaranteed vs best-effort

| Boundary | Class |
| --- | --- |
| Open leases ≤ the admission bound; overload parks typed; a dropped dispatch response holds its slot until reconciled | **guaranteed** (durable lease CAS; lost responses hold) |
| Mismatched backup halves / wrong schema head refused at preflight before any model turn; restore fidelity | **guaranteed** (verify-before-restore + the schema-head bind) |
| Quota exhaustion stops storage growth typed, never deleting pinned WIP | **guaranteed** (typed store refusal; pins defended) |
| Root credentials outside the model-facing set | **guaranteed by construction** (asserted from the recorded envelope; deny-probed on the live surfaces) |
| Queue wait / received→applied / pause-cancel percentiles | **best-effort** — measured and reported per run with stated percentiles and scope; objectives in the report's table |
| Restore time | **measured, not extrapolated** — remeasure as the store grows |
| Scaling beyond one CAS volume | **not supported** — outside the demonstrated topology (§1) |
| Measurements on a deployment differing from the frozen profile | **not evidence** — `unqualified-for-profile` (§0) |

## 11. The operating envelope and its alerts (R40-15 / #351)

The selected workflow's own shape — a delivery that includes a review
round (#338), a guarded budget amendment (#340) and a redemption-mode
dispatch (#343, the lane mode this profile pins) — has its own
envelope, measured on the actual lab (the
`workflow_envelope`/`partition`/`degradation_parking`/`redemption_lane`/
`workflow_restore` sections of `run_deployment_ops.py`; the 2026-09-26
record is `qualification/deployment-ops-2026-09-26.json`). The full
table lives in the [support agreement](support-agreement.md); the
ALERTS and their recovery steps live here.

### The envelope, one paragraph

Active execution is bounded by the durable lease CAS (3 per project —
accepted backlog is a DIFFERENT counter: 10 queued, 6 runs/user/hour,
5/issue); a review round holds the lineage's ONE outstanding-round slot
(a second /fix refuses typed) and its child holds an execution slot only
while IT executes; slots stay occupied while native work runs OR its
start/cancel outcome is unknown — under a simulated partition the
reconciler's pass released NOTHING (3 held) and ONE pass after the heal
drained all 3; a queued burst during sustained provider 429 parks
bounded (9-request burst → 4 admitted + 5 typed `queue_full` refusals,
12 dispatch attempts = exactly the 4×(1+2) budget, ZERO re-plans); the
redemption leg mints the grant before the provider call and the lane
redeems through the real endpoint (ledger joined, refusals typed with
zero broker calls); a data-bearing restore recovers runs, checkpoints,
`review_rounds`, `budget_amendments`, grants+redemptions and native
intent, gating the resume dispatch behind consistency verification.

### The alerts (each: observable → threshold basis → recovery step)

| Alert | Observable | Threshold basis | Recovery step |
| --- | --- | --- | --- |
| **Oldest unresolved effect** | `operator.unresolved_effect_age` (per publication-intent row still unresolved) and `native_start.unknown_age` in the operator occupancy view | the partition drill's held-unknown window resolves by OBSERVATION in one reconciler pass after heal — an unresolved effect or unknown occupancy whose age exceeds the reconciler's normal pass interval ×10 with the provider reachable is STUCK, not slow | run the reconciler's probe once (it is bounded); if the provider is provably terminal and the probe cannot observe, use the AUDITED override (§9 step 3) — never a bare release |
| **Sustained queue age** | `queue.age` — oldest admitted-not-executing run (`accepted/preflight/planning/waiting_approval/waiting_harness/waiting_ci`) | the degradation-parking drill: sustained 429 parks every admitted run blocked within its bounded revival budget (≤ 2 revives); a queued run whose age exceeds the revival ladder's ceiling (900 s backoff cap) + one lane run's measured wall clock means the queue is NOT draining | check the provider route (LiteLLM → vendor) first — the same 429 never re-plans (0 replans in the drill), so a stuck queue is infrastructure, not code: resolve the vendor window, then `/retry` the parked runs |
| **Storage pressure** | `storage.unreferenced_bytes` / the CAS volume's bytes vs the per-work quota (`StoragePolicy`) and the host volume's free space | the volume-fill drill: writes refuse TYPED at the configured safety threshold and PINNED WIP survives — growth stops predictably; alert when the volume passes 70 % of the threshold that produced the first typed refusal in the last run | raise the quota or clean SUPERSEDED checkpoints via the retention pass (`apply_retention`) — never delete PINNED WIP; the sweep converges once the volume lock frees (`GCLockTimeout` is bounded, retry-later is recovery) |
| **Credential expiry** | the operation grant's `redemption_deadline` vs now, and the registry binding's revision history (`data/credential-bindings.json`) | the redemption drill: an expired window refuses typed `grant_expired` (absolute deadline, never re-anchored) and a rotation between mint and redemption refuses `binding_revision_mismatch` with the lane FAILING CLOSED (zero model calls) | re-dispatch the attempt (`/retry`) so a FRESH grant mints under the live binding — the registry is re-read on every dispatch and every redemption; never widen a stale grant's deadline |
| **Database unavailability** | `/health` `database` axis; `LaneAuthorityUnavailable` (503) on the lane-control surface | the restart drill: engine disposed and recreated — occupancy identities SURVIVE and the reconciler resolves by durable `native_intent_ref` through the fresh engine; the DB being down is a hard stop for NEW dispatches, never a data-loss event | restore connectivity (the lab owner's domain for forge-postgres); the reconciler picks up from the durable rows — no manual lease surgery. If the volume itself is lost: the restore runbook (§3) + the workflow-restore consistency gate — the resume dispatch stays closed until rounds/amendments/grants/native intent verify |

Every threshold above names its drill basis — none is a fleet SLA. The
measured numbers behind them are in the report's
`measured_limits.operating_envelope` block and the support agreement's
envelope table; re-measure on every profile re-freeze.
