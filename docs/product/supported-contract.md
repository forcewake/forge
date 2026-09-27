# The Forge supported contract (1.0 candidate)

Status: **DRAFT — the 1.0 declaration itself is a human decision** (the
maintainer's support approval + one external code owner's recorded
acceptance, per `qualification/profile-approvals.json` and the pilot record).
This document defines what will be declared, so the declaration is a decision
about facts, not a discovery process. Basis: external review `b521e1a` §9,
the R40-18 acceptance criteria, and the qualification state at the v0.41.0
release candidate (the wording corrected by R41-08/#363: no arrow is claimed
beyond its evidence kind or composition).

## 1. What Forge does

One loop, end to end, on YOUR GitLab CE (GitHub and Azure DevOps adapters
exist at the same discipline but carry their own qualification records):

```
native issue → researched plan → human approval (immutable ApprovedInput)
→ bounded execution (real runner, exact credentials, hard spend caps)
→ candidate → reserved Draft MR (never merged by the bot)
→ independent verification (the project's own CI, bound to the exact candidate)
→ readonly closing review → ready_for_human
→ human correction (/fix on the MR) → a bounded review round from the
   current head → new candidate → re-verification → ready again
→ human merge decision (recorded, never performed by the bot)
```

**Which arrows are actually proven, and where (the honest map — no arrow is
claimed above its evidence).** The correction-loop arrow ran the CURRENT
tree; the other live traces stand on their recorded (historical or
predecessor) compositions; the evidence kinds are never blended:

