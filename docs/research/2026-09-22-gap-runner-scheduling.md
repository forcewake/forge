# Queueing/scheduling many agent runs on few runners — gap research (2026-09-22)

> Gap-analysis research for forge. Sources: GitHub ARC docs + practice
> write-ups, GitLab runner fleet docs (docker-autoscaler/fleeting,
> GitLab.com fleet runbooks), fair-scheduling literature for job queues
> and agent pools; fetched 2026-09-22. Confidence marks:
> **[documented]** / **[observed]** / **[inference]**.

## Why it matters for forge

Today forge's execution lanes ride **provider CI capacity** (GitLab project
CI, GitHub Actions, Azure Pipelines): queueing, priority and autoscaling
are the *platform's* problem, and forge's `/metrics.prometheus` already
exposes queue depth. Two things change that:

1. **Agent runs are hostile CI workloads**: a single interactive lane can
   hold a runner for hours (vs minutes for a build), so a small shared
   pool saturates on concurrency *counts* long before it saturates on
   CPU — and the run that starves is someone else's `/implement`.
2. **The adaptive roadmap introduces forge-owned lane capacity**
   (`runner-sessions` endpoints, EXE-04's outbound control channel,
   self-spawned servers like `OpenCodeServer`). The moment forge owns
   runners, it owns admission, fairness, priority and preemption —
   MRP-08 ("hierarchical budgets, admission and fair scheduling") names
   this but the mechanism is undesigned.

## Findings

### 1. GitHub Actions fleet practice (the Actions lane forge already uses)

