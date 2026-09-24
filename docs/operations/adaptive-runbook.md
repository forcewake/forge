# Adaptive runbook — supported recipes and operator handoff (OPS-08)

This document covers the adaptive workflow's supported recipes, safe
control commands, lifecycle meanings, recovery procedures, credentials,
data flow, and the decision record for retaining the current controller.
It distinguishes **system read scope**, **write coordination**, and
**production deployment**.

For the classic /implement workflow, see
[operator-commands.md](operator-commands.md).

## 1. The three permission tiers

| Tier | What it means | Who grants it |
|---|---|---|
| **Read scope** | The set of repositories the discovery agent may read (immutable snapshots). Read-only; no branch, commit, or PR is ever created. | The WorkContract's `read_scope` |
| **Write coordination** | Which repository each child execution lane may write to. ONE writable repository per child lane (MRP-03); siblings receive read-only snapshots. | The WorkContract's `write_scope` (must be ⊆ read scope) |
| **Production deployment** | Merging PRs, applying migrations to production, deploying services. **Always a human decision; forge never merges, never deploys.** | The human gate |

## 2. Supported runtime profiles

| Profile | SDK | Capabilities | Status |
|---|---|---|---|
| `claude-sdk-lane` | ClaudeSDKClient | interrupt, live_input, checkpoint_export, questions | **First interactive driver** |
| `codex-app-lane` | Codex App Server | interrupt, live_input | Steering bound to the active turn |
| `opencode-server-lane` | OpenCode server | interrupt | BYOK profiles only |
| `checkpoint-restart` | Any batch CLI | checkpoint_export only | Control applies at checkpoint boundaries |

**Not supported**: Docker socket access for coding agents (privileged
test execution belongs to the trusted test executor alone).

### The DriverMatrix

Unregistered (SDK × provider_route × credential_mode) combinations
**fail during onboarding** — the DriverMatrix is fail-closed. A missing
selected credential does not fall back to another provider key.

### Live-verified combinations (since 2026-09-21)

`forge.adaptive.drivers.live_registrations.seed_live_matrix()` seeds a
matrix from the live smoke evidence — each entry cites its evidence
JSON under `docs/evaluation/`, and seeding REFUSES an entry whose file
is missing or whose recorded run failed (no evidence, no claim):

| SDK | provider_route | credential_mode | Verified against | Evidence |
|---|---|---|---|---|
| claude-sdk | zai-anthropic-gateway | byok-env-token | claude 2.1.273 + claude-agent-sdk 0.2.157 | `docs/evaluation/2026-09-21-drivers/claude-live.json` |
| codex-app | chatgpt-login | chatgpt-plan | codex-cli 0.153.4 | `docs/evaluation/2026-09-21-drivers/codex-live.json` |
| opencode-server | zai-coding-plan | server-basic+stored-key | opencode v2.0.10 | `docs/evaluation/2026-09-21-drivers/opencode-live.json` |

A green entry means the SMOKE's steps passed (session start, a
completed turn, steering, interrupt/abort) — not a full lane cycle.
Re-verify after vendor upgrades (add `--e2e` for the real-task mode —
the agent implements a failing test in a scratch repo and repo pytest
is the judge):

```bash
uv run --extra interactive python scripts/driver_live_smoke.py \
  --driver claude --out docs/evaluation/<date>-drivers/claude-live.json
```

The opencode lane needs no manual server: the spawner
(`forge.adaptive.drivers.OpenCodeServer`) owns the process, the port,
and the server password end-to-end.

## 3. Safe control commands

| Command | What it does | What it does NOT do |
|---|---|---|
| `/pause` | Closes new publication authorizations of the current epoch, then sends the runtime interrupt | Does not guarantee zero in-flight effects (see §5) |
| `/resume` | Restores from a confirmed checkpoint with a fresh execution epoch | Does not resume without a checkpoint; does not restore a native session for a non-pinned profile |
| `/steer` | Delivers bounded guidance to the agent | Does not grant new authority; acceptance-policy changes are rejected and routed to the revision gate |
| `/answer` | Provides the authorized answer to a waiting question | Does not unblock other questions |
| `/amend` | Promotes a constraint into a ChangeProposal | Does not self-approve (the human gate decides) |
| `/approve-revision` | Activates a CAS-approved material revision | Does not fence old publication rights by itself (the epoch bump does) |

**State ladder**: `received → authorized → applied → checkpointed` (or
`rejected` / `expired`). A command that reaches `applied` has spent;
the ladder refuses skips.

