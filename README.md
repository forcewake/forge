# forge

**An agentic software factory.** You file an issue. forge studies your codebase, proposes a plan, and waits. Once you approve it, the plan becomes an immutable contract — the exact bytes you approved are the exact bytes the model executes. Work runs on your own CI runners under hard spend caps, with credentials that expire at a deadline you set. The result arrives as a Draft MR, verified by your own pipeline against that exact candidate, reviewed read-only, and parked in `ready_for_human`.

**The bot never presses Merge.** That is not a limitation — it is the product. Everything forge does is arranged so that the one irreversible act stays yours, taken on evidence you can audit.

```
 issue ──► researched plan ──► your approval ──► bounded execution ──► candidate
                                                                        │
                                                                 Draft MR (reserved)
                                                                        │
 human merge ◄── ready_for_human ◄── readonly review ◄── independent CI ◄─┘
     │                    │
     │                    └── /fix on the MR ──► bounded review round
     │                                            from the exact current head
     └── never performed by forge                ──► new candidate ──► (loop)
```

* Two supported providers: GitHub and GitLab CE.
* Native provider checks, an Invariant Suite, and a source release flow that binds tested artifacts to their exact hashes.
* One reviewed-ready task on the reference profile costs about **$0.40** in lane spend. The credential-redemption proof cost less than one cent — because it was designed to be provable without spending money.

---

## Why this exists

Every AI coding demo ends at "the code looks right." That is where forge starts, because "looks right" is the easy five percent. The hard problems are the ones around the code:

* **Authority** — who approved what, and which bytes did the model actually receive? forge pins every dispatch to an approved input with a three-way digest: what was approved, what the server sent, and what the executor consumed must be identical, or the run refuses to proceed.
* **Money** — budgets that actually enforce. Not a note in a log file: typed amendments applied atomically to the enforcing guard, a closing reserve partitioned *before* coding starts so implementation can never eat the review's allowance, and an exposure model that keeps a streaming provider's intermediate subtotal from quietly releasing reserved liability.
* **Review** — humans start reviewing *after* readiness is announced. A `/fix` comment on the MR opens a bounded review round from the exact current head; the previous delivery stays immutable, your commits stay intact, and the new candidate is re-verified from scratch. Old green checks render as history, never as current truth.
* **Failure** — everything that can go wrong has a typed name, a bounded recovery, and a paper trail. A lost webhook, a dead runner, a rotated credential, a half-finished migration: the system parks, says exactly what it is waiting for, and never improvises.

The bet: agentic work becomes trustworthy not when the models get better, but when the factory around them keeps receipts.

## Proof, not promises

Most tooling asks you to believe the README. forge's README asks you to read the records.

* **The promotion gate** refuses to publish an image unless every required check is green *at the tagged commit* — a check with no recorded result blocks, fail-closed; a check that failed and passed on retry records both attempts. Verdicts: `promote`, `conditional_promote`, `block`. A green canary never outweighs a red required check.
* **Exact artifact identity**: release verification consumes the built wheel, image digest and hash-pinned requirements. Promotion preserves those bytes; an unreleased working tree carries no hosted verification claim.
### Guarantee levels

Release verification is implemented in `tools/release/`: artifact hashes,
installed-wheel invariant results, fresh-database boot and both native provider
smokes must agree on the exact release identity. Historical qualification
records under `docs/releases/evidence/` describe their recorded versions; they
do not certify this unreleased source tree.

## What lands in your repository

A run, end to end, produces things you can point at:

1. **A plan with citations** — built from your actual sources (read-many, write-one: neighboring repositories are read only with explicit authorization), with facts, assumptions and open questions separated.
2. **An approval artifact** — the approved input is frozen with its digest; a material revision mid-run goes through the same native approval, and the executor's next dispatch carries the new text or refuses.
3. **A candidate on a reserved branch** — collected from the workspace generation that actually did the work, never from a stale checkout; a resumed attempt's generation ships, the original checkout is never swept up by accident.
4. **A Draft MR** — one per run, by construction; merge is yours.
5. **Independent verification** — your own CI, bound to the exact candidate sha; a moved head is a typed staleness, not a silent retest.
6. **A read-only closing review** on the run's own budget, inside a reserve implementation cannot touch.
7. **`ready_for_human`** — the terminal state that means "checks passed; merge is a human decision," after which your `/fix` and `/ask` comments drive bounded correction rounds.

## Features

### The delivery loop

