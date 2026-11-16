# Backup and restore

PostgreSQL holds durable runs, approvals, budgets, effect intent and lane
session state. Lane blobs live on the `blob_directory` volume (compose
mounts it at `/data/blobs`). There is no Redis queue to back up.

Stop the app and worker, then dump the database and the blob volume together:

```bash
docker compose stop app worker
docker compose exec -T postgres pg_dump -U forge -d forge -Fc > forge.dump
docker compose run --rm --no-deps --entrypoint tar app -C /data -cf - blobs > blobs.tar
```

Restore into a new empty database and an empty blob volume, then check the schema.
The app container already has `DATABASE_URL` from the private env file:

```bash
docker compose up -d postgres
docker compose exec -T postgres pg_restore -U forge -d forge --no-owner < forge.dump
docker compose run --rm --no-deps --entrypoint tar app -C /data -xf - < blobs.tar
docker compose run --rm --no-deps app python -m forge.store.schema check
docker compose up -d app worker
```

The image does not need a second schema tool. `pg_dump` and `pg_restore`
come from the Postgres service, not the app image. Keep backups outside Git.

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
4. Recreate app and worker from one schema-compatible pinned composition.
   There is no Redis tenant to clear.
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
