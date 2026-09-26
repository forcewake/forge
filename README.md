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

* Three forges in production shape: GitLab CE, GitHub, Azure DevOps — all three lanes live-verified end to end.
* **8,846 tests**, a fail-closed promotion gate, mutation-tested release traces, and a qualification record store that keeps lab proof and customer proof honestly separated.
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
* **Mutation gates** run each critical release trace twice — once as a baseline, once with the defect deliberately reintroduced. If the seeded defect does not fail the trace, the gate fails. Release traces that cannot detect their own regressions do not ship.
* **Byte-identical composition**: the wheel the release publishes is byte-identical to the wheel the live qualification trace executed — verified by digest, second release running. The code you install is the code that earned the evidence.
* **The closure matrix** grades every capability on six evidence levels — domain, wired, executed, native, recovery, customer — and leaves the gaps visible as data. "Pending" is a state you can query, not a euphemism.
* **Honest unknowns**: unknown spend is never rendered as zero; a killed job stays an unknown, never a successful cheap one; a partial receipt holds its upper-bound liability until it settles. The economics ledger keeps provider-reported, estimated, and billing-reconciled costs in three columns that are never blended.

### Guarantee levels

Every capability claim in the release manifest (`forge.release_manifest`, generated from the tagged tree) carries exactly one of four levels, and this README uses the same words with the same meanings:

* **implemented** — code + unit tests; no CI contract suite yet.
* **contract-tested** — a CI suite over fakes and stubs gates the contract, or a failure-injection suite (`coroutine_fi`) has exercised it.
* **live-canary-tested** — the shipped artifact was booted and probed by the release canary against a real database.
* **not-run** — not executed in this release's evidence; stated, never implied by silence.

Run `python -m forge.release_manifest` to regenerate the machine-readable manifest and `python -m forge.release_promotion gaps` to list every capability whose evidence class requires live qualification and does not have it. A gap with a name is a fact; a gap without one is a bug.

All of it is committed: [`docs/releases/`](docs/releases/) (per-release promotion evidence), [`qualification/`](qualification/) (profile records with derived verdicts), [`docs/adr/`](docs/adr/) (34 architecture decision records), [`docs/operations/`](docs/operations/) (runbooks that were executed, not imagined).

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
* **Real harness lanes** — claude-code, codex, copilot and opencode driven through their actual SDKs/binaries on your GitLab/GitHub/Azure runners; the lane template ships in the repo and is frozen into the supported profile by digest ([`ci/templates/`](ci/templates/), [`src/forge/lane_driver.py`](src/forge/lane_driver.py)).
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

### Qualification machinery

* **The supported profile** — a frozen manifest binding control plane, wheel, template, runner and schema together; cold-install proven fresh, upgraded (data-bearing, fingerprinted) and verified ([`qualification/profiles/`](qualification/profiles/)).
* **Deployment drills** — occupancy under partition, lost responses, backup/restore with typed refusals, degraded modes, workflow-restore covering rounds/amendments/grants.
* **The economics ledger** — run → attempt → receipt → native job → candidate → verification → human decision, joined on stable ids; unjoinable links surface as gaps, never drop ([`src/forge/adaptive/delivery_economics.py`](src/forge/adaptive/delivery_economics.py)).

## Quick start

### 1. Run the image

<!-- generated by scripts/generate_template_pins.py -- begin -->
```bash
docker run -d --name forge -p 8420:8420 \
  --env-file .env ghcr.io/forcewake/forge:0.40.0
# pin the release digest instead of the mutable tag (R30/R32-19):
#   docker run -d --name forge -p 8420:8420 --env-file .env ghcr.io/forcewake/forge@sha256:45de67f57c0058c0a5b54f2ab9bde8855138e629adf48a5757d10171917ac5a0
# or from source:
git clone https://github.com/forcewake/forge && cd forge
uv sync && set -o pipefail && .venv/bin/python -m pytest -q
```
<!-- generated by scripts/generate_template_pins.py -- end -->

### 2. Configure

```bash
cp .env.example .env    # GITLAB_URL/GITLAB_TOKEN (or GitHub App / Azure values),
                        # DATABASE_URL (Postgres), REDIS_URL, LITELLM_URL,
                        # the model route + spend caps
```

### 3. Migrate, boot, verify