* **Approved inputs, digest-pinned** — `forge.revision.approved-input/1`: evidence, server and executor agree on the exact brief bytes ([`src/forge/adaptive/revisions.py`](src/forge/adaptive/revisions.py)).
* **Real harness lanes** — claude-code, codex, copilot and opencode driven through their actual SDKs/binaries on your GitLab/GitHub runners; the lane template ships in the repo and is frozen into the supported profile by digest ([`ci/templates/`](ci/templates/), [`src/forge/lane_driver.py`](src/forge/lane_driver.py)).
* **Exact-WIP resume** — pause mid-turn, destroy the runner, resume on another: the content-addressed checkpoint restores new/modified/deleted files exactly (proven live with all three shapes in one checkpoint).
* **Steering and interruption** — `/pause`, `/steer` and material revisions land through a durable mailbox; causal effect verified live on every arm.
* **Bounded review rounds** — linked child work units after `ready_for_human`: own admission, scope, budget and generation; the original delivery stays immutable; one outstanding round per MR with a deterministic race arbiter ([`docs/operations/review-rounds.md`](docs/operations/review-rounds.md)).

### Authority and credentials

* **Operation-scoped grants** — redemption authority is a grant persisted at dispatch (subject, work, attempt generation, route, exact ref, operation, absolute deadline) — never taken from the request. The refusal matrix names every way a redemption can be refused ([`src/forge/adaptive/credential_broker.py`](src/forge/adaptive/credential_broker.py)).
* **Same ref means same binding revision** — revoke/regrant at the same locator cannot silently reuse an old authorization; issuance and cancellation have a documented linearization contract.
* **Append-only audit** — credential redemptions are INSERT-only rows committed before any bytes leave the service; the JSON is a projection.
* **Collision-safe native locators** — secret names derive from the full reference's sha256, not display names; legacy entries migrate recorded, never silently renamed.

### Budgets that enforce

* **Typed amendments** — USD / calls / tokens / wall-clock are distinct axes; an amendment names its axis, its reason and its originating native command; redelivery applies once; two identical commands are two decisions ([`src/forge/durable/budgets.py`](src/forge/durable/budgets.py)).
* **Closing reserve, partitioned before coding** — under the versioned `closing-partition/1` policy, the implementer reserves against limit − share, the reviewer against the full limits.
* **Finality-based exposure** — settled, accrued-unsettled and retained liability as three quantities; a non-final receipt with a cost keeps its envelope; an unbounded pending interval refuses hard-cap actions with a typed reason ([`src/forge/adaptive/usage_ingestion.py`](src/forge/adaptive/usage_ingestion.py)).

### Operators

* `/implement /go /pause /steer /retry /cancel /status /why-blocked /reconcile /security /fix /ask /approve-revision` — native commands on issues and MRs, each with typed refusals and idempotent redelivery.
* **One coherent projection** — execution, review round, candidate, verification and acceptance as separate but linked facts; every offered action names the version it expects and refuses if the world moved ([`src/forge/adaptive/operator_view.py`](src/forge/adaptive/operator_view.py)).
* **The measured operating envelope** — slot limits, queue policy, degradation behavior, restore rehearsals and alerts with threshold bases, measured on the real workflow ([`docs/operations/support-agreement.md`](docs/operations/support-agreement.md)).


## Quick start

### 1. Run the image

<!-- static maintenance release example -- begin -->
```bash
docker run -d --name forge -p 8420:8420 \
  --env-file .env ghcr.io/forcewake/forge:0.42.0
# pin the release digest instead of the mutable tag (R30/R32-19):
#   docker run -d --name forge -p 8420:8420 --env-file .env ghcr.io/forcewake/forge@sha256:c172ae07405978b415ef1ad9428e45f8fa261467b7f0c61fd953462b450599a8
# or from source:
git clone https://github.com/forcewake/forge && cd forge
uv sync && set -o pipefail && .venv/bin/python -m pytest -q
```
<!-- static maintenance release example -- end -->

### 2. Configure

```bash
cp .env.example .env    # GITLAB_URL/GITLAB_TOKEN (or GitHub App values),
                        # DATABASE_URL (Postgres), REDIS_URL, LITELLM_URL,
                        # the model route + spend caps
```

### 3. Migrate, boot, verify

```bash
python -m forge.migrate             # alembic chain, currently head 031
python -m forge.doctor              # read-only environment report
python -m forge.worker              # the durable worker
```

### 4. Onboard a project

Create an issue template with the bot mention, register the webhook, add the harness CI include and the CI variables (`forge doctor` walks you through it and refuses to guess). Then, on a real issue:

```text
@forge /implement     # the researched plan appears, with citations
@forge /go            # bounded execution → Draft MR → ready_for_human
```

