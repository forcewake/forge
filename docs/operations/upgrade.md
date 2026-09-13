# Upgrade (runbook)

How to move forge between versions without losing in-flight runs.

## Ordering (the one rule)

**Schema migrations land before the new code starts.** Both app and worker
must run the SAME minor version — a worker on old code with a newer schema
is the only unsupported combination.

```bash
# 0. Pre-upgrade backup (mandatory)
podman exec forge-postgres pg_dump -U forge -d forge -Fc -f /tmp/pre-upgrade.dump   # see backup-restore.md

# 1. Stop consumers
podman stop forge-worker forge-app

# 2. Pull/build the new image (same tag scheme: ghcr.io/forcewake/forge:<version>)
podman build -t ghcr.io/forcewake/forge:<version> -f Containerfile .

# 3. Migrate the schema
podman run --rm --network <forge-net> \
  -e DATABASE_URL=postgresql+asyncpg://forge:***@forge-postgres:5432/forge \
  ghcr.io/forcewake/forge:<version> uv run --no-sync alembic upgrade head

# 4. Start app + worker on the new image
#    (recreate containers with the same env; see the lab skill for the flags)

# 5. Verify
python -m forge.doctor
```

## What is safe mid-upgrade

- Runs parked in `waiting_approval`, `waiting_ci`, `waiting_harness` are
  durable and reconciler-driven — they simply wait out the restart.
- A harness job that finishes while forge is down is adopted on the next
  reconciler tick (branch head + `FORGE_RESULT` are re-verified).
- Webhooks arriving while the app is down: GitLab retries non-2xx
  deliveries; the inbox idempotency key collapses duplicates.

## Rollback

1. Stop app + worker.
2. `pg_restore` the pre-upgrade dump (see [backup-restore.md](backup-restore.md)).
3. Recreate app + worker on the previous image tag.
4. Check `forge doctor`; review runs created between upgrade and rollback —
   they may reference schema rows that no longer exist and should be
   `/cancel`-ed.

## Compatibility notes

- Within a minor (0.1.x): schema additive-only; downgrade = restore backup.
- Across minors: read the [CHANGELOG](../../CHANGELOG.md) — the durable
  state machine may gain states; never run mixed versions of app and worker.

## Schema compatibility gate (F26)

The final migration of every release writes the expected version into the
single-row `schema_version` table. On startup, `init_db` checks it:

- **Fresh (empty) database** → tables are created and the marker written
  (unchanged dev bootstrap).
- **Existing forge database with a missing or different marker** → the app
  (and worker) refuse to start:

  ```
  RuntimeError: Database schema is not compatible with this forge version:
  expected schema_version=1, found None. Run the migrations before starting
  (alembic upgrade head, or `python -m forge.migrate`) — see
  docs/operations/upgrade.md.
  ```

  This is deliberate: `create_all` is a bootstrap, never an upgrade. Run
  step 3 of the ordering section above, then start the services.
