# 0035 — Simplification program: one lifecycle engine, a separate lane, fewer moving parts

- **Status:** Accepted (2026-09-29, owner approval of the written design)
- **Date:** 2026-09-29
- **Design:** [docs/roadmap/2026-09-29-simplification/DESIGN.md](../roadmap/2026-09-29-simplification/DESIGN.md)
- **Roadmap:** [docs/roadmap/2026-09-29-simplification/ROADMAP.md](../roadmap/2026-09-29-simplification/ROADMAP.md)

## Context

forge grew from the Codeward import (2026-09-12) to v0.42.0 in 17 days:
486 commits, 42 releases, about 190K lines in `src/` and 212K lines of
tests. The safety core is sound and is kept unchanged. The weight around it
now slows every change:

- **About 28% of `src/` is not on a runtime path.** A static import analysis
  from the control-plane and lane entry points shows 33K lines reached only
  by release and qualification tooling, and 19.4K lines reached only by
  tests. Examples of test-only code: the publication saga, the work-package
  service and the reference twins.
- **The run lifecycle exists three times.** `runs/service.py` (GitLab,
  11.4K lines), `runs/github_service.py` (8.3K) and `runs/azure_service.py`
  (6.1K) repeat the same flows. `_advance_harness` alone is 701, 669 and 401
  lines. [ADR-0027](0027-lifecycle-consolidation.md) consolidated only the
  shared helpers.
- **The two providers disagree on what the executor receives.** GitLab
  resolves `ApprovedInput` from revisions; GitHub composes an
  `AttemptStartSpec`. This is the "divergent decision" defect class that
  ADR-0029 to ADR-0034 tried to control with registries and ownership maps.
- **The lane is not separable.** The lane entry points, which run in the
  customer's CI, import 132K lines of control-plane code.
- **Prose and process dominate.** About 28% of source lines are comments
  and docstrings, with at least 3,216 review, backlog and issue references
  and 705 ADR references. The docs contain about 319K words. Each release
  re-runs a qualification ceremony.

## Decision

forge follows the program "subtract → separate → unify → productize".
The original design used a strangler rollout; the owner-directed local
1.0 rewrite now uses the cold cutover described below. The details
are in the design document; the binding decisions are:

1. **Invariants I1–I8 are non-negotiable.**
   - I1: no merge.
   - I2: one validated write path.
   - I3: intent before effect, with probe-based recovery of unknown outcomes.
   - I4: consume-once approval, bound to digests.
   - I5: for metered drivers, budget is reserved from a cumulative run pool
     before spend and capped at the gateway, and unknown spend is never
     zero; unmetered drivers get wall-clock and turn caps and are
     recorded as `unmetered`.
   - I6: exactly one Draft PR/MR per run.
   - I7: crash safety.
   - I8: verification is bound to the candidate SHA.

   Each invariant has named tests in a required Invariant Suite.
2. **Scope for 1.0.** GitHub is the primary provider and GitLab the second.
   Azure DevOps moves to the maintenance branch `release/0.42.x` and returns
   after 1.0 through the new ports. The builtin LLM implementer is removed.
   Claude Code and Copilot CLI are the first-class drivers; Codex, opencode
   and Grok are contrib drivers without guarantees.
3. **Deferred and removed features.** The CI-debug lane, `/security`
   triage, the forge MCP server and multi-repository write work packages are
   deferred until after 1.0. The Codeward legacy paths are removed: the
   orchestrator, YAML flows and agents, and chat.
4. **Three packages in one uv workspace.** `forge-protocol` holds the
   versioned wire contracts. `forge-lane` is the CI runner and never imports
   the server. `forge` is the control plane.
5. **One provider-neutral lifecycle engine.** It works over narrow ports:
   `SubjectPort`, `RepoPort`, `ReviewPort`, `CIPort` and `ActorPort`.
   Provider write methods are reachable only through an `effects` layer
   that journals intent before the call and resolves unknown outcomes by
   probing. This makes I3 structural. Provider-native semantics stay inside
   the adapters: GitLab settling probes, GitHub `expectedHeadOid` and Azure
   `oldObjectId`.
