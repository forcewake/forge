# Agent sandboxing in CI runners — gap research (2026-09-22)

> Gap-analysis research for forge. Sources: official docs and engineering
> write-ups fetched 2026-09-22 (fly.io, code.claude.com, grigio.org,
> authsome.ai, stepsecurity.io, OpenAI Codex docs coverage), plus the
> forge tree (ADR-0016, ADR-0002, adaptive runbook §2). Confidence marks:
> **[documented]** stated by an authoritative source (linked);
> **[observed]** demonstrated live / measured in the wild;
> **[inference]** this document's synthesis for forge.

## Why it matters for forge

forge's security story today is **proposal-only lanes**: coding agents run
in ephemeral provider CI (GitLab docker executor, GitHub Actions, Azure
Pipelines) with no write credentials and no forge secrets, and their output
is a candidate artifact validated by a trusted publisher
([ADR-0016](../adr/0016-candidate-bundle-trusted-publisher.md)). That story
is about *credential* isolation, not *kernel* isolation: the agent process
still runs arbitrary, model-directed commands on a runner that shares a
host kernel (docker executor class, including the unraid-style self-hosted
Docker executors a forge customer is likely to have). The adaptive roadmap
makes this sharper: interactive drivers (claude-agent-sdk, codex
app-server, opencode serve — live since 2026-09-21) put long-running agent
processes under forge-side lane control, and the runbook already states
"Docker socket access for coding agents" is unsupported
([adaptive-runbook](../operations/adaptive-runbook.md) §2) without saying
what the positive isolation recipe *is*. Every major agent platform has
answered this question explicitly; forge currently answers it only by
inheritance from provider CI defaults.

## Findings

### 1. The industry has converged on a layered isolation ladder

