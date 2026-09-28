# R42-04 (#377) — the FINAL-candidate re-qualification

The first candidate (wheel `e7b48d42…`/image `98ea9031…`, manifest
`docs/evaluation/2026-09-28-r4204-build-once/candidate-manifest.json`)
was qualified at commit `9c20674`. Three review commits then landed
inside the release window and ship in the wheel — #380 the
command-to-delivery progress view (`ae14758`), #379 the budget
calibration (`96c2a65`), #389 the authority consolidation (`e305478`).
Per R42-04's own rule ("a changed candidate is a NEW candidate until
re-qualified") the FINAL candidate was minted at `e305478` and the
affected live trace re-run in full. The first candidate's record stays
under its own identity
(`qualification/records/review-loop-2026-09-28-c1-e7b48d42.json`,
legacy-marked superseded history); its evidence bundle is byte-identical
under `2026-09-28-r4204-build-once/` — never overwritten.

## 1. The candidate (built once, digests recorded BEFORE qualification)

`candidate-manifest.json` is the pre-qualification receipt:

- **Wheel**: `forge-0.42.0-py3-none-any.whl` @ sha256
  `ff1d0769ea6b0d127196665a56a15996bf24e37e00337afb322fbbb91708802a`
  — the single `uv build --wheel` of the final tree (`e305478`,
  2026-09-28T06:17Z).
- **Image**: `localhost/forge:dev` @
  `sha256:ad769a4af2fd0d7539a4c5d7c828a6697060c9c6d64d2e6c2469890ef87cf689`
  (id `a58ab381…`, reports 0.42.0, schema head 032, rollback tag
  `pre-r3708-20260928T061811Z`) — `scripts/align_lab.py --apply` built
  it from the SAME tree. The version string alone could not force the
  rebuild (the old candidate also reported 0.42.0), so the operator
  stopped the consumers first and the full receipted path ran:
  backup, rollback-tag, build, stop, migrate (032→032), recreate,
  verify (`alignment-build-receipts.json`; the document also keeps the
  first verify-only observation run — honest, both attempts recorded).
- The wheelhost (:8481) served exactly these wheel bytes; every lane leg
  sha256-verified them in-job before install (the pairing marker in each
  trace, including the halted resume-negative trace).

## 2. The trace (the same driver, re-pinned to the final candidate)

`live-run-evidence.json` carries the per-phase receipts (values never;
digests and ids only). The full narrative:

- **probe** — both credential routes at the real gateway: the
  protected-variable carrier ALIVE (200), the runner-redemption broker
  token DEAD (401 `token expired or incorrect`, typed). The model legs
  rode the carrier.
- **align** — the consumers verified on the final image with the window
  pins (credential delivery, review feedback, max rounds 3, the
  receipted 600k standard budget ceiling); the wheelhost re-staged to
  the new wheel; `align_lab --apply` re-ran verify-only
  (`alignment-verify-receipts.json`).
- **setup** — the disposable project `forge-review-loop-2026-09-28b`
  (id 184) seeded with the shipped SDK lane template VERBATIM + the ONE
  install-seam override (the candidate wheel ladder) + the precommitted
  independent oracle + the `.forge.yml` write scope. No ambient
  duplicate variable.
- **preflight** — the app's OWN doctor GREEN without any ambient
  duplicate (the #376 delivery-mode check on the final image). One
  HONEST refusal is recorded in the bundle (the first preflight attempt
  observed `litellm: unreachable` — a transient of the consumer
  recreate window; the re-run was green and the refusal entry stays).
- **delivery** — issue → `/implement` → `/go` → the real-model lane on
  the candidate wheel → run `e637067a` → candidate `e2f8a156` → Draft
  MR !1 → oracle pipeline 891 green on the exact sha → the closing
  review within reserve (`ok`) → `ready_for_human`. The ordinary
  classic parent (no staged PlanRevision) asserted. Spend $0.1603.
- **round2** — the nonconflicting human edit `b00f3713` (the round-2
  contract file, red until corrected) → the NATIVE `/fix` → the
  budgeted child `70e845a6` (closing-partition/1: 6 reserved calls /
  90k reserved tokens) from the exact human-edit head → candidate
  `6e86d1b4` on the SAME MR → oracle pipeline 896 green on the exact
  new candidate → the closing reviewer's obligation digest recomputed
  through the same durable-state join and compared → the readiness gate
  held until the REVIEWER (the human action) resolved →
  `ready_for_human`. Spend $0.1995.
- **replay** — the SAME note redelivered under a fresh delivery uuid
  (`3f34bf73…`, `deduplicated: true`): zero new rounds, pipelines,
  commits, requests.
- **round3** — a distinct second correction; the WORKER killed at the
  round's own provider commit `81fb8149` (phase `ensuring_draft_mr`,
  post-commit, pre-bookkeeping), then restarted: the #358 recovery
  adopts the round's own effect (exactly ONE provider commit), child
  `bd478f3c` completes to `ready_for_human`, oracle pipeline 899 green,
  the docstring marker present. Spend $0.2086.
