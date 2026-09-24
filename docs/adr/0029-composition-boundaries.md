# ADR-0029: Composition boundaries — the five names, the owner map, and the matrix that records what may compose with what

Status: accepted (types + ADR + compat fixtures only — no service rewiring in this slice) (2026-09-23)

Context: review finding R32-24 — "what may compose with what" is
currently enforceable only by review vigilance. The three provider
services (`runs/service.py` GitLab ~5.6k lines, `runs/github_service.py`
~6.6k, `runs/azure_service.py` ~5.8k) each hand-assemble the same
composition facts — which repository, which attempt, which resume
decision, which execution contract — at every dispatch entry, and the
ADR-0027 ladder has extracted exactly one rung so far
(`ObserveVerification` → `runs/usecases.py`; `FinalizeEvidence` →
`runs/consistency.py` before it). The repeated defect class is identity
interchange: a provider family where a connection id was expected, a run
id where an attempt oid was expected, a "latest" checkpoint where an
EXACT approved checkpoint reference was required. The concept comments
already beg for types — `ResumeSpec` is named in five modules
(`api_lane_control.py`, `lane_driver.py`, `checkpoint_channel.py`,
`adaptive/wiring.py`) but exists only as dict payloads and docstrings;
`RepositoryContext` and `AttemptStartSpec` appear nowhere.

