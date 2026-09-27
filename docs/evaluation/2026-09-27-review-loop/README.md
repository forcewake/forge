# R41-09 (#364) — the COMPLETE review-and-correction loop, live on the current profile

The headline live qualification of this cycle: ONE real GitLab CE profile,
the full native arc — delivery to `ready_for_human`, a human edit, a native
`/fix` review round from the exact current head, the corrected candidate on
the SAME merge request, the independent oracle on the exact new candidate,
the closing reviewer briefed from the active correction, the replay
idempotence, a SECOND distinct correction, a controlled worker failure at
publication with the #358 recovery, and the conflicting-head negative arm.

**Record:** `qualification/records/review-loop-2026-09-27.json` (outcome
**pass**, zero validation findings) · **Bundle:** `live-run-evidence.json`
(every phase, every identity, the honest failures) · **Alignment:**
`alignment-receipts.json` · **Lane traces:** `traces/`.

## The credential route (the window's first question)

Both routes probed at the REAL model gateway (one 8-token call each,
statuses + digests only — never values):

| Route | Token sha256-16 | Answer | Verdict |
| --- | --- | --- | --- |
| (b) gitlab-protected-variable (`FORGE_MODEL_ENV_ANTHROPIC_AUTH_TOKEN`) | `800223712be70ebe` | **200**, glm-5.3-flash answered | **ALIVE — the loop ran under it** |
| (a) runner-redemption (the broker-held `ANTHROPIC_AUTH_TOKEN`) | `6e3098b52d8991dc` | **401** `token expired or incorrect` | dead for models — the typed blocked evidence, zero model spend on it |

