# Install or migrate the schema

The new engine uses one fresh baseline. Application and worker startup call
`check_schema` and refuse a missing, old or modified schema. Startup does not
create tables, stamp revisions or migrate data.

For a new installation, select the database explicitly through `DATABASE_URL`,
then run `python -m forge.store.schema init` followed by
`python -m forge.store.schema check`. The installer requires a completely empty
database. SQLite connections must enable foreign keys before startup.

An existing installation requires an offline cutover. Stop the old service and
all native lane executions first. Resolve unfinished effects, leases, model
spend and active runs using the original release. The migration refuses
unsupported revisions, unknown development-engine history and unresolved old
authority. It does not turn historical records into runnable new-engine work.

Select three distinct databases:

- `FORGE_LEGACY_DATABASE_URL`: the stopped historical source;
- `FORGE_MIGRATION_VERIFY_URL`: an empty disposable database for restoring and
  checking the native backup, using the same database dialect as the source;
- `DATABASE_URL`: an empty destination for the fresh engine baseline.

Select a new private output directory outside every Git repository. Run
`python -m forge.store.schema migrate --output /private/backup-directory
--old-service-stopped --executions-stopped`. The declarations are operator
assertions that both the service and native executions have stopped.

The command exports every source table as typed canonical JSONL, creates a
native SQLite or PostgreSQL backup, restores it into the verification database,
and compares all row digests, physical schema and sequence state. PostgreSQL
uses a shared read-only repeatable-read snapshot. The command checks the source
again before creating the fresh destination. Failure leaves the source intact;
an incomplete output directory is not an accepted migration archive.

The accepted output contains a manifest, its SHA-256, the native backup and
read-only historical row files. Files are private and written once by the tool;
keep the directory in protected backup storage. The new installation records
both the manifest and backup digests. The verification database contains private
historical data and remains available for operator inspection and disposal.

`export-history` performs the same backup, restore and export verification
without installing a destination. It requires the same stop declarations and
an empty verification database. A later `migrate` creates and verifies a fresh
archive again; a supplied manifest is never treated as permission to skip checks.

PostgreSQL requires local `pg_dump` and `pg_restore` tools compatible with the
selected server. Connections use the explicitly selected host, database, user,
password and supported TLS mode; ambient PostgreSQL connection settings do not
select another database. SQLite migration requires persistent local files and a
private verification directory.

Retain the old release and the verified backup for rollback. Restore into a
separate database and use its matching original release. Destructive schema
downgrade is refused. Keep old services stopped when starting the new engine.
