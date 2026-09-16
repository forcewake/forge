# GitHub setup (runbook): GitHub App + project + harness

End-to-end guide for connecting a GitHub repository to forge. Order
matters: forge first, then the App, then the repo. Verification gates are
`forge doctor` and the first live run. A [FAQ](#faq) is at the bottom.

## 0. What you need

- A forge deployment reachable by GitHub for **webhooks** — a public HTTPS
  endpoint (port-forward, or a `cloudflared` named tunnel:
  `cloudflared tunnel create forge && cloudflared tunnel route dns forge
  forge.example.com`, ingress → `http://localhost:8420`).
- A GitHub account with admin on the target repository.
- forge's model key (whatever LiteLLM/the harness needs).

## 1. Register the GitHub App

GitHub → Settings → Developer settings → **GitHub Apps → New GitHub App**.

| Field | Value |
|---|---|
| GitHub App name | unique, e.g. `yourname-forge` |
| Homepage URL | your forge repo/site |
| Identifying & authorizing users | **leave empty** (no OAuth user flow) |
| Enable Device Flow | off |
| Webhook ✓ Active | URL `https://<forge-host>/webhook/github` |
| Webhook secret | generate (`openssl rand -hex 32`) — forge must get the same value |
| Repository permissions | **Issues: Read & write**, **Contents: Read & write**, **Pull requests: Read & write**, **Actions: Read-only**, **Metadata: Read-only** |
| Subscribe to events | **Issues**, **Issue comment**, **Pull request**, **Meta** (Pull request drives the reactive PR review) |
| Where can this App be installed | Only on this account |

**Security findings (optional, `/security` triage):** forge's `/security`
command pulls code-scanning, secret-scanning and Dependabot alerts. Grant
the App **Code scanning alerts: Read & write**, **Secret scanning alerts:
Read & write** and **Dependabot alerts: Read & write** — the write half is
only needed if you enable `FORGE_SECURITY_REMOTE_DISMISS`; reads alone
cover ingestion and triage. PAT/lab mode needs the classic `security_events`
scope for code + secret scanning. Alert endpoints answer **403** on repos
where Code Security / Secret Protection is not enabled (public repos are
free; private ones need the paid SKU) — forge degrades to Dependabot-only
for those repos (Dependabot alerts are free everywhere).

Then:

1. **Generate a private key** (App page → download `.pem`).
2. Note the **App ID** and the **Client ID**.
3. **Install the App** on the target repository (Only select repositories).
4. Note the **installation ID** — the number in the URL of the install page
   (`github.com/settings/installations/<ID>`).

Never give the App the Administration permission or ruleset bypass — the
no-merge guarantee depends on that.

## 2. Configure forge

Add to forge's environment (`.env` / container env):

```bash
FORGE_GITHUB_ENABLED=true
FORGE_GITHUB_WEBHOOK_SECRET=<same value as the App webhook secret>
FORGE_GITHUB_APP_ID=<client id or app id>
FORGE_GITHUB_PRIVATE_KEY=/path/to/app.pem        # PEM text or a file path
FORGE_GITHUB_INSTALLATION_ID=<installation id>
# Lab/PAT fallback (production uses the App):
# FORGE_GITHUB_TOKEN=<a PAT> — forge picks it when the App key is unset
```

Restart forge. The GitHub ingress is **fail-closed**: without
`FORGE_GITHUB_ENABLED` + webhook secret the route answers 503.

## 3. Onboard the target repository

1. **Webhook** (repo Settings → Webhooks): the same forge URL with the same
   secret; events: Issues, Issue comment, Push, Pull request, Meta. (When
   the App is installed, its webhook delivers these already — a repo hook
   is only needed for PAT/lab mode.)
2. **Actions secrets** (repo Settings → Secrets → Actions) — for the
   harness lane, per driver: `ANTHROPIC_API_KEY` + `ANTHROPIC_BASE_URL`
   (or `ANTHROPIC_AUTH_TOKEN` for gateway providers), `ZAI_API_KEY`,
   `XAI_API_KEY`. The lane never receives forge's publisher token.
3. **Harness workflow** — commit
   [ci/templates/forge-harness.github.yml](../../ci/templates/forge-harness.github.yml)
   to the repo as `.github/workflows/forge-harness.yml`. Replace the
   `<PINNED_REF>` in the template's `pip install` with a ref you trust
   (a tag or a full SHA — pin it; this is the lane's supply chain).
4. **forge side**: set `FORGE_GITHUB_HARNESS_WORKFLOW=forge-harness.yml`
   (the workflow filename) to make `/go` dispatch the harness instead of
   the builtin proposer.
5. Verify: `python -m forge.doctor` → exit 0.

## 4. First run

```text
comment /implement on an issue
  → forge replies with a plan + "Approve this exact plan by commenting
    @forge /go <run-id>"
comment @forge /go <run-id>
  → forge dispatches the harness into the repo's Actions
  → the agent runs in an ephemeral runner (no write credentials); its
    diff lands as a candidate artifact
  → forge validates and publishes it as a Draft PR
  → the repo's own CI runs on the PR
  → readonly review + evidence comment → ready_for_human
```

Label trigger: add the `forge` label (configurable,
`FORGE_TRIGGER_LABEL`) to an issue — same as `/implement`.
Cancel: `@forge /cancel <run-id>` (or bare `/cancel`).

## FAQ

**Why doesn't forge appear in the "Assign to Agent" picker?**
That picker lists GitHub's curated coding-agent partners (Copilot, Claude,
Codex) — part of GitHub's partner program, not a property of a custom App.
forge's triggers are comments and the `forge` label.

**GitHub webhooks never arrive.**
The forge host must be reachable from the internet (GitHub retries for a
while, then drops). Check the tunnel/port-forward first, then the webhook
"Recent Deliveries" tab: 401 = secret mismatch, 404 = ingress disabled
(`FORGE_GITHUB_ENABLED`), timeouts = the app is down.

**The harness job failed with `command not found`.**
The Actions lane installs each CLI with a retrying npm preamble rendered by
`forge.harness_entry`. If the CLI moved/renamed, update the pinned forge
ref the workflow installs from — never hand-edit the run in place.

**`harness_attempt_base_mismatch: artifact base <none>`**
The candidate artifact's meta lacks the base — usually means the workflow
input `attempt_base_oid` arrived empty (executor/template version drift).
Update both to the same release.

**Claude (or another CLI) can't reach its provider.**
Optional repo **variable** `FORGE_HARNESS_MCP` (Settings → Secrets and
variables → Actions → **Variables**) provisions MCP servers for the lane —
copy-paste examples in
[harness-onboarding §2b](../harnesses/onboarding.md#2b-mcp-servers-in-the-lane-forge_harness_mcp-adr-0022).
Per-driver provider env is injected from repo Actions secrets
(`ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `ZAI_API_KEY`, `XAI_API_KEY`).
Some gateways need `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL` instead of
an Anthropic key — see the template's env block.

**My run survived a forge restart. Is that expected?**
Yes — runs are durable; `waiting_approval` / `waiting_harness` /
`waiting_ci` are reconciler-driven. Blocked runs are the operator's inbox.

**Can forge merge?**
No. By capability and platform permissions — see
[ADR-0003](../adr/0003-no-merge-is-enforceable.md). The publisher token is
scoped to branch writes on `forge/*` branches; the target branch is
protected by YOUR settings.
