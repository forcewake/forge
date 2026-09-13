# Backup and restore (runbook)

forge's state of record lives in **Postgres** (runs, steps, gates, usage
ledger, action journal, event inbox/outbox). Redis holds only transient
queue/heartbeat/dedup state — it is **not** backed up. Captured webhook
payloads (optional, `FORGE_CAPTURE_DIR`) are supplementary evidence.

## Backup

```bash
# consistent logical backup (run from any host that reaches Postgres)
podman exec forge-postgres \
  pg_dump -U forge -d forge -Fc -f /tmp/forge-$(date +%F).dump
podman cp forge-postgres:/tmp/forge-$(date +%F).dump ./backups/

# captured webhook payloads (optional, plain files)
tar czf ./backups/forge-captures-$(date +%F).tgz data/captured/
```

Schedule: nightly `pg_dump` plus a pre-upgrade dump (see
[upgrade.md](upgrade.md)). Keep dumps out of any Git repository — they
contain issue titles, plan digests and model prompts.

## What is NOT in the backup and must not be

- Secrets: bot PAT, model API keys, webhook secret — those live in GitLab CI
  variables and your secrets store, never in forge's database.
- Git content: all code lives in the GitLab repositories; forge only
  references SHAs.

## Restore

```bash
# 1. Stop writers (app keeps serving read-only-ish; worker must not run)
podman stop forge-worker forge-app

# 2. Restore into a clean database
podman exec forge-postgres psql -U forge -c "DROP DATABASE IF EXISTS forge;"
podman exec forge-postgres psql -U forge -c "CREATE DATABASE forge;"
podman cp ./backups/forge-YYYY-MM-DD.dump forge-postgres:/tmp/
podman exec forge-postgres pg_restore -U forge -d forge --no-owner /tmp/forge-YYYY-MM-DD.dump

# 3. Clear transient queue state (Redis was not restored)
podman exec forge-redis redis-cli FLUSHDB

# 4. Start and verify
podman start forge-app forge-worker
python -m forge.doctor
```

## After a restore: reconcile in-flight runs

Runs that were mid-flight at backup time may point at pipelines/jobs that
have since finished. The reconciler is designed for this: it re-reads the
world (branch head, pipeline, job status) and applies the verdict on the
next tick. Runs whose external state is gone (deleted branch/pipeline) end
in `blocked` with a reason — that is the intended safe outcome, resolve them
manually or `/cancel`.
