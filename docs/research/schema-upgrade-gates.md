# Schema upgrade gates: research for R22 (stale schema-version marker) and R30 (release canary hardening)

Research input for replacing forge's hand-rolled `EXPECTED_SCHEMA_VERSION` startup gate
(`src/forge/database.py`) with an alembic-native revision check, adding upgrade-path tests,
and hardening the GHCR release job.

Forge context (verified in-repo): `alembic 1.18.4`, `sqlalchemy 2.0.48`, linear migration
chain `001 → 007b → … → 011` with `ScriptDirectory.get_heads() == ["011"]`. The current gate
compares a `schema_version` marker row (`EXPECTED_SCHEMA_VERSION = 1`) that migration 007b
introduced; migration 008 added columns the current models require without bumping the
marker, so an old database passes the gate and crashes later at runtime (review finding R22).
`python -m forge.migrate` already runs `command.upgrade(cfg, "head")` programmatically
(`src/forge/migrate.py`), and `.github/workflows/release.yml` builds the image, runs two
source-free smoke commands, and pushes mutable tags (`v*`, `latest`) without capturing a
digest or signing (review finding R30).

---

## Alembic API ground truths

All snippets verified against the installed alembic 1.18.4 in forge's venv and the current
docs (docs currently publish alembic 1.20.0; the APIs below are stable across both).

### Reading the database's current revision(s) without a config file

`alembic.runtime.migration.MigrationContext.configure()` takes a raw SQLAlchemy connection —
no `alembic.ini`, no env.py. Docs: https://alembic.sqlalchemy.org/en/latest/api/runtime.html

```python
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

engine = create_engine("postgresql://mydatabase")
conn = engine.connect()

context = MigrationContext.configure(conn)
current_rev = context.get_current_revision()
```

`configure()` signature (from the same page):
`MigrationContext.configure(connection=None, url=None, dialect_name=None, dialect=None,
environment_context=None, dialect_opts=None, opts=None)`. Passing a `Connection` selects the
dialect; `opts` accepts most `EnvironmentContext.configure()` options (e.g. `version_table`).

Semantics of the two getters (doc text plus the installed 1.18.4 source
`.venv/lib/python3.13/site-packages/alembic/runtime/migration.py`, lines 472–533):

- `get_current_heads()` — "Return a tuple of the current 'head versions' that are represented
  in the target database." Multiple unmerged branches → one value per head. **"If no version
  table is present, or if there are no revisions present, an empty tuple is returned"**
  (the source short-circuits on `self._has_version_table()` before querying).