The whole trace ran under route (b) — the v0.39.0 trace's route, an
equally qualified delivery mode the profile record names. The broker
rotation stays the operator item it was (the record's refusal_resolution).

## The composition

| Half | Identity |
| --- | --- |
| Control plane / worker | the working tree rebuilt mid-window — image `sha256:f6ff6308…` (was `11c4bb30…`), reports **0.41.0**, schema head **032** — the rebuild carries EXACTLY the live-found publisher patch (below), the #365 precedent |
| Lane package | `forge-0.41.0-py3-none-any.whl` @ sha256 `223d0f25…` (was `2616d221…`) — sha256-verified INSIDE the lane job before install, echoed by the lane's own trace marker |
| Template | the shipped `claude-sdk-lane` template VERBATIM (committed, included BY local include); the ONE override is the wheel-ladder install seam |
| Alignment pins | `FORGE_CREDENTIAL_DELIVERY=gitlab-protected-variable`, `FORGE_REVIEW_FEEDBACK_ENABLED=1`, `FORGE_MAX_REVIEW_ROUNDS=3`, `FORGE_BUDGET_PROFILES` (standard @ 600k tokens — the receipted policy adjustment, below) — on BOTH consumers, the align_lab verify-only receipt in `alignment-receipts.json` |

## The trace (the qualifying lineage: issue #5, MR !4, branch `factory/5/7319478e`)

1. **Delivery 1** — run `7319478e…` → `ready_for_human`: the real-model SDK
   lane, the candidate `6e973202…`, the Draft MR, the precommitted oracle
   (the six exact slugify cases + the three shapes, a pytest suite run by
   the `smoke` CI job) green on the exact sha, the closing review bound to
   the sha. The parent is the ORDINARY classic run — NO manual PlanRevision
   was ever staged (the evidence carries no `active_plan` pointer; the
   round derives through the classic adapter). $0.2112.
2. **Round 2** — the human edit lands first
   (`tests/test_round2_contract.py` — the correction's behavioral contract,
   red on the old head, commit `3878456c…`), then the native `/fix` note
   1497 on the MR → the budgeted CHILD run `bbd8d0d4…` admitted with
   `base_head_sha` = the human-edit head (the dispatch rode the exact
   current head, human edit included), its OWN finite budget
   (600k/40calls/3600s with `closing-partition/1`: 6 calls / 90k tokens
   reserved) → the new candidate `f72223ce…` on the SAME MR → the oracle
   green on the exact new candidate (pipeline 819) → the closing reviewer
   briefed from the SAME approved-input join as the executor (the recorded
   obligation digest EQUALS the recomputation through
   `resolve_approved_input` + the required-request filter — the correction
   text is in the reviewer's brief) → the readiness gate held the child
   until the REVIEWER resolved the discussion (a human action, `forcewake`)
   → `ready_for_human`. $0.1980.
3. **Replay** — the SAME note redelivered through the real webhook surface
   (a fresh delivery uuid, the same note id — GitLab's retry shape):
   **zero** new requests, round rows, pipelines, provider commits.
4. **Round 3 (the second distinct correction + the controlled worker
   failure)** — note 1508 (the docstring-invariant correction) → round 3,
   child `17645ea9…`, its own budget; the driver watched the branch and
   KILLED the worker (`podman stop -t 0`) the moment the round's OWN
   provider commit `7008c940…` landed — the child was in
   `ensuring_draft_mr` (post-commit, pre-bookkeeping) — then restarted it:
   the #358 recovery adopted the round's own effect (exactly ONE provider
   commit for the round across the whole trace), completed the bookkeeping,
   the oracle green on the exact candidate (pipeline 822), the reviewer
   resolved, `ready_for_human`. $0.1969.
5. **The negative arm** — a third `/fix` (round 4, child `2396987b…`)
   dispatched, then a CONFLICTING human commit (`f9e17e25…`, touching
   `src/utils/text.py`) landed mid-lane: the writer's guarded apply refused
   the typed **`branch_drift`**, the child parked `blocked`
   (`candidate_rejected: branch_drift`), the round row settled `ended`
   (the lineage's outstanding-round slot freed), the human commit stayed
   the branch head — preserved, never reverted, ZERO candidate commits
   from the round.

**Provider commit count across the trace:** exactly one bot commit per
publication effect (delivery 1, round 2, round 3) — never two; the
negative arm produced none.

**The bot never merged, never resolved a discussion, never deployed** —
the discussion resolves are the REVIEWER's (human) actions recorded above.

## Spend (all-attempt coverage)

| Leg | USD |
| --- | --- |
| Delivery 1 (qualifying) | 0.2112 |
| Round 2 lane | 0.1980 |
| Round 3 lane | 0.1969 |
| Negative arm lane | 0.2394 |
| **Qualifying total** | **0.8455** |
| Failed attempt `fbe62ad5` (the budget fence — below) | 0.2229 |
| Failed attempt `8be14a80` (the empty-scope classification — below) | 0.1876 |
| Failed round-2 lane `6e0fdf34` (the publisher defect — below) | 0.2237 |
| **All-attempt total** | **1.4797** (cap 2.50) |

Every lane job's own SDK receipt (`candidate.meta.json`), values never in
the bundle — digests and statuses only.

## LIVE-FOUND this window (all recorded in the record and the bundle)

1. **A real defect in the #359 landed code — found by this trace, patched,
   suite-green.** The harness publication path passed NO branch to
   `publish_candidate`, so a review round's child committed onto
   `factory/<issue>/<child8>` — a SECOND branch beside the lineage's
   collaboration surface — and the (correctly target-resolving) drift check
   blocked the run `external_change` (run `6e0fdf34…`, wrong-branch commit
   `c75f66e0…`). Root cause: `publisher.py::publish_candidate` had no
   branch parameter and `_adopt_harness_change` passed none — the ONE
   branch-consuming leg still deriving from the run id. The minimal patch:
   `_adopt_harness_change` resolves `_collaboration_branch_or_block(run_id)`
   (typed block on a refused target, zero provider calls) and threads
   `branch=` through; callers passing nothing keep the legacy derivation.
   **The FULL suite stayed green with the patch: 9016 passed, 75 skipped
   (the baseline).** The patched tree was rebuilt (wheel `2616d221…` →
   `223d0f25…`, image `11c4bb30…` → `f6ff6308…`) — the #365 precedent.
2. **The budget fence, observed live** (run `fbe62ad5…`): the claude-code
   harness fills a ~200k-token context regardless of task size
   (196,312/200,000 consumed on the standard profile); the finite budget
   EXHAUSTED at the token axis and the closing review stood down with zero
   reviewer spend — exactly the designed fence. The receipted resolution:
   the deployment's standard profile moved to 600k tokens (STILL finite,
   numerical, enforced; the planner's output schema carries no
   `budget_class`, so the compiler default always applied).
3. **The empty-scope fail-closed classification** (run `8be14a80…`):
   without the target repo's `.forge.yml implement.paths`, the frozen
   spec's `allowed_paths` is EMPTY and EVERY `/fix` classifies
   `material_change` — fail-closed by design (the honest reply landed, MR
   note 1474). The repo-side scope declaration is mandatory onboarding for
   the review-loop surface.
4. **Protected-variable visibility** (run `165dd1ed…`): a PROTECTED GitLab
   CI variable reaches only protected refs; the factory branches are not
   protected, so the lane failed CLOSED at its bootstrap fence (typed
   `FORGE_BOOTSTRAP_FAILED`, zero model calls). The carrier is masked, NOT
   protected, on this lab; deployments dispatching on protected refs can
   keep the protected posture.
5. **The doctor's lane check is not delivery-mode aware**:
   `check_harness_lanes` matches `DRIVER_CREDENTIAL_VARS` names only, so
   under the native route it cannot see the `FORGE_MODEL_<SEGMENT>` carrier
   and fails `project.harness_chain`. The SAME value is additionally
   provisioned under the ambient name so the app's own doctor stays green.
   Minimal patch proposal (mode-aware carrier matching) recorded in the
   record; NOT patched this window — the composition identity stays frozen.
6. **The MR document's `sha` field lags a just-pushed commit by seconds**
   — the driver reads the branch head directly.

## Reproduce

```bash
uv run python scripts/run_review_loop_qualification.py probe
uv run python scripts/run_review_loop_qualification.py align
uv run python scripts/run_review_loop_qualification.py setup
uv run python scripts/run_review_loop_qualification.py preflight
uv run python scripts/run_review_loop_qualification.py delivery
uv run python scripts/run_review_loop_qualification.py round2
uv run python scripts/run_review_loop_qualification.py replay
uv run python scripts/run_review_loop_qualification.py round3
uv run python scripts/run_review_loop_qualification.py negative
uv run python scripts/run_review_loop_qualification.py collect
uv run python scripts/run_review_loop_qualification.py teardown
```

Phases are resumable (the bundle on disk is the state); every refusal is
recorded honestly, never retried into a green. The disposable project
(`forge-review-loop-2026-09-27`, id 160) is deleted after capture — all
identities live in the bundle and the record.
