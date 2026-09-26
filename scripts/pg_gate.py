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
- ``accounting-races`` (Q39-08 / #327) — the new accounting contracts on
  REQUIRED PostgreSQL, tracked by executed critical ID, not by selected
  file alone: ``tests/test_credential_audit.py`` (the #322 CAS
  projection: concurrent redemption + evidence writers must BOTH survive)
  and ``tests/test_usage_ingestion.py`` (the #324 partial→final
  reconcile under real isolation) on a third disposable database. Their
  PG-gated arms read ``FORGE_PG_TEST_URL`` directly — a missing
  prerequisite is a REQUIRED skip and REFUSES the gate (never a green
  skip); the files' sqlite unit arms run beside them on the same
  selection, so the executed-ID manifest covers the critical ids.

Skip accounting is honest, not blanket: a skip on a REQUIRED trace whose
reason mentions ``FORGE_PG_TEST_URL`` is a ``prerequisite_missing`` skip —
the DISTINCT, named outcome (R40-08 / #344) recording that a required
profile's fixture environment is absent: the profile is reported
UNQUALIFIED and the gate refuses with the prerequisite exit (2), never an
invisible skip-green. Any other skip whose reason mentions
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

Exit codes: 0 green · 2 prerequisite (including a required profile's
``prerequisite_missing`` skip — R40-08) · 3 schema · 4 manifest ·
5 required skip · 6 test failures.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
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
    GateProfile(
        # Q39-08 (#327): the accounting races on required PostgreSQL —
        # the executed-ID manifest must cover the two critical ids below,
        # and the sqlite unit arms of the same files run beside them.
        name="accounting-races",
        selection=(
            "tests/test_credential_audit.py",
            "tests/test_usage_ingestion.py",
        ),
        database_suffix="ac",
    ),
)

#: R40-08 (#344): where the production-entry traces write their
#: machine-readable execution records during the gate run (source sha +
#: evidence class per trace; embedded into the report artifact below).
#: Unset outside the gate — the traces stay fully hermetic then.
TRACE_RECORD_DIR_ENV = "FORGE_TRACE_RECORD_DIR"
#: R40-08 (#344): the release-artifact digest when the gate runs against
#: a BUILT artifact (the canary's spelling) — the report distinguishes
#: the source tree it executed on from the artifact it installed.
RELEASE_ARTIFACT_SHA_ENV = "FORGE_RELEASE_ARTIFACT_SHA256"


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
    RequiredTrace(
        label="Q39-03 (#322) concurrent redemptions + evidence writers survive (CAS projection)",
        pattern=re.compile(
            r"tests/test_credential_audit\.py::TestRealPostgres"
            r"::test_concurrent_redemptions_and_evidence_writers_survive$"
        ),
        profile="accounting-races",
    ),
    RequiredTrace(
        label="Q39-05 (#324) concurrent partial+final reconcile under real isolation",
        pattern=re.compile(
            r"tests/test_usage_ingestion\.py::TestQ3905RealPostgres"
            r"::test_concurrent_partial_and_final_reconcile_under_real_isolation$"
        ),
        profile="accounting-races",
    ),
    # R40-08 (#344): the REQUIRED composed-trace set — one entry per
    # high-risk invariant, each WITH its mutation arm (a removed arm is
    # the same marker-removal mutation the manifest check detects).
    RequiredTrace(
        label="R40-01 (#337) FI-1 the wired /fix ingress trace (ASGI→inbox→worker→reconciler)",
        pattern=re.compile(
            r"tests/production_entry/test_feedback_ingress\.py"
            r"::TestFI1TheWiredIngressTrace::"
        ),
        profile="production-entry",
    ),
    RequiredTrace(
        label="R40-01 (#337) FI-5 the registration-revert mutation arms",
        pattern=re.compile(
            r"tests/production_entry/test_feedback_ingress\.py"
            r"::TestFI5TheRegistrationMutations::"
        ),
        profile="production-entry",
    ),
    RequiredTrace(
        label="R40-03 (#339) MG-1 partial-liability admission + the value-presence mutant",
        pattern=re.compile(
            r"tests/production_entry/test_mutation_gates\.py"
            r"::TestMG1PartialLiabilityAdmission::"
        ),
        profile="production-entry",
    ),
    RequiredTrace(
        label="R40-04 (#340) MG-2 guarded review amendment + the evidence-only mutant",
        pattern=re.compile(
            r"tests/production_entry/test_mutation_gates\.py"
            r"::TestMG2GuardedReviewAmendment::"
        ),
        profile="production-entry",
    ),
    RequiredTrace(
        label="R40-05 (#341) MG-3 grant persistence under concurrent evidence + the mutant",
        pattern=re.compile(
            r"tests/production_entry/test_mutation_gates\.py"
            r"::TestMG3GrantPersistenceUnderConcurrentEvidence::"
        ),
        profile="production-entry",
    ),
)