- `get_current_revision()` — returns `None` when `get_current_heads()` is empty (i.e. empty DB
  or table absent); **raises `CommandError`** ("Version table '%s' has more than one head
  present; please use get_current_heads()") when branches exist. The docs themselves say to
  prefer `get_current_heads()` "in order to be compatible with branch migration support".

Consequence for forge: `get_current_heads() == ()` is *ambiguous* between a truly fresh
database and a **pre-alembic legacy forge database** (forge tables exist, `alembic_version`
does not). The gate must disambiguate with a forge-table probe (the existing
`_forge_tables_present` heuristic) — alembic alone cannot tell those cases apart.

### Computing the script head(s)

`alembic.script.ScriptDirectory` — docs: https://alembic.sqlalchemy.org/en/latest/api/script.html

```python
from alembic.script import ScriptDirectory
from alembic.config import Config

config = Config()
config.set_main_option("script_location", "myapp:migrations")
script = ScriptDirectory.from_config(config)

head_revision = script.get_current_head()
```

- Only `script_location` is required in the `Config` — `Config()` with no ini path plus
  `set_main_option()` is the documented no-file construction.
- `get_heads(consider_depends_on: bool = False) -> list[str]` — "normally a list of length
  one, unless branches are present" (the `consider_depends_on` parameter is new in 1.18.5).
- `get_current_head() -> str | None` — "If the script directory has multiple heads due to
  branching, an error is raised; `ScriptDirectory.get_heads()` should be preferred." Use
  `get_heads()` in the gate, never `get_current_head()`.
- `get_revisions(id_)` accepts a single id, a sequence, or the symbols `"head"`/`"base"`;
  `walk_revisions(base="base", head="heads")` iterates the whole tree (useful for
  parametrized upgrade tests).
- Branch labels: `Revision.branch_labels: set[str]` — "Optional string/tuple of symbolic
  names to apply to this revision's branch"; `MultipleHeads` is raised when a symbolic name
  resolves to several heads. Forge's chain is linear today, but the gate should compare sets
  so it degrades gracefully if a future branch lands.

### The upgrade API, callable programmatically

Docs: https://alembic.sqlalchemy.org/en/latest/api/commands.html

```python
alembic.command.upgrade(config, revision, sql=False, tag=None) -> None
alembic.command.downgrade(config, revision, sql=False, tag=None) -> None
alembic.command.stamp(config, revision, sql=False, tag=None, purge=False) -> None
```

Verbatim doc example and the connection-sharing idiom:

```python
from alembic.config import Config
from alembic import command
alembic_cfg = Config("/path/to/yourapp/alembic.ini")
command.upgrade(alembic_cfg, "head")

# run inside an existing transaction; requires env.py to consume it:
with engine.begin() as connection:
    alembic_cfg.attributes['connection'] = connection
    command.upgrade(alembic_cfg, "head")
```

Revision targets: `"heads"` targets all current head(s); `downgrade` accepts `"base"`;
`stamp` accepts `"base"` (clears the version table) / `"heads"` / a list. `sql=True` emits
SQL to stdout instead of executing (offline mode). Forge's `src/forge/migrate.py` already
does exactly this (`command.upgrade(cfg, "head")` with the ini resolved at `/app/alembic.ini`
or repo root), so the gate and the migrate entrypoint can share one `Config` builder.

### The canonical "is the DB at head" check (alembic's own cookbook)

https://alembic.sqlalchemy.org/en/latest/cookbook.html — section "Test current database
revision is at head(s)", which the docs say "may be useful for test or installation suites":

```python
directory = script.ScriptDirectory.from_config(alembic_cfg)
with connectable.begin() as connection:
    context = migration.MigrationContext.configure(connection)
    return set(context.get_current_heads()) == set(directory.get_heads())
```

Since alembic 1.17.1 the cookbook also recommends the CLI shortcut `alembic current
--check-heads`; in forge's installed 1.18.4 the help reads: "`-c, --check-heads  Check if all
head revisions are applied to the database. Exit with an error code if this is not the
case." Useful for a doctor/CLI check, but the programmatic form gives forge the actionable
error message it needs.

### Empty DB vs pre-alembic legacy DB vs multi-head

| Database state | `get_current_heads()` | `ScriptDirectory.get_heads()` (forge) | Correct action |
| --- | --- | --- | --- |
| Fresh/empty DB | `()` | `["011"]` | Run all migrations (see policy below) |
| At head | `("011",)` | `["011"]` | Boot |
| Behind (the R22 case) | `("007b",)` or older | `["011"]` | Refuse with remediation |
| Pre-alembic legacy forge DB (tables exist, no `alembic_version`) | `()` | `["011"]` | Refuse: manual baseline (`alembic stamp 011`) — cannot distinguish from fresh by alembic alone, needs forge-table probe |
| Multiple heads in DB | `("x", "y")` | `["011"]` | Refuse; merge/stamp guidance |

Related cookbook facts: "Building an Up to Date Database from Scratch" shows the
`metadata.create_all(engine)` + `command.stamp(alembic_cfg, "head")` pattern — alembic's own
blessed fresh-DB shortcut, and the thing forge should *not* prefer for its gate (see
recommendation section); and the data-migration strategies (`bulk_insert()` for small data,
`-x data=true` inline hooks) relevant to forge's 008/011 data backfills.

---

## Gate patterns prior art

### Django — `migrate --check` (exit code is the contract)

- `python manage.py migrate --check` "exits with a non-zero status when unapplied migrations
  are detected" and applies nothing: django-admin reference,
  https://django.readthedocs.io/en/stable/ref/django-admin.html (topic overview:
  https://docs.djangoproject.com/en/6.1/topics/migrations/).
- Adam Johnson's writeup: "The `--check` flag causes the command to fail (have a non-zero
  exit code) if any migrations are missing"; before Django 4.2.9 `--plan` was also needed for
  a correct check — https://adamj.eu/tech/2024/06/23/django-test-pending-migrations/
