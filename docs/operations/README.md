# Operations

Runbooks for running forge against a GitLab CE instance. Start with
[onboarding.md](onboarding.md); everything else supports the day after.

| Runbook | Scope | Status |
|---------|-------|--------|
| [Project onboarding and doctor](onboarding.md) | Execution-profile approval, bot identity and permissions, webhook, CI variables, harness template include, `doctor` gates | Written (v0.1.0) |
| [Backup and restore](backup-restore.md) | Postgres dumps, what is (and is not) in a backup, restore order, reconciler behavior after restore | Written (v0.1.0) |
| [Upgrade](upgrade.md) | Migrations before code, deploy/rollback ordering between app and worker, mixed-version rules | Written (v0.1.0) |
| [Token rotation](token-rotation.md) | Bot PAT, webhook secret, model keys — rotation clocks and no-lost-run order | Written (v0.1.0) |
| [Audit and log retention](audit-retention.md) | What is kept where, retention defaults, pruning commands, no-secrets rule | Written (v0.1.0) |

## Related material

- [Architecture decision records](../adr/0000-record-architecture-decisions.md)
  — the design these runbooks operate
- [Threat model](../security/threat-model.md) — trust boundaries and risks
  the operational procedures must preserve
- [Harness onboarding](../harness-onboarding.md) — harness-specific setup
  and triage
