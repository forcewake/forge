# The Forge supported contract (1.0 candidate)

Status: **DRAFT — the 1.0 declaration itself is a human decision** (the
maintainer's support approval + one external code owner's recorded
acceptance, per `qualification/profile-approvals.json` and the pilot record).
This document defines what will be declared, so the declaration is a decision
about facts, not a discovery process. Basis: external review `b521e1a` §9,
the R40-18 acceptance criteria, and the qualification state at v0.40.0.

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

Every arrow above is proven live on the supported profile — the trace record
(`qualification/records/`, the composition + redemption records) names the
run, the pipelines, the spend and the honest unknowns.

## 2. Supported profile identities

| Axis | Identity (v0.40.0) |
| --- | --- |
| Control plane / worker image | the promoted GHCR digest (release promotion record) |
| Lane wheel | the promoted wheel, sha256-pinned (byte-identical to the qualification freeze) |
| CI template | the frozen supported profile manifest (`qualification/profiles/supported-gitlab-ce-v1.json`) |
| Schema | alembic head at the release (with the guarded-downgrade policy) |
| Provider | GitLab CE 19.3.x (behavior-fingerprinted in the record) |
| Credential route | native/protected-variable AND runner-redemption (each separately qualified) |
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
   bounded rounds (#338, live-proven) without rewriting history.
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