- CI/CD idiom: `python manage.py migrate --check` as a deploy precondition; a separate
  `python manage.py migrate` does the upgrade. Detection and mutation are separate commands.

Failure mode: non-zero exit; operator sees migration names in `--plan` output. Django never
auto-upgrades on boot — refuse, then run `migrate` explicitly.

### Rails — refuse on first use, status per migration

- `ActiveRecord::Migration::CheckPending` "is used to verify that all migrations have been
  run before loading a web page if `config.active_record.migration_error` is set to
  `:page_load`" — https://api.rubyonrails.org/v4.0.13/classes/ActiveRecord/Migration/CheckPending.html
- Generated dev config ships `config.active_record.migration_error = :page_load`; a page
  load with pending migrations raises `ActiveRecord::PendingMigrationError` ("Migrations are
  pending…"), and disabling means setting it to `false` —
  https://makandracards.com/makandra/36091-disable-rails-raising-errors-pending-migrations-development
- `bin/rails db:migrate:status` "displays the status (up or down) of each migration", and
  shows `********** NO FILE **********` for versions applied but no longer in the tree
  (orphaned revisions — a class of error worth echoing in forge's gate output) —
  https://guides.rubyonrails.org/active_record_migrations.html
- `bin/rails db:prepare` is the idempotent create-or-upgrade entrypoint: DB missing →
  create+load schema+seed; DB exists without tables → load schema + pending migrations; DB
  exists with tables → do nothing (same guide).

Failure mode: exception surfaced to the request (dev only) with the exact pending versions
in the error text; `db:migrate:status` gives the operator the current-vs-required picture.

### Flyway — validate compares "resolved" vs "applied"

- `flyway validate` compares migrations resolved from the local classpath against the
  `flyway_schema_history` table and fails on: applied-but-missing files (checksum drift),
  checksum mismatches, and **pending** migrations — error text of the form
  **"Detected resolved migration not applied to database: 1.1.1.2"** —
  https://www.red-gate.com/hub/product-learning/flyway/flyways-validate-command-explained-simply/
  and https://www.red-gate.com/hub/product-learning/flyway/how-to-fix-or-avoid-ignored-migrations-in-flyway/
  (community thread: https://stackoverflow.com/questions/36077766/detected-resolved-migration-not-applied-to-database-on-flyway)
- `ignoreMigrationPatterns` (e.g. `*:future`) lets teams allow "future" migrations from newer
  deployments while still failing on locally-pending ones — same Redgate pages.
- Remediation named in the error: run `flyway migrate`.

Failure mode: validation error listing the specific version(s); message includes what to run.

### Kubernetes — init container "migrate-then-start"

- Init containers run to completion before app containers start; if the migration init
  container fails, the app container never starts (built-in gate, retries via `restartPolicy`)
  — https://atlasgo.io/guides/deploying/k8s-init-container
- Andrew Lock's comparison of init containers vs a separate Kubernetes Job for migrations,
  including the multi-replica race: init containers run **per pod**, so `replicas > 1` can
  run migrations concurrently — prefer a one-shot Job or serialize with a Postgres advisory
  lock — https://andrewlock.net/deploying-asp-net-core-applications-to-kubernetes-part-8-running-database-migrations-using-jobs-and-init-containers/

Failure mode: pod stuck `Init:Error`/`Init:CrashLoopBackOff` with the migration container's
stderr in `kubectl logs <pod> -c migrate`.

### What a good gate error message must include

Synthesis of all four prior art forms (Django: exit code + plan; Rails: pending versions in
the exception; Flyway: resolved-vs-applied versions + remediation; K8s: container logs):

1. Current revision(s) exactly as recorded (`alembic_version` contents, or "(no
   alembic_version table)" / "(pre-alembic forge database)").
2. Required revision(s) — the script heads, same strings `alembic heads` prints.
3. The exact remediation command, copy-pasteable with the operator's environment:
   `DATABASE_URL=… python -m forge.migrate` (and equivalently `alembic upgrade head`).
4. What the process will do without it: refuse to start (non-zero exit), and the guarantee
   that the gate never mutates the database.
5. Special cases spelled out: multiple heads → say so and name them; orphaned/unknown
   revisions in the DB (Rails' `NO FILE` case) → say so; legacy pre-alembic DB → manual
   `stamp` instructions, since no migration path is knowable.
6. Pointer to docs (forge: `docs/operations/upgrade.md`).

---

## Upgrade-path test recipes

### Established tooling

- **pytest-alembic** (https://github.com/schireson/pytest-alembic) — pytest plugin, "runs on
  Python 3.10–3.15 with alembic 1.9+ and SQLAlchemy 1.4+". Four built-in default tests,
  enabled by `pip install pytest-alembic` + `pytest --test-alembic`:
  - `test_single_head_revision` — "Assert that there only exists one head revision" (catches
    the diverged-history-after-merge failure);
  - `test_upgrade` — "Assert that the revision history can be run through from base to head";
  - `test_model_definitions_match_ddl` — coalesced migrations DDL must equal the models, i.e.
    "`revision --autogenerate` should always generate an empty migration". **This is the test
    that would have caught forge's R22 drift** (models vs migrations disagreeing);
  - `test_up_down_consistency` — "Assert that all downgrades succeed".
  - Experimental: `all_models_register_on_metadata` (every model statically importable from
    env.py) and `downgrade_leaves_no_trace` (strict before/after autogenerate equality).
  - Custom migration-specific tests use the `alembic_runner` fixture:

    ```python
    def test_gnarly_migration_xyz123(alembic_engine, alembic_runner):
        # Migrate up to, but not including this new migration
        alembic_runner.migrate_up_before('xyz123')
    ```

    plus "custom static data … inserted automatically before a given revision" — the
    seeded-old-schema-data recipe for data migrations.
- **alembic-verify** (https://github.com/gianchub/alembic-verify) — pytest fixtures +
  utilities; requires Python 3.10–3.14, SQLAlchemy 1.4/2.0+, Alembic >= 1.8, pytest >= 7.
  Core helper, applied at any revision:

  ```python
  from alembicverify.util import prepare_schema_from_migrations

  @pytest.mark.usefixtures("alembic_new_db")
  def test_migrations(alembic_config, alembic_db_uri):
      with prepare_schema_from_migrations(alembic_db_uri, alembic_config, revision="head") as (engine, script):
          with engine.connect() as conn:
              ...  # query tables, verify schema
  ```

  Fixtures `alembic_new_db` / `alembic_config`; the README's URI fixture pattern creates a
  **unique temporary database per test** (`f"{base_uri}test_{uuid4().hex}"`); helpers
  `get_head_revision(config, engine, script)` / `get_current_revision(...)`; documented
  "Testing Upgrade/Downgrade Cycles" and branched-migrations sections.
- **pytest-postgresql** (https://github.com/ClearcodeHQ/pytest-postgresql) — `postgresql_proc`
  (session-scoped fixture that starts and stops a real PostgreSQL instance),
  `postgresql_noproc` (attach to an already-running server, e.g. a Docker/CI service), and
  template-database cloning: "The process fixture pre-populates the database once per
  session into a **template database**. The client fixture then clones this template for each
  test, which significantly **speeds up your tests**." Relevant to forge because asyncpg
  needs a *real* Postgres for migration tests (SQLite masking DDL bugs is exactly how a
  008-shaped bug ships).
- Alembic's own cookbook heads-check (snippet above) is the seed for a hand-rolled fixture if
  forge prefers zero new dependencies.

### Recipes, mapped to forge

1. **Fresh bootstrap → head** ("last-release→head" generalizes to it): unique temp database
   (pytest-postgresql template clone or uuid-suffixed DB), `command.upgrade(cfg, "head")`,
   then assert ORM queries work on the migrated schema. Equivalent to pytest-alembic's
   `test_upgrade`.
2. **Models == migrations** (the R22 regression test): pytest-alembic
   `test_model_definitions_match_ddl`, or a manual equivalent — upgrade to head, run
   `alembic.autogenerate.compare_metadata`, assert empty diff. Add
   `all_models_register_on_metadata` if forge's env.py import surface grows.
3. **N-1 → head (last real upgrade)**: upgrade to the previous revision
   (`sd.walk_revisions()` second-to-last, or a `PREVIOUS_RELEASE_REV` constant bumped at
   release), then `upgrade(cfg, "head")`. Parametrizing over *every* revision N ("upgrade to
   N, then N→head") is cheap for an 11-revision chain and catches every intermediate break,
   not just the latest.
4. **Data-migration tests with seeded old-schema data**: insert rows in the revision-N shape
   (raw SQL, not current models — the current models are what's wrong on old schemas), then
   upgrade N→head and assert the migrated rows (forge: the 008 github-column backfill and
   011 budget-liability migration are the candidates). pytest-alembic's
   `alembic_runner.migrate_up_before(...)` + custom static data, or alembic-verify's
   `revision="some_revision"`, both support this directly.
5. **Restore-from-backup → head**: `pg_dump` a database at revision N, `pg_restore` into a
   fresh database (or `CREATE DATABASE … TEMPLATE migrated_template` for speed), upgrade to
   head, assert row counts and a few invariants. This is the recipe that would have caught
   R22 end-to-end: a backup from a pre-008 deployment restored and upgraded must boot.
6. **Gate unit tests** (pure, no long migrations): stub/fake the MigrationContext — at-head
   passes; behind raises with both revisions in the message; `()` heads on a DB *with* forge
   tables raises (legacy case); `()` on an empty DB passes through to the fresh-DB policy;
   multi-head DB raises naming both heads.
7. **Downgrade checks**: only if forge commits to working `downgrade()` (see open questions);
   pytest-alembic `test_up_down_consistency` / `downgrade_leaves_no_trace` will fail
   otherwise — run them as an explicit, opt-in signal rather than a passing-by-accident gate.

CI wiring: a `postgres` service container (or `postgresql_noproc` against one) in
`.github/workflows/ci.yml`, one session-scoped engine factory in `conftest.py`, everything
above as ordinary pytest tests in the existing `tests/` tree (forge already has
`tests/test_database_lifecycle.py` pinning the current marker behavior — that file gets
rewritten by the R22 fix).

---

## Canary hardening

### Digest pinning ground truth

`docker pull NAME@sha256:…` — "When pulling an image by digest, you specify exactly which
version of an image to pull … and guarantee that the image you're using is always the same",
in contrast to tags, which move; the page also shows digest pinning in Dockerfiles
(`FROM ubuntu@sha256:…`) and warns pinning means you must consciously update to get security
fixes — https://docs.docker.com/reference/cli/docker/image/pull/

### Build once → smoke → push, in GitHub Actions

Official Docker pattern ("Test before push",
https://docs.docker.com/build/ci/github-actions/test-before-push/): build and `load: true`
to the local Docker daemon, `docker run --rm $TEST_TAG` the smoke test, *then* build+push —
"the `linux/amd64` image is only built once in this workflow … the second step only builds
`linux/arm64`" (cache reuse). Forge is single-arch today, so the even simpler shape applies:
build once, smoke the local image, push.

To make the smoke run target the *published, digest-pinned* artifact rather than the local
tag, the canonical mechanics are:

- `docker/build-push-action` exposes a **`digest` output** ("`digest | String | Image
  digest`") — https://github.com/docker/build-push-action (README outputs table). Build with
  `push: true` but no tags (push-by-digest), capture `steps.build.outputs.digest`, smoke
  `ghcr.io/org/forge@sha256:<digest>`, then apply tags afterwards.
- The historical multi-platform reference workflow does exactly this —
  `outputs: type=image,name=${REGISTRY_IMAGE},push-by-digest=true,name-canonical=true`, a
  `digest="${{ steps.build.outputs.digest }}"` export step, and a merge job that assembles
  the manifest from digests via `docker buildx imagetools create` — archived at
  https://github.com/docker/docs/blob/e3aa78b72c9f/content/build/ci/github-actions/multi-platform.md
  (the live page https://docs.docker.com/build/ci/github-actions/multi-platform/ now
  delegates the same flow to the `docker/github-builder` reusable workflow).
- Passing the image between jobs without a registry push (artifact tarball) is also
  documented, but only for single-platform images:
  https://docs.docker.com/build/ci/github-actions/share-image-jobs/

For forge: push-by-digest first, run the DB-backed smoke **against the digest** (see minimal
canary below), then `docker buildx imagetools create -t ghcr.io/…:vX.Y.Z -t ghcr.io/…:latest
ghcr.io/…@sha256:<digest>` to attach mutable tags to the validated digest. The digest is
then the thing the job summary publishes.

### Signing and provenance

- **cosign** (https://github.com/sigstore/cosign): keyless signing with Fulcio/Rekor is the
  default (`cosign sign $IMAGE` prompts the OIDC flow); in CI the non-interactive form is
  `cosign sign --yes`. The README's explicit rule: "make sure to reference any images you
  sign **by their digest** to make sure you don't sign the wrong thing" — sign the digest,
  never a mutable tag. Verification pins the signer identity, e.g.
  `--certificate-identity "https://github.com/ORG/REPO/.github/workflows/release.yml@refs/heads/main"`
  and `--certificate-oidc-issuer "https://token.actions.githubusercontent.com"`.
- **sigstore/cosign-installer** (https://github.com/sigstore/cosign-installer) — install step
  `uses: sigstore/cosign-installer@v4.1.0`; for keyless GitHub Actions signing the job needs
  `permissions: id-token: write` (OIDC).
- **actions/attest-build-provenance** (https://github.com/actions/attest-build-provenance) —
  generates "signed build provenance attestations for workflow artifacts", binding "some
  subject (a named artifact along with its digest) to a SLSA build provenance predicate"
  with "a short-lived Sigstore-issued signing certificate", stored in the GitHub attestations
  API and tied to the repo; free for public repos (private needs Enterprise Cloud). Consumers
  verify with `gh attestation verify`. Note per the README: as of v4 the action is "simply a
  wrapper on top of actions/attest" and new implementations should prefer `actions/attest`.

### Failure modes the canary should catch (why smoke *the digest*)

A local-tag smoke run validates "the Dockerfile as built on this runner", not the artifact a
user pulls. Smoke against the pushed digest additionally proves: registry round-trip intact
(layers pushed completely), the image is self-contained ("the image ships migrations; no
source checkout needed" — `src/forge/migrate.py` docstring), and the exact bytes later
pinned/signed are the bytes tested. That is the "release artifact passes the workflow
without source mount" property R30 asks for.

---

## Recommended design for forge (R22/R30)

### (a) Replace the marker gate with a revision-comparison gate

New function (suggested home: `src/forge/migrate.py`, next to the `Config` builder it
already has; called from `init_db` in `src/forge/database.py` and from app startup before
serving):

```python
from alembic.config import Config
from alembic import migration, script


def build_alembic_config() -> Config:          # shared with forge.migrate.main()
    cfg = Config(str(_resolve_alembic_ini()))  # /app/alembic.ini or repo root
    cfg.set_main_option("script_location", str(_resolve_alembic_dir()))
    return cfg


async def check_schema_compatible(database_url: str) -> None:
    """Raise RuntimeError (process exits non-zero) unless DB revision == script heads."""
    cfg = build_alembic_config()
    script_heads = set(script.ScriptDirectory.from_config(cfg).get_heads())
    engine = get_engine(database_url)
    async with engine.connect() as conn:
        context = await conn.run_sync(
            lambda sync_conn: migration.MigrationContext.configure(sync_conn)
        )
        db_heads = tuple(context.get_current_heads())          # () when table absent
        forge_present = await _forge_tables_present(conn)      # existing probe

    if db_heads and set(db_heads) == script_heads:
        return
    raise RuntimeError(_gate_message(db_heads, script_heads, forge_present))
```

Message template (per the checklist above):

```
Database schema is out of date with this forge release.
  database is at: 007b (or: <no alembic_version table — pre-alembic forge database>)
  release requires: 011
Fix: DATABASE_URL=<your url> python -m forge.migrate   (equivalent: alembic upgrade head)
Forge refuses to boot against a stale schema; create_all is a bootstrap, never an upgrade.
See docs/operations/upgrade.md.
```

Behavior matrix (matches "Alembic API ground truths" table): at-head → boot; behind → refuse
with remediation; forge tables + no `alembic_version` → refuse, instruct manual baseline
(`alembic stamp 011` after verifying the schema matches, or restore-and-upgrade from backup)
because no knowable migration path exists; multiple DB heads → refuse naming them with
`alembic upgrade heads`/stamp guidance; empty DB → policy in (b). Keep the
`schema_version` table purely cosmetic (007b still writes it; dropping it is a separate
migration 012 — decide separately). Delete `EXPECTED_SCHEMA_VERSION` and the marker
comparison; update `tests/test_database_lifecycle.py` accordingly.

Optionally expose the same check in `python -m forge.migrate --check` (mirroring Django's
`migrate --check`) so CI/deploy scripts get a zero-mutation gate, and
`alembic current --check-heads` remains available as the raw CLI equivalent (confirmed in the
installed 1.18.4 help text).

### (b) Fresh-DB policy: run all migrations; retire create_all

**Decision: fresh database ⇒ `command.upgrade(cfg, "head")` (full 001→011 chain);
`Base.metadata.create_all` is removed from the startup path entirely.** Rationale:

- **Single source of truth.** Alembic's cookbook does offer the alternative —
  `create_all` + `command.stamp(cfg, "head")` — but that asserts "models' DDL == migrations'
  DDL", which is precisely the invariant R22 proved forge does not maintain (models gained
  008's columns while history said otherwise). Two schema factories double the surface for
  the same bug class.
- **Fresh installs then exercise the same code path upgrades do**; the chain is 11 small
  migrations — seconds on dev hardware, and pytest-postgresql template cloning makes it fast
  in CI too.
- **No drift hook needed for correctness, but add the tripwire anyway**: enable
  pytest-alembic's `test_model_definitions_match_ddl` so "models == migrations" becomes a CI
  invariant instead of an assumption.
- Startup keeps the *gate* semantics: `init_db` checks and refuses; bootstrapping is
  explicit (`python -m forge.migrate`), matching Django (never auto-migrates) rather than
  Rails' dev-only `db:prepare`. An opt-in `FORGE_AUTO_MIGRATE=1` for dev convenience is a
  possible follow-up, default off.

### (c) CI test matrix for upgrade paths

Add to `tests/` (Postgres service + `postgresql_proc`/`postgresql_noproc` fixture in
`conftest.py`), roughly in priority order:

1. `test_gate.py` — unit tests of the new gate: at-head passes; stale (`007b` vs `["011"]`)
   raises with both revisions and the fix command in the message; forge-tables-without-
   `alembic_version` raises with baseline instructions; empty DB routes to migrate; multi-head
   raises. (Replaces the marker assertions in `tests/test_database_lifecycle.py`.)
2. `test_models_match_migrations.py` — autogenerate diff empty at head (pytest-alembic
   `test_model_definitions_match_ddl`, or hand-rolled `compare_metadata`). **The direct R22
   regression test.**
3. `test_upgrade_paths.py` — parametrized over every revision N: build schema at N,
   `upgrade(N → head)`, ORM smoke query at head. With 11 linear revisions this is 11 fast
   cases and covers last-release→head automatically.
4. `test_data_migrations.py` — seeded-old-schema rows (raw SQL at revision N) for the
   data-bearing migrations (008, 011), upgraded to head, asserting preserved/backfilled data
   (pytest-alembic `migrate_up_before` + static data, or alembic-verify
   `prepare_schema_from_migrations(..., revision=N)`).
5. `test_restore_backup.py` — `pg_dump` at N (or `CREATE DATABASE … TEMPLATE`), restore,
   upgrade to head, invariant checks — end-to-end R22 scenario.
6. Opt-in downgrade signal (`test_up_down_consistency`) once the downgrade policy (open
   question) is decided.

### (d) Minimal digest-pinned canary for the GHCR release job

Rewrite `publish-ghcr` in `.github/workflows/release.yml` to the push-by-digest → smoke the
digest → tag → sign flow (no source mount anywhere; every smoke command runs the pushed
image):

```yaml
permissions: { contents: read, packages: write, id-token: write }  # id-token for cosign
steps:
  - login to ghcr
  - id: build
    uses: docker/build-push-action@v6
    with:
      push: true
      outputs: type=image,name=ghcr.io/${{ github.repository }},push-by-digest=true,name-canonical=true
  - name: Smoke the digest (ephemeral Postgres service, health-checked)
    run: |
      DIGEST=ghcr.io/${{ github.repository }}@${{ steps.build.outputs.digest }}
      docker compose -f ci/canary-compose.yml up -d db          # pg_isready gate
      docker run --rm --network canary $DIGEST \
        python -c "import forge; assert forge.__version__"
      docker run --rm --network canary -e DATABASE_URL=postgresql+asyncpg://... \
        $DIGEST python -m forge.migrate                          # full 001→head inside the image
      docker run --rm --network canary -e DATABASE_URL=... $DIGEST \
        python -m forge.gate_smoke                               # gate passes on fresh DB (stub workload)
  - name: Attach tags to the validated digest
    run: |
      docker buildx imagetools create \
        -t ghcr.io/${{ github.repository }}:${GITHUB_REF_NAME#v} \
        -t ghcr.io/${{ github.repository }}:latest \
        ghcr.io/${{ github.repository }}@${{ steps.build.outputs.digest }}
  - uses: sigstore/cosign-installer@v4.1.0
  - name: Sign by digest (keyless)
    run: cosign sign --yes ghcr.io/${{ github.repository }}@${{ steps.build.outputs.digest }}
  - name: Report the pinning digest
    run: echo "digest: ${{ steps.build.outputs.digest }}" >> "$GITHUB_STEP_SUMMARY"
```

Minimal viable version if cosign lands later: keep build(smoke)-then-push but capture and
emit `steps.build.outputs.digest` and run the migrate smoke against the digest; add
`actions/attest-build-provenance` (subject-name `ghcr.io/${{ github.repository }}`,
subject-digest `sha256:<digest>`) when provenance matters to consumers. The one
non-negotiable from R30: **the smoke target is the digest, and the digest — not `latest` —
is what the job summary publishes for deployers to pin.**

---

## Open questions

1. **Legacy baseline**: do any deployed forge databases predate `alembic_version` (pre-007b)?
   If yes, the gate's refusal message needs a documented manual procedure (verify schema ==
   `011` DDL, then `alembic stamp 011`); if no deployments are in the wild, treat that state
   as fatal-with-no-remediation.
2. **Drop the `schema_version` table?** It becomes cosmetic after (a). Removing it needs a
   migration 012 (and a `downgrade`), which is harmless but touches the chain the tests in
   (c) parametrize over. Defer or do with the R22 fix?
3. **Auto-migrate on boot?** Recommended default is refuse-with-command (Django semantics);
   is an opt-in `FORGE_AUTO_MIGRATE=1` wanted for single-node dev deployments, or does that
   blur the bootstrap/upgrade distinction that caused R22's confusion in the first place?
4. **Downgrade policy**: are `downgrade()` implementations required and tested, or
   best-effort? Decides whether pytest-alembic's `test_up_down_consistency` /
   `downgrade_leaves_no_trace` run in CI or stay opt-in.
5. **Concurrent migration runners**: with multiple replicas starting simultaneously
   (K8s init-container pattern), `command.upgrade` can race — serialize with
   `pg_advisory_lock` in `env.py` or accept "one migrator per deployment" as an operational
   rule in `docs/operations/upgrade.md`?
6. **Cosign rollout**: signing requires `id-token: write` on the release job and a public
   repo for keyless (public-good Sigstore); forge releases currently depend on a PAT
   (`FORGE_RELEASE_TOKEN`). Keyless cosign + `actions/attest` vs `attest-build-provenance`
   wrapper — pick one before the first signed release so verification docs are stable.
7. **Gate placement**: app startup only, or also `forge doctor` and the worker entrypoints?
   Cheap to call everywhere via one function; decide which processes must hard-fail vs warn.
