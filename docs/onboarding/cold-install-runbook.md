# Install the 1.0 source candidate

Forge 1.0 has one server runtime, one SQL schema and an independent CI lane.
This source candidate is not a published release. The old runtime is removed from both the source and the installed server.

Use Python 3.13+ and uv. For development, run
`uv sync --all-packages --extra postgres --extra claude`. Install a built
server wheel with its selected database driver: `forge[postgres]` for
PostgreSQL or `forge[sqlite]` for local development. The lane installs
`forge-lane[claude]` separately; the server has no vendor SDK dependency.

Copy [forge.example.toml](../../forge.example.toml) to a private `forge.toml`.
Replace the example native IDs, URLs, immutable lane requirement digest and
reviewed gateway artifact digest. Configure named required verification jobs
and native workflow paths. Supply secrets through exactly the environment
variable names declared in that file; the server does not load `.env`.
The signing key needs at least 32 bytes. Keep App/project write credentials
on the server; the coding lane receives no repository write credential.

Build and configure the [pinned gateway](../../tools/litellm/README.md).
Use the same reviewed tariff bytes and independently built artifact manifest
in the gateway and server configuration. `doctor` verifies native prospective
budget enforcement without making a model request.

For a new, explicitly empty database, run `forge --config forge.toml install`.
Then run `forge --config forge.toml doctor` and
`forge --config forge.toml onboard project`. Start
`forge --config forge.toml serve` and `forge --config forge.toml worker`
against the same configuration and database. Liveness is `/healthz`;
readiness is `/readyz`. Authenticated native events arrive at
`/hooks/<connection-key>`. Operator API and metrics require an explicitly
configured operator token.

Startup checks the schema without changing it. An existing 0.42 database
refuses startup and initialization. Preserve it and use the explicit offline
`forge-schema export-history` and `forge-schema migrate` procedures only
after stopping its service and executions. These commands require a separate
empty restore-verification database and private archive location. An install
must never experiment on an owner's existing database.

The container installs hash-locked dependencies and built wheels, runs as an
unprivileged user and includes native Git and CA certificates. Mount the
configuration, pricing policy and writable blob storage explicitly. The
[Compose file](../../docker-compose.yml) runs the app, worker and PostgreSQL;
the guarded HTTPS model gateway is an explicit external dependency. Set its
three private configuration/password inputs, then follow the explicit
first-install commands in that file. It never reinitializes a database on
startup. Offline archive/cutover commands also need native PostgreSQL client
tools on the machine where those commands are run.

A local install, owned fixture workflow, hosted CI and live provider acceptance
are separate results. Check the [implementation record](../roadmap/2026-09-29-simplification/IMPLEMENTATION-1.0.md)
for the verified local scope and external acceptance boundaries.
