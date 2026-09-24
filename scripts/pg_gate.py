#!/usr/bin/env python3
"""The required PostgreSQL qualification gate (R36-08 / AT-09, issue #267).

What this runner proves, and refuses to prove:

- The PG-gated production-entry (PE-4), checkpoint-repository and GC traces
  EXECUTED on this exact source tree against a real PostgreSQL, provisioned
  through the ALEMBIC MIGRATION CHAIN (never ``create_all`` — the gate's own
  schema comes from ``python -m forge.migrate``; ``create_all`` appears only
  inside tests that explicitly self-provision).
- Nothing skipped silently. A missing ``FORGE_PG_TEST_URL``, an unreachable
  fixture, a wrong schema revision or a removed test marker each REFUSE the
  gate with a distinct non-zero exit — a green run can never be built from
  skipped tests (AT-09).

Profiles (the required selection lives HERE, as data):

- ``production-entry`` — ``tests/production_entry/`` on its OWN disposable
  database. The suite's ``conftest`` resets the whole ``public`` schema per
  test when ``FORGE_PG_TEST_URL`` is set (drop-all + create_all, by design),
  so it MUST NOT share a database with the checkpoint traces below.
- ``checkpoint-lifecycle`` — ``tests/test_checkpoint_gc.py``,
  ``tests/test_checkpoint_repository.py`` and
  ``tests/test_checkpoint_retry_authority.py`` on a second, fully
  migration-provisioned database that no test resets underneath the others.

Skip accounting is honest, not blanket: a skip whose reason mentions
``FORGE_PG_TEST_URL`` is a REQUIRED skip (the qualification prerequisite
failed — refuse), a skip in the podman-bound retry-authority lab (its
disposable database is created via the local ``forge-postgres`` container,
which CI service containers cannot provide) is recorded as an
``environment`` skip — visible in the report and in ``-rs`` output, never
silent. Anything else that skips in the selection refuses too.

Usage:

    FORGE_PG_TEST_URL=postgresql+asyncpg://forge:forge@127.0.0.1:5432/forge \
        uv run python scripts/pg_gate.py --report pg-gate.json

``--mode use`` runs the selection against the database named in
``FORGE_PG_TEST_URL`` as-is (schema verified at head, NOT re-provisioned) —
the negative-arm mode for AT-09's "wrong schema revision / dropped table"
drills. Mind that the production-entry profile resets that database's
public schema; point it at a disposable database only.

Exit codes: 0 green · 2 prerequisite · 3 schema · 4 manifest ·
5 required skip · 6 test failures.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Bound every dial so a black-holed port refuses in seconds, not minutes.
_CONNECT_ARGS = {"timeout": 10}

# ---------------------------------------------------------------------------
# Typed refusals — each is a distinct, non-green outcome (never a skip)
# ---------------------------------------------------------------------------


class PgGateError(Exception):
    """Base class: the gate refuses to qualify."""

    exit_code = 2


class PrerequisiteError(PgGateError):
    """FORGE_PG_TEST_URL missing, or the fixture cannot be reached (AT-09)."""

    exit_code = 2


class SchemaError(PgGateError):
    """The database is not at the migration head / misses migration tables."""

    exit_code = 3


class ManifestError(PgGateError):
    """A required test id is absent from the collected manifest."""

    exit_code = 4


class RequiredSkipError(PgGateError):
    """A required (PG-gated) test skipped — qualification refuses."""

    exit_code = 5


class SelectionFailed(PgGateError):
    """The selection ran and produced failures."""

    exit_code = 6


# ---------------------------------------------------------------------------
# The required selection (data, not flags — R36-08 scope item 6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateProfile:
    """One coherent execution profile: a selection on its own database."""

    name: str
    selection: tuple[str, ...]
    database_suffix: str


PROFILES: tuple[GateProfile, ...] = (
    GateProfile(
        name="production-entry",
        selection=("tests/production_entry/",),
        database_suffix="pe",
    ),
    GateProfile(
        name="checkpoint-lifecycle",
        selection=(
            "tests/test_checkpoint_gc.py",
            "tests/test_checkpoint_repository.py",
            # NOTE(#267): the sibling-owned retry-authority suite is a full
            # member of the selection. Its AT-04 lab creates its disposable
            # database through the local `forge-postgres` podman container;
            # where that container does not exist (CI service containers) the
            # lab records an *environment* skip — visible, never silent.
            "tests/test_checkpoint_retry_authority.py",
        ),
        database_suffix="ck",
    ),
)


@dataclass(frozen=True)
class RequiredTrace:
    """A test identity the gate cannot qualify without.

    ``profile`` scopes the trace to the profile whose selection must
    collect it (each profile's manifest check demands its OWN traces —
    the PE-4 trace belongs to ``production-entry``, the checkpoint
    traces to ``checkpoint-lifecycle``).
    """

    label: str
    pattern: re.Pattern[str]
    profile: str


REQUIRED_TRACES: tuple[RequiredTrace, ...] = (
    RequiredTrace(
        label="PE-4 (AT-04) Postgres upload, restart, exact-spec resume",
        pattern=re.compile(
            r"tests/production_entry/test_production_entry\.py"
            r"::TestPE4PostgresUploadRestartResume::"
        ),
        profile="production-entry",
    ),
    RequiredTrace(
        label="GC first-upload quota barrier (real Postgres)",
        pattern=re.compile(
            r"tests/test_checkpoint_gc\.py::TestConcurrentFirstUploads"
            r"::test_concurrent_first_uploads_respect_the_quota_real_postgres$"
        ),
        profile="checkpoint-lifecycle",
    ),
    RequiredTrace(
        label="P04 post-final-scan GC barrier (real Postgres, two engines)",
        pattern=re.compile(r"tests/test_checkpoint_gc\.py::TestP04ScheduleRealPostgres::"),
        profile="checkpoint-lifecycle",
    ),
    RequiredTrace(
        label="Q35-03 checkpoint-repository authority (real Postgres)",
        pattern=re.compile(
            r"tests/test_checkpoint_repository\.py::TestPostgresAuthorityOverRealPostgres::"
        ),
        profile="checkpoint-lifecycle",
    ),
)

#: The podman-bound lab: executed wherever the `forge-postgres` container
#: exists (the local lab), an *environment* skip elsewhere (CI).
ENVIRONMENT_SCOPED_TESTS = re.compile(
    r"tests/test_checkpoint_retry_authority\.py::TestAT04PostgresRetryAuthority::"
)
_PODMAN_LAB_SKIP = re.compile(r"podman|disposable database cannot be created", re.IGNORECASE)
_PG_URL_SKIP = re.compile(r"FORGE_PG_TEST_URL", re.IGNORECASE)


def check_selection_on_disk() -> None:
    """The selection must reference real files — fail loudly otherwise."""
    missing = sorted(
        path
        for profile in PROFILES
        for path in profile.selection
        if not (REPO_ROOT / path).exists()
    )
    if missing:
        raise ManifestError(
            f"the required selection references missing files: {missing} — "
            "the gate refuses rather than run a partial selection"
        )


# ---------------------------------------------------------------------------
# Skip classification (pure — unit-tested in tests/test_pg_gate.py)
# ---------------------------------------------------------------------------


def classify_skip(test_id: str, reason: str) -> str:
    """Classify one skipped test: ``required`` (refuse) or ``environment``.

    - any skip whose reason names ``FORGE_PG_TEST_URL`` refuses: the gate
      exported the URL, so that skipif firing means the prerequisite broke
      (AT-09's "remove the URL" arm);
    - the podman-bound retry-authority lab skips as ``environment`` only when
      the skip is exactly about that container/disposable database;
    - anything else refuses too: a required gate must not skip silently.
    """
    if _PG_URL_SKIP.search(reason):
        return "required"
    if ENVIRONMENT_SCOPED_TESTS.search(test_id) and _PODMAN_LAB_SKIP.search(reason):
        return "environment"
    return "required"


# ---------------------------------------------------------------------------
# Output parsing (pure — unit-tested)
# ---------------------------------------------------------------------------


def parse_collected_manifest(collect_output: str) -> list[str]:
    """Node ids from ``pytest --collect-only -q`` output."""
    ids: list[str] = []
    for line in collect_output.splitlines():
        stripped = line.strip()
        if stripped and "::" in stripped and not stripped.startswith(("=", "-", " ")):
            ids.append(stripped)
    return ids


@dataclass
class TestRecord:
    """One executed test as the gate accounts for it."""

    test_id: str
    outcome: str  # passed | failed | error | skipped
    duration: float
    skip_reason: str | None = None


def junit_key(node_id: str) -> tuple[str, str]:
    """The ``(classname, name)`` junitxml pair a collected node id reports as."""
    parts = node_id.split("::")
    path = parts[0]
    name = parts[-1]
    module = (path[: -len(".py")] if path.endswith(".py") else path).replace("/", ".")
    classes = ".".join(parts[1:-1])
    return (f"{module}.{classes}" if classes else module, name)


def parse_junit(xml_text: str, manifest: list[str]) -> list[TestRecord]:
    """JUnit XML → records, keyed back to the collected manifest ids."""
    by_key: dict[tuple[str, str], str] = {}
    for node_id in manifest:
        by_key[junit_key(node_id)] = node_id

    records: list[TestRecord] = []
    for element in ET.fromstring(xml_text).iter("testcase"):
        classname = element.get("classname", "")
        name = element.get("name", "")
        test_id = by_key.get((classname, name), f"{classname}::{name}")
        children = list(element)
        skipped = next((child for child in children if child.tag == "skipped"), None)
        failed = any(child.tag in {"failure", "error"} for child in children)
        if skipped is not None:
            records.append(
                TestRecord(
                    test_id=test_id,
                    outcome="skipped",
                    duration=float(element.get("time", "0") or 0),
                    skip_reason=skipped.get("message", ""),
                )
            )
        else:
            records.append(
                TestRecord(
                    test_id=test_id,
                    outcome="failed" if failed else "passed",
                    duration=float(element.get("time", "0") or 0),
                )
            )
    return records


def missing_required(
    manifest: list[str], traces: tuple[RequiredTrace, ...] | None = None
) -> list[RequiredTrace]:
    """Required traces whose pattern matches NOTHING in the manifest.

    This is the marker-removal mutation detector: deleting or renaming any
    required test (or its marker) makes its id vanish from collection and
    the gate refuses instead of quietly running fewer tests.
    """
    checked = REQUIRED_TRACES if traces is None else traces
    return [trace for trace in checked if not any(trace.pattern.search(node) for node in manifest)]


def traces_for_profile(profile: GateProfile) -> tuple[RequiredTrace, ...]:
    return tuple(trace for trace in REQUIRED_TRACES if trace.profile == profile.name)


def required_test_ids(
    manifest: list[str], traces: tuple[RequiredTrace, ...] | None = None
) -> dict[str, list[str]]:
    """Required-trace label → the manifest ids it captured (insertion order)."""
    checked = REQUIRED_TRACES if traces is None else traces
    matched: dict[str, list[str]] = {}
    for trace in checked:
        matched[trace.label] = [node for node in manifest if trace.pattern.search(node)]
    return matched


# ---------------------------------------------------------------------------
# Schema state (pure decision + async introspection)
# ---------------------------------------------------------------------------


def check_schema_state(
    version: str | None, tables: set[str], expected_head: str, expected_tables: set[str]
) -> None:
    """Refuse unless the database carries the full migration-chain schema."""
    if version is None:
        raise SchemaError(
            "the database has no alembic_version — it was never provisioned "
            "through the migration chain (R36-08: migrations, not create_all)"
        )
    if version != expected_head:
        raise SchemaError(
            f"the database is at revision {version!r}, expected the head "
            f"{expected_head!r} — a wrong schema revision refuses qualification "
            "(AT-09), it never skips green"
        )
    missing = sorted(expected_tables - tables)
    if missing:
        raise SchemaError(
            f"the schema is missing migration tables {missing} — a broken "
            "schema refuses qualification (AT-09), it never skips green"
        )


def alembic_head() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    if not head:
        raise SchemaError("the alembic chain has no head revision")
    return head


def migration_tables() -> set[str]:
    """Tables the migration chain creates (parsed from the chain sources)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    tables: set[str] = set()
    for revision in script.walk_revisions():
        source = Path(revision.path).read_text(encoding="utf-8")
        tables |= set(re.findall(r'op\.create_table\(\s*["\']([^"\']+)["\']', source))
    return tables


# ---------------------------------------------------------------------------
# PostgreSQL plumbing
# ---------------------------------------------------------------------------


def mask_url(url: str) -> str:
    return re.sub(r"(://[^:/@]+:)[^@]+(@)", r"\1***\2", url)


async def ensure_prerequisite(admin_url: str) -> None:
    """AT-09: a missing or unreachable fixture refuses before anything runs."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(admin_url, connect_args=_CONNECT_ARGS)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001 — any dial failure is the same refusal
        raise PrerequisiteError(
            f"the PostgreSQL fixture cannot be reached at {mask_url(admin_url)}: {exc}"
        ) from exc
    finally:
        await engine.dispose()


async def create_gate_database(admin_url: str, database: str) -> None:
    """Create a DISPOSABLE database next to the admin one (drop-if-exists)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
            await connection.execute(text(f'CREATE DATABASE "{database}"'))
    finally:
        await engine.dispose()


async def drop_gate_database(admin_url: str, database: str) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
    finally:
        await engine.dispose()


async def read_schema_state(database_url: str) -> tuple[str | None, set[str]]:
    """(alembic revision, public tables) — refuses nothing, just reports."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url, connect_args=_CONNECT_ARGS)
    try:
        async with engine.connect() as connection:
            try:
                version = (
                    (await connection.execute(text("select version_num from alembic_version")))
                    .scalars()
                    .first()
                )
            except Exception:
                version = None
            tables = set(
                (
                    await connection.execute(
                        text("select tablename from pg_tables where schemaname = 'public'")
                    )
                ).scalars()
            )
        return version, tables
    finally:
        await engine.dispose()


async def verify_schema(database_url: str, expected_head: str, expected_tables: set[str]) -> None:
    version, tables = await read_schema_state(database_url)
    check_schema_state(version, tables, expected_head, expected_tables)


def run_migrations(database_url: str) -> None:
    """Provision through the SHIPPED migration entrypoint — never create_all."""
    process = subprocess.run(
        [sys.executable, "-m", "forge.migrate", "--database-url", database_url],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if process.returncode != 0:
        raise SchemaError(
            "alembic upgrade head failed on the gate database "
            f"({mask_url(database_url)}):\n{process.stdout}\n{process.stderr}"
        )


def rewire_database(url: str, database: str) -> str:
    """The same server/connection info, pointed at the gate's database."""
    from sqlalchemy.engine import make_url

    return make_url(url).set(database=database).render_as_string(hide_password=False)


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


@dataclass
class ProfileRun:
    profile: GateProfile
    database_url: str  # masked in the report
    manifest: list[str] = field(default_factory=list)
    records: list[TestRecord] = field(default_factory=list)
    required_matches: dict[str, list[str]] = field(default_factory=dict)
    skips: list[dict[str, str]] = field(default_factory=list)
    duration_seconds: float = 0.0
    pytest_returncode: int = 0


def _run_pytest(arguments: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *arguments],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def run_profile(
    profile: GateProfile, gate_url: str, junit_path: Path, base_env: dict[str, str]
) -> ProfileRun:
    run = ProfileRun(profile=profile, database_url=mask_url(gate_url))
    profile_traces = traces_for_profile(profile)
    env = {**base_env, "FORGE_PG_TEST_URL": gate_url}

    started = time.monotonic()
    collected = _run_pytest(["--collect-only", "-q", *profile.selection], env)
    run.manifest = parse_collected_manifest(collected.stdout)
    if collected.returncode != 0 or not run.manifest:
        raise ManifestError(
            f"profile {profile.name!r}: collection failed "
            f"(rc={collected.returncode}):\n{collected.stdout}\n{collected.stderr}"
        )
    absent = missing_required(run.manifest, profile_traces)
    if absent:
        labels = ", ".join(trace.label for trace in absent)
        raise ManifestError(
            f"profile {profile.name!r}: the collected manifest is MISSING required "
            f"traces: {labels} — a marker/id-removal mutation refuses the gate"
        )
    run.required_matches = required_test_ids(run.manifest, profile_traces)

    executed = _run_pytest(["-q", "-rs", f"--junitxml={junit_path}", *profile.selection], env)
    run.duration_seconds = round(time.monotonic() - started, 3)
    run.pytest_returncode = executed.returncode
    run.records = parse_junit(junit_path.read_text(encoding="utf-8"), run.manifest)

    # Every collected id must have an executed record on THIS run — a test
    # cannot inherit an executed record from another bundle or SHA.
    accounted = {record.test_id for record in run.records}
    unaccounted = [node for node in run.manifest if node not in accounted]
    if unaccounted:
        raise ManifestError(
            f"profile {profile.name!r}: collected tests with no executed record: "
            f"{unaccounted[:5]}{'…' if len(unaccounted) > 5 else ''}"
        )

    run.skips = [
        {
            "test_id": record.test_id,
            "reason": record.skip_reason or "",
            "classification": classify_skip(record.test_id, record.skip_reason or ""),
        }
        for record in run.records
        if record.outcome == "skipped"
    ]
    return run


def evaluate_profile(run: ProfileRun) -> None:
    """Skip accounting + required outcomes — refuse, never skip green."""
    required = {node for nodes in run.required_matches.values() for node in nodes}
    required_skips = [skip for skip in run.skips if skip["test_id"] in required] + [
        skip for skip in run.skips if skip["classification"] == "required"
    ]
    if required_skips:
        details = "; ".join(f"{skip['test_id']} ({skip['reason']})" for skip in required_skips[:5])
        raise RequiredSkipError(f"profile {run.profile.name!r}: REQUIRED tests skipped — {details}")
    failed_required = [
        record.test_id
        for record in run.records
        if record.test_id in required and record.outcome in {"failed", "error"}
    ]
    failed_any = [record.test_id for record in run.records if record.outcome in {"failed", "error"}]
    if failed_required or failed_any:
        raise SelectionFailed(
            f"profile {run.profile.name!r}: {len(failed_any)} test failure(s), "
            f"required among them: {failed_required[:5]}"
        )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def source_identity() -> dict[str, Any]:
    commit, dirty = None, False
    try:
        commit = (
            subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True
            ).stdout.strip()
            or None
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True
        ).stdout
        dirty = bool(status.strip())
    except OSError:
        pass
    return {"git_commit": commit, "tree_dirty": dirty}