[documented] ([ARC on Kubernetes](https://pavanrangani.com/blog/github-actions-self-hosted-runners-kubernetes),
[ARC internals](https://matthewswong.com/en/blog/github-actions-runner-autoscaling-arc),
[warpbuild scaling guide](https://warpbuild.com/guides/github-actions-self-hosted-runner-scaling),
[markaicode architecture](https://markaicode.com/architecture/github-actions-production-system-design-architecture))

- **Actions Runner Controller (ARC) v2 runner scale sets** are the
  production model: a listener pod holds a long-poll to GitHub; when jobs
  targeting the label queue, GitHub tells the listener how many runners
  are needed and it patches an `EphemeralRunnerSet` — scaling driven
  **directly by job-queue depth, not CPU**. One job → one ephemeral pod →
  deleted on completion (`--ephemeral`/EPHEMERAL=1 also prevents stale
  registrations).
- Bounds are static (`minRunners` / `maxRunners`); **scheduled min/max is
  not supported** — time-of-day warm pools are a cron patch of the
  resource. Node capacity underneath comes from Karpenter/Cluster
  Autoscaler.
- Operational SLO guidance: track **queue-wait time** as the primary SLO,
  not success rate (queued jobs are silently canceled at 24 h); budget the
  1,000 req/h/repo Actions API rate limit including runner registration;
  runners need only egress to github.com (no ingress); persistent runners
  on k8s are the classic footgun (restarts orphan registration, jobs hang
  queued).
- [observed] Fleet economics example: a 200-engineer org moved 500+ daily
  jobs from GitHub-hosted ($10 k/mo) to ARC on EKS with min 0 / max 50 —
  idle cost ~zero, peak times down ~40 %.

### 2. GitLab runner fleet practice (the docker-executor lane forge already uses)

[documented] ([fleet scaling guide](https://gitlab-docs-d6a9bb.gitlab.io/runner/fleet_scaling),
[docker-autoscaler executor](https://docs.gitlab.com/runner/executors/docker_autoscaler),
[GitLab.com k8s runner managers runbook](https://runbooks.gitlab.com/ci-runners/linux/kubernetes-runner-managers))

- The modern autoscaling path is the **Docker Autoscaler executor with
  fleeting plugins** (AWS/GCP/Azure) replacing docker-machine: the runner
  *manager* provisions ephemeral VM instances on demand; recommended
  secure profile is **capacity-per-instance 1, use-count 1** — each job
  gets a fresh single-tenant instance deleted immediately after;
  `idle_count` (warm instances) / `idle_time` (20 m typical) /
  `max_instance_count`; `concurrent = max_instances × capacity`.
- GitLab.com itself runs **sharded fleets**: manager Deployments on GKE
  (ArgoCD-managed, config-as-Helm-values) per shard×project-class, each
  spawning ephemeral COS VMs — sharding by size class (small/medium/
  large) is how they keep noisy neighbors apart. [observed at scale]
- Legacy docker-machine autoscaling (`IdleCount`/`IdleTime`/`MaxBuilds`,
  time-window autoscaling stanzas) still works and is the path on
  unraid-class single hosts running the plain docker executor — but there
  capacity is simply `concurrent`, no elasticity.

### 3. Fair scheduling, priority, preemption — the general theory that transfers

[documented] ([priority queues & fairness](https://task-queues.com/queue-fundamentals-architecture/priority-queues-and-job-fairness),
[job scheduling system design](https://letsbuildsolutions.com/blog/system-design/designing-a-job-scheduling-system-priority-queues-fair-scheduling-and-failure-recovery-at-scale),
[WFQ for LangChain-style agents](https://medium.com/@Modexa/priority-queues-that-make-langchain-agents-feel-fair-d0c6651eac70))

- **Strict priority starves**: a never-empty high queue means lower queues
  never run. Fix by **aging** (effective priority improves with wait:
  `priority − age × k`, or promote after a max-wait threshold).
- **Weighted Fair Queuing (WFQ)** for multi-tenant fairness: per-tenant
  sub-queues + weights; scheduler picks min virtual finish time
  (`vtime += cost / weight`); tenant with weight 2 gets ~2× the
  throughput of weight 1 regardless of queue depth. Clamp idle tenants'
  vtime to global time or returning idlers get an unbounded burst.
- Priority **and** fairness must be layered: priority classes solve
  importance, per-tenant fairness solves noise (one project's 200 queued
  runs must not eat the pool).
- **Weight by estimated cost, not job count** — agent runs vary by orders
  of magnitude; a count-fair queue is compute-unfair.
- SLA shape used in practice: tiers with explicit p95-wait targets
  (e.g. critical < 30 s, high < 5 min, normal < 30 min) and **preemption
  reserved for short windows only** (e.g. P0 = 40 % reserved + burst,
  may preempt; P2 = 20 % minimum guaranteed).
- Fairness reason codes on dispatch failure (`tenant_limit`,
  `no_workers`, `pool_overloaded`) so operators can distinguish fairness
  pressure from infrastructure loss; retry-debt caps and no-capacity
  cooldowns (2 s) to stop retry storms amplifying contention.
  [documented, cordum.io production guide](https://cordum.io/blog/ai-agent-priority-fair-scheduling)

### 4. Agent-pool specifics

[documented] ([how2.sh fair queuing for platform agents](https://how2.sh/posts/how-to-build-agent-queue-fairness-policies-for-tooling-reliability-in-internal-platform-engineering))
The classic agent-pool fix is **class weights**: interactive requests
(weight 3) over CI-triggered bulk (1) and batch (1), enforced by a
weighted-round-robin schedule — interactive latency stays ~flat while bulk
trickles. [inference] forge's analog classes are: operator-interactive
runs (someone is watching a gate/steering), ordinary `/implement` runs,
and background repair/review lanes.
[documented] Idle-session discipline matters as much as admission:
waiting for a human must not hold an active lane (architecture plan §7.3
already says tear down at the checkpoint threshold and restore on answer
— Claude's self-hosted runner ships exactly this: `--release-idle-session-min`,
`--kill-session-after-min`).

## Concrete recommendations (ranked by effort/impact)

1. **Lane-fleet recipes as docs + doctor checks (low effort, high
   impact).** Codify the two fleet patterns forge's providers recommend —
   ARC scale sets (ephemeral pods, queue-depth listener, min/max bounds)
   and GitLab docker-autoscaler with fleeting (capacity 1 / use 1,
   idle_count/idle_time) — as tested reference configs for customers
   running shared self-hosted pools, plus a `forge doctor` queue-health
   check (queue-wait, not just depth). Most forge deployments will hit
   this before they need forge-internal scheduling. [inference]
2. **Admission control in forge's own dispatch queue (medium effort,
   high impact when adaptive runners land).** When lanes are forge-owned:
   per-project WFQ weights + aging over strict priority; class tiers
   (interactive / standard / background) with p95-wait targets;
   fairness reason codes surfaced in `/why-blocked` and metrics. Design to
   be dispatch-side only so it works unchanged whether capacity is
   forge-owned or provider CI. [inference — implements MRP-08]
3. **Cost-weighted accounting for fairness (low effort once topic-4
   landing exists).** Weight scheduler fairness by reserved budget
   (tokens/USD), not run counts — the "weight by estimated task cost"
   rule; forge's reserve-before-dispatch ledger already produces the
   estimate. [inference]
4. **Preemption = pause + checkpoint + reschedule (high effort, ties to
   the durable-sessions research).** Agent runs are long and partially
   irreversible; the only safe preemption is forge's own pause protocol
   (epoch bump → interrupt → checkpoint) followed by requeue — never
   SIGKILL-by-scheduler. Define the scheduler↔pause-protocol contract
   before any priority tier is allowed to preempt. [inference]
5. **Idle-lane hygiene (low effort).** Question-wait and gate-wait must
   release lane capacity (teardown + restore-on-answer is already the
   plan's stance); make it a measured behavior in the adaptive pilot with
   the delivery-ladder metrics, mirroring `--release-idle-session-min`
   semantics. [inference]

Relationship to existing plans: MRP-08 (admission + fair scheduling) and
OPS-05 (capacity controls) are the backlog anchors; this research supplies
the concrete mechanisms (WFQ + aging + class weights + reason codes), the
provider-fleet recipes, and the preemption-safety constraint.
