# R38-12 (#313) — Combined steering: native command → real SDK lane → material revision (live)

**Date:** 2026-09-25 · **Issue:** [#313 / backlog R38-12](https://github.com/forcewake/forge/issues/313)
**Artifacts:** `evaluation/steering/combined-2026-09-25/` (`combined-trace.json`, `report.json`,
`live-run-evidence.json`) · offline pins: `tests/test_combined_steering.py`
**Driver:** `scripts/run_combined_steering.py` (resumable phases; the evidence bundle is the state)
**Verdict: `causal = true` — all five graded arms PASS · spend $1.1651 of the $2.00 cap**

## 1. What was composed (and why this run exists)

R37-10 (#291) proved the two halves separately: the scripted process-level trace
(commands + revisions through the real machinery, `tests/production_entry/test_causal_steering.py`)
and a real-model conversation pair (`evaluation/steering/live-run.json`, $0.0091). R38-12
demands the COMPOSITION on ONE live run: a **native operator command** consumed by the
**actually-running claude-sdk lane** driving the **real model** (litellm `fast` =
glm-5.3-flash), an **observable next-action change**, **useful WIP**, a **material
revision** through its native approval, and the **actual next executor input** carrying the
revised identity over the preserved WIP.

Everything ran on the aligned lab (the #306 receipts; verified read-only via
`scripts/inventory_lab.py` — its two mismatch axes, the promoted-image digest pin and the
lab project's lane-template pin, are recorded verbatim in the bundle's preflight; the axes
this drill depends on — app version == repo 0.37.0, schema head 027, budget caps,
`FORGE_ADAPTIVE_COMMANDS_ENABLED=1`, online runner — all refused pre-paid had they failed).
The disposable project `forge-steer-2026-09-25` (id 96) was created, seeded and **deleted
after capture**; #307's projects were never touched; no `align_lab` re-ran.

## 2. The task and its decision point

- **Approach X (the plan):** the issue asks for email validation in
  `src/validators/email.py` whose public entrypoint is `check(email)` (plus the bulk
  `validate_many`). The frozen base ships a stub `def check(...)` — approach X's shape.
- **Approach Y (the steer):** mid-work, the operator's native `/steer` redirects the
  entrypoint to `validate_email` — an independently checkable transformation the grader
  parses from the instruction itself (`entrypoint validate_email (not check) in
  src/validators/email.py`).
- **The oracle (precommitted):** the project's `smoke` CI job asserts the exact
  semantic cases (either spelling must satisfy the SAME assertions — steering can choose
  the name, never the semantics) plus the bulk scope; the driver additionally asserts the
  candidate diff never touches `tests/`, `.gitlab-ci.yml` or `README.md`.
- **The lane:** the SHIPPED `ci/templates/claude-sdk-lane.gitlab-ci.yml` VERBATIM (the
  #302 finalization), pinned to `FORGE_LANE_REF=59ba869…` (the #306 immutable install
  pin), with the project variables `FORGE_STEERING_ENABLED=1` and
  `FORGE_CLAUDE_DRAIN_TIMEOUT=3` — the documented driver knob that fires the steer's
  follow-up INTO the running turn instead of queueing behind the 300 s drain.

## 3. The composed trace (cycle 2 — the qualified run)

Run `c3236a3e9314452e934a60e6c6db668c`, issue #2 of the disposable project. Every milestone
is a separate durable record with its causal identifier (`combined-trace.json →
milestones`):

| Milestone | Evidence | Identity / timing (one DB clock unless noted) |
|---|---|---|
| issue → /implement → /go | plan note, dispatch ledger | run `c3236a3e…`, lane job 782, driver anchor `00:39:0xZ` (job trace) |
| **the native /steer** | GitLab note → `control_commands` row | note posted `00:39:44.92` (driver host) → row `cmd-04d388ce4c96` **received** `00:39:45.31` (ack ≈ 0.4 s) |
| authorization + CAS delivery | row's own audit journal | `authorized` `00:39:45.68` → **`dispatching`** `00:39:45.75` (the rung the vendor effect is gated on) |
| **vendor application (the real SDK)** | lane's steering journal (candidate meta artifact) | `application_observed` `00:39:48.83` — **delivered `mid-turn`**, 3.07 s after the CAS rung |
| **the subsequent artifact** | the pause checkpoint transaction | checkpoint `8c8fafe4e8e6…`, `files=1`, uploaded `00:40:36` — its blob carries `def validate_email(` and **no** `def check(` |
| **urgent-pause interleaving** | rows `cmd-0d8660b50d56` (steer, seq 2) + `cmd-3a25c458e5b1` (pause, seq 3) | both climbed the FULL ladder to `checkpointed`; the pause's interrupt was **acknowledged in 0.001 s** with a verified checkpoint receipt; the queued guidance's fate is on record (consumed, not dropped) |
| job-level cancel + blocked | job 782 cancel, run classification | `blocked (harness_code)` — never a false applied ack |
| **the material revision** | staged via the app's own `revisions` module inside `forge-app`; **approved natively** | decision `dec-combined-c3236a3e9314`; `/approve-revision` note → the REAL activation transaction |
| three-way digest equality | recomputed == staged == `active_plan` == `flow_runs.plan_digest` | all `19a92667bb5f…`; the row's digest was the run's REAL `/go` digest `6520b670…` before the approval and switched **exactly at it** |
| WIP reuse decision | `checkpoint_reuse_decision` evidence + outbox | route **`preserve`** ("every write-carrying step survived revision 2 byte-for-byte"), preserved checkpoint `8c8fafe4e8e6…` |
| **the next executor input** | worker journal `gitlab.dispatch_envelope_digest` | `resume=required`, checkpoint `8c8fafe4e8e6…`, decision `3b30d6f20901`, envelope `61fe471ea856` |
| the standing direction | note 557 → row `cmd-b2ff9c86b04f` | posted while blocked (PENDING); the resumed lane's drain delivered it **mid-turn** (`00:42:30`, lane journal) and climbed to `checkpointed` |
| **the final candidate** | Draft MR !2 | green oracle pipeline 440 on candidate `2d21d6159719`; the file shows `def validate_email(` + `validate_many`; only `src/validators/email.py` touched; **Draft, never merged** |

### The grader's verdict (`forge.adaptive.steering_causality.grade_combined_trace`)

| Arm | Verdict | The durable reason |
|---|---|---|
| `ack_precedes_edit` | **PASS** | ack `00:39:45.31` → durable applied `00:39:45.75` → vendor application `00:39:48.83` (`mid-turn`), then 1 later checkpoint |
| `counterfactual_differs` | **PASS** | the same task without the steer produced a different edit set (issue #3, lane job 798, MR !3 — `def check(` kept) |
| `target_matched` | **PASS** | the final artifact shows `check` swapped to `validate_email`, and the unsteered arm did not do it |
| `revision_identity_switched` | **PASS** | recomputed == staged == active-plan == durable-row digests (`19a92667bb5f…`); the row switched exactly at the approval |
| `wip_preserved_and_redispatched` | **PASS** | the activation routed the checkpoint to `preserve`; the post-revision dispatch is a `required` resume over exactly it (envelope `61fe471ea856…`) |

## 4. Honest findings (the record keeps them all)

1. **Cycle 1's revert (the driver's real continuation semantics).** Cycle 1 (run
   `e18fb442…`) proved everything above — its checkpoint `6b7653313a54` carried the Y
   rename, its revision activated with route `preserve`, its post-revision dispatch was
   the required-resume envelope `20d6944ac002` — but the **final candidate reverted to
   `def check(`**: the GitLab lane's brief is **spec-frozen at /go** (it still names
   approach X), and the resumed model obeyed the brief over the restored WIP's naming.
   The steer's causal effect on the turn's work stood (the checkpoint blob proves it);
   the continuation lost it. Recorded as `honesty.cycle1_revert_finding` with the
   full prior-cycle grade beside cycle 2's.
2. **The remedy is native, and cycle 2 composes it.** The operator's channel for a
   direction that must outlive a continuation on THIS driver is the durable guidance
   itself: a steer posted while the run is blocked sits PENDING, and the resumed lane's
   drain delivers it mid-turn (the control plane's pending view serves undelivered rows
   to the continuation's cursor). Cycle 2's standing-direction steer
   (`cmd-b2ff9c86b04f`) was delivered `mid-turn` by the resumed lane at `00:42:30`, and
   the final candidate kept Y. No guard was weakened: the revision gate still owns scope
   (an `amend`-class steer is still refused), the oracle still enforces the same
   semantics for either spelling, and the MR stays Draft.
3. **The staging leg is a seam, labelled.** The live GitLab planner does not yet emit
   plan revisions, so revision 1 (active, carrying the run's REAL `/go` digest) and
   revision 2 (pending — the operator's scope extension: the `src/validators/__init__.py`
   export surface joins the plan as a new step, every existing step byte-identical) were
   staged through the REAL `stage_pending_revision` inside the `forge-app` container.
   The **approval** (`/approve-revision` through the real ingress) and the **activation**
   (the CAS transaction: digest switch, gate rebind, reuse decision, outbox rows) are
   fully native. The PE-7 brief-rebind leg remains GitHub-path machinery; on this driver
   the revision's TEXT cannot re-enter the frozen brief — only its identity does (see
   finding 1).
4. **The urgent-pause interleaving.** The ordinary steer (seq 2) and the urgent pause
   (seq 3) both climbed the full ladder to `checkpointed`; the pause's interrupt was
   acknowledged in 0.001 s and its checkpoint transaction verified — the pause was never
   blocked by the queued guidance, and the guidance's fate is a durable row, not a
   dropped message.
5. **Budget honesty.** Both cycles' runs ended `blocked (budget_exhausted)` at the
   REVIEWER leg (the lane legs consume ~100 k tokens each against the 200 k run budget);
   the MRs, their green oracle pipelines and the Draft state all stand — recorded, never
   retried into a green.
6. **Infra iterations (all recorded in `failures`):** one transient litellm health flap
   during the first preflight (re-ran clean), one staging-program import fix, one
   dispatch-tracking fix (a per-phase pipeline filter briefly adopted the branch's OLD
   /go pipeline), and the worker journal's 12-char checkpoint-id spelling (accepted as a
   prefix of the full content address, with the lane trace's full-id echo recorded beside
   it).

## 5. Spend

| Lane leg | Job | SDK `total_cost_usd` |
|---|---|---|
| cycle-1 steered lane | 775 | $0.2467 |
| cycle-1 resumed lane | 779 | $0.2920 |
| cycle-2 steered lane | 782 | $0.2500 |
| cycle-2 resumed lane | 784 | $0.1961 |
| counterfactual lane | 798 | $0.1803 |
| **total** | | **$1.1651** (cap $2.00; guard $1.60) |

All figures are the SDK's own `total_cost_usd` receipts from the lane artifacts
(`cost_basis: sdk-total_cost_usd`) — estimates against the lab rate card, never billing
records.

## 6. Reproduction

```bash
uv run python scripts/run_combined_steering.py setup
uv run python scripts/run_combined_steering.py preflight
uv run python scripts/run_combined_steering.py steer            # cycle 1
uv run python scripts/run_combined_steering.py revision
uv run python scripts/run_combined_steering.py resume
uv run python scripts/run_combined_steering.py steer --cycle 2  # the standing-direction cycle
uv run python scripts/run_combined_steering.py revision --cycle 2
uv run python scripts/run_combined_steering.py resume --cycle 2
uv run python scripts/run_combined_steering.py counterfactual --cycle 2
uv run python scripts/run_combined_steering.py collect
uv run python scripts/run_combined_steering.py teardown
```

Every phase is resumable and REFUSES on any precondition failure (the failure is
recorded, never retried into a green). The combined grader and the record schema are
pure and pinned offline by `tests/test_combined_steering.py`; the R37-10 grader and the
production-entry traces are untouched and still green.