## 4. Lifecycle meanings

```
DiscoveryRun:
  pending → running → complete          (evidence bundle is durable)
                  ↗ waiting_question → running (after /answer)
                  ↘ blocked            (critical question unresolved)

PlanRevision:
  revision N → N+1 (tactical_internal: apply without approval)
             → material_* → ChangeProposal → human gate → N+1

WorkPackage:
  running → complete (all phases landed)
          → failed   (a child failed; later phases are HELD)
```

## 5. Recovery procedures

### A cancelled run with in-flight effects

A cancel **before** the native dispatch forbids the write (zero
commits, zero PRs). A cancel **after** dispatch records the effect as
`superseded` — already-accepted provider operations cannot be undone
retroactively; they are correlated and shown as evidence.

### A failed WorkPackage child

The saga records `partially_published`. Recovery is by **publication
intents** (the durable reservation/reconciliation machinery), never by
deleting branches that may carry human edits.

### An expired artifact

An expired checkpoint produces a recoverable explicit state (`resolve()`
returns false), not a fresh empty workspace. The caller re-checkpoints
from the last confirmed state.

### The checkpoint authority (Q35-03): one store for upload, resume and operations

Upload (`PUT /lane/checkpoints/{work}`), `/resume`, the operator list
and the retention passes all read ONE configured checkpoint repository,
resolved by `forge.adaptive.checkpoint_repository.resolve_repository`
from `FORGE_CHECKPOINT_DURABILITY` (`best_effort` filesystem index, or
`postgres` — the `checkpoint_metadata` table), `FORGE_CHECKPOINT_STORE_DIR`
(the blob root, shared by both modes), and the process's session
factory. Changing the metadata backend therefore never changes whether
confirmed work can be resumed: an API-confirmed checkpoint is visible
to a fresh resume producer in the SAME mode.

Operator rules:

- **Switching modes is an explicit migration, never a drift.** The
  postgres index starts EMPTY (no backfill from the JSON files — see
  the alembic 026 note). Use the Q35-21 command set above
  (`inventory` → `import` → `verify` → `cutover`) instead of manual
  re-uploads: it is idempotent, restartable and gated, and since R36-05
  the STANDARD `resolve_repository()` composition carries the mutation
  fence — exactly one backend accepts metadata mutations after the
  cutover, old processes included (see the migration section below).
- **Half-configurations refuse at startup.** `postgres` without a
  session factory (or an unknown mode value) makes
  `control_service_from_env` raise `CheckpointRepositoryMisconfigured`
  and the HTTP channel answer `503` naming the variable — the process
  never silently degrades to the filesystem index.
- **A database outage is `503`/typed-unavailable, never `404`.** Both
  the channel and the resume producer surface
  `CheckpointRepositoryUnavailable`; no surface falls back to the
  filesystem, and an outage is never reported as "no checkpoint".
  Recovery is bringing the metadata database back — nothing to replay.
- **Which mode am I on?** `GET /lane/checkpoints/health` names
  `durability` and `authority` for the configured deployment.

### Migrating checkpoint metadata to the postgres authority (Q35-21, R36-05)

Adopting `FORGE_CHECKPOINT_DURABILITY=postgres` for an installation
that already holds checkpoints is an EXPLICIT migration — a bare schema
migration (alembic 026) migrates no index contents, and a silent flip
would strand every paused work behind an index the new authority has
never seen. The command set (module CLI, exit 0 clean / 3 partial
import / 1 refused):

```bash
uv run python -m forge.adaptive.checkpoint_migration inventory
uv run python -m forge.adaptive.checkpoint_migration import   --database-url "$DATABASE_URL"
uv run python -m forge.adaptive.checkpoint_migration verify   --database-url "$DATABASE_URL"
uv run python -m forge.adaptive.checkpoint_migration cutover  --database-url "$DATABASE_URL"
uv run python -m forge.adaptive.checkpoint_migration rollback --database-url "$DATABASE_URL" --verify-report <path>
```

- **`inventory`** scans `works/<id>.json`, every manifest and referenced
  blob (digests verified), the pins overlay, and writes
  `<store-root>/migration/inventory.json`. Missing and corrupt entries
  are LISTED with their digests — never invented, never silently
  skipped.
- **`import`** lands every importable entry in `checkpoint_metadata`
  through the repository protocol (the same verified landing the upload
  route uses), with on-upload retention DISABLED so the migration never
  drops history. A missing blob makes that ENTRY unimportable —
  reported, the run continues with the rest, exit 3. Re-running is a
  no-op: the natural keys `(work_id, checkpoint_id)` prevent duplicates
  and selection is derived from the rows, so a re-import can never
  alter which checkpoint is active. Interrupted anywhere, a re-run
  converges (verify names any gap).