#: Lab-environment-bound traces: executed wherever the local lab exists
#: (the podman postgres container / the live GitLab credentials), an
#: *environment* skip elsewhere (CI). The R37-14 native failpoint matrix
#: needs BOTH the disposable real Postgres AND the live lab GitLab —
#: executed in the lab and recorded in
#: docs/evaluation/2026-09-24-two-writer-native/; CI reruns the sqlite
#: reference arms (test_saga_native/test_saga_durable) and the recorded
#: evidence stands for the native arm.
ENVIRONMENT_SCOPED_TESTS = re.compile(
    r"tests/test_checkpoint_retry_authority\.py::TestAT04PostgresRetryAuthority::"
    r"|tests/production_entry/test_two_writer_native\.py::"
)
_LAB_SKIP = re.compile(
    r"podman|disposable database cannot be created"
    r"|FORGE_GITLAB_LIVE_URL/FORGE_GITLAB_LIVE_TOKEN",
    re.IGNORECASE,
)
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
    """Classify one skipped test: ``prerequisite_missing`` (a required
    profile's fixture env is absent — the DISTINCT, named R40-08 outcome),
    ``required`` (refuse) or ``environment``.

    - the lab-environment traces classify FIRST: their combined-reason
      skips name FORGE_PG_TEST_URL alongside the live-GitLab prerequisite
      and are environment-bound by design (the PG half IS provided; the
      live-lab half is the recorded lab evidence);
    - a skip on a REQUIRED trace whose reason names ``FORGE_PG_TEST_URL``
      is ``prerequisite_missing``: the gate exported the URL, so that
      skipif firing means the required profile's fixture environment
      broke — the profile is reported UNQUALIFIED under its own name,
      never an invisible skip (R40-08 / #344 acceptance 6);
    - any other skip whose reason names ``FORGE_PG_TEST_URL`` refuses: the
      gate exported the URL, so that skipif firing means the prerequisite
      broke (AT-09's "remove the URL" arm);
    - anything else refuses too: a required gate must not skip silently.
    """
    if ENVIRONMENT_SCOPED_TESTS.search(test_id) and _LAB_SKIP.search(reason):
        return "environment"
    if _PG_URL_SKIP.search(reason) and _is_required_trace(test_id):
        return "prerequisite_missing"
    if _PG_URL_SKIP.search(reason):
        return "required"
    return "required"


def _is_required_trace(test_id: str) -> bool:
    """Whether *test_id* belongs to the required (critical) manifest."""
    return any(trace.pattern.search(test_id) for trace in REQUIRED_TRACES)


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
    profile: GateProfile,
    gate_url: str,
    junit_path: Path,
    base_env: dict[str, str],
    trace_record_dir: Path | None = None,
) -> ProfileRun:
    run = ProfileRun(profile=profile, database_url=mask_url(gate_url))
    profile_traces = traces_for_profile(profile)
    env = {**base_env, "FORGE_PG_TEST_URL": gate_url}
    if trace_record_dir is not None:
        # R40-08 (#344): the mutation-gate traces write their execution
        # records (source sha + evidence class) here during the run; the
        # report embeds them — see :func:`trace_records`.
        env[TRACE_RECORD_DIR_ENV] = str(trace_record_dir)

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

    # R40-08 (#344): an unhandled database-thread exception FAILS the
    # test that leaked it — the gate promotes
    # PytestUnhandledThreadExceptionWarning to an error for its own runs,
    # so "zero unhandled thread errors" is enforced mechanically, never
    # asserted by absence.
    executed = _run_pytest(
        [
            "-q",
            "-rs",
            "-W",
            "error::pytest.PytestUnhandledThreadExceptionWarning",
            f"--junitxml={junit_path}",
            *profile.selection,
        ],
        env,
    )
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
    prerequisite_missing = [
        skip for skip in run.skips if skip["classification"] == "prerequisite_missing"
    ]
    if prerequisite_missing:
        # R40-08 (#344): a required profile whose fixture environment is
        # missing is UNQUALIFIED under its own named outcome — never an
        # invisible skip, and never folded into a generic test failure.
        details = "; ".join(
            f"{skip['test_id']} ({skip['reason']})" for skip in prerequisite_missing[:5]
        )
        raise PrerequisiteError(
            f"profile {run.profile.name!r} is UNQUALIFIED — required traces skipped "
            f"on a missing prerequisite ({details}); the gate never counts a "
            "skip as passed coverage"
        )
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


