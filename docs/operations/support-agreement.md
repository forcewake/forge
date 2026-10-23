# Support boundary

The current source tree is unreleased. No 1.0 support decision, hosted
release result, external acceptance or fleet SLA is declared here.
Historical lab measurements and pending decisions remain at original path
`docs/operations/support-agreement.md` in the [archive](../archive.md).
Their version, environment and evidence class limit every claim.

The operator API is scoped and read-only. It exposes actual state,
occupancy, candidate verification, checkpoint recovery, lineage and command
progress. Missing authority is unknown or null, never invented capacity.
Out-of-scope and unknown runs are indistinguishable. Reading never starts
work or performs provider writes. Optional support-bundle and limits-grade
reports are removed; the durable operational contracts remain.

The forge operator owns the control plane, database, storage, backups and
model-route configuration. Provider and runner owners own those systems.
Humans own approval, budget amendment, merge and external acceptance.
Network/DNS/TLS failures, vendor quotas, customer CI beyond the declared
contract and lost durable data without backups are outside a software
recovery promise. Unknown effects and occupancy remain held until observed.

Operational readiness requires a pinned composition, real backups, a
rehearsed restore and actual provider workflows. Release checks, recovery
rehearsals, second-operator repetition and external acceptance are separate
facts. Historical pending credential refreshes or approvals are not marked
complete by this documentation change.

Use [deployment boundaries](deployment-boundaries.md),
[operator diagnostics](operator-view.md), [backup/restore](backup-restore.md)
and the [release flow](release.md).
