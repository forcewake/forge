# Operations

This directory collects operational runbooks for running forge against a
GitLab CE instance. The runbooks below are **planned for M4** (the limited
production release milestone); until then this file is the index of what will
be documented.

## Planned runbooks

| Runbook | Scope | Status |
|---------|-------|--------|
| Project onboarding and doctor (`onboarding.md`) | Registering a project: webhook setup, bot permissions, protected-branch rules, execution profile approval, and the `doctor` readiness checks (including Draft-MR pipeline-skipping rules) | Planned for M4 |
| Backup and restore (`backup-restore.md`) | Backing up and restoring forge's Postgres state (runs, steps, approvals, usage ledger, event inbox/outbox) and staged payload storage | Planned for M4 |
| Upgrade (`upgrade.md`) | Schema migrations (Alembic), deploy/rollback ordering between app and worker, and compatibility notes between forge versions | Planned for M4 |
| Token rotation (`token-rotation.md`) | Rotating the bot Personal Access Token, the webhook secret, the management/MCP bearer token, and model API keys without losing in-flight runs | Planned for M4 |
| Audit and log retention (`audit-retention.md`) | Retention periods for audit rows, action journals, usage records, and prompt snapshots; ensuring audit data does not retain secrets or full raw prompts | Planned for M4 |

## Related material

- [Architecture decision records](../adr/0000-record-architecture-decisions.md)
  — the design these runbooks will operate
- [Threat model](../security/threat-model.md) — trust boundaries and risks the
  operational procedures must preserve