def source_identity(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """WHAT the gate executed on: the source tree, or a release artifact.

    R40-08 (#344) report hygiene: the two identities are kept DISTINCT —
    ``basis: source-main`` names the checked-out tree (commit + dirty
    flag), ``basis: release-artifact`` names the built artifact the run
    installed (``FORGE_RELEASE_ARTIFACT_SHA256``, the canary's spelling).
    A report can never present an artifact run as source-main evidence or
    the reverse.
    """
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
    artifact = (environ or os.environ).get(RELEASE_ARTIFACT_SHA_ENV, "").strip()
    return {
        "basis": "release-artifact" if artifact else "source-main",
        "git_commit": commit,
        "tree_dirty": dirty,
        "artifact_sha256": artifact or None,
    }


def critical_test_sources(manifest: list[str]) -> dict[str, dict[str, str]]:
    """Every executed critical test id → its EXACT source SHA (R40-08).

    The sha256 of the test's own source FILE at run time: an executed
    record from a different source (a renamed file, a moved class) can
    never masquerade as the critical id — the manifest pattern matches,
    the source digest pins the bytes.
    """
    sources: dict[str, dict[str, str]] = {}
    for node in manifest:
        if not any(trace.pattern.search(node) for trace in REQUIRED_TRACES):
            continue
        rel_path = node.split("::", 1)[0]
        path = REPO_ROOT / rel_path
        entry: dict[str, str] = {"file": rel_path}
        if path.is_file():
            entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:  # pragma: no cover — collection produced it; refuse-shaped
            entry["sha256"] = "MISSING"
        sources[node] = entry
    return sources


def trace_records(environ: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """The #344 mutation-gate traces' own execution records (if the gate
    exported FORGE_TRACE_RECORD_DIR, the traces wrote one JSON record per
    executed baseline/mutation arm — label, outcome, source identity).
    Embedded into the report so the artifact is self-contained."""
    directory = (environ or os.environ).get(TRACE_RECORD_DIR_ENV, "").strip()
    if not directory:
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):  # pragma: no cover — a torn record
            records.append({"schema": "unreadable", "path": str(path)})
    return records


def build_report(
    mode: str,
    admin_url_masked: str,
    head: str,
    runs: list[ProfileRun],
    started_iso: str,
    total_duration: float,
    refusal: PgGateError | None,
    databases: dict[str, Any],
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The qualification evidence artifact. *environ* names the run's
    environment (the main() plumbing passes the gate's own); it decides
    the source-vs-artifact identity and where the trace records live."""
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
    prerequisite_skips = [
        skip
        for run in runs
        for skip in run.skips
        if skip["classification"] == "prerequisite_missing"
    ]
    critical_sources: dict[str, dict[str, str]] = {}
    for run in runs:
        critical_sources.update(critical_test_sources(run.manifest))
    return {
        "gate": {
            "script": "scripts/pg_gate.py",
            "issue": "#267 (R36-08 / AT-09), extended by #344 (R40-08)",
            "mode": mode,
            "started_utc": started_iso,
            "duration_seconds": round(total_duration, 3),
            "admin_url": admin_url_masked,
            "alembic_head": head,
            "databases": databases,
            "source_identity": source_identity(environ),
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
            # R40-08 (#344): the executed CRITICAL test ids with their
            # EXACT source sha — an executed record is bound to the bytes
            # that ran, never to a pattern alone.
            "critical_test_sources": critical_sources,
            "durations_seconds": durations,
            "required_skips": required_skips,  # MUST stay [] for green
            "environment_skips": environment_skips,
            # R40-08 (#344): the named, distinct outcome for a required
            # profile whose fixture environment is missing — the profile is
            # UNQUALIFIED, never invisibly skipped (must stay [] for green).
            "prerequisite_missing_skips": prerequisite_skips,
            "skip_accounting": {
                "collected": sum(len(run.manifest) for run in runs),
                "executed": len(executed_ids),
                "skipped": skipped_count,
                "required": len(required_skips),
                "environment": len(environment_skips),
                "prerequisite_missing": len(prerequisite_skips),
            },
            # The issue's observability vocabulary, verbatim keys.
            "tests.critical_trace_executed": len(
                [node for node in executed_ids if node in critical_sources]
            ),
            "tests.prerequisite_missing": len(prerequisite_skips),
            # A mutation arm "killed" = its record executed green (the arm
            # proves the baseline detects the seeded defect; a failing arm
            # means the detector itself broke).
            "tests.mutation_killed": len(
                [
                    record
                    for record in trace_records(environ)
                    if record.get("mutation") and record.get("outcome") == "passed"
                ]
            ),
            # Structurally zero on green: the gate's pytest promotes the
            # unhandled-thread-exception warning to an error (run_profile),
            # so a green report CANNOT carry one.
            "tests.unhandled_thread_errors": 0 if not refusal else None,
            "tests.duration_by_layer": {run.profile.name: run.duration_seconds for run in runs},
            # The #344 mutation-gate traces' own records (label + mutation
            # + outcome + source identity per trace/arm), when the gate
            # exported FORGE_TRACE_RECORD_DIR.
            "mutation_gate_trace_records": trace_records(environ),
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
    trace_dir: Path | None = None
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
        # The traces' records outlive the junit dir: the report embeds them
        # (trace_records()) BEFORE this second tempdir is removed below.
        trace_dir = Path(tempfile.mkdtemp(prefix="pg-gate-traces-"))
        environ[TRACE_RECORD_DIR_ENV] = str(trace_dir)  # the report reads it back
        try:
            for profile in PROFILES:
                run = run_profile(
                    profile,
                    profile_urls[profile.name],
                    junit_dir / f"{profile.name}.xml",
                    environ,
                    trace_record_dir=trace_dir,
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
        args.mode,
        mask_url(admin_url),
        head,
        runs,
        started_iso,
        total,
        refusal,
        databases,
        environ=environ,
    )
    if trace_dir is not None:
        shutil.rmtree(trace_dir, ignore_errors=True)  # embedded above
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
