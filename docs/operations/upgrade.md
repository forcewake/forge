# Upgrade and rollback

Operate one pinned composition: app and worker use the same image, schema
and lane/template compatibility. This unreleased source is not a published
upgrade recommendation. Read the [changelog](../../CHANGELOG.md) and binding
[design](../roadmap/2026-09-29-simplification/DESIGN.md) first.

## Upgrade order

1. Back up the database and corresponding checkpoint/blob data, deployment
   configuration and required signing keys through the private secret store.
2. Stop app and worker writers. Identify in-flight native jobs and preserve
   their durable intent; stopping a consumer does not prove a remote job stopped.
3. Select the exact reviewed release image digest and wheel/template identity.
   Verify available signatures and provenance; never substitute a moving tag.
4. Run `python -m forge.migrate` from that image against the intended database
   before starting new consumers. No source mount or automatic schema bypass.
5. Recreate both consumers on that same composition and preserved shared data.
   Run doctor and health checks, then reconcile in-flight work before dispatch.

Startup compares the live Alembic revision with the shipped chain. A stale,
unknown or branched existing schema refuses; the gate does not auto-upgrade
it. PostgreSQL fresh initialization runs the migration chain. SQLite's
local create-and-stamp shortcut is not PostgreSQL migration evidence.

## Rollback

Stop both consumers first. Determine whether the prior image can use the
current schema. Phase 1's unchanged-schema rollback and later additive or
breaking migrations have different requirements; never infer compatibility
from a version string. A guarded downgrade refuses while it would destroy
authorization, grant or collaboration evidence. Preserve that refusal;
never delete authority rows merely to force it green.

When schema rollback is required, use the approved pre-upgrade backup and
its matching checkpoint/blob/configuration/key set through
[backup/restore](backup-restore.md). Recreate both consumers on the previous
exact digest, inspect work created after the backup and reconcile external
heads/jobs/intents before any retry. Restored state is older than the remote
world; it does not authorize replay of effects.

The [release flow](release.md) checks installed wheels, exact-image fresh
boot and the previous-release seeded migration canary, followed by both
provider smokes. Hosted success must be observed on the selected artifacts;
local source validation is not that evidence. Development canary tooling
lives under `tools/`, outside the wheel. Historical release-canary commands
remain at `docs/operations/upgrade.md` in the [archive](../archive.md).
