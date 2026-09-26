# Operations

Runbooks for running forge against a GitLab CE instance. Start with
[onboarding.md](../getting-started/gitlab.md); everything else supports the day after.

| Runbook | Scope | Status |
|---------|-------|--------|
| [Project onboarding and doctor](../getting-started/gitlab.md) | Execution-profile approval, bot identity and permissions, webhook, CI variables, harness template include, `doctor` gates | Written (v0.1.0) |
| [Backup and restore](backup-restore.md) | Postgres dumps, what is (and is not) in a backup, restore order, reconciler behavior after restore | Written (v0.1.0) |
| [Upgrade](upgrade.md) | Migrations before code, deploy/rollback ordering between app and worker, mixed-version rules | Written (v0.1.0) |
| [Token rotation](token-rotation.md) | Bot PAT, webhook secret, model keys — rotation clocks and no-lost-run order | Written (v0.1.0) |
| [Credential consumption](credential-consumption.md) | R38-04: the consumer receipt join at the runner boundary, rotation semantics, the registry's multi-worker TTL contract, the strict/compat policy matrix and presence-version honesty | Written |
| [Audit and log retention](audit-retention.md) | What is kept where, retention defaults, pruning commands, no-secrets rule | Written (v0.1.0) |
| [Delivery evaluation cohort](delivery-cohort.md) | The 14 bounded tasks (A17), the runner, receipt exports, per-ACCEPTED-unit economics and the honesty rules | Written (v0.11.0) |
| [Adaptive runbook](adaptive-runbook.md) | The adaptive workflow: supported recipes, control commands, recovery, credentials, the Postgres-controller decision record | Written (v0.19.0) |
| [Operator view](operator-view.md) | The read-only operator console: the projection/state vocabulary, recovery-action matrix, bounded drill-down and typed blocked-reason diagnostics (R37-16), the support-bundle export bounds and the pilot failure-case runbook slice | Written (v0.36.0) |
| [Lab alignment runbook](lab-alignment-runbook.md) | R37-06: the read-only lab inventory (`scripts/inventory_lab.py`), the observed-vs-pinned compatibility verdict, the podman alignment procedure, the numerical budget caps and the refusal-resolution matrix | Written (v0.36.0) |
| [Conformance gate](conformance-gate.md) | R38-16 (#317): the every-push native template + secret-consumer gate (`scripts/gate_conformance.py`) — the executed shipped-recipe shells, the dispatch capture fixture, the sentinel proofs, exit codes and the report manifest | Written |
| [Review rounds](review-rounds.md) | R40-02 (#338): the bounded post-readiness correction — the linked child work unit, the eligibility ladder (MR state, one outstanding round, head fence, round bound), the classic-run adapter, the reconciler recovery pass and the replay discipline | Written |
| [Production-entry mutation gates](production-entry-mutation-gates.md) | R40-08 (#344): the REQUIRED composed-trace set (feedback ingress, partial-liability admission, guarded review amendment, grant persistence) with their seeded-mutation arms, the pg-gate `prerequisite_missing` classification, the critical-id source SHAs and the trace-record format | Written |
| [Support agreement](support-agreement.md) | Q39-15 (#334): the supported profile's measured operating limits (live + code-proven, each number's evidence named), the operator surface and the four `ops.*` measures, response ownership, the EXCLUDED failure domains (home network/NAT/DNS, with the 2026-09-25 outage as the worked example) and the pending-human items stated as pending | Written |

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