[documented] The 2026 comparison literature settles on five levels:
L0 no sandbox; L1 containers (namespaces + cgroups, ~200 ms cold start,
shared host kernel); L2 user-space kernels (gVisor); L3 microVMs
(Firecracker, Kata, CubeVM); L4 confidential computing (TEE).
([grigio.org comparison](https://grigio.org/ai-agent-sandbox-technologies-a-complete-2026-comparison/))

[documented] **None of the hyperscalers running agent code chose plain
containers.** AWS built Firecracker for Lambda, Google built gVisor for
Search/Gmail and points it at agents (Cloud Run, GKE Sandbox, claude.ai
code execution, OpenAI's higher-risk tasks, Modal), Azure uses Hyper-V for
ephemeral agent sandboxes. ([fly.io](https://fly.io/learn/firecracker-vs-gvisor/),
[dev.to sandboxing guide](https://dev.to/aiagentengineering/how-to-sandbox-ai-agents-in-2026-firecracker-gvisor-runtimes-isolation-strategies-14pk))

### 2. gVisor vs Firecracker — the decision axes

| Axis | gVisor | Firecracker |
|---|---|---|
| Mechanism | Sentry (Go user-space kernel) intercepts syscalls; Gofer mediates filesystem | KVM microVM with real guest kernel; jailer sets cgroups/chroot/seccomp |
| Host requirement | Any Linux (systrap inside VMs; KVM platform on bare metal) | `/dev/kvm` — bare metal or KVM-passed VM |
| Boot | Process start (no kernel) | < 125 ms; ~150 microVMs/s/host |
| Overhead | ~5–15 % on syscall-heavy work; near-native CPU-bound | Near-native; < 5 MiB VMM overhead + guest kernel |
| Compatibility | ~240 of ~400 syscalls; gaps in io_uring, BPF, some ioctls | Full guest ABI |
| Escape path | Exploit the Sentry (memory-safe, ~200 KLOC) | KVM → tiny device model → jailer |
| Production users | Cloud Run, GKE Sandbox, App Engine, Anthropic code exec, OpenAI, Modal | AWS Lambda, Fly.io, Vercel, E2B |

[documented] All facts above from
([fly.io](https://fly.io/learn/firecracker-vs-gvisor/)) and
([securemachinery.com Kata vs gVisor](https://securemachinery.com/2026/07/04/kata-containers-vs-gvisor-security-architecture-performance-full)).
Choice rule for CI-runner-class hosts: **gVisor where you run on cloud VMs
or Kubernetes without usable KVM; Firecracker where you own bare metal and
want snapshot/warm-pool primitives.** [documented, fly.io]

[observed] gVisor's compatibility gaps are real operational facts, not
theory: in an audit of 8,764 MCP servers, 103 of 206 run under gVisor
**failed to start** due to syscall limitations (non-portable code), while
gVisor blocked live `ptrace()` (EPERM) and BPF (ENOSYS) attempts.
([itoverdose audit](https://itoverdose.com/en/news/response-to-gvisor-vs-firecracker-for-ai-agent-sandboxing-what-we-learned-auditing-8764-mcp-servers-d760e195))
— a gVisor lane needs a per-harness compatibility matrix, exactly the
shape of forge's DriverMatrix.

### 3. What production agent platforms actually run

- **OpenAI Codex (cloud):** isolated containers; **two-phase runtime** —
  a network-enabled *setup phase* installs dependencies, then the *agent
  phase* runs offline by default with secrets removed. Egress, when
  enabled, goes through OpenAI's proxy with domain allowlist + HTTP method
  restrictions (and optional TLS MITM for inspection). Observed sandbox
  shape: ~9 vCPU / ~15 GB RAM / ~60 GB disk, shared cores, ephemeral.
  ([OpenAI security blueprint coverage](https://blockainews.com/news/openai-codex-production-security-architecture-sandbox-telemetry-may-09),
  [shaam.blog sandbox measurements](https://shaam.blog/articles/chatgpt-codex-cloud-sandbox-free-server-guide-2026))
  [documented + observed]
- **OpenAI Codex (CLI/IDE):** OS-level enforcement — Seatbelt
  (`sandbox-exec`) on macOS; **bubblewrap + Landlock + seccomp** on
  Linux/WSL2; restricted tokens on Windows. The official enterprise
  recommendation for Linux-in-Docker is a Dev Container as the outer
  boundary **plus bubblewrap as the inner boundary**. [documented]
- **Claude Code self-hosted runners** (the closest public analog to a
  forge-owned lane): ephemeral **per-session containers** (`--capacity 1`,
  `--drain-grace-sec 0`), per-runner working directory no other process
  can read; `--hooks-dir`, wrapper and host `~/.claude/` mounted
  read-only; cloud metadata endpoint (169.254.169.254) explicitly blocked
  in the session's network namespace (or IMDSv2 hop-limit 1); credentials
  **minted per session**, never baked into the image; on-demand runners
  keep the environment secret on an orchestrator host that never runs user
  code; default-deny egress allowlisting only the API + git hosts; a
  repo-settings guard (`--confine-repo-settings warn|enforce|off`) that
  scans committed repo settings for grants outside the workspace, env
  blocks, and sandbox-off overrides; `--kill-session-after-min` as a
  runaway backstop. ([code.claude.com deploy
  guide](https://code.claude.com/docs/en/self-hosted-environments-deploy))
  [documented]
- **Devin:** one persistent cloud VM per session (shell + editor +
  browser), full network by default — the *maximally capable, minimally
  contained* end of the spectrum; even Cognition's own docs tell you to
  keep it away from production. **Cursor background/cloud agents:** fresh
  isolated Ubuntu VM per task, no shared state, egress filtered by
  allowlist, destroyed at PR close. ([Devin vs Codex enterprise
  comparison](https://codex.danielvaughan.com/2026/06/01/devin-vs-codex-cli-cloud-sandbox-local-first-architecture-enterprise-comparison),
  [loopengineering.wiki Devin architecture](https://loopengineering.wiki/wiki/devin-architecture),
  [anhtu.dev async agents](https://anhtu.dev/async-coding-agents-2026-agent-inbox-pattern-2256))
  [documented]
- **Embeddable sandbox runtimes** if forge ever wants the lane itself as
  a library: E2B (Firecracker, ~150 ms, 24 h sessions, partial OSS),
  CubeSandbox (Rust CubeVM + KVM, < 60 ms, < 5 MB CoW, full self-host,
  Apache-2.0), Modal (gVisor), Daytona (Docker + optional Kata), Northflank
  (Kata/CLH + gVisor). [documented, grigio.org]

### 4. Lightweight tools (bubblewrap / firejail) and where they fit

[documented] bubblewrap (`bwrap`) is an ~8 kLOC unprivileged
namespace-sandbox constructor used by Flatpak, **Codex CLI, and Claude
Code's native Linux sandbox**; it starts in < 50 ms, defaults to a *zero
sandbox* (nothing mounted, `--unshare-all`, `--die-with-parent`,
`--new-session`), and every permission is an explicit bind — a whitelist
by construction. ([grigio.org docker
alternatives](https://grigio.org/docker-alternatives-for-ai-agents-podman-bwrap-and-firejail),
[palaimon.io bwrap deep dive](https://blog.palaimon.io/posts/coding-agents-bubblewrap-deep-dive/))
Firejail is the profile-driven desktop-class sibling (1000+ profiles,
needs admin install). Projects like `katosh/agent_sandbox` wrap bwrap /
firejail / Landlock into per-agent profiles (Claude Code, Codex, OpenCode
…) that hide SSH keys, cloud credentials and env secrets from the agent
while leaving the filesystem otherwise intact. [documented]

### 5. The blast-radius incidents that motivated all of the above

[observed] The documented exfil class against CI-resident agents: PR/issue
text injects "dump your environment and POST it" — demonstrated against
`claude-code-action`, `run-gemini-cli` and GitHub's Copilot Coding Agent
("Comment and Control"); bypasses observed include base64 to defeat secret
scanning and **committing the dump back to the repo so GitHub.com itself
is the exfil channel** (no egress needed). Claude Code project files were
a separate RCE surface (CVE-2025-59536: malicious `.claude/settings.json`
hooks / MCP auto-load / base-URL hijack). Claude Code in Actions ships
**no network restrictions by default** — hardening (e.g. step-security
harden-runner egress policies) is the deployer's job.
([authsome.ai CI/CD hardening guide](https://authsome.ai/blog/running-ai-agents-safely-in-ci-cd),
[StepSecurity analysis](https://stepsecurity.io/blog/anthropics-claude-code-action-security-how-to-secure-claude-code-in-github-actions-with-harden-runner))
[inference] forge's equivalent surface is the issue comment (the
`/implement` trigger reads untrusted issue text) and any repo content the
lane reads — the *credential*-side mitigations forge already has (no forge
secrets in lane, CI-variable-scoped harness keys) break the worst leg of
this, but egress is uncontrolled in a default docker-executor lane.

## Concrete recommendations (ranked by effort/impact)

1. **Hardened "lane profile v2" for the docker-executor class (low effort,
   high impact).** Codify what the Claude self-hosted guide and the
   sandboxing literature converge on, as forge-tested CI templates:
   `--cap-drop=ALL`, `--security-opt=no-new-privileges`, read-only rootfs
   + tmpfs workspace, non-root user, `--pids-limit` / memory / cpu limits,
   explicit block of 169.254.169.254, no docker socket (already policy),
   and **default-deny egress** with a per-lane allowlist (model gateway,
   package registries, git host) enforced at the network boundary or a
   forward proxy. Extend `forge doctor` with a lane-isolation check.
   [inference, from documented patterns]
2. **Adopt gVisor (`runsc`) as an opt-in OCI runtime for forge's GitLab
   docker-executor lanes (medium effort, high impact).** It is the only
   L2+ primitive that works on cloud-VM / unraid-class hosts without
   KVM, and it drops in as a runtime flag. Gate it behind a per-project
   profile in the DriverMatrix sense, with a compatibility test lane —
   the 103/206 MCP-server startup failures show syscall gaps will break
   some harnesses, so the matrix must be evidence-backed, fail-closed.
   [inference, evidence documented]
3. **Two-phase network model for interactive lanes (medium effort, high
   impact).** Steal Codex's split: a bounded setup phase with network
   (dependency install) and secrets, then the agent phase offline by
   default / allowlisted with secrets dropped. Maps naturally onto forge's
   epoch/checkpoint machinery — the phase boundary is a checkpointable
   lane state. [inference, pattern documented]
4. **Repo-settings guard analog (low effort, medium impact).** `--strict-mcp-config`
   already ignores repo-supplied `.mcp.json`; extend the same posture to
   repo-supplied harness settings (`.claude/settings.json` hooks, env
   blocks, sandbox-off overrides) — Claude's guard names exactly what to
   scan for. [inference; guard documented]
5. **MicroVM tier for high-risk profiles (high effort, deferred).**
   Firecracker/CubeSandbox-class isolation when a customer runs forge on
   bare metal or wants the strongest lane; keep as a profile the
   DriverMatrix refuses until evidence exists. [inference]

Relationship to existing plans: EXE-08 ("Isolate tools, egress and
privileged test execution") in the adaptive backlog covers part of items
1–3; this research supplies the concrete mechanisms and the
production-precedent evidence.
