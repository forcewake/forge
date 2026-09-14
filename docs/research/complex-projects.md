# Running Agent Factories on Complex Estates — Research (2026)

Scope: what is known in 2026 about running durable, gated agent runs (forge's plan → gate → agent → publisher) against *complex* software estates — monorepos, very large repos, multi-repo changes, legacy-modernization programs, dependency fleets — and what forge must add to RunSpec, verification, and the run model to be credible there. Primary sources: official docs (Nx/Bazel/Pants, Claude Code, AGENTS.md, GitHub, GitLab), engineering blogs and papers (Google, Anthropic, OpenAI, Cognition, Mend), and the benchmark literature. Each section ends with **What forge should build** and Sources. A ranked top-10 roadmap list is at the end.

Caveat used throughout: vendor case-study numbers (AWS "1,000 apps in 2 days", Anthropic "quarters instead of years") are marketing-adjacent; the papers and docs behind them are cited for *mechanics*, not headline numbers. Where published evidence is weak, that is said explicitly.

---

## 1. Monorepos

### 1.1 Path-scoped task routing

Monorepo discipline starts with the question "which paths may this task touch?" — answered before any agent runs, and re-checked after:

- **CODEOWNERS / GitLab CODEOWNERS** is the de-facto machine-readable ownership map: path globs → owning teams. Both forges use it to compute required reviewers and approval rules. Nx productized this idea for CI: the **Nx Owners plugin** defines ownership at *project-graph* level (not just file globs) and can generate CODEOWNERS files and split distributed CI tasks per owner — ownership derived from the graph instead of hand-maintained globs ([Nx owners docs](https://nx.dev/docs/enterprise/owners), [overview](https://nx.dev/docs/reference/owners/overview)). Sourcegraph goes further and *infers* ownership from a code graph ([boosting code ownership](https://sourcegraph.com/blog/boosting-code-ownership)).
- For an agent factory, ownership is not only a review-routing concern: it is an **authorization surface**. A work package should carry a path allowlist (and denylist); the publisher checks the produced diff against it before opening the MR/PR, and the gate routes human review to owners of touched paths. An agent writing outside its scope is a failed run, not a review comment.
- Task *intake* should also be path-aware: an issue's title/body plus repo topology determines which subsystem, which nested instruction files (§1.3), which test profile (§1.2), and which owners are involved. Google's migration tooling does exactly this classification before spending LLM calls (Kythe-based target identification + bucketing into not-migrated / irrelevant / relevant / left-over — see §4).

### 1.2 Affected-area and test selection (Nx / Bazel / Pants)

Three mature answers to "run only what the change can affect":

- **Nx**: builds a project graph from imports/tsconfig/package deps, diffs `base..head`, walks the graph downstream from changed projects, and runs only affected tasks (`nx affected:test`). Explicit knobs (`implicitDependencies`, `namedInputs`, target defaults) control over-approximation; the classic failure mode is "everything is affected" because a global file or over-broad input hash is in every project's closure ([Run only tasks affected by a PR](https://nx.dev/docs/features/ci-features/affected), [LeanIX case study](https://engineering.leanix.net/blog/smarter-nx-affected-checks/), [Nx discussion on over-broad affected sets](https://github.com/nrwl/nx/discussions/5580)).
- **Pants**: first-class changed-target selection — `--changed-since=<git ref>`, `--changed-include-dependees=direct/transitive`, `--test-changed` — designed exactly for presubmit checks; plus content-addressed caching so unchanged targets never rerun ([Advanced target selection](https://www.pantsbuild.org/dev/docs/using-pants/advanced-target-selection)).
- **Bazel**: no built-in "affected" command. Standard practice is `bazel query` / `cquery` over changed files, or **bazel-diff** (Tinder), which computes the exact set of impacted targets between two revisions via `cquery` on both graphs and set-difference ([bazel-diff](https://github.com/Tinder/bazel-diff), [Bazel query reference](https://bazel.build/query/language)). Wix documents the same pattern for their 60-repos-into-one monorepo ([Wix engineering, part 2](https://www.wix.engineering/post/from-60-repos-to-one-how-wix-tackled-monorepo-migration-part-2)).

The common contract: `(base revision, head revision) → set of targets/tests`. Every monorepo adopter builds or configures this once; CI and agents should consume the *same* selector.

### 1.3 Per-directory conventions — how agents resolve nested instruction files

This is now well-specified and differs per tool; a factory must model it explicitly because instructions are the cheapest quality lever on big repos:

- **Claude Code** (`CLAUDE.md`) has the most precise documented semantics ([memory docs](https://code.claude.com/docs/en/memory)):
  - Load order: managed policy → `~/.claude/CLAUDE.md` → project `./CLAUDE.md` (or `.claude/CLAUDE.md`) → `CLAUDE.local.md`. Rules in `.claude/rules/` load at launch; rules *with* `paths:` frontmatter are path-scoped.
  - **Ancestors up the tree are loaded and concatenated root-down** (deeper = later); **subdirectory files load lazily** — when Claude reads a file in that subtree.
  - `@path` imports expand at launch (relative to the importing file, max 4 hops); no automatic dedup — conflicting files cause arbitrary behavior; `claudeMdExcludes` exists specifically to prune other teams' irrelevant files in monorepos; target <200 lines per file; `/context` shows what loaded.
- **AGENTS.md standard** ([agents.md](https://agents.md/), 60k+ projects): root file for repo-wide rules, **nested files for subprojects with the closest file taking precedence**. Codex reads AGENTS.md before work and documents the root=global / nested=service-specific pattern with nested overriding root; **GitHub Copilot coding agent supports root + nested AGENTS.md scoped to paths** (changelog 2025-08-28, [announcement](https://github.blog/changelog/2025-08-28-copilot-coding-agent-now-supports-agents-md-custom-instructions/)); **opencode** reads root AGENTS.md (+ global rules), with nested-file-by-directory still an open ask ([rules docs](https://opencode.ai/docs/rules/)).
- Practical convergence: **layered, nearest-wins, lazily materialized by touched path**. A factory should not depend on the runtime agent discovering nested files; it should resolve the instruction set itself (root + ancestors + nested files for every path the task may touch) and inject/verify it.

### 1.4 Diff-size discipline and branch policies

- Google's migration paper measures **LLMΔ and HumanΔ** (Levenshtein edit distance on file snapshots) as the effort proxy and treats oversized/hallucinated diffs (reformatting, comment spam) as a failure mode to filter — diff size is a *validation signal*, not just style ([Migrating Code At Scale With LLMs At Google](https://arxiv.org/abs/2504.09691)).
- The stacked-diff world (Pragmatic Engineer's canonical writeup, Graphite, GitHub's native **stacked pull requests, public preview July 2026** — ordered layers, per-layer review/CI, one-click stack merge, automatic restack after bottom merge, `gh-stack` CLI integrated with Copilot) exists because small diffs review and revert better ([Pragmatic Engineer, stacked diffs](https://newsletter.pragmaticengineer.com/p/stacked-diffs), [GitHub changelog](https://github.blog/changelog/2026-07-30-stacked-pull-requests-are-now-in-public-preview/)). Rule of thumb from Graphite: don't serialize reviews of a stack — let downstack and upstack proceed in parallel ([Graphite stack review best practices](https://graphite.com/docs/best-practices-for-reviewing-stacks)).
- Branch policy baseline for agents: never push to protected branches; agent output lands as a draft MR/PR from a namespaced branch; required checks = the affected-task selector result; merge only through the merge queue/merge train. GitLab's Duo Agent Platform (GA Jan 2026) standardizes sessions that end in MRs on GitLab ([GA announcement](https://about.gitlab.com/press/releases/2026-01-15-gitlab-announces-duo-agent-platform-general-availability/), [docs](https://docs.gitlab.com/user/duo_agent_platform/)); Copilot coding agent does the same on GitHub.

### What forge should build (monorepos)

- **Path scope in RunSpec**: `allowed_paths` / `denied_paths` globs per work package; publisher hard-fails any diff outside scope (this belongs in the publisher check, not the agent's honor system).
- **Affected-area adapter interface**: pluggable selector (`nx affected`, `pants --changed-since`, `bazel-diff`, or a generic path→test-map fallback) that answers `(base, head) → verification targets`; the **verification profile** consumes it instead of repo-wide suites. Cheap-to-compute selector results are evidence artifacts in the run.
- **Ownership routing**: parse CODEOWNERS (or Nx owners) at plan time; gate = owners of touched paths; refuse tasks whose owners cannot approve (bot ≠ human-approver identity per ADR-0018).
- **Instruction-set resolution as a first-class step**: resolve CLAUDE.md/AGENTS.md layering per touched path (root + ancestors + nearest-per-path, lazy but materialized), inject into the agent context, and record digests in RunSpec so instruction drift is detectable.
- **Diff budget**: max-files / max-edit-distance budgets per task; overflow → auto-split into a stack (§6) or fail with a decomposition hint.

Sources: [Nx affected](https://nx.dev/docs/features/ci-features/affected) · [LeanIX on Nx affected](https://engineering.leanix.net/blog/smarter-nx-affected-checks/) · [Pants advanced target selection](https://www.pantsbuild.org/dev/docs/using-pants/advanced-target-selection) · [bazel-diff](https://github.com/Tinder/bazel-diff) · [Bazel query](https://bazel.build/query/language) · [Wix monorepo part 2](https://www.wix.engineering/post/from-60-repos-to-one-how-wix-tackled-monorepo-migration-part-2) · [Nx owners](https://nx.dev/docs/enterprise/owners) · [Sourcegraph ownership inference](https://sourcegraph.com/blog/boosting-code-ownership) · [Claude Code memory](https://code.claude.com/docs/en/memory) · [agents.md](https://agents.md/) · [opencode rules](https://opencode.ai/docs/rules/) · [Copilot AGENTS.md support](https://github.blog/changelog/2025-08-28-copilot-coding-agent-now-supports-agents-md-custom-instructions/) · [Google LLM migration paper](https://arxiv.org/abs/2504.09691) · [Stacked diffs (Pragmatic Engineer)](https://newsletter.pragmaticengineer.com/p/stacked-diffs) · [GitHub stacked PRs preview](https://github.blog/changelog/2026-07-30-stacked-pull-requests-are-now-in-public-preview/) · [Graphite stack review](https://graphite.com/docs/best-practices-for-reviewing-stacks) · [GitLab Duo Agent Platform GA](https://about.gitlab.com/press/releases/2026-01-15-gitlab-announces-duo-agent-platform-general-availability/)

---

## 2. Large repositories / long context

### 2.1 Repo maps: the aider pattern

Aider's repo map remains the reference design ([Building a better repository map with tree sitter](https://aider.chat/2023/10/22/repomap.html), [docs](https://aider.chat/docs/repomap.html)):

1. Parse every file with **tree-sitter** (one grammar per language, no embeddings); extract definitions and references of classes/functions/methods.
2. Build a directed graph: files are nodes, cross-file references are edges.
3. Rank with **a PageRank-style algorithm** — widely-referenced files (base classes, core utils) surface first; symbols mentioned in the current chat get boosted.
4. Binary-search the map down to a **token budget** (≈1k tokens by default).

The insight is that a *map* (signatures + importance ranking) beats a *bag of files*: aider reached strong SWE-bench results with tree-sitter + PageRank and no vector RAG. The pattern is now catalogued generically ([Repository Map Pattern](https://agentpatterns.ai/context-engineering/repository-map-pattern/)).

### 2.2 What actually moves success rates (SWE-bench-ish, without over-claiming)

- **Localization is the binding constraint.** SWE-bench's "oracle retrieval" setting (gold files handed to the model) is the upper bound; the gap between oracle and agent-driven localization is where most headroom lives ([SWE-bench oracle setting](https://evalscope.readthedocs.io/en/latest/third_party/swe_bench.html), [oracle dataset](https://huggingface.co/datasets/princeton-nlp/SWE-bench_oracle)); the remaining unsolved mass is concentrated in retrieval-heavy/hard tasks while easy ones saturate ([subsets analysis](https://jatinganhotra.dev/blog/swe-agents/2025/06/05/swe-bench-verified-discriminative-subsets.html)); SWE-Explore (2026) isolates repository-exploration ability as a distinct measurable capability ([paper](https://arxiv.org/html/2606.07297v1)).
- **Prompt optimization saturates fast; retrieval/model choice don't.** Augmentcode's #1-open-source SWE-bench run (65.4% at the time) attributed gains to model pairing + retrieval/localization, with prompt tuning as a small one-off ([post](https://www.augmentcode.com/blog/1-open-source-agent-on-swe-bench-verified-by-combining-claude-3-7-and-o1)).
- **Test-time compute is real**: sampling multiple trajectories and selecting via verification beats single-shot at fixed model; small models with efficient scaffolds reached ~74.8% by spending test-time compute ([Nebius on training+search](https://nebius.com/blog/posts/training-and-search-for-software-engineering-agents), [74.8% small-model run](https://blog-en.fltech.dev/entry/2026/04/07/swebench)); a reviewer/verifier agent added to a solver lifted resolution materially in a small-n experiment ([100-task comparison](https://www.reddit.com/r/ClaudeAI/comments/1qi2gh0/i_ran_100_swebench_tests_comparing_1_agent_vs_2/)).
- **Benchmarks flatter reality**: a benchmark-mutation study estimates classic SWE-bench-style scores overestimate agent capability by **20–50%** ([arXiv:2510.08996](https://arxiv.org/html/2510.08996v2)); measured training-data leakage ~10.6% on Verified ([arXiv:2512.10218](https://arxiv.org/html/2512.10218v2)); OpenAI argues Verified is contaminated with ≥16.4% flawed tests; SWE-bench Pro (long-horizon, proprietary code) sits near ~23% for frontier models while Verified is 70%+; SWE-rebench / SWE-bench-Live exist to counter this ([SWE-rebench](https://swe-rebench.com/about)). Translation: treat any single published % as marketing until it survives a fresh-task or internal-fleet check.

### 2.3 Symbol-level retrieval vs raw file reads; budgets; ordering

- Prefer **symbol-granular retrieval** (tree-sitter/LSP/ctags indexes: "who calls `publishOrder`", "where is `RetryPolicy` defined") over whole-file reads; read whole files only when editing them (Google's migration editor takes the *whole file plus suggestive line numbers*, not fragments — for edits, whole-file context reduces patch errors).
- **Evidence budgets**: cap each evidence item (test output tail, stack trace, log excerpt, CI failure) at a token budget and keep the *structured* part (exit codes, failing target names, first error) — never paste 10k-line logs. This is the same cheap-to-expensive philosophy Google uses in validation ordering (§4).
- **Caching-friendly context ordering**: put the stable prefix first — system prompt, repo-level instruction files, repo map, dependency manifests — and volatile content (file contents being edited, command output) last, so provider prompt caches (KV-cache reuse) hit across steps and across parallel attempts of the same task. Layer instruction files deepest-last (Claude Code concatenates root-down so nearest instructions read last — [memory docs](https://code.claude.com/docs/en/memory)); keep each instruction file small (<200 lines) or adherence measurably degrades.
- **Instruction layering** (root → subsystem → module) doubles as retrieval: a well-maintained nested-AGENTS.md tree encodes what humans already know about which conventions bind where, at zero runtime index cost.

### What forge should build (large repos)

- **Repo-map service**: incremental tree-sitter symbol index per repo snapshot (cache keyed by commit OID, matching RunSpec's source-snapshot digest); expose `repo_map(token_budget, focus_paths)` to every agent step.
- **Symbol retrieval tool** for agents (LSP or tree-sitter queries) instead of raw grep-and-read; measure tool-usage mix per run (reads vs symbol queries) as a quality signal.
- **Evidence budget enforcement in the harness**: truncate/structure test output before it enters agent context or gate evidence; store full logs out-of-band with digests.
- **Context-order contract** in the harness prompt builder: stable prefix (system, resolved instruction files, repo map) → task brief → volatile evidence; this is a cheap 2–3x cost/latency win via prompt caching on repeated attempts and best-of-N sampling.
- **Localization-quality metric**: per run, record (files touched by agent) vs (files actually needed per review/human fix); feeds §7's eval fleet.

Sources: [aider repo map](https://aider.chat/2023/10/22/repomap.html) · [aider repomap docs](https://aider.chat/docs/repomap.html) · [Repo Map Pattern](https://agentpatterns.ai/context-engineering/repository-map-pattern/) · [SWE-bench oracle retrieval](https://evalscope.readthedocs.io/en/latest/third_party/swe_bench.html) · [SWE-Explore](https://arxiv.org/html/2606.07297v1) · [SWE-bench subsets analysis](https://jatinganhotra.dev/blog/swe-agents/2025/06/05/swe-bench-verified-discriminative-subsets.html) · [Augmentcode SWE-bench](https://www.augmentcode.com/blog/1-open-source-agent-on-swe-bench-verified-by-combining-claude-3-7-and-o1) · [Nebius training+search](https://nebius.com/blog/posts/training-and-search-for-software-engineering-agents) · [fltech 74.8%](https://blog-en.fltech.dev/entry/2026/04/07/swebench) · [Benchmark mutation study](https://arxiv.org/html/2510.08996v2) · [Leakage study](https://arxiv.org/html/2512.10218v2) · [SWE-rebench](https://swe-rebench.com/about) · [Claude Code memory](https://code.claude.com/docs/en/memory) · [Google migration paper](https://arxiv.org/abs/2504.09691)

---

## 3. Multi-repo changes

### 3.1 State of the art: mostly single-repo agents + human coordination

- **Devin** is the furthest along: environments (Blueprints) can mount multiple repos, periodic indexing builds "Repo Knowledge" (wikis, architecture diagrams) for cross-repo awareness, and its docs describe a coordinator role — scoping work, monitoring progress, resolving conflicts, and compiling results across work spanning "many files, modules, or repositories" ([advanced capabilities](https://docs.devin.ai/work-with-devin/advanced-capabilities), [environment setup](https://docs.devin.ai/onboard-devin/environment), [Dec '24 update](https://cognition.com/blog/dec-24-product-update)). This is coordination around single-repo sessions, not transactional multi-repo commits.
- **Copilot coding agent** runs in a GitHub Actions VM scoped to one repository and produces one draft PR; multi-repo is not a first-class capability. **SWE-agent/swe-bench-lineage scaffolds** are single-repo by construction. Anthropic's and OpenAI's harnesses are likewise repo-session-scoped; cross-repo work is decomposed by the human or orchestrator into per-repo sessions.
- **Change stacks** are the cross- and intra-repo coordination primitive that got native support in 2026: GitHub stacked PRs (public preview, July 2026) give ordered layers, per-layer checks, one-click whole-stack merge, and auto-restack; Graphite/Mergify/Aviator merge queues merge stacks in dependency order ([GitHub changelog](https://github.blog/changelog/2026-07-30-stacked-pull-requests-are-now-in-public-preview/), [Graphite](https://graphite.com/docs/best-practices-for-reviewing-stacks)).
- **Copy-on-sync** (Google's monorepo pattern for synchronizing snapshots into/out of the monorepo, e.g. Android/third-party) remains the reference for "one logical change, N destinations": export is mechanical, and atomicity is replaced by *tooling-enforced sequence*.

### 3.2 What replaces atomicity

Since cross-repo atomic merge is impossible with public forges, real programs replace it with ordered, evidenced coordination:

1. **Contract-first sequencing**: define/publish the shared contract first (API schema, protobuf, interface module, versioned package) and merge it; consumers then migrate in waves. Google's migrations do this via dependency-driven ramp-up (small, localized IDs first — §4).
2. **Coordinated PR chains with cross-links**: every PR in the set links the parent change; merge order is declared, not implied; merge queue enforces it. Version pins / lockstep releases keep intermediate states coherent.
3. **Evidence bundle replacing atomicity**: (a) contract/consumer compatibility tests run against both old and new contract versions; (b) cross-repo CI matrix (the *consumer* repos' affected suites run against the producer's candidate head); (c) declared merge order executed by a queue; (d) revert plan per repo (which revert makes the estate coherent again).
4. **A coordinator role with full context** — Devin's pattern, and consistent with Cognition's context-engineering rules (§6): one planner holds the cross-repo picture; per-repo executors get scoped briefs; the coordinator owns ordering and conflict resolution.

### What forge should build (multi-repo)

- **Work-package spanning N repos** in the run model: one run → N child task executions, each with its own branch/PR, sharing a declared merge order (DAG over repos). RunSpec carries the full repo set + merge order digest.
- **Stack/merge-order enforcement at the publisher**: open PRs with base = previous PR's branch (stack) or via merge-queue ordering; integrate with GitHub stacked PRs / GitLab merge trains rather than reimplementing.
- **Cross-repo verification gate**: "consumer matrix" step that checks out declared consumer repos at their pinned refs against the candidate producer head and runs their affected suites — this is the evidence that replaces atomicity.
- **Cross-repo conflict check at admission**: if two concurrent runs touch the same producer/consumer edge, serialize them (path-lease semantics across repos, §6).
- **Estate-level revert story**: each multi-repo run emits a revert plan (ordered per-repo reverts) stored with the run record.

Sources: [Devin advanced capabilities](https://docs.devin.ai/work-with-devin/advanced-capabilities) · [Devin environment setup](https://docs.devin.ai/onboard-devin/environment) · [Devin Dec '24 update](https://cognition.com/blog/dec-24-product-update) · [GitHub stacked PRs preview](https://github.blog/changelog/2026-07-30-stacked-pull-requests-are-now-in-public-preview/) · [Graphite stack review](https://graphite.com/docs/best-practices-for-reviewing-stacks) · [Stacked PRs guide](https://pullnotifier.com/tools/stacked-prs) · [Copilot coding agent AGENTS.md](https://github.blog/changelog/2025-08-28-copilot-coding-agent-now-supports-agents-md-custom-instructions/)

---

## 4. Legacy modernization / migrations at scale

This is the area with the strongest *documented industrial mechanics* in 2025–2026. Five programs, then the repeating pattern.

### 4.1 Google — LLM migrations in the monorepo ([paper](https://arxiv.org/abs/2504.09691), [blog](https://research.google/blog/accelerating-code-migrations-with-ai/))

Workflow for the int32→int64 ID migration (39 migrations, ~93.5k Levenshtein edits, 3 developers):

1. **Static-analysis target identification** via Kythe: direct + transitive (≤5 hops) references — deliberately conservative superset; precise dataflow deemed too expensive to maintain.
2. **Categorization** into buckets: not-migrated / irrelevant / relevant / left-over — effectively a machine-maintained **keep / fix / exclude ledger**.
3. **Whole-file LLM editing** (fixed model, temp 0.0, entire file + suggestive line comments; fuzzy-apply diffs; 3 attempts).
4. **Staged validation, cheap → expensive**, any failure discards to manual: completes → not whitespace-only → AST parses *and changed* → "was this change even necessary?" LLM punt-check → build → regression tests.
5. **Mandatory human review in Critique, then code owners.** No auto-merge anywhere.

Results: **74.45% of changes LLM-generated; developers estimated ~50% total-effort reduction** (self-reported). Lessons: hallucinated reformatting/comments are the top noise; golden-file and pre-broken tests block validation and need triage; ramp up by dependency complexity (small local IDs first, cross-team ones last); measure LLMΔ vs HumanΔ.

### 4.2 Anthropic — AI-driven COBOL modernization methodology ([blog](https://claude.com/blog/how-ai-helps-break-cost-barrier-cobol-modernization))

Phases: AI reads the codebase → entry points + execution paths; **dependency mapping including implicit coupling** (shared files, DBs, global state — the stuff static call graphs miss); workflow documentation ("pipelines nobody remembers building"); risk analysis (high-coupling = risky, isolated = early candidates); AI proposes sequencing, **humans own priorities/architecture/standards**; incremental implementation one component at a time with API wrappers around not-yet-migrated pieces and **old/new running side by side**. Guardrails: **test design settled before any code changes**; equivalence tests assert identical outputs vs legacy; "small blast radius — you never have massive changes in flight"; wave planning from bounded pilot to harder components. Claim ("quarters not years") is directionally plausible but vendor-graded; Thoughtworks' critique is a useful corrective ([Thoughtworks reality check](https://thoughtworks.medium.com/claude-code-and-cobol-modernization-whats-the-reality-2e6022b5022a)).

### 4.3 OpenAI — Codex modernization cookbook ([Modernizing your codebase with Codex](https://developers.openai.com/cookbook/examples/codex/code_modernization))

The most factory-like published workflow:

- **Planning contract up front**: `AGENTS.md` + `PLANS.md` define when/where plans live (`ExecPlan` with Progress / Decision log / Outcomes sections) — auditable document trail as the product.
- **Bounded pilot**: agent proposes 1–2 realistic-but-bounded flows; pilot plan contains inventory, technical report, target design, and a **parity test plan written before implementation** (scaffold tests with placeholder assertions first).
- **Inventory phase** produces a human-verified overview (programs, jobs, data files, data-flow diagram); engineers add what the agent can't infer (SLAs, owners, which jobs actually run).
- **Parity-first implementation**: generated code keeps comments tracing back to original COBOL paragraphs; parity tests run legacy and modern flows on the same inputs and compare outputs; failures trigger "smallest change to the modern implementation" loops.
- **Scale**: turn the pilot into templates; one customer fed **hundreds of Jira tickets** through Codex with risk flagging and cross-cutting dependency surfacing, plus a **separate validator role doing review and merges** — i.e., producer/consumer separation, exactly a factory gate.

### 4.4 AWS — Amazon Q transform agents ([Java upgrades](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/code-transformation.html), [.NET transform](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/transform-dotnet-IDE.html), [deep dive](https://aws.amazon.com/blogs/devops/amazon-q-developer-java-upgrades-a-deep-dive-into-new-selective-transformation-feature/))

Agent-shaped framework upgrades: analyze codebase → build a transformation plan → execute stepwise (code changes, dependency updates, build fixes) → iterate on build errors. Java 8/11→17 (Maven), 17→21 "selective transformation" (minimal required dependency changes), .NET Framework → cross-platform .NET on Linux. The "1,000+ production apps upgraded in 2 days" figure is an AWS internal claim — treat as marketing; the *mechanics* (plan-then-execute agent, build-fix loop, selective/minimal-change mode) are the transferable part.

### 4.5 IBM — watsonx Code Assistant for Z ([product](https://www.ibm.com/products/watsonx-code-assistant-z), [Z Validation Assistant](https://www.ibm.com/docs/en/watsonx/watsonx-code-assistant-4z/1.x?topic=validate-using-z-validation-assistant), [quality-eval paper](https://arxiv.org/html/2507.23356v1), [CROZ review](https://croz.net/honest-take-on-watsonx-code-assistant-for-z/))

The most explicit **equivalence-evidence** machinery on the market: generate COBOL test data, run it through the original COBOL program and the translated Java, compare results — equivalence testing as a productized gate, exposed in-chat from 2.0. Independent reviews (CROZ) note it is assist-per-artifact rather than a fully automated program: consistent with the pattern that equivalence evidence, not translation quality, is the hard part.

### 4.6 The repeating pattern (what every credible program shares)

1. **Inventory** (machine-built, human-verified) — programs, callers, data flows, hidden coupling.
2. **Phase**: risk-ranked sequencing; humans own priority/target architecture.
3. **Pilot**: one bounded flow end-to-end; produce templates from it.
4. **Test-scaffolding-first**: tests (characterization tests in Feathers' sense, parity tests, generated unit tests) exist *before* the transformation; the Sourcegraph 7-Rs playbook and both Anthropic/OpenAI methodologies converge here ([Sourcegraph legacy modernization](https://sourcegraph.com/blog/legacy-code-modernization)).
5. **Guardrails + review gates**: deterministic codemods (OpenRewrite for Spring Boot/JVM recipes, .NET Upgrade Assistant, jscodeshift/EF codemods) do mechanical steps; LLMs do judgment steps; every step passes cheap→expensive validation; humans review everything that ships.
6. **Campaigns as first-class work packages**: waves with entry/exit criteria, a per-target ledger (keep / fix / exclude + reason), and a source-oracle that tracks % migrated and blocks "done" claims.

### What forge should build (modernization)

- **Campaign spec** (new work-package type, not a big issue): inventory artifact ref, target architecture doc, transformation rules (codemod set + LLM instructions per rule), wave plan (ordered batches with entry/exit criteria), per-target ledger (`source_ref, decision: keep|fix|exclude, reason, wave`).
- **Wave runner**: batch N targets through the same transformation rule as sibling work packages; per-wave evidence bundle (what changed, tests, parity results); wave gate = equivalence evidence + review, then next wave unlocks.
- **Source-oracle tracking**: the campaign's inventory is the oracle — every target's state (migrated / blocked / excluded) is derived from run outcomes, never hand-maintained; percent-migrated and blocking-reason dashboards fall out for free.
- **Equivalence evidence primitive**: side-by-side runner (legacy input dataset → legacy impl + new impl → diff report) as a verification-profile step; for framework upgrades, the equivalents are characterization-test suites and before/after contract-test runs.
- **Codemod-before-LLM policy**: mechanical transformations run as deterministic tools inside the agent environment; the agent handles residuals. Record which tool vs LLM produced each hunk (Google's LLMΔ/HumanΔ split, per-hunk).
- **Keep/fix/exclude decisions as gate data**: excluded targets require a human-approved reason; "done" for a wave is defined as no unledgered targets remain.

Sources: [Google paper](https://arxiv.org/abs/2504.09691) · [Google blog](https://research.google/blog/accelerating-code-migrations-with-ai/) · [Anthropic COBOL](https://claude.com/blog/how-ai-helps-break-cost-barrier-cobol-modernization) · [Thoughtworks critique](https://thoughtworks.medium.com/claude-code-and-cobol-modernization-whats-the-reality-2e6022b5022a) · [OpenAI cookbook](https://developers.openai.com/cookbook/examples/codex/code_modernization) · [Amazon Q Java](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/code-transformation.html) · [Amazon Q .NET](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/transform-dotnet-IDE.html) · [Q selective transformation](https://aws.amazon.com/blogs/devops/amazon-q-developer-java-upgrades-a-deep-dive-into-new-selective-transformation-feature/) · [WCA4Z](https://www.ibm.com/products/watsonx-code-assistant-z) · [Z Validation Assistant](https://www.ibm.com/docs/en/watsonx/watsonx-code-assistant-4z/1.x?topic=validate-using-z-validation-assistant) · [WCA4Z quality eval](https://arxiv.org/html/2507.23356v1) · [CROZ review](https://croz.net/honest-take-on-watsonx-code-assistant-for-z/) · [Sourcegraph 7 Rs](https://sourcegraph.com/blog/legacy-code-modernization)

---

## 5. Dependency upgrades

### 5.1 Baseline tooling (non-AI)

- **Renovate**: config-driven update PRs with grouping, schedules, stability/status checks, `minimumReleaseAge` (don't take releases younger than N), and **automerge gated on required status checks**; automerge is intentionally conservative and needs merge-queue-aware configuration ([automerge docs](https://docs.renovatebot.com/key-concepts/automerge/)).
- **Mend Merge Confidence / Smart Merge Control**: the notable 2025-26 productization — per-update confidence from **age, adoption, and CI pass rates across Mend's fleet**, used to allow automerge only for high-confidence updates ([Merge Confidence workflows](https://docs.mend.io/wsk/renovate-smart-merge-control-implementation-exampl)).
- **Dependabot**: grouped version updates, ecosystem-specific registries; first-party AI *remediation* beyond changelog notes is thin — most AI value in 2026 is added by pairing Renovate output with your own agent.

### 5.2 What AI adds

- **Breaking-change analysis**: reading changelogs/commits/types to predict whether an update breaks *this* codebase — research and products converged on this in 2025 ([ACM: automatically fixing dependency breaking changes with LLMs](https://dl.acm.org/doi/10.1145/3729366)); Mend's confidence data is the fleet-level complement.
- **Code adaptation**: when CI fails on an update, an agent adapts call sites (the same plan→build-fix loop as §4.4 applied to upgrades). This is where a factory beats both Dependabot and Renovate: they stop at "CI failed", an agent repairs.
- **Triage**: classifying the update stream (security vs minor vs major; touched subsystems; owners) and merging safe ones aggressively while spending agent budget only on majors with real breakage.

### 5.3 Batch vs single

Empirical tooling defaults: **single-PR-per-update for majors** (isolated blame/revert), **grouped batches for minor/patch and for lockfile maintenance**; security fixes immediate. Batch big only when the test loop is cheap and trusted (fast affected suites), otherwise bisecting a red batch costs more than the extra PRs. Renovate's group + automerge + merge-queue combination is the working pattern at scale.

### What forge should build (dependency upgrades)

- **Upgrade profile per repo** (a RunSpec template family): allowlist/denylist of packages, grouping policy, `minimumReleaseAge`-style holdback, evidence tier by risk (patch: affected tests; minor: affected + smoke; major/security-sensitive: full profile + contract tests + canary deploy before merge where the estate allows).
- **Recurring campaign, not one-offs**: scheduled sweep produces a ledger of pending updates with Mend-style confidence + AI breaking-change verdicts; automerge the green singles via a merge queue; agent-repair the red ones as work packages; majors become reviewed tasks.
- **Adaptation loop with bounded retries**: on red CI, agent gets the failing evidence (§2 evidence budgets) and N attempts; persistent failure downgrades the update to "needs human" with the evidence attached — never merge on retries.
- **Canary evidence**: for services, upgrade evidence includes a canary deploy check when configured; for libraries, downstream-repo consumer matrix (reuse §3's cross-repo gate).

Sources: [Renovate automerge](https://docs.renovatebot.com/key-concepts/automerge/) · [Mend Smart Merge Control](https://docs.mend.io/wsk/renovate-smart-merge-control-implementation-exampl) · [Mend Renovate enterprise](https://www.mend.io/blog/mend-renovate-enterprise-cloud-dependency-updates-at-scale/) · [ACM breaking-change repair](https://dl.acm.org/doi/10.1145/3729366) · [Renovate 42 (minimumReleaseAge semantics)](https://github.com/renovatebot/renovate/discussions/39133) · [Amazon Q upgrades deep dive](https://aws.amazon.com/blogs/devops/amazon-q-developer-java-upgrades-a-deep-dive-into-new-selective-transformation-feature/)

---

## 6. Task decomposition

### 6.1 Plan as DAG, with a strong default toward sequences

- **Cognition's position** ("Don't Build Multi-Agents") is the sharpest articulation of the risk: (1) **share full context** — agents acting on partial context make conflicting decisions; (2) **actions carry implicit decisions** — parallel actors with divergent context produce incoherent systems; a single-threaded linear agent with compression is often all you need ([post](https://cognition.com/blog/dont-build-multi-agents)). Devin's coordinator pattern (§3) and OpenAI's cookbook (§4.3, one producer + separate validator) are consistent: *sequential pipeline with role separation*, not free-running parallel agents.
- **Anthropic's counterpoint**: parallelism works when contexts are **disjoint by construction** — Claude Code's documented approach is git worktrees (each session its own checkout, so file conflicts are impossible) plus an orchestrator/agent-teams pattern with a shared task list ([run agents in parallel](https://code.claude.com/docs/en/agents), [Claude Agent SDK — orchestrator workers]). The synthesis both camps accept: **parallel is safe when tasks share no files and no decisions; otherwise serialize or stack**.
- **Plan representation**: forge's plan is already a DAG of tasks; the 2026 lesson is that DAG edges should encode two different things — *data dependency* (needs artifact from predecessor) and *conflict* (touches overlapping paths → must not run concurrently), and both must be visible in RunSpec.

### 6.2 Stack semantics for dependent work

When packages are dependent, the correct run-model primitive is the **change stack**: each task's branch bases on its predecessor's branch; each layer is independently reviewed/checked; merging propagates (restack) automatically ([GitHub stacked PRs](https://github.blog/changelog/2026-07-30-stacked-pull-requests-are-now-in-public-preview/), [Graphite best practices](https://graphite.com/docs/best-practices-for-reviewing-stacks)). Rules that matter for a factory: don't block upstack work on downstack review; restack on merge (mechanical, automatable); the merge queue must merge in order. GitLab equivalent: stacked MRs + merge trains.

### 6.3 When parallel is worth it: the conflict graph

Compute a conflict graph over candidate work packages (shared files/modules → edge; shared test targets → edge). Parallelism payoff:

- **High**: packages in disjoint subtrees with disjoint owners and test profiles (e.g., N services, N modules, per-package migration waves); embarrassingly parallel campaigns (§4 waves are sibling-disjoint by construction).
- **Low/negative**: packages sharing hot files (changelogs, lockfiles, generated APIs, shared schemas, `public_api`); tasks whose verification is repo-wide (two agents invalidating each other's green builds); anything touching one lockfile — serialize.
- **Cheap alternative to parallel code-writing**: parallel *verification* of one artifact — best-of-N sampling with a verifier/selector spends the same compute with no merge risk, and is what actually moves measured success rates (§2.2). Prefer this over agent-per-shard for single tasks.

### What forge should build (decomposition)

- **Task graph with edge semantics**: `depends_on` (order) vs `conflicts_with` (disallow concurrency) derived from a planner that predicts touched paths per task; conflict edges validated at admission against actual diffs so far.
- **Path leases**: at dispatch, a task reserves its predicted file set for the repo; concurrent dispatch checks leases (DB unique/conditional constraints — forge already has fenced leases in the step runtime, this is the same idea at file granularity).
- **Stack primitives**: `stack_parent` on work packages; publisher opens stacked PRs; restack-on-merge handler; stack-level merge via queue.
- **Disjointness guardrail**: refuse to parallelize two tasks whose predicted path sets intersect (or auto-stack them) — encode Cognition's rule as an invariant, not advice.
- **Best-of-N verification mode**: one writer, N candidate patches, verifier selects (covers test-time-compute wins without multi-agent context fragmentation).

Sources: [Cognition: Don't Build Multi-Agents](https://cognition.com/blog/dont-build-multi-agents) · [jxnl analysis](https://jxnl.co/writing/2025/09/11/why-cognition-does-not-use-multi-agent-systems/) · [Claude Code parallel agents](https://code.claude.com/docs/en/agents) · [GitHub stacked PRs](https://github.blog/changelog/2026-07-30-stacked-pull-requests-are-now-in-public-preview/) · [Graphite stack review](https://graphite.com/docs/best-practices-for-reviewing-stacks) · [Devin advanced capabilities](https://docs.devin.ai/work-with-devin/advanced-capabilities) · [OpenAI cookbook (validator role)](https://developers.openai.com/cookbook/examples/codex/code_modernization) · [Nebius training+search](https://nebius.com/blog/posts/training-and-search-for-software-engineering-agents)

---

## 7. Evaluation: measuring a factory on complex estates

### 7.1 Task success, defined honestly

A ladder, each rung needing stronger evidence; report the rung, not a single number:

1. Patch applies & compiles (AST/build check) — nearly meaningless alone.
2. Selected tests pass (affected-area evidence, §1.2) — the SWE-bench-style definition; watch for weak/absent tests (≥16% of SWE-bench Verified tasks have flawed tests that reject correct patches — assume your repos have some too).
3. Full verification profile passes (build + affected + contract/equivalence where applicable).
4. **Merged via the normal gate** (human approved, owners signed off) — the first commercially meaningful rung.
5. **Merged without material human rework** (measured: Google's LLMΔ vs HumanΔ on review edits; or commit-since-PR-diff) — the factory-quality rung.
6. **Survives** (no revert, no follow-up fixup PR attributable to the change within N weeks) — the truth rung nobody publishes.

### 7.2 Human-acceptance rate is the primary factory metric (done right)

Not "suggestion acceptance rate" (the Copilot completion metric) — that one is documented as easy to measure, easy to misuse, and uncorrelated with value ([GetDX/Tacho](https://getdx.com/blog/ai-acceptance-rate-easy-measure-misuse-laura-tacho/), [Augment DHI](https://www.augmentcode.com/tools/developer-happiness-index-benchmarking-ai-coding-tools)). The factory-grade version: **PR-acceptance rate** = share of published PRs merged without human-authored corrections, trended per repo type, campaign, and model/scaffold version; supplemented by reviewer-comment counts, time-in-review, edit distance after review, and revert rate. DORA-style throughput metrics destabilize when 30–70% of code is machine-written — interpret them as estate load, not productivity ([DORA-in-AI-era critiques](https://larridin.com/developer-productivity-hub/why-dora-metrics-break-ai-era)).

### 7.3 Published numbers: what to trust

- Trust: **oracle-gap analyses and ablations** (localization matters most; test-time compute helps; prompt tuning saturates) — mechanism-level findings reproduce across teams ([Augmentcode](https://www.augmentcode.com/blog/1-open-source-agent-on-swe-bench-verified-by-combining-claude-3-7-and-o1), [Nebius](https://nebius.com/blog/posts/training-and-search-for-software-engineering-agents)).
- Discount: headline SWE-bench Verified % (contamination ~10%, flawed tests ≥16%, mutation studies showing 20–50% overestimates; prefer fresh-task benchmarks SWE-rebench / SWE-bench-Live / SWE-bench Pro for external signals) ([mutation study](https://arxiv.org/html/2510.08996v2), [leakage](https://arxiv.org/html/2512.10218v2), [SWE-rebench](https://swe-rebench.com/about)).
- Vendor case-study numbers (2 days / 1000 apps; quarters not years; 50% effort cut) — the 50% figure is at least *instrumented-ish* (developer-estimated, published with failure modes); the others are marketing. Re-derive internally.

### What forge should build (evaluation)

- **Internal golden-task fleet**: 20–50 replayable tasks per onboarded repo (real past issues with known-good resolutions), executed on demand; used as (a) onboarding check, (b) model/scaffold upgrade canary, (c) regression gate for harness changes. Success = rung 3+; acceptance by repo owner for the calibration set.
- **Success-rung telemetry on every run**: validation stage reached, human edits post-PR (diff), reviewer round-trips, revert within 30 days. Make rung-5-without-rework the north-star; publish per-campaign.
- **Campaign metrics**: percent-of-oracle migrated, equivalence-evidence coverage, excluded-with-reason count, waves on schedule.
- **Prompt-cache/tool-mix telemetry**: cache hit rate and tool mix (symbol queries vs file reads vs shell) as leading indicators of context health.

Sources: [GetDX: acceptance rate misuse](https://getdx.com/blog/ai-acceptance-rate-easy-measure-misuse-laura-tacho/) · [Augment DHI](https://www.augmentcode.com/tools/developer-happiness-index-benchmarking-ai-coding-tools) · [DORA metrics in AI era](https://larridin.com/developer-productivity-hub/why-dora-metrics-break-ai-era) · [Mutation study](https://arxiv.org/html/2510.08996v2) · [Leakage study](https://arxiv.org/html/2512.10218v2) · [SWE-rebench](https://swe-rebench.com/about) · [Google paper (LLMΔ/HumanΔ)](https://arxiv.org/abs/2504.09691) · [SWE-Explore](https://arxiv.org/html/2606.07297v1)

---

## 8. Top 10 capabilities for forge v0.6–v0.8 (ranked, impact / effort)

1. **Monorepo-aware verification profiles via an affected-area adapter** (`nx affected` / `pants --changed-since` / `bazel-diff` / path-map fallback). The single biggest credibility unlock: evidence becomes "affected targets green", and cost scales with change size, not repo size. *Impact: critical; Effort: medium (one interface + 3 adapters + fallback).*
2. **Path-scoped work packages with publisher-enforced scope** (`allowed_paths`/`denied_paths` in RunSpec; hard fail on out-of-scope diff) + **CODEOWNERS-based gate routing**. *Impact: critical (safety + trust); Effort: low-medium.*
3. **Human-acceptance telemetry + success-rung ladder on every run** (merged-without-rework, post-review edit distance, 30-day revert). You cannot steer what you don't measure; this is cheap and everything else depends on it. *Impact: critical; Effort: low (publisher/forge-side bookkeeping + a dashboard).*
4. **Internal golden-task fleet per repo** (replay tasks, upgrade canaries, harness regression gate). *Impact: high; Effort: medium (mostly curation).*
5. **Stack semantics in the run model** (`stack_parent`, stacked PR publishing, restack-on-merge, merge-order for multi-repo DAGs) — rides GitHub's native stacked PRs / GitLab merge trains rather than reinventing. *Impact: high (decomposition + multi-repo); Effort: medium.*
6. **Campaign spec + wave runner + source-oracle ledger** (inventory-derived state, keep/fix/exclude with human-approved reasons, wave gates, equivalence-evidence step). This is the modernization product. *Impact: high (opens the biggest workload class); Effort: high — do after 1–5.*
7. **Evidence budgets + staged validation harness** (cheap→expensive: parse → AST-changed → punt-check → build → tests; truncated structured evidence artifacts; full logs by digest). Directly lifts success rate per Google's workflow. *Impact: high; Effort: medium.*
8. **Instruction-set resolution service** (CLAUDE.md/AGENTS.md layering per touched path, digests in RunSpec, drift detection) — turns the cheapest quality lever (conventions) into infrastructure. *Impact: medium-high; Effort: low.*
9. **Repo-map service + symbol-retrieval tool** (incremental tree-sitter index keyed by snapshot OID; PageRank ranking; token-budgeted maps; agent tool for symbol queries). *Impact: medium-high on large repos; Effort: medium.*
10. **Conflict-aware parallel scheduler** (`conflicts_with` edges from predicted path sets, path leases at dispatch, disjointness guardrail, best-of-N verify mode) — after 5 exists, this is what turns the DAG into safe throughput. *Impact: medium (high for campaigns); Effort: medium.*

Explicitly *not* top-10 for now: free-running multi-agent swarms (evidence says serialize disjoint-context work and keep a single coordinator); repo-wide "full CI" as default evidence (unaffordable on complex estates); auto-merge of migration output (every credible program keeps human review at the gate).

---

*Research compiled 2026-09-14. Every mechanism claim above traces to the linked primary source; vendor headline numbers are flagged as such and should be re-derived inside forge's own telemetry (§7) before being used in roadmap justifications.*