The full second-engineer path — every command, every expected observable, every typed failure — is the committed runbook: [`docs/onboarding/cold-install-runbook.md`](docs/onboarding/cold-install-runbook.md).

## How it works

A modular monolith over Postgres, one release train, provider differences kept in adapters rather than averaged away:

```
webhook ──► gateway (token-checked ingress, two-layer dedup)
              │
              ▼
        durable inbox (Postgres) ──► worker + reconciler
              │                        │  discovery → planning → approval
              │                        │  dispatch: envelope + grant + budget
              ▼                        ▼
        lane on YOUR runner ──► candidate collector ──► trusted publisher
                                       │                    │
                                       ▼                    ▼
                              independent CI          Draft MR + evidence
                                       │
                                       ▼
                            readonly review ──► ready_for_human ──► /fix rounds
```

* [`src/forge/gateway/`](src/forge/gateway/) — authenticated native ingress; commands are durable rows before anything slow happens.
* [`src/forge/runs/`](src/forge/runs/) — the service, the reconciler, the publisher: one validated write path per decision (ADR-0034's ownership map names the owner and its real callers).
* [`src/forge/durable/`](src/forge/durable/) — Postgres models, budgets, leases, the append-only stores.
* [`src/forge/adaptive/`](src/forge/adaptive/) — the authority surface: credential broker, revisions, closing budget, usage exposure, operator view and ops limits.
* [`scripts/`](scripts/) — program guardrails and the required PostgreSQL gate. [`tools/`](tools/) holds development, lab and source release tools outside the product wheel.

## Providers and harnesses

Azure DevOps is supported only on the [`release/0.42.x` maintenance line](docs/operations/maintenance-0.42.md).

### Provider capability matrix

| Capability | GitLab CE | GitHub |
|---|---|---|
| Commands (`/implement`, `/go`, `/cancel`, `/retry`, `/pause`, `/steer`, `/status`, `/why-blocked`, `/reconcile`, `/security`) | ✅ comments | ✅ comments + `forge` label |
| Review feedback (`/fix`, `/ask`, `/approve-revision`) and bounded review rounds | ✅ live — the reference path | not yet |
| Plan comment + human gate | ✅ | ✅ |
| Harness execution | ✅ project CI (docker executor) | ✅ Actions (`workflow_dispatch`) |
| Trusted publisher | Commits API | GraphQL CAS (`expectedHeadOid`) |
| Draft MR / PR before CI — one per run, reserved | ✅ | ✅ |
| Readonly LLM review | ✅ MR notes | ✅ native reviews |
| CI-failure debug lane | ✅ | ✅ (Actions timeline) |
| Reactive review on push | ✅ | ✅ (incremental via before/after) |
| Credential delivery | native secrets, protected variables, runner redemption (all live-qualified) | native secrets |
| MCP servers in the lane | ✅ | ✅ |
| Identity | bot user + PAT | GitHub App installation (+ PAT lab mode) |
| Webhook authenticity | secret token | HMAC signature |

Delivery semantics, with the evidence level honestly stated per cell:

| Capability | GitLab CE | GitHub |
|---|---|---|
| Publication policy — harness lane | live | contract-tested (shared publisher validation) |
| CI verification gate (`waiting_ci`) | contract-tested · required-jobs profile | live · `waiting_ci` + checks |
| Repair-in-place | implemented · contract-tested | implemented · contract-tested |
| Operator `/retry` + auto-revive | live | live |
| Exact-WIP cross-runner resume | live (three file shapes) | contract-tested |

Historical GitLab CE qualification records remain under [`qualification/records/`](qualification/records/). Current release verification requires independent native GitHub and GitLab smokes; local source changes alone do not establish a live result.

### Drivers

Real clients against real binaries — [`src/forge/adaptive/drivers/`](src/forge/adaptive/drivers/), not prompt stubs; live verification status is tracked per binary in the DriverMatrix with evidence. Per-driver setup guides: [docs/harnesses/](docs/harnesses/README.md).

| Driver | Headless posture | Notes |
|---|---|---|
| **[Claude Code](docs/harnesses/claude-code.md)** | `-p` + stream-json, `--strict-mcp-config` | live-verified on all three providers |
| **[Grok Build](docs/harnesses/grok-build.md)** | `--always-approve` + deny rules, hardened npm preamble | platform-binary hang workaround |
| **[opencode](docs/harnesses/opencode.md)** | permission map via injected config | schema-translated MCP |
| **[GitHub Copilot CLI](docs/harnesses/copilot-cli.md)** | `-p` + deny-wins tool rules | subscription auth (fine-grained PAT) |

## Status

<!-- static maintenance release example -- begin -->
(`ghcr.io/forcewake/forge:0.42.0` — digest `sha256:c172ae07405978b415ef1ad9428e45f8fa261467b7f0c61fd953462b450599a8`, verdict `promote`,
evidence `docs/releases/evidence/v0.42.0/promotion.json`, qualifying CI run `36405280551`).
<!-- static maintenance release example -- end -->

**v0.42.0** (release candidate, the BUILD-ONCE window — the frozen qualification manifest `23ac40df…` binds the FINAL candidate: wheel `ff1d0769…` + the alignment image of the same tree e305478, digests recorded BEFORE qualification and RE-QUALIFIED live after three review commits (#380/#379/#389) landed inside the release window — the build-once rule applied to the cycle's own qualification; the exact-composition gate refuses any promotion whose bytes are not the qualified candidate's) — the honest tiers — what is machine-proven, what is live-proven on the recorded compositions, what waits on this cycle's live traces and the world:

* **Live-proven on HISTORICAL compositions** (the reference GitLab profile, records committed): issue → plan → approval → lane → Draft MR → independent CI → closing review → `ready_for_human`; material revision consumed by the resumed executor (zero rescue steers); cross-runner exact-WIP resume; operation-grant redemption end to end with sentinel identity proof.
* **Live-proven on THE EXACT CANDIDATE** (the #377 build-once review-loop record, re-qualified 2026-09-28 at the FINAL tree: wheel `ff1d0769…`/image `ad769a4a…`, spend $0.7742, zero validation findings, under the gitlab-protected-variable credential route with the #376 doctor preflight green without ambient duplicates): the complete review-and-correction loop — a native `/fix` on the Draft MR admits a BUDGETED child round from the exact current head (a human edit included), the corrected candidate lands on the same MR, the precommitted oracle evaluates the exact new candidate, the closing reviewer is briefed from the active correction (obligation digest verified), a redelivered note creates nothing, a second distinct correction works, a worker killed at publication recovers with exactly one provider commit, a conflicting human edit is a typed `branch_drift` conflict that is preserved, never reverted, and a required-resume dispatch over a corrupted checkpoint halts BEFORE any model session (zero vendor turns).
* **Machine-proven** (gates + mutation arms in CI, drill-level on the lab): everything this README claims about refusals, idempotence, concurrency and budget enforcement — bounded review rounds and guarded budget amendments now carry BOTH the machine proof and the #364 live trace (the round path's live leg is closed; the runner-redemption route's model legs stay blocked on the expired broker credential, named in the records).
* **Honestly open** (named in the records, not hidden): external design-partner acceptance; the blind planning review by real reviewers; a customer two-writer change; the second-engineer install. The 1.0 declaration gate is written down — [`docs/product/supported-contract.md`](docs/product/supported-contract.md) — with its evidence-kind map keeping live, drill, human and external claims separate.

## What forge will never do

* **Merge, deploy, or resolve your discussions.** Reserved branches and Draft MRs are structural, not configurational.
* **Turn unknowns into zeros.** Unknown spend, missing evidence and ungraded work stay visible as exactly that.
* **Substitute silently.** A moved head, a rotated credential, a changed contract invalidates the evidence that depended on it — loudly.
* **Average your providers away.** GitLab and GitHub keep their real semantics in their adapters; no generic interface promises more than the platform beneath it delivers.

## Documentation

* [`docs/operations/`](docs/operations/) — runbooks: operator commands, closing budgets, credential delivery, deployment drills, the support agreement with its measured envelope.
* [`docs/onboarding/cold-install-runbook.md`](docs/onboarding/cold-install-runbook.md) — install the exact supported composition, step by step.
* [`docs/adr/`](docs/adr/) — 34 decision records, each still true.
* [`docs/releases/`](docs/releases/) — the promotion gate, per-release evidence, the record format.
* [`docs/product/supported-contract.md`](docs/product/supported-contract.md) — the 1.0 candidate contract.

## Development

```bash
uv sync
uv run python -m pytest -q                     # the suite
uv run lint-imports                         # enforced product import boundaries
uv run python scripts/pg_gate.py               # required PG profiles (fail-closed on missing fixtures)
uv run ruff check . && uv run ruff format --check .
```

Migrations are guarded: downgrades refuse while authorization evidence exists. The schema is at head 031; the chain runs on real Postgres in CI.

## License

BSD-3-Clause. Provenance and upstream acknowledgments: [`LICENSE`](LICENSE), [`UPSTREAM.md`](UPSTREAM.md).

---

forge is built by people who think the interesting part of agentic software is not watching it write code — it's being able to trust what happened while you looked away.
