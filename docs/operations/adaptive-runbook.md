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
Re-verify after vendor upgrades:

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