- **negative** — a third `/fix` admitted (round 4, child `e68a325b`); a
  CONFLICTING human commit `5d64f034` landed mid-lane. The run blocked
  typed `harness_no_changes` with ZERO candidate commits — the
  predecessor window's LIVE-FOUND expired-premise behavior REPRODUCED
  deterministically (the reused note asks for the module-level compiled
  regex the round-2 candidate already carries, so the model honestly
  produced nothing). The refusal is recorded and kept; the human commit
  stayed the preserved head; the drift fence ran on the fresh lineage:
- **conflict_negative** — the typed `branch_drift` conflict on a REAL
  publication attempt: a second disposable lineage (its own issue →
  delivery run `8409e817` → ready → Draft MR !2, candidate `748b11ed`)
  received a `/fix` whose request is unsatisfiable at its base; mid-lane
  the CONFLICTING human commit `780464c9` landed and the child's
  (`7ed104b5`) publication refused the moved head
  (`candidate_rejected: branch_drift: branch 'factory/2/8409e817' head
  drifted: expected '748b11ed…', actual '780464c9…'`), the child parked
  blocked with zero candidate commits, the human commit preserved as
  head. Spend $0.2058.
- **resume_negative** — a checkpoint (`a54c5034…`) uploaded for the
  delivery work through the app's OWN channel API (work token minted by
  the app's HMAC derivation inside forge-app), the stored MANIFEST
  blob's last byte flipped, then a REAL lane job dispatched with
  `FORGE_LANE_RESUME=1` on the candidate wheel (pipeline 913): the
  strict restore gate failed the digest verification and halted
  `wip_restore_failed` BEFORE any credential redemption or vendor
  session — ZERO model turns, the candidate wheel's pairing marker
  present in the halted trace.
- **collect / teardown** — the typed record
  `qualification/records/review-loop-2026-09-28.json` (stamp
  `forge.profile.qualification/1`, record id
  `gitlab-ce-v1-Q4204-review-loop-2026-09-28-final`, **zero validation
  findings**) and the disposable project deleted after capture.

**Spend**: $0.7742 of the $2.50 cap (the four qualifying lane legs' own
SDK receipts; the reused-note negative arm's single uncounted turn is
bounded by a labelled estimate — its receipt lived in the deleted
disposable project). The full-suite gate at this tree: 9283 passed /
75 skipped.

## 3. LIVE-FOUND this window (driver defects, minimally patched)

1. **The align phase's image-drift check compared a 12-hex short image
   id against the inspect's full 64-hex id** — "image-drift-detected"
   on every run, an unnecessary consumer recreate each run, and the
   recreate window blipped the host-gateway litellm probe at the first
   preflight attempt. Patched in place (normalize: the full id must
   start with the short one); the suite stays green and the honest
   refusal + recreate receipts stay in the bundle.
2. **The reused round-4 note's expired premise is now a pinned
   behavior** (it reproduced identically at the final composition):
   correction notes must name work that EXISTS at their base; the
   drift fence needs the fresh unsatisfiable lineage.

## 4. Composition re-bind at this window

- The freeze manifest re-bound
  (`qualification/profiles/supported-gitlab-ce-v1.json`, digest
  `23ac40df…`): the executed-lab bind and the qualification-composition
  wheel moved TOGETHER to the final candidate; the promoted block stays
  v0.41.0 with the gap disclosed in its status field.
- The inventory snapshot
  (`qualification/inventory-2026-09-28-final.json`) records the honest
  pre-release `misaligned` verdict (the lab runs the 0.42.0 final
  candidate while v0.41.0 is promoted — the by-construction divergence,
  named not hidden).
- The committed wheel receipt
  (`qualification/profiles/receipts/working-tree-wheel-v2.json`) binds
  `ff1d0769…`.
- `qualification/records/gitlab-ce-v1@0.42.0.json` re-bound to the
  final digests (evidence entries, spend, runtime fingerprints, the
  build-once note carrying the supersession story).

## 5. Machine re-verifications at this freeze (zero model calls)

- cold-install kit `--mode from-runbook` (the runbook's own commands,
  re-pinned to the final digests first):
  `cold-install-from-runbook.json` — 9 machine steps, 0 refusals.
- the LIVE deployment-ops drills:
  `qualification/deployment-ops-2026-09-28_final.json` — 12/13 pass,
  qualified-for-profile, every drill row bound to manifest `23ac40df…`;
  the 1 fail is the known honest boundary (the workflow-envelope
  drill's redemption-mode dispatch: the lab's expired broker credential
  mints no grant — 0 grants, 0 redemptions — the lane fails CLOSED,
  zero model spend; the protected-variable route carried every
  qualified model leg).
- the offline ops drills: `qualification/ops-drills-2026-09-28-final.json`
  — 6/6 pass.
- freeze `--check` green at `23ac40df…`.
- the exact-composition guard GREEN on the final candidate
  (`generate_template_pins.py --exact-composition`): dist bytes ==
  the newest strict record's wheel == the frozen manifest's
  qualification-composition wheel.

## 6. What stays pending (named, never hidden)

- The **independent cold install** (the human gate): the kit is
  machine-verified; the second engineer's own execution is theirs.
- The runner-redemption MODEL legs: the broker token stays expired
  (401, typed evidence in the record); the protected-variable route is
  the qualified path for this composition.
- The external-acceptance boundary (the pilot record) — unchanged, a
  separate evidence kind.
