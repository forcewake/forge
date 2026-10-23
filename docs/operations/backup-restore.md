# Backup and restore

PostgreSQL holds durable runs, approvals, budgets, inbox/outbox, effect
intent and lane session/replay state. Checkpoints and lane blobs have a
separate content store. Back up their consistent set, deployment identity
and private key/configuration references. Redis queue/heartbeat state is
transient, not an authority backup.

Use a consistent logical `pg_dump` (custom format) on the selected database,
plus the matching checkpoint/blob tree and manifests. Coordinate a pause
of writers when needed to bind both halves. Keep pre-upgrade and scheduled
backups outside Git, encrypted and access-controlled. Capture files can
contain issue/comment text; they follow [retention](audit-retention.md).

Raw secrets belong in the private secret store. Encrypted cached model
grants may be present in durable run evidence; recovery needs the matching
server signing/encryption key. Preserve legacy credential anchors, registry
revisions and locator maps with their deployment state. A database dump
alone is not an exact-WIP or credential recovery proof.

## Restore order

1. Stop app and worker writers and prevent dispatch. Record in-flight native
   jobs; stopping forge does not stop remote jobs or undo landed effects.
2. Restore into the explicitly selected clean database and restore matching
   checkpoint/blob/configuration/key state. Never use a shared lab as a
   disposable restore target.
3. Verify the shipped migration head, durable authority rows, checkpoint
   digests and their base/generation linkage before enabling any resume.
   A missing or mismatched required half keeps dispatch closed.
4. Clear only the deployment's selected transient queue database after
   writers stop, without touching unrelated Redis tenants. Recreate app
   and worker from one schema-compatible pinned composition.
5. Run health/doctor and reconcile remote branch heads, Drafts, jobs and
   persisted intent against the restored rows. Remote state may be newer
   than the backup. Probe/adopt unknown effects; never replay blindly.

Keep dispatch closed until consistency is demonstrated. A required
checkpoint that cannot be restored means explicit retirement or a separately
authorized restart, not silent WIP loss. Audit amendments, grants,
redemptions, review-round targets and draining native occupancy are part of
the consistency check, not optional report data. Do not force a guarded
schema downgrade by deleting authorization rows.

Rehearse on disposable infrastructure and retain the observed fingerprints
and exact artifact identity. A successful database import proves neither
provider recovery nor model execution. See [upgrade](upgrade.md),
[token rotation](token-rotation.md) and [deployment boundaries](deployment-boundaries.md).
