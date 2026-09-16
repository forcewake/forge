# Harnesses: the four drivers and task-aware selection

forge ships four coding-agent drivers. Every one implements the SAME
proposal-only contract (ADR-0016): no write credentials, no forge secrets,
a mechanical commit/push deny in the driver itself, a candidate artifact
as the only output, and usage receipts where the vendor provides them
(unknown ≠ zero).

| Driver | Doc | One-liner |
|---|---|---|
| Claude Code | [claude-code.md](claude-code.md) | `-p` + stream-json, settings isolation, always-strict MCP |
| Grok Build | [grok-build.md](grok-build.md) | always-approve + deny rules, hardened npm preamble |
| opencode | [opencode.md](opencode.md) | injected permission map, schema-translated MCP |
| GitHub Copilot CLI | [copilot-cli.md](copilot-cli.md) | `-p` + scoped grants, deny-wins tool rules |

Shared setup (include lines, common variables, the MCP variable, runner
profile, how a run flows, monitoring/triage):
**[harness-onboarding.md](onboarding.md)**.

## Multi-harness with auto-selection ([ADR-0023](../adr/0023-dynamic-harness-selection.md))

By default a project pins ONE implementer
(`FORGE_IMPLEMENTER_BACKEND=ci_harness:claude-code`). With selection
enabled, the project declares an ordered **preference list** and forge
picks per task:

```yaml
# forge.yml (factory config)
forge:
  implement:
    harnesses: [claude-code, grok-build, opencode]
```

```bash
# env form (lab/CI)
FORGE_HARNESS_PREFERENCE=claude-code,grok-build,opencode
```

### How selection works

1. **Compile (plan time, deterministic).** The compiler
   (`src/forge/runs/harness_selection.py`) intersects the preference with
   the lanes the project actually onboarded — `forge doctor` reports that
   intersection (a harness cannot be selected without its credentials).
   A length-1 list reproduces the pinned behavior byte-for-byte.
2. **Planner proposes, never decides.** The planner may pick any entry of
   the list (trivial docs fix → the cheap lane; a migration → the
   strongest) and set a `budget_class` (`trivial | standard | heavy`),
   with a one-line reason. It can reorder, never extend.
3. **Freeze (RunSpec v2).** `backend_config{harness, harness_fallbacks,
   budget_class, selection_reason}` joins the policy digest — changing
   the preference invalidates pending gates, exactly like plan drift.
4. **Approve (the gate).** The plan comment carries the **Implementation
   block**:

   ```markdown
   ## Implementation
   - Harness: **claude-code** · model glm-5.3-flash[1m]
   - Fallbacks: grok-build, opencode
   - Budget class: standard
   - Commit cycles: 3
   - Selection reason: planner selection
   ```

   `/go` therefore authorizes the execution shape, not just the plan
   text. The planner is never the authority: a human curated the list,
   a human approves the pick.
5. **Dispatch.** GitLab pipelines get `FORGE_HARNESS_DRIVER`; the Actions
   and Azure lanes get the `driver` input/parameter — every template
   carries a driver filter, so multi-template repos run exactly ONE lane.

### Fallback (opt-in, off by default)

```bash
FORGE_HARNESS_FALLBACK=true   # default: false
```

When enabled, a lane leg that fails with an **infrastructure-classified**
failure (auth/quota/timeout — never code failures) before any candidate
exists advances to the chain's next entry: journaled (`action_log`:
event/from/to/reason), stated in the run's evidence, bounded by the
frozen chain. Anything else fails visibly — the CI-native posture (a
stuck job, not a silent reroute). The repair loop (commit cycles) stays
on whichever harness produced the candidate.

### What was deliberately rejected

Per-request learned routers (RouteLLM-style), provider-side "auto"
models as a default, dispatch-time LLM routing, switching harnesses
mid-candidate, and cross-project auto-selection — the reasoning lives in
[ADR-0023](../adr/0023-dynamic-harness-selection.md) and
[research/harness-selection.md](../research/harness-selection.md).
Telemetry-driven reordering (per-driver acceptance rates from the
delivery ladder) is a planned **suggestion surface** in `forge doctor` —
humans apply reorders as config.

### Reproducibility

Same RunSpec → same selection: the list, the pick, and the fallback
policy are all frozen at gate time; each candidate's meta records the
driver that produced it. A preference change is a new policy digest and
a new gate.
