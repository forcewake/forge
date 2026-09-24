# Operations

Runbooks for running forge against a GitLab CE instance. Start with
[onboarding.md](../getting-started/gitlab.md); everything else supports the day after.

| Runbook | Scope | Status |
|---------|-------|--------|
| [Project onboarding and doctor](../getting-started/gitlab.md) | Execution-profile approval, bot identity and permissions, webhook, CI variables, harness template include, `doctor` gates | Written (v0.1.0) |
| [Backup and restore](backup-restore.md) | Postgres dumps, what is (and is not) in a backup, restore order, reconciler behavior after restore | Written (v0.1.0) |
| [Upgrade](upgrade.md) | Migrations before code, deploy/rollback ordering between app and worker, mixed-version rules | Written (v0.1.0) |
| [Token rotation](token-rotation.md) | Bot PAT, webhook secret, model keys — rotation clocks and no-lost-run order | Written (v0.1.0) |
| [Audit and log retention](audit-retention.md) | What is kept where, retention defaults, pruning commands, no-secrets rule | Written (v0.1.0) |
| [Delivery evaluation cohort](delivery-cohort.md) | The 14 bounded tasks (A17), the runner, receipt exports, per-ACCEPTED-unit economics and the honesty rules | Written (v0.11.0) |
| [Adaptive runbook](adaptive-runbook.md) | The adaptive workflow: supported recipes, control commands, recovery, credentials, the Postgres-controller decision record | Written (v0.19.0) |

## Qualified profile: `gitlab-ce-v1` (#268 / R36-09)

The first named, cold-installable GitLab CE customer configuration is
frozen in **`qualification/profiles/gitlab-ce-v1.md`** — GitLab CE
19.3.2, docker-executor runner, `claude-sdk-lane` template with
claude-code 2.1.273, python-3.13, `glm-5.3-flash[1m]` via the z.ai
gateway, BYOK credentials, the promoted-wheel install route (R36-07)
and the `smoke` independent-verification contract. Operators qualify
(or re-qualify after any upgrade) with the staged driver:

```bash
uv run python scripts/qualify_gitlab_ce.py --stage preflight     # free; refuses before paid work
uv run python scripts/qualify_gitlab_ce.py --stage install-check # free; clean venv, wheel sha256 + identity gate
uv run python scripts/qualify_gitlab_ce.py --stage flow          # PAID; native surfaces only
uv run python scripts/qualify_gitlab_ce.py --stage report        # evidence bundle finalization
```

Operator rules: read the profile document before touching a broken
setup (it is written to be sufficient without reading implementation
files); never restart shared lab containers to simulate runner loss —
cancel the CI JOB through the GitLab API; a refused stage in
`qualification/profiles/gitlab-ce-v1-evidence.json` with its reason is
the expected outcome when a prerequisite is missing; the Draft MR the
flow produces is left for human review (forge never merges). The
offline twin of the whole arc is the production-entry trace
CE-1..CE-4 (`tests/production_entry/test_gitlab_ce_entry.py`).

## Related material

- [Architecture decision records](../adr/0000-record-architecture-decisions.md)
  — the design these runbooks operate
- [Threat model](../security/threat-model.md) — trust boundaries and risks
  the operational procedures must preserve
- [Harness onboarding](../harnesses/onboarding.md) — harness-specific setup
  and triage
