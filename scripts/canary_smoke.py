"""Release-artifact canary (R30): smoke the IMAGE, not the source tree.

Drives a container runtime (podman locally, docker in CI) against a
digest-pinned image ref and asserts the supported recipes end to end, with
NO source mount anywhere — every command runs inside the artifact:

  fresh   the full release recipe on a disposable Postgres:
            migrate (the real alembic chain, never create_all — R22)
            → boot gate (init_db passes at head, DB revision == script head)
            → /health answers ok with the release version
            → MCP mount fails closed (401 without a Bearer token)
            → forge doctor passes every check the canary env can satisfy
              (database, redis, litellm; GitLab checks are out of scope —
              there is no GitLab fixture in CI)
  migrate  the previous-release → this-release upgrade (the R22 scenario):
            previous image migrates a fresh DB, this image's boot gate must
            REFUSE the stale schema, then migrate prev→head and pass the
            gate. Skipped (with a notice) when the previous image is not
            pullable — e.g. the first release — or predates the migrate
            entrypoint; the refusal assertion is waived when the previous
            chain head equals this one (re-runs, no schema delta).
            With --seed-real-data the previous-schema DB is seeded with
            REAL-shaped rows (a run with its frozen spec, bounded steps,
            a control command, a publication intent — see _SEED_SQL) and
            the upgrade must preserve them: row counts plus a sha256
            fingerprint per table, compared across the upgrade. Schema
            survival alone is no longer enough (R32-19).

--results-json PATH writes the machine-readable stage outcomes — each row
tagged with the manifest capability slug it qualifies, at collection time —
for forge.release_promotion's gate and gaps query. pass/skip/fail only: a
self-skipped stage is recorded as a skip and is never counted as a pass.

No LLM keys are needed: LiteLLM reachability is the boundary, satisfied by
a stub HTTP server that answers 200 on GET /health — served by the image
itself (python -m http.server on a one-file directory), so the stub is one
more proof the artifact is self-sufficient. No model call is made.

Usage:
    python scripts/canary_smoke.py ghcr.io/forcewake/forge@sha256:... \
        --expected-version 0.9.0 \
        --previous-image ghcr.io/forcewake/forge:latest

    # local build (tag instead of digest — requires the flag):
    python scripts/canary_smoke.py localhost/forge:canary --allow-unpinned

Options --stages fresh,migrate select the stages; the default is both,
with the migrate stage self-skipping when no previous image resolves.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

APP_PORT = 8420  # the port the image exposes (Containerfile CMD)
HOST_PORT = 18420  # host-side publish target (avoids clashing with a local forge)
PG = "postgres:17-alpine"
REDIS = "redis:7-alpine"
NET = "forge-canary"
PG_NAME = "forge-canary-pg"
REDIS_NAME = "forge-canary-redis"
STUB_NAME = "forge-canary-litellm-stub"
APP_NAME = "forge-canary-app"
PREV_DB = "canary_previous_schema"
BASE_ENV = {
    # Settings() requires these; the canary never calls GitLab. Doctor's
    # GitLab checks are expected to FAIL here and are scoped out explicitly.
    "GITLAB_URL": "http://forge-canary-gitlab.invalid",
    "GITLAB_TOKEN": "canary-not-a-token",
    "GITLAB_WEBHOOK_SECRET": "canary-not-a-secret",
    "FORGE_MCP_KEY": "canary-mcp-key",
    "LOG_LEVEL": "WARNING",
}
#: doctor checks that may FAIL in the canary env (no GitLab fixture exists).
GITLAB_BOUND_CHECKS = frozenset({"gitlab.token", "forge.bot_token"})
#: doctor checks that must PASS outright (the env satisfies them fully).
MUST_PASS_CHECKS = frozenset({"python.version", "database", "redis", "litellm"})

# In-container gate probe: the R22 boot gate (init_db) plus a script-vs-DB
# head comparison. mode=check → must pass; mode=refuse → must raise.
_GATE_PROBE = """\
import asyncio, os, sys