- **`verify`** compares the old index against the database per work
  (entry sets, active identity), resolves every PINNED exact reference
  through the new backend, and re-checks blob reachability through the
  configured root. A disagreement (JSON says A, DB says B) is reported
  with BOTH actives — the operator resolves it; no timestamp ever picks
  a winner. Exit 1 while anything disagrees. **R36-05:** the report also
  BINDS both inventories by content digest
  (`migration.inventory_generation` — one sha256 per authority over its
  entries, derived actives and, on the filesystem side, the pins), and
  both flips re-derive those digests from the CURRENT state: a stale or
  tampered report can never authorize a changed inventory on EITHER
  side, and a timestamp alone authorizes nothing.
- **`cutover`** flips the deployment authority MARKER — the state file
  `<store-root>/migration/authority.json`, written atomically under an
  exclusive fence (`<store-root>/migration/cutover.lock`) — ONLY when
  the verify report is clean AND BOTH inventories still hash to the
  generations the report bound (`--database-url` re-binds the target;
  an unreachable target database is a typed refusal, never a flip).
  The marker carries its flip `generation` (1, 2, … — reported at
  startup and by doctor).
- **The fence is STANDARD since R36-05 — no wrapper to compose.**
  `resolve_repository()` (the one composition point the upload route,
  the resume producer, workers and retention share) attaches the
  mutation fence by DEFAULT: the marker is read at construction and
  re-checked before every metadata mutation (`put`, retention/delete
  family). An already-running old-authority process refuses its NEXT
  mutation with the typed `MutationsFencedError` naming
  configured-vs-active once the flip lands — no restart needed to STOP
  the writes. Immutable reads through the retired process stay
  available and distinguishable (`authority()` keeps naming the
  authority that answered; `GET /lane/checkpoints/health` names the
  configured deployment). The HTTP upload route answers a fenced
  upload with 503 carrying the fence message. Repositories constructed
  DIRECTLY (the migration tool's own imports) stay unfenced;
  `enforce_authority_marker(repository, root)` remains the documented
  wrapper for them — ONE fence implementation shared with the standard
  composition. The pin overlay (`pin`/`unpin`) is deliberately not
  fenced: it is the shared blob-volume protection, not the retired
  metadata authority, and every deletion path IS fenced.
- **Operating mode (stated honestly): drained-offline cutover is the
  FIRST supported mode.** Stop the old-authority processes (or accept
  that they refuse mutations from the flip onward), run the migration
  commands, set `FORGE_CHECKPOINT_DURABILITY=postgres`, and start the
  new processes. The fence makes concurrent processes SAFE TO REFUSE —
  it does NOT promise an online zero-downtime migration: a mutation
  that already passed the fence check instants before the flip may
  still commit to the retired authority (the very next mutation is
  refused). Lock order, stated once: every fenced mutation checks the
  marker BEFORE taking the store's volume-wide GC lock
  (`cas-refs.lock` / its postgres advisory twin) and before any
  per-work lock — a fence refusal never waits on a contended volume,
  no path holds the volume lock while waiting for the cutover fence,
  and the orders cannot cycle (both waits are bounded).
- **Startup observability (R36-05).** Wherever the app composes the
  repository (the app lifespan, the control service's
  `control_service_from_env`), one line reports
  `migration.configured_vs_active_authority` with the state
  (`unmarked` / `aligned` / `mismatch`), both authorities and the
  marker generation; a mismatch is a WARNING. Every refusal logs
  `migration.mutation_refused`.
- **Rollback is forward-only by default.** `rollback` refuses unless a
  CLEAN verify report for the CURRENT data state is supplied
  (`--verify-report`, taken after the cutover; a report that predates
  the flip, the store's current shape, OR the target inventory's
  current generation is refused — a post-report database upload blocks
  the rollback exactly as a filesystem change would). Checkpoints
  uploaded after the cutover exist only in the database — the
  documented recovery is `import --reverse` (database entries back into
  the filesystem index; the blobs are shared CAS bytes), then `verify`,
  then `rollback`. The old inventory is NEVER deleted by any step. The
  fence recovers symmetrically: after a rollback the still-running new
  process's next mutation is refused while the old one's works again.
- **`forge doctor` preflight** (read-only): `checkpoint.migration_coverage`
  (works still only on the filesystem while postgres is active — with
  the `import` command named), `checkpoint.pointer_conflicts`
  (disagreeing actives → operator resolution),
  `checkpoint.blob_topology`, and `checkpoint.authority_marker` (R36-05:
  the marker's authority and generation, the CONFIGURED repository, and
  a MISMATCH verdict — failing with both remedies named when the process
  is configured for the retired authority).

**Blob volumes and replicas (the unsupported topology).** The CAS blobs
are content-addressed filesystem bytes under BOTH contracts: a shared
database does NOT make node-local content shared. Two replicas reading
separate volumes is unsupported — each would see only its own blobs and
report the others' digests unreachable. `checkpoint.blob_topology`
warns (never blocks) on the simplest honest heuristic: a store root that
is RELATIVE (each process's own CWD) or under a temp directory looks
node-local; any other absolute path is ASSUMED shared — confirm every
replica mounts it. Under `best_effort` the warning names the
single-process contract itself: multi-replica deployments need the
postgres authority AND a shared blob volume.

## 6. Credentials and data flow

```
WorkContract (approved bytes)
  → PlanRevision (how, replaceable)
    → child execution lanes (CI harness, ephemeral)
      → candidate artifacts (content-addressed store)
        → CandidateSet (frozen identity for verification)
          → verification lanes (trusted test executor)
            → readonly review
              → human acceptance
```

Credentials **never** appear in RunSpecs, artifacts, or the evidence
chain. They are referenced by broker-owned IDs (the
`CredentialBinding.credential_ref` is a name, never a value). The
lane's harness keys are CI variables scoped to the pipeline; the coding
agent never receives forge's own credentials.

## 7. The decision record: retaining the Postgres controller

**Decision**: retain the current Postgres controller (ADR-0004/0005
state machine) for the adaptive workflow. Do not introduce Temporal,
LangGraph, or a new authoritative workflow engine.

**Rationale**:
- The controller already provides the primitives the adaptive workflow
  needs: guarded CAS transitions, durable steps, fenced claims,
  publication intents, and the revival/recovery machinery.
- A second authoritative engine alongside the existing one creates more
  migration/recovery work than benefit for the first customer slice.
- Temporal message passing is a useful **pattern reference** for
  durable commands (the Mailbox follows it), but it does not solve
  repository scope, source identity, permissions, or reconciliation of
  external writes — those are forge's authority boundary, not the
  engine's.

**Criteria for later evaluation** (when ANY of these is observed):
- Sustained >50 concurrent active runs with measurable lock contention
  on the `flow_runs` table (Postgres-level).
- A requirement for cross-process long-running sagas that the
  publication-intent + epoch model cannot express.
- A provider integration that demands a Temporal-native workflow.

## 8. Doctor checks

`python -m forge.doctor` verifies the environment (read-only; never
mutates permissions or creates infrastructure):

| Check | What it proves |
|---|---|
| `gitlab.token` / `forge.bot_token` | The provider credentials work |
| `redis` / `database` / `litellm` | The infrastructure is reachable |
| `harness.lanes` | Per-driver credential variables are present |
| `azdo.*` | The Azure DevOps lane is configured |
| `credential.legacy_deadline` | The legacy-credential window's anchor, deadline and drain count (Q35-06) |
| `checkpoint.migration_coverage` | Not-yet-migrated works while the postgres authority is active (Q35-21) |
| `checkpoint.pointer_conflicts` | JSON/DB active-pointer disagreements → operator resolution, never timestamp selection |
| `checkpoint.authority_marker` | The cutover marker's authority + generation, the configured repository, mismatch → the two remedies (R36-05) |
| `checkpoint.blob_topology` | The blob root does not look node-local (heuristic warning; a shared DB does not share blobs) |

**The adaptive doctor additions** (in the substrate):
- `CapabilityMatrix`: unknown profiles fail closed (not advertised)
- `DriverMatrix`: unregistered SDK/route/mode combinations fail at onboarding
- `privileged_ok`: the Docker socket belongs to the trusted test executor alone

## 9. Versioned and tested examples

Every code example in this runbook is either:
- **Versioned and tested** (the pydantic models under
  `src/forge/adaptive/` parse the review package's own examples
  verbatim in `tests/test_adaptive_contracts.py`), or
- **Clearly labeled illustrative** (the YAML snippets in this document
  are documentation, not test fixtures).

The release manifest (`python -m forge.release_manifest`) distinguishes
declared coverage from executed evidence: a file containing a finding
ID cannot alone close that finding.
