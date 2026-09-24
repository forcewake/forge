# Deployment boundaries — the supported topology, its limits and the operator runbook (R37-20)

Issue #301 (external review `4af6b33`, item R37-20). The lab drills
([ops-drills.md](ops-drills.md), R36-21/#280) prove the invariants on
disposable fixtures; this document declares what the ACTUAL deployment
supports, with the executed evidence in
`qualification/deployment-ops-2026-09-24.json` (produced by
`scripts/run_deployment_ops.py` — read-only probes plus explicitly
disposable resources; no lab container is ever started, stopped or
recreated by the run).

## 1. The supported topology (declared, and demonstrated — not inferred)

| Element | Supported shape | Observed in the executed run |
| --- | --- | --- |
| API replicas | **1** (`forge-app`) | 1 container, image `localhost/forge:dev`, `/health` ok |
| Worker replicas | **1** (`forge-worker`) | 1 container, same image |
| CAS storage | **one shared volume** bind-mounted at `/app/data` into BOTH consumers | both containers carry the mount (the run's topology section) |
| Database | one PostgreSQL (`forge-postgres`, host port 5433, database `forge`) | schema head `027`, version `0.36.0` |
| Queue | one Redis (`forge-redis`) | `/health` redis ok |
| Model route | one LiteLLM proxy (`forge-litellm`, host port 4000) | `/health` litellm ok |
| Runner | the lab's own runner (id 4, `unraid`, instance-type) — no public runner fleet | runner inventory recorded read-only |
| Admission bound | `FORGE_ADMISSION_MAX_ACTIVE_PER_PROJECT` (default 3 — no override observed) | see §2 |

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
  and the topology section of the executed report lists any consumer
  found without the mount as a DISCREPANCY, never a silent assumption.

### Model/provider quotas (budget uncertainty is never free capacity)

Numerical budgets are configured on both consumers:
`FORGE_BUDGET_PROFILES` (trivial 8 calls / 40k tokens / 900s; standard
40 / 200k / 3600s; heavy 120 / 600k / 10800s) plus
`FORGE_LANE_BUDGET_SECONDS=1800` per lane. A run without a budget row
never converts to "free capacity" or "zero spend" — refusals and
reconciliation are the only outs.

## 2. Capacity against real remote occupancy (executed)

Driven through the app's OWN dispatch entry (issue → `/implement` →
`/go`) on a disposable GitLab project, with REAL lane jobs on runner 4,
cancelled job-level immediately after the observation:

- **Open leases never exceeded the observed per-project bound** (peak
  occupancy vs limit is in the report's
  `execution.occupied_vs_limit` signal — sampled for the whole load
  window, including across the cancel legs).
- **Overload is a typed park, never oversubscription**: the cycles
  beyond the bound park `blocked(execution_capacity)` with the capacity
  snapshot in the run evidence and an issue note quoting the reason.
- **Queue wait is measured separately from execution**: every cycle
  carries a queue-wait measurement (request → lease acquired, or
  request → the typed park verdict); execution is measured only over
  the slot's own window. The two are reported per cycle, never blended.
- **Lost native responses never release capacity early**: the drill's
  lost-response leg cancels a native job and DROPS the answer at the
  client seam — the lease stays accounted (`draining` /
  `dispatched_unknown` visible in the sampler) until the deployment's
  own reconciler observes the native job terminal.
- **A failed cancel moves nothing**: cancelling an already-completed
  job is refused by the provider and releases no capacity.

## 3. Backup/restore across the deployment (executed)

The report's `checkpoint.reachability` signal is the contract:

- `backup_store` snapshots the REAL `/app/data` CAS root read-only (no
  container stop; locks are transient coordination, never state), the
  metadata half is exported with it, and a read-only `pg_dump` of
  `checkpoint_metadata` lands beside the report.
- The restore goes into a DISPOSABLE target (a temp root plus a
  disposable `forge_ops_restore` database, dropped afterwards).
- **Fidelity, not repair**: every checkpoint whose verified read works
  in the source also resolves after the restore; a checkpoint already
  unavailable in the source stays exactly that (preserved and reported
  as `source_unavailable` — the restore never silently "resolves"
  missing bytes). A zero-file checkpoint is a legal verified read.
- **Mismatched halves are refused typed**: metadata naming a
  checkpoint the blob half lacks is detected by
  `verify_backup_consistency` BEFORE any restore; the restore refuses
  with `BackupMismatchError` listing the affected works and writes
  nothing.

Recovery-time objective: the measured `restore_seconds` for the
deployment's current store size is in the report (small today — remeasure
after the store grows; the number does not extrapolate).

## 4. Credential isolation and egress (executed)

- The **model-facing variable set** of a dispatch is exactly the lane
  envelope (`FORGE_LANE_RESUME_MODE`, `FORGE_LANE_RESUME`,
  `FORGE_RESUME_CHECKPOINT`, `FORGE_ATTEMPT_GENERATION`,
  `FORGE_CONTINUATION_DECISION_ID`, `FORGE_LANE_CONTROL_URL`,
  `FORGE_LANE_CONTROL_TOKEN`) — asserted from the RECORDED dispatch
  envelope of the live run: no control-plane root credential name
  (`FORGE_LANE_CONTROL_SECRET`, `FORGE_MCP_KEY`, `GITLAB_TOKEN`,
  `DATABASE_URL`, …) appears, and nothing credential-shaped beyond the
  work-scoped lane token.
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
| Slow control ACK | tolerated and MEASURED: `control.received_to_applied` (command received → applied) through the app's real lane-control endpoints; the objective is in the report's service-objectives table |
| Provider outage / lost dispatch | occupancy holds `dispatched_unknown`/`draining` and stays visible until the reconciler's probe decides |

The publication and cancellation fences are never disabled by any
degraded mode; the app's health is asserted around every leg.

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

## 7. One incident, recovered from the runbook (no DB patching)

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

## 8. What is guaranteed vs best-effort

| Boundary | Class |
| --- | --- |
| Open leases ≤ the admission bound; overload parks typed | **guaranteed** (durable lease CAS; lost responses hold) |
| Mismatched backup halves refused; restore fidelity | **guaranteed** (verify-before-restore) |
| Root credentials outside the model-facing set | **guaranteed by construction** (asserted from the recorded envelope; deny-probed on the live surfaces) |
| Queue wait / received→applied latencies | **best-effort** — measured and reported per run; objectives in the report's table |
| Restore time | **measured, not extrapolated** — remeasure as the store grows |
| Scaling beyond one CAS volume | **not supported** — outside the demonstrated topology (§1) |
