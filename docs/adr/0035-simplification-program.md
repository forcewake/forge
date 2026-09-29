# 0035 — Simplification program: one lifecycle engine, a separate lane, fewer moving parts

- **Status:** Proposed. It becomes Accepted when the owner approves the written design.
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
  by release and qualification tooling, and 19.5K lines reached only by
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
  and docstrings, with 2,558 review, issue and ADR references. The docs
  contain about 319K words. Each release re-runs a qualification ceremony.

## Decision

forge follows a strangler program, called "subtract → separate → unify →
productize". The product stays deployable after every phase. The details
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
  - A period of dual paths exists, guarded by per-provider
    `FORGE_ENGINE_<PROVIDER>=v2` flags. Engine tables (`eng_*`) coexist
    with the legacy tables until both providers migrate. Flag rollback
    cancels in-flight engine runs and needs no database restore.
  - The model gateway becomes a required, critical component.
  - OIDC is required: GitLab ≥ 15.7, or GHES with OIDC.
  - Azure users stay on 0.42.x until the port adapter returns.
  - Historical qualification evidence is archived rather than maintained.