from alembic import migration
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import create_async_engine

from forge.database import init_db
from forge.migrate import build_alembic_config


async def main() -> int:
    url = os.environ["DATABASE_URL"]
    heads = sorted(set(ScriptDirectory.from_config(build_alembic_config()).get_heads()))
    try:
        await init_db(url)
    except RuntimeError as exc:
        if sys.argv[1] == "refuse":
            print(f"CANARY gate refused as required: {exc}".splitlines()[0][:300])
            return 0
        print(f"gate refused unexpectedly: {exc}", file=sys.stderr)
        return 1
    if sys.argv[1] == "refuse":
        print("gate did NOT refuse a stale schema", file=sys.stderr)
        return 1
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            db_heads = sorted(
                await conn.run_sync(
                    lambda c: migration.MigrationContext.configure(c).get_current_heads()
                )
            )
    finally:
        await engine.dispose()
    assert db_heads == heads, f"DB at {db_heads} != script head {heads}"
    print(f"CANARY gate ok db={db_heads}")
    return 0


sys.exit(asyncio.run(main()))
"""


class CanaryError(RuntimeError):
    """A canary stage failed — the artifact did not pass a supported recipe."""


class Canary:
    """Thin podman/docker driver with a network, a Postgres, and helpers."""

    STACK = (PG_NAME, REDIS_NAME, STUB_NAME, APP_NAME)

    def __init__(self, runtime: str) -> None:
        self.runtime = runtime
        self.containers: list[str] = []
        self._pg_up = False

    def run(
        self,
        *args: str,
        input: str | None = None,
        timeout: int = 300,
        ok_exit: frozenset[int] = frozenset({0}),
    ) -> str:
        proc = subprocess.run(
            [self.runtime, *args], input=input, capture_output=True, text=True, timeout=timeout
        )
        if proc.returncode not in ok_exit:
            raise CanaryError(
                f"`{' '.join([self.runtime, *args])}` failed "
                f"(exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
            )
        return proc.stdout

    def spawn(self, *args: str) -> None:
        proc = subprocess.run([self.runtime, *args], capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            raise CanaryError(
                f"`{' '.join([self.runtime, *args])}` failed:\n{proc.stdout}\n{proc.stderr}"
            )

    def try_pull(self, image: str) -> bool:
        """Resolve *image* — local storage first, then the registry."""
        local = subprocess.run(
            [self.runtime, "image", "inspect", image], capture_output=True, text=True, timeout=60
        )
        if local.returncode == 0:
            return True
        proc = subprocess.run(
            [self.runtime, "pull", "-q", image], capture_output=True, text=True, timeout=600
        )
        return proc.returncode == 0

    def start(self, name: str, *args: str) -> None:
        """Run a long-lived canary stack container DETACHED (--rm on stop)."""
        self.spawn("run", "-d", "--rm", "--name", name, *args)
        self.containers.append(name)

    def network_up(self) -> None:
        # Both stages share one stack: drop anything a previous crashed
        # canary left behind, then create the network fresh.
        for name in self.STACK:
            subprocess.run(
                [self.runtime, "rm", "-f", "-v", name], capture_output=True, text=True, timeout=60
            )
        subprocess.run(
            [self.runtime, "network", "rm", NET], capture_output=True, text=True, timeout=60
        )
        self.spawn("network", "create", NET)

    def pg_up(self) -> None:
        if self._pg_up:
            return
        self._pg_up = True
        self.start(
            PG_NAME,
            "--network",
            NET,
            "-e",
            "POSTGRES_USER=forge",
            "-e",
            "POSTGRES_PASSWORD=forge",
            "-e",
            "POSTGRES_DB=forge",
            PG,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            proc = subprocess.run(
                [
                    self.runtime,
                    "exec",
                    PG_NAME,
                    "pg_isready",
                    "-U",
                    "forge",
                    "-d",
                    "forge",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0:
                return
            time.sleep(1)
        raise CanaryError("postgres did not become ready within 60s")

    def stub_up(self, image: str) -> None:
        """The LiteLLM boundary, stubbed BY THE IMAGE ITSELF: a static file
        server answering 200 on GET /health. Reachability only — no model."""
        self.start(
            STUB_NAME,
            "--network",
            NET,
            image,
            "sh",
            "-c",
            "mkdir -p /tmp/stub && : > /tmp/stub/health "
            "&& exec python -m http.server 4000 -d /tmp/stub",
        )

    def redis_up(self) -> None:
        self.start(REDIS_NAME, "--network", NET, REDIS)

    def in_container(
        self,
        image: str,
        *cmd: str,
        env: dict[str, str] | None = None,
        input: str | None = None,
        timeout: int = 300,
        ok_exit: frozenset[int] = frozenset({0}),
    ) -> str:
        args = ["run", "--rm", "-i", "--network", NET]
        for key, value in (env or BASE_ENV).items():
            args += ["-e", f"{key}={value}"]
        return self.run(*args, image, *cmd, input=input, timeout=timeout, ok_exit=ok_exit)

    def down(self) -> None:
        for name in reversed(self.containers):
            subprocess.run(
                [self.runtime, "rm", "-f", "-v", name], capture_output=True, text=True, timeout=60
            )
        self.containers = []
        subprocess.run(
            [self.runtime, "network", "rm", NET], capture_output=True, text=True, timeout=60
        )


def last_line(text: str) -> str:
    """The last non-empty stdout line (the probe's CANARY verdict)."""
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else "<no output>"


# ---------------------------------------------------------------------------
# Machine-readable stage outcomes (R32-19): every stage is tagged with the
# manifest capability slug it qualifies AT COLLECTION TIME, so the promotion
# gate and the gaps query join canary evidence onto capability claims
# without re-deriving anything at promotion time.
# ---------------------------------------------------------------------------

#: stage -> the manifest capability slug its evidence qualifies.
STAGE_CAPABILITIES = {
    "fresh": "release-artifact-canary",
    "migrate": "release-canary/previous-release-upgrade",
    "seed-real-data": "release-canary/previous-release-upgrade",
}


class Recorder:
    """Accumulates stage outcomes and flushes them to --results-json.

    Written after EVERY add (a failed run must leave its fail row behind —
    the file is evidence, not a success trophy).
    """

    def __init__(self, image: str, path: str | None) -> None:
        self.image = image
        self.path = path
        self.stages: list[dict[str, str]] = []

    def add(self, stage: str, outcome: str, detail: str = "") -> None:
        self.stages.append(
            {
                "stage": stage,
                "capability": STAGE_CAPABILITIES.get(stage, stage),
                "outcome": outcome,
                "detail": detail[:400],
            }
        )
        self.flush()

    def flush(self) -> None:
        if self.path:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump({"image": self.image, "stages": self.stages}, fh, indent=2)

    def note_failure(self, stage: str, detail: str) -> None:
        """Record a fail row for *stage* unless it already has a verdict."""
        if not any(row["stage"] == stage for row in self.stages):
            self.add(stage, "fail", detail)


# ---------------------------------------------------------------------------
# --seed-real-data: schema-accurate direct seeding at the N-1 head.
#
# Honest limitation (documented in docs/releases/README.md): the rows are
# inserted with raw SQL against the schema the PREVIOUS image just created
# (its own migration chain, at its own head) — real-shaped, but not produced
# by the previous app's business logic. Driving the previous image's app to
# mint rows would need a provider fixture the canary deliberately lacks.
# Tables that do not exist at the previous head (e.g. checkpoint_metadata,
# new in 026) cannot be seeded and are simply absent from the set.
# ---------------------------------------------------------------------------

_SEED_SQL = """\
-- One run mid-flight with its frozen spec, bounded steps, one control
-- command and one publication intent: the durable shape a real deployment
-- carries across an upgrade.
INSERT INTO flow_runs (id, project_id, issue_iid, provider, status, base_sha,
                       plan_digest, spec_digest, config_digest, commit_cycle,
                       created_at, updated_at)
VALUES ('seedrun0000000000000000000001', 4242, 7, 'gitlab', 'waiting_approval',
        '0123456789abcdef0123456789abcdef01234567',
        'aa62d845f279c1f7a3ea5f6df4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3',
        'bb73e956f380d2e8b4fb6a7ea5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4',
        'cc84fa67e491e3f9c5ac7b8fb6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5',
        1, now(), now());

INSERT INTO run_specs (id, run_id, schema_version, document, digest, created_at)
VALUES ('seedspec0000000000000000000001', 'seedrun0000000000000000000001', 3,
        '{"task_digest": "deadbeef", "frozen": true}'::jsonb,
        'bb73e956f380d2e8b4fb6a7ea5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4',
        now());

INSERT INTO step_runs (flow_run_id, step_name, status, attempt, max_attempts,
                       payload, started_at)
VALUES ('seedrun0000000000000000000001', 'plan', 'succeeded', 0, 3,
        '{"checkpoint": "plan-v1"}'::jsonb, now()),
       ('seedrun0000000000000000000001', 'writer.apply', 'scheduled', 0, 3,
        '{"checkpoint": "apply-v1"}'::jsonb, now());

INSERT INTO control_commands (id, work_id, run_id, kind, payload, status,
                              sequence, dedup_key, actor_ref, actor_origin,
                              journal)
VALUES ('seedcmd000000000000000000000001', 'seed-work-1',
        'seedrun0000000000000000000001', 'pause',
        '{"reason": "human review"}'::jsonb, 'received', 1, 'delivery:9001',
        'user:42', 'server_authenticated_human', '[]'::jsonb);

INSERT INTO publication_intents (id, run_id, provider, repo, operation,
                                 target_ref, idempotency_scope, operation_key,
                                 commit_cycle, content_digest, status,
                                 created_at, updated_at)
VALUES ('seedintent0000000000000000000001', 'seedrun0000000000000000000001',
        'gitlab', '4242', 'commit', 'forge/runs/seedrun1', 'cycle-1',
        'forge-op:seed-key-1', 1,
        'dd95ab78f502f4abadbd8c9ac7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6',
        'requested', now(), now());
"""

#: (table, identity-expression) for the preservation fingerprint: the sha256
#: over the ordered per-row identity strings — upgrade must keep it EXACT.
_SEED_FINGERPRINTS: tuple[tuple[str, str], ...] = (
    (
        "flow_runs",
        "id || '|' || status || '|' || coalesce(plan_digest, '') "
        "|| '|' || coalesce(spec_digest, '')",
    ),
    ("run_specs", "id || '|' || run_id || '|' || digest || '|' || schema_version"),
    (
        "step_runs",
        "flow_run_id || '|' || step_name || '|' || status || '|' || attempt",
    ),
    (
        "control_commands",
        "id || '|' || work_id || '|' || kind || '|' || status || '|' || sequence",
    ),
    (
        "publication_intents",
        "id || '|' || run_id || '|' || status || '|' || operation_key",
    ),
)


def _psql(canary: Canary, sql: str, database: str = PREV_DB) -> str:
    # ``-i`` is load-bearing: without it ``docker exec`` drops the piped
    # stdin, psql reads an EMPTY script, runs nothing, and exits 0 — the
    # canary then parses an empty string as a row count (the v0.34.0 CI bite).
    return canary.run(
        "exec",
        "-i",
        PG_NAME,
        "psql",
        "-U",
        "forge",
        "-d",
        database,
        "-v",
        "ON_ERROR_STOP=1",
        "-tA",
        input=sql,
    )


def _psql_count(canary: Canary, table: str) -> int:
    out = _psql(canary, f"SELECT count(*) FROM {table};").strip()
    if not out:
        raise CanaryError(
            f"count(*) for {table!r} returned no output — did the SQL reach "
            "psql (docker exec -i) and does the table exist at this schema?"
        )
    return int(out)


def seed_real_data_fingerprint(canary: Canary) -> str:
    """Seed the previous-schema DB and return its preservation fingerprint."""
    _psql(canary, _SEED_SQL)
    rows = []
    for table, expression in _SEED_FINGERPRINTS:
        count = _psql_count(canary, table)
        digest = _psql(
            canary,
            "SELECT encode(sha256(convert_to("
            "coalesce(string_agg(x, E'|' ORDER BY x), ''), 'UTF8')), 'hex') "
            f"FROM (SELECT {expression} AS x FROM {table}) s;",
        ).strip()
        rows.append(f"{table} {count} {digest}")
    fingerprint = "\n".join(rows)
    print(
        "migrate/seed: seeded real-shaped rows into the previous-schema DB "
        f"({len(_SEED_FINGERPRINTS)} tables); fingerprint recorded"
    )
    for row in rows:
        print(f"  seed: {row}")
    return fingerprint


def verify_seeded_data(canary: Canary, seeded_fingerprint: str) -> None:
    """The upgrade preserved every seeded row: counts AND digests equal."""
    mismatches = []
    for table, expression in _SEED_FINGERPRINTS:
        count = _psql_count(canary, table)
        digest = _psql(
            canary,
            "SELECT encode(sha256(convert_to("
            "coalesce(string_agg(x, E'|' ORDER BY x), ''), 'UTF8')), 'hex') "
            f"FROM (SELECT {expression} AS x FROM {table}) s;",
        ).strip()
        expected = f"{table} {count} {digest}"
        if expected not in seeded_fingerprint.splitlines():
            mismatches.append(f"{table}: now {expected}")
    if mismatches:
        seeded = "\n".join(f"  was: {line}" for line in seeded_fingerprint.splitlines())
        now = "\n".join(f"  {m}" for m in mismatches)
        raise CanaryError(f"the upgrade did NOT preserve the seeded rows:\n{seeded}\n{now}")
    print(
        "migrate/seed: upgrade preserved every seeded row "
        f"({len(_SEED_FINGERPRINTS)} tables — counts and sha256 fingerprints equal)"
    )


def http_probe(url: str, timeout: int = 10) -> tuple[int, dict[str, object] | None]:
    request = urllib.request.Request(url)  # noqa: S310 — fixed http://localhost target
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            body = resp.read().decode()
            try:
                return resp.status, json.loads(body)  # type: ignore[no-any-return]
            except json.JSONDecodeError:
                return resp.status, None
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return 0, None  # connection-level failure: app not up yet / port closed


def wait_for_health(canary: Canary, port: int, timeout: int = 120) -> dict[str, object]:
    """Poll the published /health until the app answers 200."""
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + timeout
    last = ""
    tail: list[str] = []
    while time.monotonic() < deadline:
        status, body = http_probe(url)
        if status == 200 and body is not None:
            return body
        last = f"HTTP {status}"
        logs = subprocess.run(
            [canary.runtime, "logs", APP_NAME], capture_output=True, text=True, timeout=30
        )
        tail = (logs.stdout + logs.stderr).strip().splitlines()[-3:]
        time.sleep(2)
    raise CanaryError(f"app never answered 200 on {url} (last: {last})\nlogs tail: {tail}")


def stage_fresh(canary: Canary, image: str, expected_version: str | None, port: int) -> None:
    """The full release recipe against a fresh database."""
    canary.pg_up()
    canary.stub_up(image)
    canary.redis_up()

    db_url = f"postgresql+asyncpg://forge:forge@{PG_NAME}:5432/forge"

    # 1. R22: the real migration chain runs inside the image (no create_all).
    canary.in_container(
        image, "python", "-m", "forge.migrate", env=BASE_ENV | {"DATABASE_URL": db_url}
    )
    print("fresh: migrate ok (alembic chain applied inside the image)")

    # 2. The boot gate passes at head, and DB revision == script head.
    out = canary.in_container(
        image, "python", "-", "check", env=BASE_ENV | {"DATABASE_URL": db_url}, input=_GATE_PROBE
    )
    print(f"fresh: {last_line(out)}")

    # 3. Boot the app as a deployment would (the image CMD, no source mount).
    stack_env = BASE_ENV | {
        "DATABASE_URL": db_url,
        "LITELLM_URL": f"http://{STUB_NAME}:4000",
        "REDIS_URL": f"redis://{REDIS_NAME}:6379/0",
    }
    app_args = ["run", "-d", "--name", APP_NAME, "--network", NET]
    for key, value in stack_env.items():
        app_args += ["-e", f"{key}={value}"]
    canary.spawn(*app_args, "-p", f"127.0.0.1:{port}:{APP_PORT}", image)
    canary.containers.append(APP_NAME)
    health = wait_for_health(canary, port)
    if health.get("status") != "ok":
        raise CanaryError(f"/health is not ok: {health}")
    for component in ("database", "redis", "litellm"):
        if health.get(component) != "ok":
            raise CanaryError(f"/health reports {component}={health.get(component)!r}: {health}")
    version = health.get("version")
    if expected_version is not None and version != expected_version:
        raise CanaryError(f"/health version {version!r} != release tag {expected_version!r}")
    if not version:
        raise CanaryError(f"/health reports no version: {health}")
    print(f"fresh: /health ok (version {version!r}, db/redis/litellm all ok)")

    # 4. MCP mount fails closed: 401 without a Bearer token.
    status, _ = http_probe(f"http://127.0.0.1:{port}/mcp")
    if status != 401:
        raise CanaryError(f"unauthenticated /mcp answered HTTP {status}, expected 401")
    print("fresh: /mcp fails closed (401 unauthenticated)")

    # 5. Doctor on the in-container env (the app's own env: db, redis and
    #    the LiteLLM stub are all reachable). GitLab checks are out of scope
    #    (no GitLab fixture); every other check must pass.
    report = canary.in_container(
        image,
        "python",
        "-m",
        "forge.doctor",
        "--json",
        env=stack_env,
        # doctor exits 1 when any check fails — here that is the EXPECTED
        # GitLab-scope failure (no GitLab fixture); anything else the JSON
        # triage below rejects.
        ok_exit=frozenset({0, 1}),
    )
    checks = json.loads(report)["checks"]
    failures = [c for c in checks if c["status"] == "fail"]
    unexpected = [c for c in failures if c["name"] not in GITLAB_BOUND_CHECKS]
    if unexpected:
        raise CanaryError(f"doctor failed checks it must pass: {unexpected}")
    passed = {c["name"] for c in checks if c["status"] == "pass"}
    missing = MUST_PASS_CHECKS - passed
    if missing:
        raise CanaryError(f"doctor did not pass required checks: {sorted(missing)}")
    print(
        "fresh: doctor — "
        f"{len(passed)} passed; out-of-scope failures (no GitLab fixture): "
        f"{sorted(c['name'] for c in failures)}"
    )


# Reads the shipped alembic.ini directly — stable across releases (do not
# depend on forge.migrate internals, which changed between 0.9 and 0.10).
_HEAD_PROBE = (
    "from pathlib import Path;"
    "from alembic.config import Config;"
    "from alembic.script import ScriptDirectory;"
    "ini = Path('/app/alembic.ini');"
    "cfg = Config(str(ini));"
    "cfg.set_main_option('script_location', str(ini.parent / 'alembic'));"
    "print(','.join(ScriptDirectory.from_config(cfg).get_heads()))"
)


def _script_head(canary: Canary, image: str) -> str | None:
    """The migration head shipped INSIDE an image (no source mount)."""
    try:
        return canary.in_container(image, "python", "-c", _HEAD_PROBE).strip()
    except CanaryError:
        return None


def stage_migrate(
    canary: Canary,
    image: str,
    previous_image: str,
    seed_real_data: bool = False,
    recorder: Recorder | None = None,
) -> bool:
    """prev-release schema → this release: refuse stale, upgrade, boot-gate ok."""
    if not canary.try_pull(previous_image):
        print(f"migrate: SKIP — previous image {previous_image} not pullable (first release?)")
        if recorder:
            recorder.add("migrate", "skip", f"previous image {previous_image} not pullable")
            if seed_real_data:
                recorder.add("seed-real-data", "skip", "no previous schema to seed")
        return False
    prev_head = _script_head(canary, previous_image)
    if prev_head is None:
        print(
            f"migrate: SKIP — previous image {previous_image} predates python -m forge.migrate; "
            "the mechanical upgrade path starts at the first release that ships migrations"
        )
        if recorder:
            recorder.add("migrate", "skip", "previous image predates the migrate entrypoint")
            if seed_real_data:
                recorder.add("seed-real-data", "skip", "no previous schema to seed")
        return False
    new_head = _script_head(canary, image)
    chain_is_new = prev_head != new_head

    canary.pg_up()
    prev_db_url = f"postgresql+asyncpg://forge:forge@{PG_NAME}:5432/{PREV_DB}"

    canary.run(
        "exec", PG_NAME, "psql", "-U", "forge", "-d", "forge", "-c", f"CREATE DATABASE {PREV_DB}"
    )

    # 1. The previous release migrates its own fresh DB (schema at prev head).
    canary.in_container(
        previous_image,
        "python",
        "-m",
        "forge.migrate",
        env=BASE_ENV | {"DATABASE_URL": prev_db_url},
    )
    print(
        f"migrate: previous image {previous_image} applied its chain (head {prev_head}) to {PREV_DB}"
    )

    # 1b. Seed the previous-schema DB with real-shaped rows and fingerprint
    #     them, so the upgrade must preserve DATA, not just schema version.
    seeded_fingerprint = ""
    if seed_real_data:
        try:
            seeded_fingerprint = seed_real_data_fingerprint(canary)
            if recorder:
                recorder.add("seed-real-data", "pass", f"seeded at previous head {prev_head}")
        except CanaryError as exc:
            if recorder:
                recorder.note_failure("seed-real-data", str(exc))
            raise

    if chain_is_new:
        # 2. The R22 gate must REFUSE this release's boot against the stale schema.
        out = canary.in_container(
            image,
            "python",
            "-",
            "refuse",
            env=BASE_ENV | {"DATABASE_URL": prev_db_url},
            input=_GATE_PROBE,
        )
        print(f"migrate: {last_line(out)}")
    else:
        print(
            "migrate: previous chain head == this head (re-run or no schema delta) — "
            "refusal assertion not applicable"
        )

    # 3. This release migrates prev→head (the delta only — never create_all).
    canary.in_container(
        image, "python", "-m", "forge.migrate", env=BASE_ENV | {"DATABASE_URL": prev_db_url}
    )
    print("migrate: this release upgraded the previous schema to head")

    # 3b. The seeded rows survived the upgrade: counts and digests equal.
    if seed_real_data:
        try:
            verify_seeded_data(canary, seeded_fingerprint)
            if recorder:
                recorder.add("seed-real-data", "pass", "upgrade preserved counts + digests")
        except CanaryError as exc:
            if recorder:
                recorder.note_failure("seed-real-data", str(exc))
            raise

    # 4. The boot gate now passes (the upgrade path a real deployment takes).
    out = canary.in_container(
        image,
        "python",
        "-",
        "check",
        env=BASE_ENV | {"DATABASE_URL": prev_db_url},
        input=_GATE_PROBE,
    )
    print(f"migrate: {last_line(out)}")
    if recorder:
        recorder.add("migrate", "pass", f"prev head {prev_head} -> {new_head}; gate ok")
    return True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("image", help="image ref to canary (digest-pinned: name@sha256:...)")
    parser.add_argument(
        "--expected-version",
        default=None,
        help="version /health must report (the release tag without 'v'); "
        "default: only assert a version is reported",
    )
    parser.add_argument(
        "--previous-image",
        default=None,
        help="previous release image for the schema-upgrade stage",
    )
    parser.add_argument(
        "--stages",
        default="fresh,migrate",
        help="comma-separated subset of: fresh,migrate (default: both)",
    )
    parser.add_argument(
        "--runtime",
        default=None,
        help="container runtime binary (default: podman, then docker)",
    )
    parser.add_argument(
        "--allow-unpinned",
        action="store_true",
        help="accept a tag ref instead of a digest (local builds only)",
    )
    parser.add_argument(
        "--seed-real-data",
        action="store_true",
        help="migrate stage: seed the previous-schema DB with real-shaped rows "
        "(run + spec + steps + control command + publication intent) and assert "
        "the upgrade preserves them (counts + sha256 fingerprints)",
    )
    parser.add_argument(
        "--results-json",
        default=None,
        help="write machine-readable stage outcomes here (capability-tagged, "
        "pass/skip/fail) for the promotion gate",
    )
    parser.add_argument("--port", type=int, default=HOST_PORT, help="host port for the app")
    parser.add_argument("--keep", action="store_true", help="leave the canary stack running")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if "@sha256:" not in args.image and not args.allow_unpinned:
        print(
            f"canary: {args.image} is not digest-pinned — pass the ref as "
            "name@sha256:... (R30: the smoke target is the digest, never a "
            "movable tag) or use --allow-unpinned for a local build",
            file=sys.stderr,
        )
        return 2
    runtime = args.runtime or shutil.which("podman") or shutil.which("docker")
    if runtime is None:
        print("canary: no container runtime found (podman or docker)", file=sys.stderr)
        return 2
    stages = [stage.strip() for stage in args.stages.split(",") if stage.strip()]
    unknown = set(stages) - {"fresh", "migrate"}
    if unknown:
        print(f"canary: unknown stages {sorted(unknown)}", file=sys.stderr)
        return 2
    if args.seed_real_data and "migrate" not in stages:
        print("canary: --seed-real-data needs the migrate stage", file=sys.stderr)
        return 2

    canary = Canary(runtime)
    recorder = Recorder(args.image, args.results_json)
    print(f"canary: image={args.image} runtime={runtime} stages={stages}")
    shipped: list[str] = []
    current = "fresh"
    try:
        canary.network_up()
        if "fresh" in stages:
            stage_fresh(canary, args.image, args.expected_version, args.port)
            recorder.add("fresh", "pass", "migrate, boot gate, /health, /mcp 401, doctor")
            shipped.append("fresh")
        if "migrate" in stages:
            current = "migrate"
            if not args.previous_image:
                print("migrate: SKIP — no --previous-image given (nightly follow-up: pass it)")
                recorder.add("migrate", "skip", "no --previous-image given")
            elif stage_migrate(
                canary,
                args.image,
                args.previous_image,
                seed_real_data=args.seed_real_data,
                recorder=recorder,
            ):
                shipped.append("migrate(previous→head)")
    except CanaryError as exc:
        recorder.note_failure(current, str(exc))
        print(f"canary: FAIL\n{exc}", file=sys.stderr)
        return 1
    finally:
        recorder.flush()
        if not args.keep:
            canary.down()
        else:
            print(f"canary: keeping stack ({canary.containers}); network {NET}")

    print(f"canary: PASS — stages shipped: {', '.join(shipped)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
