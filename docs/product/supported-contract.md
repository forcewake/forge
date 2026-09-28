# The Forge supported contract (1.0 candidate)

Status: **DRAFT — the 1.0 declaration itself is a human decision** (the
maintainer's support approval + one external code owner's recorded
acceptance, per `qualification/profile-approvals.json` and the pilot record).
This document defines what will be declared, so the declaration is a decision
about facts, not a discovery process. Basis: external review `b521e1a` §9,
the R40-18 acceptance criteria, and the qualification state at the v0.42.0
release candidate — every arrow below now points at a trace that ran THE
EXACT candidate composition (R42-04/#377's build-once contract: one build,
digests recorded before qualification, the promotion gate refuses any other
bytes).

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
claimed above its evidence).** Since R42-04 (#377) the correction-loop arrow
and the correction-loop composition are ONE identity: the trace ran the
build-once v0.42.0 FINAL candidate (wheel `ff1d0769…` + image
`sha256:ad769a4a…` of tree e305478, digests recorded in the candidate
manifest BEFORE qualification — re-qualified live when three review
commits (#380/#379/#389) landed after the first qualification,
superseding the first candidate `e7b48d42…`/`98ea9031…` per the
build-once rule itself), and the
promotion of v0.42.0 is gated on digest equality with that record — the
v0.41.0 historical-composition gap (live trace `223d0f25` vs promoted
`00919993`) cannot recur silently:

| Arrows | Evidence kind | Status |
| --- | --- | --- |
| native issue → plan → approval → execution → Draft MR → independent verification → closing review → `ready_for_human` | model-task execution (live trace) | live-proven on the **historical** composition the v0.39.0 release promoted byte-identical (`gitlab-ce-v1@0.39.0`…`@0.41.0` records, trace `supported-composition-v2-2026-09-25`) — superseded for composition purposes by the delivery leg of the build-once review-loop trace below, which runs the same arc on the exact candidate |
| cross-runner exact-WIP resume; approved material revision consumed by the resumed executor | model-task execution (live trace) | same historical composition as above (`useful-wip-cross-runner-resume`, `approved-revision-rebind`); the required-resume NEGATIVE (a corrupted checkpoint halts the lane before any model session — zero vendor turns) is live-proven ON the candidate (the review-loop trace's `resume_negative` arm) |
| the credential lane: dispatch-minted operation grant → lane redemption → the model consumer presenting the broker-selected credential | model-task execution (live trace) | live-proven on the redemption lab alignment (`ddcb9137…`, schema 031, 2026-09-26) — the nearest predecessor composition; its model legs stay blocked on the expired broker credential (typed 401 evidence), and the native/protected-variable route carried every current-composition model leg |
| `/fix` on the MR → bounded review round from the current head → new candidate → re-verification → ready again | model-task execution (live trace) | **LIVE-PROVEN on the BUILD-ONCE candidate composition** (2026-09-28, the #377 review-loop record `review-loop-2026-09-28`): delivery 1 → a nonconflicting human edit → the native `/fix` → the budgeted child round from the exact current head → the new candidate on the SAME MR → the oracle on the exact new candidate → the reviewer's obligation digest verified → replay idempotence → a second correction with the #358 worker-failure recovery (exactly one provider commit per effect) → the conflicting-head typed conflict with the human commit preserved → the required-resume negative halting before any model session. The lane sha-verified the candidate wheel in-job; the model legs ran under the **gitlab-protected-variable** route with the #376 delivery-mode doctor preflight green WITHOUT any ambient duplicate |
| source review (required CI checks on the tagged sha) / release canary (fresh + seeded-upgrade stages) / human support approval / external acceptance (the pilot record) | source review · release canary · human decision · external acceptance | each a SEPARATE evidence kind (§8) — one never substitutes for another, and none of them is a live-arrow claim. The exact-composition gate (`generate_template_pins.py --exact-composition`, wired into CI and the release workflow) now makes "the promotion shipped the qualified bytes" a mechanical check, not a promise |

The trace records (`qualification/records/`, the composition + redemption
records) name the run, the pipelines, the spend and the honest unknowns for
every live row above — including which composition each trace executed.

## 2. Supported profile identities

| Axis | Identity (the v0.42.0 BUILD-ONCE candidate — promotion pending, gated on digest equality with the qualification record) |
| --- | --- |
| Control plane / worker image | the BUILD-ONCE FINAL alignment build `localhost/forge:dev` @ `sha256:ad769a4a…` (reports 0.42.0, schema 032, receipts in the alignment build receipts; digests recorded in the candidate manifest BEFORE qualification) + the promoted GHCR digest **v0.41.0** (`sha256:f1316aaf…`) as the released axis — two bound identities, never merged |
| Lane wheel | `forge-0.42.0-py3-none-any.whl` @ sha256 `ff1d0769…` — the ONE `uv build` of the final candidate tree; sha-verified in-job by every lane leg of the qualification trace; the release must promote THESE bytes (the exact-composition gate refuses otherwise) |
| CI template | the frozen supported profile manifest (`qualification/profiles/supported-gitlab-ce-v1.json`, re-frozen at the candidate) |
| Schema | alembic head **032** (predecessor 031) with the guarded-downgrade policy (a downgrade that would destroy round/grant linkage evidence refuses typed) |
| Provider | GitLab CE 19.3.2 (behavior-fingerprinted in the record) |
| Credential route | native/protected-variable (LIVE on the build-once candidate 2026-09-28 — the review-loop model legs; the #376 doctor preflight green WITHOUT ambient duplicates) AND runner-redemption (transport identity live on the predecessor composition; its MODEL legs wait on the broker rotation) |
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
   bounded rounds (#338) without rewriting history — LIVE-PROVEN on the
   exact candidate composition (the build-once review-loop record,
   re-qualified 2026-09-28 at the final tree: the full ready → `/fix` →
   ready arc, replay idempotence,
   the worker-failure recovery, the conflicting-head typed conflict and
   the required-resume negative, all on wheel `ff1d0769…`/image
   `ad769a4a…`).
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