6. **One contract for executor input:** `ApprovedInput`, identified by
   digest.
7. **The lane talks to the server over one HTTP protocol (v1),
   authenticated by CI OIDC tokens.** The protocol carries the brief,
   controls, checkpoints, candidate upload, report and model key. Each
   token is bound to exactly one attempt: a per-dispatch audience nonce
   on GitHub, the recorded pipeline id on GitLab, first-session binding
   of the provider run id, and `jti` replay protection. There is no
   shared-secret fallback in 1.0. CI artifacts serve only human
   debugging.
8. **Model credentials and hard spend caps come from the model gateway.**
   Each attempt generation gets a LiteLLM virtual key with a USD cap and a
   TTL, drawn from one cumulative implementer pool per run. Mint, update
   and block are journaled effects with probe-by-alias. Spend settles
   only after a settle window and two equal reads.
9. **Qualification machinery leaves the package.** Releases become: build
   once → tests and the Invariant Suite → one live end-to-end smoke per
   provider on the built artifact → sign and attest → publish.
10. **Guardrails for AI-agent execution:**
    - import-linter contracts;
    - size ratchets: no file or function may grow, and new code is limited
      to files of at most 600 lines and functions of at most 80 lines;
    - mypy strict for `domain`, `engine` and `ports`;
    - no review IDs in code;
    - PRs of at most about 500 net lines, except pure deletions.

## ADR impact

| ADR | Effect |
| --- | --- |
| 0000, 0001, 0003, 0004, 0005, 0006, 0007, 0008, 0009, 0010, 0011, 0012, 0014, 0016, 0017, 0018, 0026 | **Kept.** Their rules become Invariant Suite tests or engine behavior. |
| 0002, 0020, 0022, 0025, 0032 | **Amended.** Lane v1 dispatch, reusable workflow and OIDC, MCP config and trace filter move into `forge-lane`. Protocol versioning replaces the execution-spec pin. |
| 0013 | **Amended.** Hard caps are enforced at the model gateway through per-attempt keys; forge still reserves before spend and never records unknown spend as zero. |
| 0015 | **Amended.** The builtin backend is removed; the lane is the only implementer backend. |
| 0023 | **Amended.** Two first-class drivers plus contrib drivers; the selection chain is limited to installed drivers. |
| 0024 | **Superseded on `main`.** It remains valid on `release/0.42.x`. |
| 0028 | **Amended.** Draft-MR uniqueness and the authored-marker self-trigger guard become invariant tests; the Azure part goes with Azure. |
| 0019, 0021, 0027, 0029, 0030, 0031, 0033, 0034 | **Superseded.** Ports, the `effects` layer and import-linter contracts replace adapters, registries and ownership maps. ADR-0021 is historical. |

## Consequences

- **Positive:**
  - The server shrinks to at most about 75K lines and the lane to at most
    about 8K lines.
  - A lifecycle change is written once.
  - GitHub gains `/fix`, `/ask`, pause, steer and resume.
  - The install shrinks: no shared HMAC secrets in target repositories, no
    credential broker, and Redis becomes optional.
  - Agents work in a codebase that fits in their context.
- **Negative:**
  - Existing installations require an explicit migration boundary. The new
    service refuses an old schema; no flag restores the retired engine.
  - The model gateway becomes a required, critical component.
  - OIDC is required: GitLab ≥ 15.7, or GHES with OIDC.
  - Azure users stay on 0.42.x until the port adapter returns.
  - Historical qualification evidence is archived rather than maintained.

## Lane authentication implementation clarification (2026-10-23)

