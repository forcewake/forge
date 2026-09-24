# Operational drills (R36-21)

The drill runner is the operating proof for capacity, storage recovery
and degraded-mode service objectives: bounded degradation and safe
capacity accounting under realistic concurrency, provider latency,
restarts and storage limits — not a peak-throughput demonstration.

```bash
uv run python scripts/run_ops_drills.py --drill all --report /tmp/ops-drills.json
```

The report states, per drill, the **achieved objectives** and the exact
**tested limits** (N, budget, lock-wait). It does not generalize to
larger fleets or production hardware without new measurement — the
scope sentence is part of every report.

PostgreSQL variant (the checkpoint index in `checkpoint_metadata`
through the real postgres repository), on a disposable database:

```bash
podman exec forge-postgres psql -U forge -d forge \
    -c 'CREATE DATABASE forge_ops_drills'
uv run python scripts/run_ops_drills.py --drill all \
    --db-url postgresql+asyncpg://forge:forge@127.0.0.1:5432/forge_ops_drills \
    --authority postgres --report /tmp/ops-drills-pg.json
```

Exit code 0 means every selected drill passed; 1 means at least one
objective failed — a failed drill is a finding, never a yellow state.

## What each drill proves, and the escalation path when it fires

### `native_start_load` — capacity under concurrent dispatch

N workers drive dispatch-intent → handle → terminal cycles through the
real admission API (`execution_leases`) against a real database, with
lost start responses and failed provider-side cancellations mixed in.

Proves:

- open leases never exceed `FORGE_ADMISSION_MAX_ACTIVE_PER_PROJECT`,
  including when starts lose responses or cancellations fail
  (uncertain occupancy holds its slot — never a free-capacity fiction);
- unknown occupancy is visible (`native_start.unknown_age`) and
  reconciliation is **bounded**: once the native jobs are terminal, one
  reconciler pass drains every held slot;
- a definite 4xx start refusal returns capacity immediately.

Escalation: sustained `dispatched_unknown`/`draining` with growing
`native_start.unknown_age` → the reconciler's provider probe cannot
decide; check the provider, then either wait for the probe or apply an
**audited override** (below). Never force-free by hand-editing rows.

### `checkpoint_upload_load` — bounded uploads, contended sweeps

Concurrent checkpoint puts under a declared memory/admission budget
(concurrent-upload and in-flight-byte caps); a contended volume lock
exercises the #263 `GCLockTimeout` contract.

Proves:

- concurrent uploads never exceed the declared budget; overload is the
  **typed** `UploadBudgetExceeded` refusal (re-deliver later — the put
  is idempotent), never a silent unbounded queue;
- a sweep that cannot take the volume lock within
  `FORGE_CHECKPOINT_GC_LOCK_WAIT_SECONDS` aborts typed, deletes
  nothing, and converges on retry once the holder releases;
- every landed checkpoint reads back verified after the load.

Escalation: repeated `UploadBudgetExceeded` → in-flight uploads are
saturated (raise the budget or drain the backlog). Repeated
`GCLockTimeout` → a holder is stuck on `cas-refs.lock` (or the
postgres advisory twin); find the holder named in the lock diagnostics
before raising `FORGE_CHECKPOINT_GC_LOCK_WAIT_SECONDS`.

### `control_responsiveness` — the operator path stays live

Control commands (occupancy / saturation / lease snapshots) issued
concurrently with uploads and a retention pass.

Proves: `control.command_latency` p95 stays under the objective (2s)
while storage contends — these reads never take the volume-wide GC
lock.

Escalation: p95 above the objective with storage idle means database
contention; with storage busy it means a read path started contending —
file an issue, that is a regression of the read/no-lock discipline.

### `degraded_faults` — typed behavior, never hangs

Injected provider latency, 429, 503, a definite 404, a database engine
restart and a full-disk-shaped quota.

Proves each fault produces its typed outcome: latency completes (no
hang); 429/503 leave unknown occupancy **held and visible**; 404
returns capacity immediately; a restarted engine still sees every
occupancy identity and the reconciler resolves it by intent ref; a
quota refusal is typed (`StorageQuotaExceededError`) and leaves the
store byte-identical.

Escalation: `native_start.unknown_count` climbing after a provider
incident → provider probes are undecidable; follow the occupancy
report's escalation (probe or audited override).

### `backup_restore` — metadata and blobs, together

`backup_store` / `restore_store` over a real store: works, CAS blobs,
pins and pending-GC state in one backup.

Proves:

- the restore recovers the selected **active** and **pinned**
  checkpoints with verified reads (`backup.restore_coverage`);
- a mismatched pair (metadata from one moment, blobs from another) is
  **refused** with `BackupMismatchError` listing the affected works —
  never silently accepted, and the refused restore writes nothing.

Escalation: a `BackupMismatchError` on restore means the backup halves
describe different store states — re-take the backup from one
consistent state (quiesce uploads first; see
[backup-restore.md](backup-restore.md) for the runbook).

### `operator_override_audit` — the audited force-release

An explicitly approved force-release of uncertain occupancy.

Proves: without the override the same release parks draining (the
local verdict alone never frees the slot); with the override
(`release_lease_with_evidence(..., override="approver=…;reason=…")`) the
release carries the approver and reason verbatim in the audit trail
(`override.audit`) — the risk stays visible instead of being relabeled
observed-terminal.

Escalation: an override requires a named approver and reason; it is a
deliberate capacity decision, recorded — never a routine cleanup.

## Saturation signals

| Signal | Meaning |
| --- | --- |
| `execution.occupied_vs_limit` | occupied slots vs the configured limit; `at_limit` is when dispatches start parking |
| `native_start.unknown_age` | oldest unknown-occupancy age (seconds) — the number to watch after an incident |
| `checkpoint.upload_memory_budget` | peak concurrent uploads / in-flight bytes vs the declared budget, plus typed refusals |
| `control.command_latency` | median/p95/max control-command latency under contention |
| `backup.restore_coverage` | works / checkpoints / pins / pending-GC restored, plus mismatch refusals |
| `override.audit` | approver + reason of every audited override |

All are counts and ages over durable rows — no secrets, no prompts.
