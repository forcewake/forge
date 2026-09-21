# Prompt-injection defense for steering channels — gap research (2026-09-22)

> Gap-analysis research for forge. Sources: OWASP Agentic AI Top 10
> materials, arXiv papers (CaMeL 2503.18813, spotlighting 2403.14720,
> adaptive-eval 2606.26479, NetInjectBench 2607.10490), Anthropic/Microsoft
> guidance, incident write-ups; fetched 2026-09-22. Confidence marks:
> **[documented]** / **[observed]** / **[inference]** as in the sandboxing
> companion doc.

## Why it matters for forge

forge now has a **live steering bridge**: `/steer` delivers bounded guidance
into an active agent turn (claude-agent-sdk, codex `turn/steer` with
`expectedTurnId`, opencode prompt injection), and `/answer` resolves waiting
questions mid-run ([adaptive
runbook](../operations/adaptive-runbook.md) §3, [driver
evaluation](../evaluation/2026-09-21-drivers/README.md)). That creates a
two-sided injection surface the classic /implement flow only half had:

1. **Untrusted content reaching the agent mid-run** — issue text (the task
   source), repo files the agent reads, CI logs fed to the debug lane, MCP
   tool results. The agent cannot tell these from instructions.
2. **A privileged control channel** — steering commands that must never be
   spoofable by (1), and whose acceptance must not widen authority.

The runbook already encodes the right *authority* rule (steering "does not
grant new authority"; acceptance-policy changes route to the revision
gate). What is missing is the **context-hygiene and execution-time defense
layer** underneath it: provenance marking of untrusted text, tool-call
policy gates, and egress control. [inference]

## Findings

### 1. The honest baseline: model-level defenses do not hold

