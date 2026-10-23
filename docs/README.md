# Documentation

Forge plans an issue, waits for approval, executes in the target's CI and
publishes a verified candidate as a Draft. Merge stays with a human.
The [main README](../README.md) states the current release status.

| Start or operate | Guide |
| --- | --- |
| Connect a repository | [GitHub](getting-started/github.md), [GitLab](getting-started/gitlab.md) |
| Install from this source tree | [Installation](onboarding/cold-install-runbook.md) |
| Configure the current CI lane | [Harness onboarding](harnesses/onboarding.md), [driver index](harnesses/README.md) |
| Understand the new lane | [Lane v1](harnesses/lane-v1.md), [architecture](overview.md) |
| Approve, cancel or diagnose work | [Commands](operations/operator-commands.md), [read-only operator API](operations/operator-view.md) |
| Operate the deployment | [Operations](operations/README.md) |
| Recover data or change versions | [Backup and restore](operations/backup-restore.md), [upgrade](operations/upgrade.md) |
| Rotate credentials | [Tokens](operations/token-rotation.md), [current lane credentials](operations/credential-rotation.md) |
| Assess risks and limitations | [Threat model](security/threat-model.md), [FAQ](reference/faq.md), [support boundary](operations/support-agreement.md) |
| Develop safely | [Agent instructions](../AGENTS.md), [testing](reference/testing.md), [edge cases](reference/edge-cases.md) |
| Read architectural decisions | [ADRs](adr/0000-record-architecture-decisions.md), [simplification design](roadmap/2026-09-29-simplification/DESIGN.md) |
| Check implementation progress | [Implementation record](roadmap/2026-09-29-simplification/IMPLEMENTATION-1.0.md), [roadmap](roadmap/2026-09-29-simplification/ROADMAP.md), [metrics](roadmap/2026-09-29-simplification/metrics.md) |
| Recover historical material | [Archive guide](archive.md), [0.x changelog](archive/CHANGELOG-0.x.md) |

The canonical simplification plans are execution records, not setup guides.
Historical measurements do not certify the current checkout.
