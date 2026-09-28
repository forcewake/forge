# R42-04 (#377) — the build-once candidate qualification

The disclosed gap: the v0.41.0 live loop ran wheel `223d0f25…`/image
`f6ff6308…` while the release promoted wheel `00919993…` built from the
later tree — honestly documented, but a passed HISTORICAL trace cannot
serve as an exact release qualification. This window is the process fix:
**build ONE candidate, qualify THE candidate, promote THOSE bytes.**

## 1. The candidate (built once, digests recorded BEFORE qualification)

`candidate-manifest.json` is the pre-qualification receipt (written
before any phase of the loop ran):

- **Wheel**: `forge-0.42.0-py3-none-any.whl` @ sha256
  `e7b48d42e4851e9c142657f7f9597106f80e7da43de66e0ea39d5d6477462d33`
  — the single `uv build` of the tree (base `0f0dc8c` + the M0 fixes
  already in it + the version bump).
- **Image**: `localhost/forge:dev` @
  `sha256:98ea903125d1c4c6d05582682f1d5a0ed8d15fc6bdb00ee37747a854f5b58bed`
  (id `7e9077d5…`, reports 0.42.0, schema head 032) —
  `scripts/align_lab.py --apply` built it from the SAME tree
  (`alignment-build-receipts.json`: backup, rollback-tag, build, stop,
  migrate, recreate, verify — every step receipted).
- The wheelhost (:8481) served exactly these wheel bytes; every lane leg
  sha256-verified them in-job before install (the pairing marker in each
  trace).

## 2. The trace (the #364 driver, re-pinned to the candidate — no
replacement loop)

`live-run-evidence.json` carries the per-phase receipts (values never;
digests and ids only). The full narrative, per phase:

- **probe** — both credential routes at the real gateway: the
  protected-variable carrier ALIVE (200), the runner-redemption broker
  token DEAD (401 `token expired or incorrect`, typed). The model legs
  rode the carrier.
- **align** — the consumers verified on the candidate image with the
  window pins (credential delivery, review feedback, max rounds, the
  receipted 600k standard budget ceiling); `align_lab --apply` re-ran
  verify-only (`alignment-verify-receipts.json`).
- **setup** — the disposable project seeded with the shipped SDK lane
  template VERBATIM + the ONE install-seam override (the candidate wheel
  ladder) + the precommitted independent oracle + the `.forge.yml` write
  scope. **No ambient duplicate variable** — the #376 delivery-mode
  doctor preflight sees the `FORGE_MODEL_ENV_ANTHROPIC_AUTH_TOKEN`
  carrier.
- **preflight** — the app's OWN doctor GREEN without any ambient
  duplicate (the #376 fix proven live on the candidate image:
  `project.harness.claude-sdk-lane: delivered credential substitutes the
  ambient ANTHROPIC_AUTH_TOKEN`).
- **delivery** — issue → `/implement` → `/go` → the real-model lane on
  the candidate wheel → candidate `1a0c2ee0…` → Draft MR !1 → oracle
  pipeline 847 green on the exact sha → the closing review within
  reserve → `ready_for_human`. The ordinary classic parent (no staged
  PlanRevision) asserted.
- **round2** — the nonconflicting human edit (the round-2 contract file,
  red until corrected) → the NATIVE `/fix` → the budgeted child from the
  exact human-edit head → the new candidate on the SAME MR → the oracle
  green on the exact new candidate → the closing reviewer's obligation
  digest recomputed through the same durable-state join and compared →
  the readiness gate held until the REVIEWER (the human action) resolved
  → `ready_for_human`.
- **replay** — the SAME note redelivered under a fresh delivery uuid:
  zero new rounds, pipelines, commits, requests.
- **round3** — a distinct second correction; the WORKER killed at the
  round's own provider commit, pre-bookkeeping, then restarted: the
  #358 recovery adopts the round's own effect (exactly ONE provider
  commit) and completes to `ready_for_human`.
- **negative** — a third `/fix` admitted; a CONFLICTING human commit
  lands mid-lane. LIVE-FOUND: the reused round-4 note's premise had
  EXPIRED (the round-2 candidate already carried the module-level
  compiled regex the note asked to introduce), so the model honestly
  produced ZERO changes — the run blocked typed `harness_no_changes`,
  zero candidate commits, the human commit stayed the preserved head,
  the round slot freed. Recorded, kept; the drift fence then ran on a
  fresh lineage:
