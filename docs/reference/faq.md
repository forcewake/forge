# FAQ

## What happens after `/implement`?

Admission checks authority and configured CI before paid planning. Forge
posts a plan and waits for an approver's `/go <run-id>`. The gate binds the
exact input, base and policy digests and is consumed once. The CI lane
proposes a candidate; the trusted publisher validates and writes it as a
Draft. Required independent checks must cover its current SHA. Merge is
always human. See [commands](../operations/operator-commands.md).

## Can I stop or recover work?

`/cancel` revokes publication authority before stopping execution. Late
results cannot publish. `/status` and `/why-blocked` describe the durable
reason. Unknown publication effects require `/reconcile`; retry applies
only where the rejection rules permit. Required checkpoint loss must be
restored or explicitly retired, never silently treated as fresh work.
A restart reuses durable intent; it does not prove an unknown effect failed.

## Which providers and drivers?

GitHub and GitLab are active; Azure remains on the
[0.42.x maintenance line](../operations/maintenance-0.42.md).
The builtin implementer is removed. Claude Code and Copilot CLI are the
first-class targets; other drivers are experimental. Existing templates
still serve current provider services while lane v1 migrates. A package's
presence is not evidence that a deployment uses it.

## Who holds credentials?

The control plane holds the publisher credential. The lane gets no
repository write token, database credentials or production secrets.
Existing lane credential routes and new OIDC/gateway grants remain separate.
Missing or expired credentials refuse; they do not authorize ambient
fallback. Unknown usage stays unknown, with wall-clock limits still active.
See [tokens](../operations/token-rotation.md).

## What does green mean?

A vendor completion is not delivery; a candidate is not verification;
green CI is not human acceptance. Required checks must name the exact
current candidate. Historical evidence certifies only its recorded version
and environment. This unreleased source declares no hosted release or
current live acceptance.

## Which features are deferred?

Security triage (`/security` returns `feature_not_available`), standalone
CI-debug, the forge MCP server and multi-repository writes are outside the
current scope. Explicitly configured lane-side MCP clients remain separate.
GitLab review feedback exists behind its flag; GitHub parity is migration
work. See [ADR-0035](../adr/0035-simplification-program.md).

## How do I deploy or change versions?

Use [GitHub](../getting-started/github.md) or
[GitLab](../getting-started/gitlab.md) onboarding, run doctor for the configured
environment, and test an authorized disposable workflow.
[Back up](../operations/backup-restore.md) before
[upgrading](../operations/upgrade.md); migrate deliberately and keep app and
worker on one composition. The [threat model](../security/threat-model.md)
states the remaining environmental risks.