```bash
python -m forge.migrate             # alembic chain, currently head 031
python -m forge.doctor              # environment + capability report
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
* [`src/forge/adaptive/`](src/forge/adaptive/) — the authority surface: credential broker, revisions, closing budget, economics, operator view, ops limits, the closure matrix.
* [`scripts/`](scripts/) — the gates: conformance, PG qualification, profile freeze, cold-install checks, promotion. The release pipeline is in [`.github/workflows/`](.github/workflows/) and refuses to flatter itself.

## Providers and harnesses

### Provider capability matrix

| Capability | GitLab CE | GitHub | Azure DevOps |
|---|---|---|---|
| Commands (`/implement`, `/go`, `/cancel`, `/retry`, `/pause`, `/steer`, `/status`, `/why-blocked`, `/reconcile`, `/security`) | ✅ comments | ✅ comments + `forge` label | ✅ work-item + PR comments |
| Review feedback (`/fix`, `/ask`, `/approve-revision`) and bounded review rounds | ✅ live — the reference path | not yet | not yet |
| Plan comment + human gate | ✅ | ✅ | ✅ (work-item comment) |
| Harness execution | ✅ project CI (docker executor) | ✅ Actions (`workflow_dispatch`) | ✅ Pipelines (Runs-API dispatch) |
| Builtin LLM implementer (no CI needed) | ✅ | ✅ | ✅ |
| Trusted publisher | Commits API | GraphQL CAS (`expectedHeadOid`) | Push API CAS (`oldObjectId`) |
| Draft MR / PR before CI — one per run, reserved | ✅ | ✅ | ✅ (`isDraft: true`) |
| Readonly LLM review | ✅ MR notes | ✅ native reviews | ✅ PR threads (inline, sticky) |
| CI-failure debug lane | ✅ | ✅ (Actions timeline) | ✅ (Pipelines timeline + task logs) |
| Reactive review on push | ✅ | ✅ (incremental via before/after) | ✅ (incremental via PR iterations) |
| Credential delivery | native secrets, protected variables, runner redemption (all live-qualified) | native secrets | staged env |
| MCP servers in the lane | ✅ | ✅ | ✅ |
| Identity | bot user + PAT | GitHub App installation (+ PAT lab mode) | service account + PAT (Entra SPN = upgrade path) |
| Webhook authenticity | secret token | HMAC signature | Basic credentials (no HMAC exists) over HTTPS |

Delivery semantics, with the evidence level honestly stated per cell:

| Capability | GitLab CE | GitHub | Azure DevOps |
|---|---|---|---|
| Publication policy — builtin lane | live · enforced | contract-tested · validated at publish, not platform-enforced (known gap) | contract-tested |
| Publication policy — harness lane | live | contract-tested (shared publisher validation) | contract-tested |
| CI verification gate (`waiting_ci`) | contract-tested · required-jobs profile | live · `waiting_ci` + checks | contract-tested · parity in progress |
| Repair-in-place | implemented · contract-tested | implemented · contract-tested | implemented · contract-tested |
| Operator `/retry` + auto-revive | live | live | live |
| Exact-WIP cross-runner resume | live (three file shapes) | contract-tested | contract-tested |

GitLab CE 19.x is the reference profile: every capability this README describes is proven live on it, with the records committed under [`qualification/records/`](qualification/records/). GitHub keeps native CAS semantics explicit, never approximated; Azure verifies pipeline runs against `System.History` semantics.

### Drivers

Real clients against real binaries — [`src/forge/adaptive/drivers/`](src/forge/adaptive/drivers/), not prompt stubs; live verification status is tracked per binary in the DriverMatrix with evidence. Per-driver setup guides: [docs/harnesses/](docs/harnesses/README.md).

| Driver | Headless posture | Notes |
|---|---|---|
| **[Claude Code](docs/harnesses/claude-code.md)** | `-p` + stream-json, `--strict-mcp-config` | live-verified on all three providers |
| **[Grok Build](docs/harnesses/grok-build.md)** | `--always-approve` + deny rules, hardened npm preamble | platform-binary hang workaround |
| **[opencode](docs/harnesses/opencode.md)** | permission map via injected config | schema-translated MCP |
| **[GitHub Copilot CLI](docs/harnesses/copilot-cli.md)** | `-p` + deny-wins tool rules | subscription auth (fine-grained PAT) |

## Status

<!-- generated by scripts/generate_template_pins.py -- begin -->
(`ghcr.io/forcewake/forge:0.40.0` — digest `sha256:45de67f57c0058c0a5b54f2ab9bde8855138e629adf48a5757d10171917ac5a0`, verdict `promote`,
evidence `docs/releases/evidence/v0.40.0/promotion.json`, qualifying CI run `36261278972`).
<!-- generated by scripts/generate_template_pins.py -- end -->

**v0.40.0** — the honest tiers — what is machine-proven, what is live-proven on the lab, what waits on the world:

* **Live-proven** (the reference GitLab profile, records committed): issue → plan → approval → lane → Draft MR → independent CI → closing review → `ready_for_human`; material revision consumed by the resumed executor (zero rescue steers); cross-runner exact-WIP resume; operation-grant redemption end to end with sentinel identity proof; bounded review rounds; guarded budget amendments; measured operating envelope.
* **Machine-proven** (gates + mutation arms in CI): everything this README claims about refusals, idempotence, concurrency and budget enforcement.
* **Honestly open** (named in the records, not hidden): external design-partner acceptance; the blind planning review by real reviewers; a customer two-writer change; the second-engineer install. The 1.0 declaration gate is written down — [`docs/product/supported-contract.md`](docs/product/supported-contract.md) — with four of its six items already linked to their proof.

## What forge will never do

* **Merge, deploy, or resolve your discussions.** Reserved branches and Draft MRs are structural, not configurational.
* **Turn unknowns into zeros.** Unknown spend, missing evidence and ungraded work stay visible as exactly that.
* **Substitute silently.** A moved head, a rotated credential, a changed contract invalidates the evidence that depended on it — loudly.
* **Average your providers away.** GitLab, GitHub and Azure keep their real semantics in their adapters; no generic interface promises more than the platform beneath it delivers.

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
uv run python scripts/gate_conformance.py      # shipped-template conformance + mutation arms
uv run python scripts/pg_gate.py               # required PG profiles (fail-closed on missing fixtures)
uv run python scripts/freeze_supported_profile.py --check
uv run ruff check . && uv run ruff format --check .
```

Migrations are guarded: downgrades refuse while authorization evidence exists. The schema is at head 031; the chain runs on real Postgres in CI.

## License

BSD-3-Clause. Provenance and upstream acknowledgments: [`LICENSE`](LICENSE), [`UPSTREAM.md`](UPSTREAM.md).

---

forge is built by people who think the interesting part of agentic software is not watching it write code — it's being able to trust what happened while you looked away.
