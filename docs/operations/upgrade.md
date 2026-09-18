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

# 2. Pull/build the new image — pin the DIGEST from the release summary
#    (see the canary section below); tags move, digests do not.
podman pull ghcr.io/forcewake/forge@sha256:<digest>   # or build from source

# 3. Migrate the schema (the image ships the chain; no source checkout)
podman run --rm --network <forge-net> \
  -e DATABASE_URL=postgresql+asyncpg://forge:***@forge-postgres:5432/forge \
  ghcr.io/forcewake/forge@sha256:<digest> python -m forge.migrate

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

## Schema compatibility gate (F26, R22)

On startup (app and worker), `init_db` compares the database's live alembic
revision against the migration chain shipped in this build — the marker
comparison against the `schema_version` table it replaced was the R22 bug
(migration 008 added required columns without bumping the marker, so a stale
database passed the gate and crashed later). The gate NEVER mutates an
existing database:

- **Database at the chain head** → boot.
- **Database behind head** → refuse to start, naming both revisions:

  ```
  RuntimeError: Database schema is out of date with this forge release:
  database is at 007b, release requires 016. Run the migrations before
  starting: `uv run alembic upgrade heads` (in the release image:
  `python -m forge.migrate`). Forge refuses to boot against a stale schema
  and never auto-upgrades an existing database — see docs/operations/upgrade.md.
  ```

- **Forge tables but no `alembic_version`** (pre-alembic database) → refuse
  with manual baseline instructions (`alembic stamp <head>` after verifying
  the schema — no knowable migration path).
- **Multiple heads in the database** (branched history) → refuse.
- **Fresh (empty) database** → the schema is created by running the full
  001→head migration chain. `create_all` is never a second schema factory
  on Postgres (that drift is exactly what caused R22); SQLite dev/test
  keeps the alembic-cookbook shortcut (create + stamp), because migrations
  004+ use ALTER CHECK, which only Postgres supports.

## Release canary and digest pinning (R30)

Every release is canaried against the **published artifact**, not the
source tree — the tag-triggered release workflow pushes the built image **by
digest**, runs `scripts/canary_smoke.py` against that digest (pulled back
from GHCR, no source mount), and only then attaches the `:<version>` and
`:latest` tags and signs. The digest is published in the job summary —
**pin the digest, not a tag**:

```bash
docker pull ghcr.io/forcewake/forge@sha256:<digest>   # from the release summary
```

The canary stages, and what each proves about the image itself:

- **fresh** — `python -m forge.migrate` on a disposable Postgres runs the
  real alembic chain inside the image; the R22 boot gate passes at head
  (database revision == shipped script head); the image CMD serves
  `/health` with `status: ok` and the release version; `/mcp` answers 401
  unauthenticated (fail closed); `forge doctor` passes database, redis and
  litellm. GitLab checks are out of scope (no GitLab fixture in CI).
- **migrate** — the previous release's image migrates a second fresh
  database; this image must REFUSE to boot on that stale schema (the R22
  gate), then upgrade prev→head and pass the gate. Skips with a notice on
  the first release (nothing to upgrade from).

No LLM keys are needed: LiteLLM *reachability* is the boundary, satisfied
by a stub HTTP endpoint served by the image itself; no model call is made.

The same script runs locally against any image, and nightly in CI against
the `main` build (`.github/workflows/ci.yml` job `release-canary`):

```bash
docker build -f Containerfile -t forge:canary .
python3 scripts/canary_smoke.py forge:canary --allow-unpinned --stages fresh,migrate

# with a previous release for the upgrade path:
python3 scripts/canary_smoke.py forge:canary --allow-unpinned \
  --previous-image ghcr.io/forcewake/forge:latest
```

Signatures and provenance (public repo): images are keyless-signed with
cosign and carry a build-provenance attestation, both bound to the digest.

```bash
cosign verify ghcr.io/forcewake/forge@sha256:<digest> \
  --certificate-identity-regexp '^https://github.com/forcewake/forge/.github/workflows/release.yml@.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
gh attestation verify oci://ghcr.io/forcewake/forge@sha256:<digest> -R forcewake/forge
```

Rolling out an upgrade with a pinned digest: substitute the digest ref for
the tag in step 2 of the ordering section — migrate with the same ref you
will run.
