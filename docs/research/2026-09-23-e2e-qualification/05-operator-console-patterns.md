# Operator console patterns for CI/CD and agent systems — research (2026-09-23)

> E2E-qualification research for forge, topic 5 of 6. Sources:
> internal-console architecture guides (2026), GitOps operator UX
> (Argo CD / Spinnaker / Tekton), agent-operations console patterns
> (operator states, six-plane observability, closed operator loop),
> approval/audit-trail practice; fetched 2026-09-23. Confidence
> marks: **[documented]** / **[observed]** / **[inference]**.

## Why it matters for forge

R32-23 asks for an operator view: attempt state, safe recovery, and
audit — read-only where possible, evidence-linked, with safe control
commands. forge already has the substrate (journals, epoch/pause
protocol, `/metrics.prometheus`, the doctor, `/why-blocked`) and a
hard rule from the roadmap: control-plane reads must never be
confused with write coordination. The 2026 pattern language for
exactly this shape — long-running attempts, human-in-the-loop waits,
safe interventions — has consolidated fast in both the CI/CD world
and the agent-operations world, and the two converged on the same
design.

## Findings

### 1. The dual-path console: fast reads, guarded writes

[documented] ([operator-friendly developer console](https://dev.to/therizwansaleem/writing-an-operator-friendly-developer-console-a-practical-guide-to-building-a-low-latency-internal-260),
[admin dashboard architecture](https://edilec.com/blog/gen-sw-0035/admin-dashboard-architecture-checklist-for-client-facing-workflows))

- The canonical split: **fast path = read-only, cached/pre-aggregated,
  instant**; **safe path = writes that are minimal, confirmed,
  dry-run-previewed, idempotent, audit-logged**. The remediation
  flow: select unhealthy entity → quick stats + **dry-run preview of
  the action** → confirm → API executes with idempotent safeguards →
  new state rendered + audit entry appended.
- Every operator journey is mapped before UI is drawn: trigger,
  owner, authoritative record, decision, protected action, expected
  result, exception path. Each queue item must answer **"why the item
  appears now"** from server-defined state — not client inference.
- Outcome states are explicit: **pending / succeeded / failed /
  partially completed / unknown**, and "a timeout does not prove
  failure — use a correlation identifier and a reconciliation query
  before allowing a retry." Exceptions stay in the queue with a named
  owner "instead of disappearing behind a toast message."
- Concurrency discipline: version check or explicit conflict policy,
  with a changed-since-open warning; recovery verbs are first-class
  (retry, compensate, escalate, reverse) and shown as "actionable
  failure state."

### 2. What mature CI/CD platforms expose to operators

[documented] ([Argo CD vs Spinnaker vs Tekton operator surface](https://kubernetes.ae/argocd-vs-spinnaker),
[deploy approvals & sign-offs](https://senior-stack.uz/Roadmap/Programming/quality-engineering/quality-gates/04-deploy-approvals-and-signoffs/middle),
[CNCF CI/CD UX notes](https://ijcttjournal.org/2025/Volume-73/Issue-5/IJCTT-V73I5P119.pdf))

- Argo CD's operator value is **state reconciliation made visible**:
  app health + sync status (Live vs Desired vs Git), drift detection
  with self-heal, sync history, and one-click rollback that is "a git
  revert, and the cluster self-heals back" — the audit trail *is* the
  Git history. Spinnaker's is **pipeline stage visibility + Manual
  Judgment stages**: the pipeline halts and presents named options
  (Promote / Roll back) to authorized roles, with execution history
  persisting who triggered and who approved every deployment.
- The audit record that passes review binds **four facts**: who
  approved (a person, not a shared bot), what exactly (artifact
  digest / commit SHA — "not 'the latest build'"), when (server-side
  timestamp), and why/linkage (the change record it traces to).
  "Deploy → change → ticket must form an unbroken chain" — and the
  platform records must capture the digest so "the approval can't
  later be claimed to be of a different build."
- Operator UX must-haves named in the CNCF practice literature: find
  **which step failed**, scan logs per step, **rerun pipeline** and
  **dry-run**, diff-and-approval visibility ("observability into what
  changes and when").
- The anti-pattern dossier is instructive: dashboard-staring
  promotions replaced by automated analysis on the exact SLO; the
  self-approved hotfix that failed SOC 2 sampling ("the fix was four
  lines of protected-environment rule; lead time unchanged"); CAB
  boards that slowed delivery without improving failure rates (the
  DORA finding). Human gates belong where judgment is irreducible,
  machine gates everywhere else.

### 3. Agent-operations consoles: states, evidence links, no drive-by writes

[documented] ([agent coordination dashboard design](https://github.com/shakacode/agent-coordination-dashboard/blob/main/CONTEXT.md),
[agent management observability MVP](https://huangruiteng.github.io/loopx/docs/product/surfaces/agent-management-observability-mvp))

- An operator view over agent work is "an operator view over
  coordination state. Its primary job is to answer **what is running,
  what is stuck, and where is a referenced PR/issue/branch handle** —
  and it **must not launch agents, edit code, merge PRs, or mutate
  claims and heartbeats**" (read-only by charter, with a separate
  guarded control path).
- The operator state vocabulary that emerged in practice —
  **running / wedged** (alive but no transition for N minutes — the
  "looked-launched-but-stalled" failure) **/ paused (intentional) /
  blocked (needs input) / stale / dead / ready / done / unknown** —
  is deliberately derived from signals (heartbeat + phase/event
  activity), not asserted by workers. "Live operation needs scanning
  more than motion": dense rows, narrow badges, timestamps, stable
  filters.
- Evidence links are **thin by design**: latest run record, validation
  command labels, handoff note IDs, review-packet refs. "Do not inline
  raw logs, raw trajectories, credentials, or status JSON blobs. The
  row should show that evidence *exists* and let the operator drill
  into safe refs."
- The checklist for the first screen: which agents are active for
  this goal; what is each claimed on; running/waiting/blocked/monitoring/
  stale/unknown; **what evidence or handoff makes the next step
  reviewable**; what quota/cadence/workspace hint the operator should
  notice. Fresh-context delegations render as a warning against hidden
  inherited assumptions.

### 4. Six-plane observability and the closed operator loop

[documented] ([why agent traces are not enough](https://cognilode.com/blog/why-agent-traces-are-not-enough))

- "A trace shows what ran. An operator needs to understand what
  happened." Six planes: **process & custody** (what is running, who
  owns it, can it be controlled — launch *receipts*, not "task
  complete"), **runtime protocol** (semantic transitions: turn
  started, waiting-on-dependency, delegation started/returned,
  terminal — "waiting is progress information"), **model-visible
  context** (what the model actually saw, with context receipts),
  **semantic work** (objective, plan, artifacts changed), **coordination
  & delivery** (did the result reach its destination — a completion
  ladder, not one event), **resources & outcomes** (tokens/cost per
  phase).
- Architecture rules that match forge's journal/projection split:
  **append-only source events, derived current state** — "current
  status should be a reproducible projection, not another
  independently mutated truth store"; **stable identity across every
  plane** so specialized ledgers join; **freshness belongs in the
  projection** ("a current view should state when each source was
  last observed and whether expected sources are absent"); **bounded
  views over deep expandable history**.
- The **closed operator loop**: Status (bounded projection) / Watch
  (streams meaningful changes, not repeated output) / Expand (direct
  access to the underlying record) — and structured interventions:
  continue-with-context, **steer toward a revised objective**, stop
  the owned process group, resume-on-dependency. "The intervention
  itself should be durable: a steer command should retain its target,
  reason, timestamp, and application result," and the resulting
  transition appears in the same projection. This is forge's
  epoch/pause/steer protocol described from the UI side.
- Console-object criteria for long-running jobs: it has **identity
  (URL), a visible step timeline, cancel, rerun, and a downloadable
  evidence pack**. "A job that cannot cancel is a cost accident
  waiting for a meeting. A job that cannot rerun is a screenshot, not
  a method. Both actions should write a trail so the next person can
  see what you stopped and what you restarted."
  ([long-running analysis jobs](https://infinisynapse.com/en/blog/long-running-analysis-job))

### 5. Gate-design lessons that shape the console

[documented] ([bimodal approval stops case study](https://dev.to/humzakt/four-stops-instead-of-thirty-rebuilding-the-dashboard-and-retiring-hand-rolled-ui-1a35))

- Approval UX failure mode: **bimodal oversight** — "thirty
  confirmation stops or zero, nothing in between." The fix collapsed
  30 gates to 4 checkpoints "deliberately keeping the ones that
  actually gate money," with explicit reasoning about which stops
  earned their place vs accumulated ceremony.
- Recovery paths rot silently: the failed-run recovery link built a
  URL against a route deleted weeks earlier — "the recovery path from
  a failure was itself a 404." Audit every screen's inbound links;
  test the recovery affordance from the failed state, not from a
  fresh session.

## Concrete recommendations (ranked by effort/impact)

1. **Ship the operator view as a read-only projection with derived
   states (medium effort, high impact — R32-23 core).** One dense
   table over WorkPackage/attempt/run state using the derived-state
   vocabulary (running/wedged/paused/blocked/stale/dead/done/unknown),
   computed from journal heartbeats and phase transitions — never
   asserted by workers. Read-only by charter: no merges, no claim
   mutation, no repo writes from the console. Thin evidence links
   (journal refs, DriverMatrix evidence IDs, receipt digests) with
   drill-down, never inlined logs. [documented pattern, maps directly
   onto forge's journal projection]
2. **Dual-path control commands (medium effort).** The few safe
   controls forge already has — pause/steer/answer/resume, cancel,
   rerun — go through the safe path: dry-run preview, explicit
   confirmation, idempotency, durable audit entry with the four facts
   (who/what-digest/when/why). Everything else stays CLI/API. The
   "cancel and rerun write a trail" rule makes recovery itself
   auditable. [documented]
3. **Wedged detection as a first-class state (low effort).** "Alive
   but no semantic transition for N minutes" is the failure operators
   actually need surfaced (the looked-launched-but-stalled case);
   derive it from journal phase timestamps with a configurable
   threshold, and surface it in `/why-blocked` and the console alike.
   [documented, inference application]
4. **Semantic-progress timeline, not activity noise (medium
   effort).** The per-attempt view shows semantic transitions (turn
   started, tool completed, waiting-on-dependency, gate hit,
   checkpoint, delegation returned) joined with work-bearing changes
   (files/PRs/artifacts) and cost per phase — the bounded view over
   expandable journal history. Freshness stamps per source in the
   projection. [documented]
5. **Audit ledger binds digest + actor + linkage (low effort).** Every
   approval/intervention/decision record carries the exact candidate
   digest and the ticket/WorkPackage it traces to — "deploy → change
   → ticket must form an unbroken chain," and self-approval is
   structurally restricted. Feeds topic 3's evidence chain directly.
   [documented]
6. **Audit the recovery affordances quarterly (low effort).** The
   404'd-recovery-link failure: verify from the *failed* state that
   every recovery entry point resolves, and that gate count is
   deliberate (money-gating checkpoints kept, ceremony collapsed).
   [documented case study]

Relationship to existing plans: implements R32-23; the projection
rules restate forge's existing journal discipline from the consumer
side; the durable-intervention requirement matches the epoch/pause
protocol and the adaptive plan's §7.3 teardown/restore semantics.