The original DESIGN §6.7 named `job_workflow_ref`, which GitHub emits for
jobs using a reusable workflow. A directly dispatched workflow instead
has `workflow_ref` ([GitHub OIDC reference](https://docs.github.com/en/actions/reference/security/oidc)).
The trusted dispatch composition explicitly chooses one claim name and
pins its complete workflow path and ref. Missing or mismatched claims
refuse authentication; there is no fallback to the other claim. Both
GitHub run id and run attempt remain bound on reconnection.
The caller `ref` is independently pinned to the dispatch's canonical
branch ref or explicit tag ref; a reusable workflow pin alone grants no
authority to run from another caller branch.

Lane sessions use a separate server-only signing key of at least 32 bytes
and expire after five minutes by default (configurable from 30 to 900
seconds). The provider JWT must have an integer expiry, issue time and
not-before time, a lifetime of at most 24 hours and an exact audience.
Configured HTTPS issuers and key endpoints are the only trust roots;
redirects and token-selected key URLs are refused. Replay receipts store
only a globally unique JTI hash and expiry in processed inbox rows. The
binding and receipt are committed together on the current run row, with
its cancellation counter captured separately from the lane generation.

The pending-dispatch deadline limits the first exchange. Reconnection
after that deadline still requires the already bound provider job, a
fresh provider JWT and an active, current dispatch. Every bearer call
rechecks the current dispatch and cancellation counter; API mutations
repeat that check in their database transaction. This is an implementation
clarification for review, not a claim of live provider acceptance.

## Lane API implementation (2026-10-23)

The `/lane/v1` router uses the shared protocol models and the OIDC service.
Its run evidence namespace is `lane_v1_api`; the authentication namespace
is retained separately. Controls are produced only by trusted server
composition. Every write locks the run row and conditionally updates the
current identity and captured cancellation generation. SQLite reserves a
write transaction before the read; PostgreSQL locks the row. Receipts,
including conflicting terminal submissions, survive service restart.

Blob uploads stream to a private temporary file, enforce a 16 MiB per-blob
limit, verify the complete SHA-256 digest, sync and link atomically. Reads
refuse symlinks, non-regular files, oversize files and corrupt digests.
Database references authorize each dispatch's reads; knowledge of a hash
alone grants no access. An interrupted or fenced upload may leave an
unreferenced immutable blob, never an authorized reference. Blob cleanup
and total storage quotas remain separate work. JSON requests are limited
to 1 MiB; dispatch references and checkpoint entries to 4,096; controls and
retained checkpoint manifests to 256 each. Limit violations refuse writes.

Checkpoint restore serves only the dispatch's explicitly named previous
generation from the same run and attempt, with matching base, approved
digest and driver. Candidate and report resubmission returns the exact
original receipt; conflicting bytes or metadata return a conflict.
Completed reports require an accepted candidate. A terminal report fences
new artifacts while allowing identical receipt retries.

New model-key minting is unavailable without an explicitly injected
provider; existing successful grants can be read without that provider.
Tests inject a fake; no direct provider key or legacy secret is substituted. Redemption is reserved durably before provider I/O and is
never replayed after a lost outcome. A successful grant is cached encrypted
with AES-GCM under a context-separated key derived from the server's
signing secret. Its authenticated data is the exact lane identity. Duplicate
and reconnect requests return that same unexpired grant, including after
server restart; expired or unauthenticatable cache entries fail closed.
Cancellation is rechecked before storing and delivering the grant. No raw
gateway key is stored in run evidence. Actual gateway minting belongs to
the model-gateway task. The old `/lane` surfaces remain separate.

## Owner-directed clean cutover (2026-10-23)

The owner requested a new 1.0 implementation and then explicitly required
"no legacy bullshit". The local rewrite therefore removes the planned
dual-engine routing, compatibility shims and flag rollback. It delivers one
engine, one schema and one required frozen-input contract. This amends the
original deployment sequence in DESIGN §6.10 and the coexistence portions
of P3-03, P3-17 and P3-20; their engine functionality and safety requirements
remain.

Additive tables may exist in disposable development databases while the
source is assembled. This does not authorize a mixed production deployment
or new compatibility tables/triggers. The assembled service must refuse an
old schema before accepting commands or starting a worker. Migration is an
explicit offline operation with backup and export; fresh installations use
one baseline. There is no automatic migration during startup.

The old service must be stopped before migration and cannot run against
the final schema. The new engine enforces canonical subject uniqueness,
consume-once digest-bound gates, cancellation fencing and effect recovery
itself. Historical invariants and applicable edge-case pins are ported to
these owners. No existing database, provider repository or hosted setting
is changed by this source decision. Hosted, live and pilot acceptance
remain separate from local implementation.