def build_report(
    mode: str,
    admin_url_masked: str,
    head: str,
    runs: list[ProfileRun],
    started_iso: str,
    total_duration: float,
    refusal: PgGateError | None,
    databases: dict[str, Any],
) -> dict[str, Any]:
    required_ids = sorted(
        {
            node
            for run in runs
            for node in run.manifest
            if any(trace.pattern.search(node) for trace in REQUIRED_TRACES)
        }
    )
    executed_ids = [
        record.test_id for run in runs for record in run.records if record.outcome != "skipped"
    ]
    required_skips = [
        skip for run in runs for skip in run.skips if skip["classification"] == "required"
    ]
    environment_skips = [
        skip for run in runs for skip in run.skips if skip["classification"] == "environment"
    ]
    durations = {record.test_id: record.duration for run in runs for record in run.records}
    skipped_count = sum(1 for run in runs for record in run.records if record.outcome == "skipped")
    return {
        "gate": {
            "script": "scripts/pg_gate.py",
            "issue": "#267 (R36-08 / AT-09)",
            "mode": mode,
            "started_utc": started_iso,
            "duration_seconds": round(total_duration, 3),
            "admin_url": admin_url_masked,
            "alembic_head": head,
            "databases": databases,
            "source_identity": source_identity(),
        },
        "profiles": [
            {
                "name": run.profile.name,
                "selection": list(run.profile.selection),
                "database_url": run.database_url,
                "collected_test_ids": run.manifest,
                "required_matches": run.required_matches,
                "executed_test_ids": [r.test_id for r in run.records if r.outcome != "skipped"],
                "outcome_counts": {
                    "passed": sum(1 for r in run.records if r.outcome == "passed"),
                    "failed": sum(1 for r in run.records if r.outcome in {"failed", "error"}),
                    "skipped": sum(1 for r in run.records if r.outcome == "skipped"),
                },
                "skips": run.skips,
                "duration_seconds": run.duration_seconds,
                "pytest_returncode": run.pytest_returncode,
            }
            for run in runs
        ],
        "qualification": {
            # green / refused — a refusal never inherits green from skips
            "result": "refused" if refusal else "green",
            "refusal": None
            if not refusal
            else {
                "type": type(refusal).__name__,
                "detail": str(refusal),
                "exit_code": refusal.exit_code,
            },
            # The selection is entirely PG-integration on real PostgreSQL;
            # unit tests belong to the `test` job, real-subprocess OS drills
            # to `integration-os`, real-provider runs to the release canary.
            "execution_profile": "postgres-integration (service-container PostgreSQL)",
            "required_patterns": [
                {"label": trace.label, "pattern": trace.pattern.pattern}
                for trace in REQUIRED_TRACES
            ],
            "required_test_ids": required_ids,
            "executed_test_ids": executed_ids,
            "durations_seconds": durations,
            "required_skips": required_skips,  # MUST stay [] for green
            "environment_skips": environment_skips,
            "skip_accounting": {
                "collected": sum(len(run.manifest) for run in runs),
                "executed": len(executed_ids),
                "skipped": skipped_count,
                "required": len(required_skips),
                "environment": len(environment_skips),
            },
            "flake_attempts": 1,  # never retried until green
        },
        "ci": {
            "profile_duration_seconds": {run.profile.name: run.duration_seconds for run in runs},
            "total_duration_seconds": round(total_duration, 3),
            "runner": "github-actions" if os.environ.get("GITHUB_ACTIONS") else "local",
        },
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pg-gate", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="write the qualification evidence JSON here (the CI artifact)",
    )
    parser.add_argument(
        "--mode",
        choices=("create", "use"),
        default="create",
        help="create: fresh disposable databases via alembic (default); "
        "use: run against the database in FORGE_PG_TEST_URL as-is (schema "
        "verified at head, NOT re-provisioned — the AT-09 negative arms)",
    )
    parser.add_argument(
        "--database",
        default=None,
        help="pin the run suffix (gate databases become "
        "forge_pg_gate_<profile>_<suffix>; default: a unique id per run)",
    )
    parser.add_argument(
        "--keep-databases",
        action="store_true",
        help="do not drop the created gate databases (debugging)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    environ = dict(os.environ) if env is None else dict(env)
    started_iso = datetime.now(UTC).isoformat(timespec="seconds")
    started = time.monotonic()

    admin_url = environ.get("FORGE_PG_TEST_URL", "")
    refusal: PgGateError | None = None
    runs: list[ProfileRun] = []
    created: dict[str, str] = {}  # database name → admin url (for cleanup)
    databases_dropped = False
    head = ""
    try:
        if not admin_url:
            raise PrerequisiteError(
                "FORGE_PG_TEST_URL is not set — the required PostgreSQL "
                "qualification refuses to run (AT-09); it never skips green"
            )
        check_selection_on_disk()
        asyncio.run(ensure_prerequisite(admin_url))
        head = alembic_head()
        tables = migration_tables()

        suffix = args.database or uuid.uuid4().hex[:10]
        profile_urls: dict[str, str] = {}
        if args.mode == "create":
            for profile in PROFILES:
                database = f"forge_pg_gate_{profile.database_suffix}_{suffix}"
                asyncio.run(create_gate_database(admin_url, database))
                created[database] = admin_url
                url = rewire_database(admin_url, database)
                run_migrations(url)
                asyncio.run(verify_schema(url, head, tables))
                profile_urls[profile.name] = url
        else:
            # use: the given database as-is, verified at head BEFORE any test
            # runs — the wrong-revision / dropped-table arms refuse here.
            asyncio.run(verify_schema(admin_url, head, tables))
            profile_urls = {profile.name: admin_url for profile in PROFILES}

        junit_dir = Path(tempfile.mkdtemp(prefix="pg-gate-"))
        try:
            for profile in PROFILES:
                run = run_profile(
                    profile,
                    profile_urls[profile.name],
                    junit_dir / f"{profile.name}.xml",
                    environ,
                )
                runs.append(run)
                evaluate_profile(run)
        finally:
            shutil.rmtree(junit_dir, ignore_errors=True)
    except PgGateError as exc:
        refusal = exc
        print(f"pg-gate: REFUSED ({type(exc).__name__}) — {exc}", file=sys.stderr)
    finally:
        if args.mode == "create" and not args.keep_databases:
            for database, admin in created.items():
                try:
                    asyncio.run(drop_gate_database(admin, database))
                except Exception as exc:  # noqa: BLE001 — cleanup must not mask
                    print(f"pg-gate: WARNING: could not drop {database}: {exc}", file=sys.stderr)
            databases_dropped = True

    total = time.monotonic() - started
    databases = {
        "created": sorted(created),
        "dropped": databases_dropped,
        "provisioned_via": "alembic upgrade head (python -m forge.migrate)"
        if args.mode == "create"
        else "pre-existing (verified at head, not re-provisioned)",
    }
    report = build_report(
        args.mode, mask_url(admin_url), head, runs, started_iso, total, refusal, databases
    )
    if args.report is not None:
        args.report.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    if refusal is not None:
        return refusal.exit_code

    required_ids = report["qualification"]["required_test_ids"]
    executed = len(report["qualification"]["executed_test_ids"])
    print(
        f"pg-gate: GREEN — {executed} tests executed across "
        f"{len(PROFILES)} profiles, {len(required_ids)} required traces "
        "present, 0 required skips"
    )
    if args.report is not None:
        print(f"pg-gate: qualification evidence written to {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