The e2e-qualification research (topic 6 §1–2, topic 3 §2) supplies the
enforcement shapes, not just the vocabulary: compatibility as
**directional matrix edges** (verified / untested / unsupported —
"v8 is blocked until v3 ships and v8 adapts; the gate stays consistent
in both directions"), **can-i-deploy as a query over recorded edges**
(never a re-run), **expand-contract** (add alongside, migrate visibly,
remove only after verified-zero usage), and **inventory drift as a
finding** (a shipped combination with no qualification evidence in scope
reads as loss of control, never as fine). This slice does NOT replace
the workflow engine or extract from the services — it names the
boundaries, freezes their types, and pins the compat-fixture contract
the expand-contract rule needs. Extraction candidates are listed in §6.

## Decision

### 1. Five composition boundaries, each ONE immutable value

**`RepositoryContext`** (`forge.runs.composition`) — provider family +
connection identity + repository identity in one frozen value. The
family is the `FlowRun.provider` vocabulary (`gitlab` | `github` |
`azure_devops`, migration 012); connection/repository identities may be
bare or family-qualified (`github:{owner}/{repo}`,
`azure_devops:{org}:{project}` — the convention the gateways and
findings ingestion already write), and a qualified identity whose family
disagrees with the declared family is refused at construction
(**cross-family mixing is a construction error, not a runtime surprise**).
Provider family, connection, run and attempt identities are NEVER
interchangeable: every one of them is a distinct field of a distinct
type, and the only coercion between them is a refusal that names the
field.

**`AttemptStartSpec`** — the frozen envelope every dispatch entry must
construct BEFORE any external effect: run id, attempt oid,
`RepositoryContext`, execution profile digest, resume mode, lease
identity. This is ADR-0028 §2 (intent-first dispatch identity) as a
type: the envelope is the intent, `assert_attempt_start` is the
pre-effect check, and **permissive defaults are forbidden** — a missing
field is a `TypeError` naming the field, never a defaulted guess. The
execution profile digest is the A18 pairing (`spec.execution_profile`
vs the lane's observed echo) reduced to one comparable member of the
envelope; the lease identity is the `execution_leases` row (migration
023) the dispatch holds.

**`ResumeSpec`** — the documented concept, formalized. Resume mode
(`fresh` / `required` / `restart` — NEXT-03's three DISTINCT dispatch
modes), checkpoint reference (`<work_id>@<checkpoint_id>`, the durable
form `format_checkpoint_ref` writes), generation (the checkpoint
sequence, R28-06's arrival-order-is-not-authority rule), and authority
(the durable control-command identity that decided the resume). A
`required` spec without a well-formed exact reference is refused; a
`fresh`/`restart` spec WITH one is refused — the modes are distinct
dispatch shapes, not hints. A pre-NEXT-03 resume payload (no reference)
parses only as the explicitly labelled legacy fallback, exactly as
`lane_driver._maybe_restore_wip` documents.

**Execution authority** — the owner map. ONE documented owner for every
durable decision and every external effect; a second owner of the same
effect is a defect even if both copies agree today:

| Decision / effect | Owner module | Invariant |
|---|---|---|
| dispatch / admission | `adaptive.admission` + `execution_leases` (mig. 023) | one CAS lease per `(project, provider, slot)`; no dispatch without a held lease (the `AttemptStartSpec` lease identity) |
| publish (commits, MRs, notes) | `adaptive.publication_saga` + `runs` publisher policy (ADR-0016/0026) | intent persisted before effect; `uq_create_mr_per_branch` (mig. 018) + `mr_reservations` (mig. 019) arbitrate; one validated write boundary |
| verification verdict | `runs.usecases.observe_verification` (+ `runs.verification` for GitLab) | positive proof over the FROZEN `required_jobs`; unified R02 verdict vocabulary; WAIT is never a verdict |
| control (pause/steer/resume) | `adaptive.command_router` + `adaptive.pause_fence` | commands are authenticated-then-work-scoped; the pause fence is ONE persisted row both sides read; the resume decision is the durable command row |
| finalization evidence | `runs.consistency` (ADR-0027 slice 1) | one reason table, one evidence shape, iron checks raise `ReadyInvariantError` |
| checkpoint retention / active selection | `checkpoint` store (`api_checkpoint_channel`, `checkpoint_metadata` mig. 026) | content-addressed identity; `(sequence, checkpoint_id)` ordering; active selection derived, never stored as a second authority |

**`ToolObservation` ownership** (NEXT-08) — produced by research tools
under frozen snapshots, **owned by `adaptive.research_planner`** (the
type's home), and consumed via recorded traces. Producers never
fabricate content: an observation is what a tool ACTUALLY returned
(`content`) or its real error (`error`), `truncated` says whether the
model saw a cut window, and the observation is deliberately separate
from `ResearchFinding` (the citation anchor). No other module constructs
one; anything needing the feedback channel reads the recorded trace.

### 2. `CompositionMatrix` — composition permission is recorded evidence, not review memory

Directional edges between boundary versions/combinations
(`from_key` depends on `to_key`): `verified` (ONLY via
`register_verified` with an evidence reference — a promotion/CI record
id), `untested` (the honest default, including unregistered pairs), or
`unsupported` (explicitly recorded refusal). `blocked_by(key)` is the
can-i-deploy lookup: a QUERY over recorded edges that names every
non-verified edge touching the key — the same edge found from either
endpoint, so the gate is consistent in both directions (a consumer not
verified against its provider is the provider's blocked-by finding too,
which is how "v8 is blocked until v3 ships" surfaces without re-running
anything). `matrix_drift(shipped, matrix)` is inventory drift as a
finding: a shipped combination with no verified path in the matrix is
listed, never silently passed — the doctor check the research names.

### 3. Expand-contract for every boundary in this list

Interfaces are versioned (`run_specs.schema_version`, the
`forge.proposal.control-command/1` literal, `EXECUTION_PROFILE_SCHEMA_
VERSION`); additive fields are optional-with-validation (the R13/A13/A18
precedent in `runs/spec.py`); **removal happens only after
verified-zero usage** — a matrix edge that still names the old shape
blocks the removal, and the registry (the matrix) shows WHO uses WHAT.
Old document versions stay READABLE through the compat fixtures below
until their matrix edges are gone.

### 4. The compat-fixture contract (`adaptive/compat_fixtures.py`)

Every shipped document version has a versioned fixture — a canned honest
document plus its SUPPORTED read/recovery semantics:
`run_spec` v1 (Stage B2 digest-only, GitLab-only forge),
v2 (multi-provider digest-only, ADR-0023 harness selection — the
documents `SpecLegacy` exists for: recovery is
`blocked(spec_legacy: re-approval required)`, never a best-effort v3
read), v3 (executable, parses via `ExecutableRunSpec.from_document`);
`checkpoint_metadata` v1 (the pre-migration-026 per-work filesystem
index; recovery is the documented no-backfill/re-upload contract);
`control_command` v1 (pre-NEXT-03 resume row, empty payload — recovery
is the labelled active-checkpoint fallback) and v2 (NEXT-03 row carrying
the ResumeSpec payload). `load_compat_document(kind, version, payload)`
returns the parsed view or raises `UnsupportedDocumentVersion` — a
silent best-effort parse of an unknown shape is forbidden;
`compat_inventory()` lists kind×version so drift between shipped
fixtures and the matrix is detectable.

### 5. Import boundary

`forge.runs.composition` is core: it imports no `forge.integrations.*`,
no `forge.gateway.*` (the ADR-0027 §3 rule, applied at creation). The
services adopt these types one dispatch entry at a time; until a service
constructs `AttemptStartSpec`, the type is the contract, not a
requirement — no service signature changes in this slice.

### 6. Extraction candidates (NOT executed here — the ADR-0027 ladder's next rungs)

Named application services, each "extract invariant → rewire three
services → pin with a cross-provider identical-output test", ordered by
how much of the repeated defect class it closes:

1. **`StartRun`** (plan/gate/freeze leg) — the spec-freeze + gate-leg
   duplication (ADR-0027 slice 4); emits the `AttemptStartSpec` envelope
   naturally.
2. **`DispatchAttempt`** — the `_advance_harness` twins: lease take,
   intent-first journal (ADR-0028 §2), envelope construction and
   `assert_attempt_start` before the provider call.
3. **`ObserveVerification`** for GitLab (joining the A01 twins at the
   verdict level it already shares) — the pipeline-gate decision core in
   `runs/verification`.
4. **`ContinueRevision`** (the repair/re-plan cycle) — commit-cycle
   budget + frozen-input re-entry.
5. **`ResearchStep`** — the discovery/research loop around
   `research_planner` + `ToolObservation` traces (multi-provider only
   where the lane drivers already share shape).

`PublishCandidate` stays consolidated (ADR-0016/0026 — the template).

## Consequences

- The identity-interchange defect class gets a construction-time fence:
  the wrong id in the wrong slot cannot become a `RepositoryContext`,
  `AttemptStartSpec` or `ResumeSpec` — it raises, naming the field.
- Compatibility claims become auditable: a `verified` edge without an
  evidence reference cannot be recorded, and demoting a verified edge
  requires new version keys (expand-contract) rather than an in-place
  flip — qualification is never silently lost or gained.
- Shipped-but-unqualified combinations are a list, not a lurking
  surprise: `matrix_drift` is the doctor check R32-24 names; wiring it
  into `forge doctor` is follow-up work (it needs the shipped-inventory
  source first).
- The fixtures freeze today's honest recovery semantics as executable
  tests — when a migration retires a document version, the fixture's
  removal IS the verified-zero-usage checkpoint the matrix must show
  first.
- No behavior change ships in this slice: nothing imports the new types
  on the production path yet, so the full suite's 5462-green baseline is
  the proof the slice is additive.

## Amendment — 2026-09-23, R36-20 (ADR-0030): the owner map is now a registry and a fence

The §1 owner-map table above was PROSE; every R36 sibling since
(#260–#281) landed one owning module per decision, and ADR-0030
consolidated the result WITHOUT another extraction wave:

- the owner map is now machine-readable
  (`forge.adaptive.boundary_registry`) and mechanically enforced over
  the production tree by `tests/test_architecture_boundaries.py`
  (static import/call-graph checks plus negative trap tests) — a module
  entering an owned decision without registration fails CI with an
  instruction, and a stale registration fails too;
- the compat-fixture contract (§4) grew the schemas the boundaries now
  persist: `attempt_start` v1/v2, `continuation_decision` v1/v2 and
  `checkpoint_lookup` v1/v2 — additive entries in the same
  `compat_inventory()` surface;
- the measured end state (duplicated decisions removed, counted, not
  module count) is recorded in ADR-0030 §5.

The five names, the matrix and the expand-contract rule are unchanged;
this amendment adds enforcement and inventory, not new abstractions.