| Arrows | Evidence kind | Status |
| --- | --- | --- |
| native issue → plan → approval → execution → Draft MR → independent verification → closing review → `ready_for_human` | model-task execution (live trace) | live-proven on the **historical** composition the v0.39.0 release promoted byte-identical (`gitlab-ce-v1@0.39.0`…`@0.40.0` records, trace `supported-composition-v2-2026-09-25`) — **not** yet re-run on the v0.41.0 candidate build |
| cross-runner exact-WIP resume; approved material revision consumed by the resumed executor | model-task execution (live trace) | same historical composition as above (`useful-wip-cross-runner-resume`, `approved-revision-rebind`) |
| the credential lane: dispatch-minted operation grant → lane redemption → the model consumer presenting the broker-selected credential | model-task execution (live trace) | live-proven on the redemption lab alignment (`ddcb9137…`, schema 031, 2026-09-26) — the nearest predecessor composition; the current-composition grant pairing is this cycle's #365 live trace |
| `/fix` on the MR → bounded review round from the current head → new candidate → re-verification → ready again | model-task execution (live trace) | **LIVE-PROVEN on the current-tree composition** (2026-09-27, the #364 review-loop record): delivery 1 → a nonconflicting human edit → the native `/fix` → the budgeted child round from the exact current head → the new candidate on the SAME MR → the oracle on the exact new candidate → the reviewer's obligation digest verified → replay idempotence → a second correction with the #358 worker-failure recovery (exactly one provider commit per effect) → the conflicting-head typed conflict with the human commit preserved. The model legs ran under the **gitlab-protected-variable** credential route (the broker token stays expired — 401, typed evidence); the composition carries the one live-found publisher patch (`223d0f25…`/`f6ff6308…`, the suite green at the 9016/75 baseline) |
| source review (required CI checks on the tagged sha) / release canary (fresh + seeded-upgrade stages) / human support approval / external acceptance (the pilot record) | source review · release canary · human decision · external acceptance | each a SEPARATE evidence kind (§8) — one never substitutes for another, and none of them is a live-arrow claim |

The trace records (`qualification/records/`, the composition + redemption
records) name the run, the pipelines, the spend and the honest unknowns for
every live row above — including which composition each trace executed.

## 2. Supported profile identities

| Axis | Identity (the v0.41.0 release candidate — freeze digest `ab08a317…`, pre-release window) |
| --- | --- |
| Control plane / worker image | the promoted GHCR digest **v0.40.0** (`sha256:45de67f5…` — the latest promotion; the v0.41.0 promotion runs against this tag and re-binds on the follow-up) + the executed-lab bind `localhost/forge:dev` @ `sha256:ddcb9137…` (the build that RAN the recorded traces; two bound identities, never merged) |
| Lane wheel | `forge-0.41.0-py3-none-any.whl` @ sha256 `bb8f18a8…` — the CURRENT tree's `uv build` (the qualification composition; promotion pending, never assumed) |
| CI template | the frozen supported profile manifest (`qualification/profiles/supported-gitlab-ce-v1.json` @ `3d74be37…`) |
| Schema | alembic head **032** (predecessor 031) with the guarded-downgrade policy (a downgrade that would destroy round/grant linkage evidence refuses typed) |
| Provider | GitLab CE 19.3.2 (behavior-fingerprinted in the record) |
| Credential route | native/protected-variable (LIVE on the current-tree composition 2026-09-27 — the #364 review-loop model legs) AND runner-redemption (transport identity live on the predecessor composition; its MODEL legs wait on the broker rotation) |
| Harness | claude-sdk-lane (claude-code 2.1.273 pinned) |

Upgrade range: one minor back (the N-1 → head migration with seeded-data
preservation is exercised by the release canary every release). Older paths:
explicit typed refusals, never silent best-effort.

## 3. Operator responsibilities (what we need from you)

- Provision the GitLab project, the bot PAT (Developer), the read-only lane
  PAT, the model route (BYOK) and the webhook secret.
- Approve plans and revisions; make the merge decision. **The bot never
  merges, never resolves discussions, never deploys.**
- Set the spend caps and closing reserve (`FORGE_SPEND_CAP_USD`,
  `FORGE_CLOSING_RESERVE_USD`) and honor the abort policy when they trip.
- Run the supported release artifacts (the pinned digest/wheel/template
  above) — a moving tree is not a supported identity.

## 4. Data and model boundaries

- Your source stays in your VCS; Forge writes only to the reserved branch of
  the approved target repository. Read-many/write-one: neighbors are read
  only with explicit authorization; no write grant ever widens silently.
- Credentials: broker-managed, operation-scoped grants with absolute
  redemption deadlines; the audit trail records redemptions without values.
- Model traffic: your route, your keys, your spend caps; every dispatch's
  cost is receipted (provider-reported / estimate / billing columns never
  blended; unknowns stay visible, never zero).
- Evidence artifacts committed by the pipeline are allowlisted and
  value-free (the public-artifact gate runs on every release).

## 5. When it fails (the honest part)

- The failure taxonomy is typed: every refusal names its reason and the next
  safe action (`/status`, `/why-blocked` render it on the operator surface).
- Recovery is rehearsal-proven: pause/resume with exact WIP, checkpoint
  restore with consistency checks, bounded auto-revive, review-only budget
  continuation, and the rollback/restore drills in the operations runbooks.
- Excluded failure domains (home-network/NAT/DNS outages on the operator's
  infrastructure, provider 429 storms, runner loss) degrade BOUNDED — the
  work parks typed, never loops, never silently overspends. See
  `docs/operations/support-agreement.md` for the measured envelope, response
  ownership and worked outage examples.

## 6. What Forge does NOT promise

- No auto-merge, no autonomous production deployment, no unlimited
  cross-service orchestration, no fleet SLA from a lab envelope, no claim
  that SDK cost estimates equal invoices, and no universal enterprise
  certification. Pre-1.0: expect breaking changes before 1.0.

## 7. The 1.0 declaration gate (from the review §9, all required)

1. A second engineer installs the exact profile from
   `docs/onboarding/cold-install-runbook.md` without fixing code by hand
   (the runbook's machine steps are executed-and-verified; the human step is
   theirs to perform).
2. Plans cite studied source bytes or ask a specific question — never a
   confident reconstruction (the approved-input digest chain proves what the
   executor consumed).
3. Human intervention preserves WIP: pause, exact resume, approved revision
   (live-proven traces).
4. Review is not a dead end: corrections after `ready_for_human` run as
   bounded rounds (#338) without rewriting history — machine-proven and
   drill-proven; the LIVE correction round is the pending #364 trace (see
   §1's honest map — it is not yet a live-proven arrow).
5. Budget decisions are executable: amendments change the enforcing guard,
   the closing reserve is partitioned before coding (#340, mutation-gated).
6. At least one external code owner records an acceptance decision
   (`pilot.*` records; grades are never generated).

Items 1 and 6 are the open human gates; items 2–5 are machine-proven and
linked above. The declaration is made by recording the two human decisions in
their artifacts — nothing else moves.

## 8. Release health is not adoption

Automated promotion (`docs/releases/`), technical qualification
(`qualification/records/`), human support approval
(`qualification/profile-approvals.json`) and external acceptance (the pilot
record) are SEPARATE evidence kinds — one never substitutes for another, and
green CI is never an acceptance claim.
