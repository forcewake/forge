# FAQ — forge on GitLab CE, GitHub and Azure DevOps

Short answers with pointers. Setup guides:
[GitLab project onboarding](operations/onboarding.md) ·
[GitHub setup](github-setup.md) ·
[Azure DevOps setup](azure-setup.md) · harness details:
[harness-onboarding](harness-onboarding.md).

## Runs and lifecycle

**What exactly happens after `/implement`?**
forge plans the work with the configured model, posts the plan as a comment
(issue/MR), freezes it into an immutable RunSpec, and parks the run at the
human gate. Nothing is written to your repository until a human comments
`/go <run-id>`.

**Can I stop a run?**
`@forge /cancel <run-id>` (short ids accepted; bare `/cancel` targets the
latest active run on the issue). Cancel revokes the publication grant
FIRST, then stops the runner — a late agent result cannot become a commit.

**Only one person can start runs?**
Who may start/consume runs is your `FORGE_APPROVERS` list. Use
provider-specific logins: a GitLab username and a GitHub login are
different identities even for the same human
([ADR-0018](adr/0018-immutable-run-spec.md) — identity is
connection-scoped).

**What about Azure DevOps?**
It works as a third adapter: `/implement` on a work item, the plan
as a work-item comment, `/go` dispatches the harness into YOUR Azure
Pipelines, the candidate comes back as a pipeline artifact and is
published as a Draft PR via the Push API — forge never votes or
completes. Approvers are AzDO identities (`uniqueName`, e.g.
`dev@fabrikam.example`) in `FORGE_AZDO_APPROVERS`; PR CI on Azure Repos
is your Build-validation branch policy (YAML `pr:` triggers are ignored).
The loop is fully contract-tested; live verification against a real org
is pending — setup and the verification checklist:
[azure-setup](azure-setup.md), decision record:
[ADR-0024](adr/0024-azure-devops-adapter.md).

**A run is stuck in `waiting_approval` / `waiting_harness` / `waiting_ci`.**
Those waits are durable by design. Deadlines exist (`decision` TTL,
`FORGE_HARNESS_TIMEOUT_SECONDS`, `FORGE_CI_WAIT_SECONDS`) and block the run
with a reason when they fire. Triage:
[forge-debug-run skill](../.claude/skills/forge-debug-run/SKILL.md).

**The worker crashed mid-run. Did I lose anything?**
No. Commands and steps are persisted in Postgres before anything executes;
a surviving worker reclaims and converges the run. This is proven by the
failure-injection suite (two workers, kill at six checkpoints).

## Agents and execution

**Which coding agents are supported?**
Claude Code, Grok Build CLI, opencode (as ephemeral CI jobs), plus the
`builtin` LLM implementer (forge-side, no CI). All of them produce a
*candidate* that forge validates and publishes — agents never hold write
credentials to your repository.

**Where do the agents run?**
In the target project's own CI (GitLab CI / GitHub Actions) — ephemeral
containers, your runner, your quota. Forge's own host never executes agent
code.

**Can an agent change CI configuration or workflows?**
No — denied by policy at the trusted publisher, for every driver.

**What stops a cancelled agent from committing later?**
Publication grants are revoked before the runner is stopped; the publisher
checks the grant and the run fence at write time. A late result is recorded
as superseded evidence.

**How is the agent's result trusted?**
It isn't — it's verified: the candidate artifact (diff + meta + usage) is
checked against the approved base, policy, and RunSpec; the published
commit's head is verified; the readonly reviewer then reads the diff and
files a verdict bound to the exact SHA.

## Models and providers

**Which models work?**
Anything LiteLLM routes (self-hosted gateways included) for the builtin
path, plus the native provider auth of each CLI harness for the agent path
(Claude API/key, xAI, OpenAI-compatible). BYOK is a credential-ownership
mode, not a harness switch.

**Are agent token costs tracked?**
Yes — per attempt, with an explicit completeness flag (exact / aggregate /
unknown). Unknown stays unknown, never zero; failed attempts stay in the
ledger.

## Security

**Can forge merge my code?**
No — enforced by the bot's capabilities and platform permissions
([ADR-0003](adr/0003-no-merge-is-enforceable.md)). Keep the publisher token
scoped to `forge/*` branches and the target branch protected.

**Does the agent see my secrets?**
The execution lane gets ONLY the harness provider credentials you put in
project CI variables/secrets. Forge's publisher token, database, and
webhook secret never enter the lane, and the lane's git remote cannot push.

**MCP endpoints?**
Fail-closed: without `FORGE_MCP_KEY` the MCP server is not mounted. The
key is the all-scope master; per-token scoped principals
(`FORGE_MCP_SCOPED_TOKENS`, e.g. a read-only `forge:read` bot token) are
enforced per tool call and audited. The run-surface tools read forge's own
durable state — they never act with forge's provider tokens. Mounted
behind a proxy, list the public host in `FORGE_MCP_ALLOWED_HOSTS` or the
SDK's DNS-rebinding protection answers 421.

## Ops

**How do I upgrade?**
Migrations first, then app+worker on the new image — full ordering and
rollback in [upgrade](operations/upgrade.md).

**Where are backups?**
You own them: `pg_dump` of forge's Postgres (runs, gates, ledger) —
[backup/restore](operations/backup-restore.md). Redis is transient.

**Green CI isn't required?**
It is, by the quality contract: pipeline success + every required job
green. Configure `FORGE_REQUIRED_JOBS` — an empty profile only downgrades
the evidence to a warning.
