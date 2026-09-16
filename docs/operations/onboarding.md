# Project onboarding (runbook)

Goal: connect a GitLab project to forge so runs can be dispatched, with the
execution profile approved by a human **before** the first run
([ADR-0002](../adr/0002-ci-as-execution-profile-rce.md)). Machine-checkable
version: `python -m forge.doctor --project <id>` until it exits 0.

## 1. Approve the execution profile (human decision)

Confirm before touching CI:

- Ephemeral **docker executor** runner only (no shell runners).
- No privileged mode, no Docker socket, no production secrets in the runner's
  reach, no broad internal network access from job containers.
- Outbound network needed: the GitLab origin, the package registry the
  harness installs from, and the harness API endpoint.

## 2. Bot identity and permissions

- Bot user (e.g. `forge`) with a project-scoped PAT — in forge's env only
  (`FORGE_BOT_TOKEN`, `api` scope, 90-day expiry recommended — see
  [token-rotation.md](token-rotation.md)). The harness lane never receives
  it: optionally give the lane a READ-ONLY project PAT
  (`FORGE_BOT_READ_TOKEN`, ADR-0016).
- Target branch (`main`) protected: **Maintainers may merge; nobody pushes**.
  The bot works exclusively on `factory/<issue-iid>/<run-id>` branches.
- Bot is NOT given merge rights on the target branch — that is the enforced
  no-merge guarantee, not a convention.

## 3. Webhook

Settings → Webhooks → `http://<forge-host>:8420/webhook`, secret = the
`GITLAB_WEBHOOK_SECRET` value, events: **Notes, Merge request, Pipeline, Job**.
If forge runs on a LAN address, the GitLab admin setting
`allow_local_requests_from_web_hooks_and_services` must be enabled.

## 4. CI variables (Settings → CI/CD → Variables)

| Variable | Notes |
|----------|-------|
| `FORGE_BOT_READ_TOKEN` | optional; masked; READ-ONLY PAT (repo read). Never put a write token in the lane (ADR-0016) — the trusted publisher is the only writer |
| harness credential | `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL`, or `ZAI_API_KEY`, or `FORGE_GROK_AUTH` — exactly the backend chosen below |
| `FORGE_HARNESS_HTTPS_PROXY` | optional; only when the runner network throttles AI streams |
| `FORGE_HARNESS_MCP` | optional; MCP servers for the lane (JSON, ADR-0022) — see [harness-onboarding §2b](../harness-onboarding.md#2b-mcp-servers-in-the-lane-forge_harness_mcp-adr-0022) for a copy-paste Context7 + Microsoft Learn example |

Never store forge's own admin token in the project.

## 5. Harness template (human-applied include)

One line in `.gitlab-ci.yml` on the default branch:

```yaml
include:
  - remote: 'https://raw.githubusercontent.com/forcewake/forge/main/ci/templates/grok.gitlab-ci.yml'
```

The harness job runs only when forge triggers a pipeline with `FORGE_RUN_ID`
— normal pipelines skip it. Details and triage:
[harness-onboarding.md](../harness-onboarding.md).

## 6. Required jobs (quality contract)

`FORGE_REQUIRED_JOBS` (forge-side setting) lists job names that must be
green for a run to reach review. Seed the repo with real tests the pipeline
runs; a green icon without the required jobs is not done ([ADR-0008](../adr/0008-quality-contract-instead-of-pipeline-status.md)).

## 7. Verify and go live

```bash
python -m forge.doctor --project <id>     # exit 0
```

Then the first live run: comment `/implement` on an issue, review the plan,
reply `@forge /go <run-id>`. Watch the run reach `ready_for_human` with an
evidence comment bound to the candidate SHA.
