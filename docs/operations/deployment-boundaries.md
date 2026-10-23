# Deployment boundaries

Run app and worker from one pinned composition against the same durable
database and checkpoint/blob store. PostgreSQL is the deployment state of
record; Redis queues and heartbeat projections are transient. CI runners
are disposable, unprivileged and separate from the control plane. They
receive no publisher token, database credentials, Docker socket or
production secrets. Use HTTPS for public webhook and lane endpoints.

The current provider services still use their existing lane. New lane v1
uses the separate protocol and runner packages and requires explicit OIDC
and model-gateway composition. Do not treat its source availability as an
automatic production migration. Deployments must keep their selected lane,
server and template versions compatible.

## Outages and recovery

- Provider or model unavailability parks work with a typed reason and
  bounded retries. A network error is not proof that a write did not land.
- Native execution holds its slot while running, draining or unknown.
  Never release a lease from elapsed time or a local terminal status;
  require a provider terminal observation or the explicitly audited
  override after independent evidence. Investigate unresolved-effect age
  and native unknown age before authorizing another effect.
- Database loss of connectivity stops new authoritative work. Restore
  connectivity and let reconciliation use persisted intent; do not edit
  leases or replay external writes by hand.
- Storage pressure must preserve pinned WIP and investigation holds.
  Remove only eligible superseded data through the store's retention
  policy. A missing required checkpoint blocks resume.
- Credential expiry requires rotation/rebind and a new authorized attempt;
  do not widen an expired grant's absolute deadline or use ambient keys.
- Queue age, storage consumption, credential expiry and oldest unresolved
  effect need deployment-specific alert thresholds. Historical lab latency
  or capacity measurements are not fleet SLAs.

The forge operator owns app/worker, durable state, storage and configured
model routing. The provider/runner owner owns their platform; vendor quota,
network, DNS, TLS termination and customer CI outside the declared contract
remain external responsibilities. Physical data loss without backups is
not recoverable by reconciliation.

Before an upgrade or restore, stop writers, preserve consistent database
and checkpoint data, migrate deliberately and verify before dispatch
resumes. Use [backup/restore](backup-restore.md), [upgrade](upgrade.md),
[operator diagnostics](operator-view.md), [credentials](credential-rotation.md)
and the [support boundary](support-agreement.md).

Historical drill reports and thresholds remain in the [archive](../archive.md)
at original paths `docs/operations/deployment-boundaries.md` and
`qualification/deployment-ops-*.json`; they do not certify this checkout.