- **conflict_negative** — the typed `branch_drift` conflict on a REAL
  publication attempt: a second disposable lineage (its own issue →
  delivery → ready → Draft MR) received a `/fix` whose request is
  unsatisfiable at its base; mid-lane the CONFLICTING human commit
  landed and the child's publication refused the moved head
  (`candidate_rejected: branch_drift`), the child parked blocked with
  zero candidate commits, the human commit preserved as head.
- **resume_negative** — the required-resume negative (new this window,
  #377's acceptance): a checkpoint uploaded for the delivery work
  through the app's OWN channel API (work token minted by the app's HMAC
  derivation inside forge-app — never a hand-seeded index row), the
  stored MANIFEST blob's last byte flipped (bit-rot under the CAS; the
  checkpoint id IS that blob's digest), then a REAL lane job dispatched
  with `FORGE_LANE_RESUME=1` on the candidate wheel: the strict restore
  gate fails the digest verification and halts `wip_restore_failed`
  BEFORE any credential redemption or vendor session — ZERO model turns,
  the candidate wheel's pairing marker present in the halted trace.
- **collect / teardown** — the typed record
  `qualification/records/review-loop-2026-09-28.json` (stamp
  `forge.profile.qualification/1`) and the disposable project deleted
  after capture.

## 3. The build-once guard (the durable part)

`scripts/generate_template_pins.py --exact-composition` (R42-04 AC-8):

- **release time** — the release workflow runs it right after
  `uv build` with `--expect-version`/`--expect-wheel-sha256`: the
  just-built wheel must EQUAL the newest strict qualification record's
  wheel for that version, else the promotion FAILS (a changed candidate
  is refused until re-qualified).
- **tree time** — CI's lint job runs it: in the pre-release window the
  pending version must have a bound candidate and every artifact naming
  it (dist/ when present, else the committed wheel receipt; the frozen
  supported-profile manifest) must BE it; post-release, strict records
  for the promoted version must pin exactly the promoted wheel.
- The v0.41.0 shape (record pins one wheel, promotion ships another) is
  exactly what the post-release arm refuses.

## 4. Composition re-bind

The freeze manifest
(`qualification/profiles/supported-gitlab-ce-v1.json`, digest
`3bdb6790…`) re-bound at this window: the executed-lab bind and the
qualification-composition wheel moved TOGETHER to the candidate; the
promoted block stays v0.41.0 with the gap disclosed in its status field.
The inventory snapshot (`qualification/inventory-2026-09-28-review-loop.json`)
records the honest pre-release `misaligned` verdict (the lab runs the
0.42.0 candidate while v0.41.0 is promoted — the by-construction
divergence, named not hidden).

## 5. Machine re-verifications at this freeze (zero model calls)

- cold-install kit `--mode from-runbook` (the runbook's own commands):
  `cold-install-from-runbook.json` — 9 machine steps, 0 refusals.
- the LIVE deployment-ops drills:
  `qualification/deployment-ops-2026-09-28.json` — 12/13 pass,
  qualified-for-profile; the 1 fail is the honest blocked live leg (the
  workflow-envelope drill's redemption-mode dispatch — the lab's expired
  broker credential mints no grant, the lane fails CLOSED with zero
  model spend; the same named boundary as the previous window; the
  protected-variable route carried every qualified model leg).
- the offline ops drills: `qualification/ops-drills-2026-09-28.json` —
  6/6 pass.
- freeze `--check` green at `3bdb6790…`.

## 6. What stays pending (named, never hidden)

- The **independent cold install** (the human gate): the kit is
  machine-verified; the second engineer's own execution is theirs to
  perform (the runbook's H-steps).
- The runner-redemption MODEL legs: the broker token stays expired
  (401, typed evidence in the record); the protected-variable route is
  the qualified path for this composition.
- The external-acceptance boundary (the pilot record) — unchanged, a
  separate evidence kind.

Spend: every lane job's own SDK receipt is summed in the record
(cap $2.50); the honestly-failed attempts of this window, if any, are
counted beside the qualifying legs.
