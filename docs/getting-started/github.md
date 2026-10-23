# Connect a GitHub repository

This guide configures the existing Actions lane used by current provider
services. The separate [lane v1](../harnesses/lane-v1.md) is not activated
automatically. Start with a pinned reviewed composition and a disposable
target repository. Setup changes require the repository owner's authority.

## 1. Prepare the execution boundary

Expose forge's webhook endpoint over HTTPS. Approve an ephemeral,
unprivileged runner with no production secrets, Docker socket or broad
internal-network access. Protect the target branch, deny the bot merge
rights and ruleset bypass, and keep the publisher credential out of CI.
The coding lane must not have a repository write token.

## 2. Register and install the App

Create a GitHub App scoped to the selected repositories. Configure its
webhook as `https://<forge-host>/webhook/github` with a generated secret.
Use repository Issues, Contents and Pull requests read/write, Actions
read/write for dispatch/cancel, and Metadata read-only. Do not grant
Administration or ruleset bypass. GitHub's
[workflow dispatch endpoint](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)
requires Actions write permission; the coding job does not inherit it.
Subscribe to Issues, Issue comment, Pull request, Push and Meta as required
by the configured ingress. Security triage is deferred and needs no alert
permissions.

Generate the App private key, install on only the intended repositories,
and record App and installation IDs. Keep the PEM in the control plane's
secret store.

## 3. Configure forge and the target

Set `FORGE_GITHUB_ENABLED=true`, `FORGE_GITHUB_WEBHOOK_SECRET`,
`FORGE_GITHUB_APP_ID`, `FORGE_GITHUB_PRIVATE_KEY` and
`FORGE_GITHUB_INSTALLATION_ID`. For GitHub-only deployments set
`FORGE_GITLAB_ENABLED=false`. Configure trusted `FORGE_GITHUB_APPROVERS`;
an empty value uses the shared fallback, not a union of provider identities.
The PAT fallback is for explicitly selected lab setups, not a reason to
put a publisher PAT in the lane.

Install the reviewed [Actions harness](../../ci/templates/forge-harness.github.yml)
as `.github/workflows/forge-harness.yml`; pin its wheel/ref and dependencies
from one compatible composition. Set
`FORGE_GITHUB_HARNESS_WORKFLOW=forge-harness.yml`. The source ref must contain
its `workflow_dispatch` entry. Provision only the selected model/subscription
secrets in Actions; see [harness onboarding](../harnesses/onboarding.md).
The App webhook already delivers events; PAT/lab mode needs an equivalent
repository webhook. Avoid duplicate delivery setup unless deliberately tested.

## 4. Verify the first workflow

Run `forge doctor` for the configured environment. Admission refuses missing
or unreadable lane configuration before paid planning. Comment `/implement`,
review the plan, then approve its exact run with `/go <run-id>`. Inspect the
Draft, current candidate SHA and independent required checks before a human
merge decision. Cancel with `/cancel`; diagnose with `/status` or
`/why-blocked`.

The `forge` label starts the same approval-bound path; removal cancels only
work still at the approval gate. Bot-authored comments never approve work.
Issue edits before approval invalidate the stale plan; in-flight approval
is not silently rewritten. Full behavior is in
[operator commands](../operations/operator-commands.md).

For delivery problems, inspect GitHub webhook deliveries, secret matching,
HTTPS reachability and the installed template/wheel identity. A setup pass
is not a live result, and an archived trace does not qualify this deployment.