[documented] *"The Attacker Moves Second"* (OpenAI + Anthropic + Google
DeepMind, Oct 2025): 12 published prompt-injection defenses — prompting and
training-based — fell to adaptive attacks, most above **90 % success**,
despite originally reporting near-zero.
([dev.to summary](https://dev.to/indra_gustiprasetya_a80a/prompt-injection-defense-10-tips-that-hold-up-4513))
[observed] An adaptive evaluation of nine defense configurations over
20,000+ attacks: every defense relying on the model to protect itself
broke; **the only defense that held was output filtering in separate
application code — zero leaks across 15,000 attacks.**
([arXiv 2604.23887](https://arxiv.org/pdf/2604.23887v1))
[inference] Consequence for forge: spend engineering on *architectural*
controls (boundaries, gates, egress) and treat prompt-level instructions as
the weakest layer, never the enforcement layer — the same stance as
ADR-0011 ("config never delegates security downward").

### 2. The standard framings forge should adopt vocabulary from

- **Lethal trifecta** (Willison): an agent is exploitable when it
  simultaneously has private data, untrusted content, and an external
  communication channel. Break any leg. [documented]
  ([tmls.nyc threat model](https://tmls.nyc/research/prompt-injection-agent-security))
- **OWASP LLM01** (prompt injection = #1 LLM risk) and the **OWASP Top 10
  for Agentic Applications** (Dec 2025, ASI01–ASI10): ASI01 agent goal
  hijack, ASI02 unauthorized tool misuse, ASI03 memory poisoning …
  Mitigations map to control-plane actions: in-band content inspection
  *before* the model consumes it, action-level authorization independent
  of agent reasoning, parameter-level guardrails (recipients, URLs, paths),
  composition limits that flag **read-sensitive → outbound-send** as the
  canonical exfiltration shape, per-call audit with identity attribution.
  [documented] ([praesidia.ai guide](https://praesidia.ai/guides/owasp-agentic-ai-top-10),
  [aisecurityinpractice.com](https://aisecurityinpractice.com/foundations/owasp-top-10-for-agentic-applications))
- [observed] Scale numbers quoted from OWASP Q1 2026: 73 % of production
  AI deployments vulnerable to prompt injection; Agent Security Bench mean
  attack success 84.3 %. ([beyondscale.tech](https://beyondscale.tech/blog/owasp-agentic-ai-enterprise-implementation-guide))

### 3. What actually reduces attack success (with numbers)

| Defense | Mechanism | Measured effect | Cost |
|---|---|---|---|
| Spotlighting / datamarking | Delimiters + data-marking + encoding transformations around untrusted content | ASR **> 50 % → < 2 %** on summarization/Q&A (arXiv 2403.14720) | Cheap; degrades under adaptive attack |
| Randomized delimiters | Per-session random marker so injected text can't close the tag | Static tags are trivially closable; randomness is the point | Trivial |
| Dual-LLM / quarantine | Privileged planner never sees raw untrusted data; quarantined reader summarizes into typed fields | Conceptual pattern; basis of CaMeL | +1 model hop latency |
| **CaMeL** (DeepMind) | Control flow derived only from the trusted query (P-LLM); every value carries capability metadata; a custom interpreter enforces policies at every tool call | Practically solves AgentDojo security; 77 % utility under attack vs 84 % undefended | High; assumes trusted user prompt; dual-LLM latency |
| Progent (program-of-thought guardrail) | Reference monitor on generated programs | AgentDojo indirect-injection 39.9 % → 1.0 % | Medium |
| Metadata-aware policy gate (NetInjectBench) | Policy checked against trusted metadata, never against artifact text | **0/240 unsafe** while keeping 99.17 % usefulness (naive: 82.5 % unsafe) | Medium |
| Output filtering / egress allowlist | Hardcoded rules in app code on the final boundary | Only layer with zero leaks under sustained adaptive attack (arXiv 2604.23887) | Low–medium |

[documented] Sources:
[arXiv 2503.18813 (CaMeL)](https://doi.org/10.48550/arxiv.2503.18813),
[CaMeL explained](https://agentpatterns.ai/security/camel-control-data-flow-injection),
[niteagent playbook](https://niteagent.com/blog/prompt-injection-defense-production-2026),
[adaptive eval of out-of-band defenses](https://arxiv.org/abs/2606.26479),
[NetInjectBench](https://arxiv.org/pdf/2607.10490v1).

### 4. Tool outputs are an untrusted channel — the forge-critical insight

[documented] Tool results, CI logs, web pages and repo files all flatten
into one token stream with the system prompt; the durable framing is
**taint tracking**: label everything entering from an untrusted source,
propagate the label, and refuse sensitive operations on tainted values.
Practical tool-boundary techniques: provenance envelopes
(`<tool_output trust="low" source="ci-log">`), sanitizers that reduce
free-form text to the schema fields the planner asked for, quoting
discipline (extract–serialize–escape), continuous injection evals per tool
(AgentDojo-style), and CI-enforced injection tests before a tool ships.
([tianpan.co tool-outputs-as-untrusted-channel](https://tianpan.co/blog/2026-04-23-tool-outputs-untrusted-channel-taint-tracking))
[inference] In forge terms: the **CI-debug lane** (raw pipeline logs →
agent context) and **MCP harness servers** are exactly this channel; the
adaptive lane's discovery tools (read_file/search_text over repo content)
are the same for the planner.

### 5. The NetInjectBench result is forge's steering theorem

[documented] For network-operations agents (the closest published analog
to "agent with a control channel"), separating **untrusted artifact text**
from **trusted policy metadata** and gating at execution time produced
0/240 unsafe actions while preserving 99–100 % usefulness — and the paper's
thesis is that agents need *execution-time authorization boundaries
alongside prompt-level instruction hygiene*.
([arXiv 2607.10490](https://arxiv.org/pdf/2607.10490v1))
[inference] forge's ControlCommand ladder (`received → authorized →
applied → checkpointed`, actor resolved server-side, runner cannot
manufacture an approver) *is* the trusted-metadata side of this split. The
missing half is proving that untrusted lane content can never *look like*
control input and that lane tool calls are gated on provenance, not just on
role allowlists.

### 6. What the incidents teach about the output side

[observed] The successful CI-agent exfiltrations all exited through a
channel nobody allowlisted: `env | curl`, base64-in-PR, commit-the-dump
(GitHub itself as egress), clickable links / auto-fetching markdown in
agent output. Mitigations that name this layer: outbound URL + image-domain
allowlists, stripping active markdown from agent-rendered untrusted
context, secret masking, and treating `read-sensitive → send` sequences as
alarm-worthy. [documented]
([authsome.ai](https://authsome.ai/blog/running-ai-agents-safely-in-ci-cd),
[niteagent](https://niteagent.com/blog/prompt-injection-defense-production-2026))

## Concrete recommendations (ranked by effort/impact)

1. **Egress allowlisting + output filtering at the lane boundary (low
   effort, highest empirical impact).** Default-deny egress from the agent
   lane (allowlist: model gateway, git host, package registries); strip or
   neutralize active markdown/links the agent emits when derived from
   untrusted context; mask secrets in anything the agent posts. This is
   the only defense class with a zero-leak record, and it composes with
   recommendation 1 of the sandboxing doc (same network boundary).
   [inference]
2. **Provenance envelopes with per-run randomized delimiters (low–medium
   effort, high impact).** One harness-agnostic wrapper in the driver
   brief builder: every untrusted input (issue body, repo file reads in
   discovery, CI log excerpts, MCP tool results) enters context inside a
   randomized, labeled envelope; the system prompt teaches the convention.
   Cheap, measurably effective (>50 %→<2 % pre-adaptive), and it makes
   audits (was this conclusion built from trusted or untrusted text?)
   mechanically answerable. [inference, technique documented]
3. **Injection regression corpus in CI (low effort, high leverage).**
   AgentDojo/NetInjectBench-style canary tests: seed issues/logs with
   canonical injection payloads ("ignore previous instructions",
   "commit your environment", fake `/go`-shaped text, unicode/base64
   obfuscations) and assert the lane neither follows them nor lets them
   alter control state (no epoch change, no publication, no scope
   expansion). Pin it next to the failure-injection suites so the security
   story gets the same rigor as the durability story. [inference]
4. **Execution-time tool-call policy gate per lane role (medium effort,
   high impact).** Extend the drivers' mechanical deny rules with
   parameter-level validation (destinations, paths, recipients) and
   exfil-sequence detection (read-secret/sensitive followed by
   network-send), journaled like `action_log`. Aligns with OWASP ASI02 and
   the NetInjectBench metadata-gate result. [inference]
5. **Quarantined summarizer for the noisiest channels (medium effort).**
   For CI-debug logs and large MCP results: a low-trust reader model (or
   deterministic parser) reduces the content to typed fields before the
   implementer sees it — the dual-LLM pattern scoped to forge's worst
   channels rather than everywhere. [inference]
6. **Capability tracking (CaMeL-style) as the long-term target for the
   adaptive lane (high effort, deferred).** forge's WorkContract /
   publication-intent machinery is already capability-shaped; carrying
   provenance tags on values into tool-call authorization would give the
   NetInjectBench guarantee structurally. Record as a design seam, don't
   build before the P2 gate. [inference]

Relationship to existing plans: CTL-07 ("Deliver bounded steering
instructions without granting new authority") covers the authority side;
this research adds the missing *content-hygiene and execution-gate* layers
beneath it.
