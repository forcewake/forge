# Connect a GitHub repository

Use the [cold install procedure](../onboarding/cold-install-runbook.md) and
[explicit server configuration](../../forge.example.toml). Changes to App
installation, repository settings, hooks and CI require the owner's authority.

## 1. Prepare the execution boundary

Use an ephemeral unprivileged runner. Keep repository write credentials,
database access and gateway administration on the server. Protect the target
branch and deny the bot merge rights and bypass permissions.

## 2. Register and install the App

Create the App with **Only select repositories**. Forge refuses an installation
that is granted to every repository. Permissions, exactly:

| permission | access |
| --- | --- |
| metadata | read |
| contents | write |
| issues | write |
| pull_requests | write |
| actions | write |
| checks | read |

Subscribe to `issues`, `issue_comment`, `pull_request`, `pull_request_review`,
`pull_request_review_comment` and `workflow_run`. Set the webhook to
`https://<forge-host>/hooks/github` when the connection key is `github`
(`POST /hooks/<connection-key>`). Record the numeric repository, installation,
bot and approver IDs. The bot id is the App's user id from the installation
token response (`id` of the account that owns the installation). Store the
App signing key and webhook secret (at least 32 bytes) under
`app_private_key_env` and `webhook_secret_env`. There is no PAT fallback.

Set `tls_ca_path` to an absolute PEM file when the GitHub or GitLab API uses
a private CA. The model gateway is separate: it must be `https`, except the
hostnames `127.0.0.1`, `localhost` and `litellm`, which may use `http`. The
gateway URL cannot embed a user, password, path or query.

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
