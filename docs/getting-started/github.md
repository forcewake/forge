# Connect a GitHub repository

Use the [cold install procedure](../onboarding/cold-install-runbook.md) and
[explicit server configuration](../../forge.example.toml). Changes to App
installation, repository settings, hooks and CI require the owner's authority.

## 1. Prepare the execution boundary

Use an ephemeral unprivileged runner. Keep repository write credentials,
database access and gateway administration on the server. Protect the target
branch and deny the bot merge rights and bypass permissions.

## 2. Register and install the App

Install the App only on selected repositories. Configure its webhook as
`https://<forge-host>/hooks/<connection-key>`. Record the numeric repository,
installation, bot and approver IDs in the connection. Store the App signing
key and webhook secret under their explicitly configured environment names.
There is no PAT fallback or shared approver fallback.

## 3. Configure forge and the target

Install the pinned [lane v1](../harnesses/lane-v1.md) workflow at the configured
native workflow path. Bind its OIDC issuer and JWKS URL to this connection.
Configure required independent verification job names and workflow paths;
another workflow's same-named job cannot substitute for required evidence.
Configure the reviewed gateway tariff and artifact digest. Select Claude Code
or Copilot with its explicit scoped authorization.

## 4. Verify the first workflow

Run `forge --config forge.toml doctor`, then
`forge --config forge.toml onboard <repository-key>`. Comment `/implement`,
review the exact displayed plan, and submit `/go <run-id>`.
Inspect the Draft, current candidate SHA and independent checks before a human
merge decision. Use [operator commands](../operations/operator-commands.md)
for cancellation, correction and recovery. Setup checks do not prove a live
deployment or authorize publishing this local source candidate.
