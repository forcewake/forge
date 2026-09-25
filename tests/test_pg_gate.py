"""Unit tests for the required PostgreSQL qualification gate (R36-08, #267).

``scripts/pg_gate.py`` is a CI runner, not a package: these tests load it
straight from its path (no packaging change) and pin the DECISION LOGIC the
gate is trusted for — the selection's integrity, the collected-manifest
check (the marker-removal mutation detector), the skip accounting (a
required skip refuses; a podman-lab environment skip stays visible), the
report shape, and AT-09's "the URL was removed" refusal.

No real PostgreSQL is needed: every test drives the gate's PURE functions
or its argument/entry plumbing. The full end-to-end (provision via alembic,
run, report, negative arms) lives in the gate's own CI step.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_SCRIPT = REPO_ROOT / "scripts" / "pg_gate.py"


def _load_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pg_gate_under_test", GATE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    return _load_gate()


# ---------------------------------------------------------------------------
# The selection itself — it must reference real files, loudly
# ---------------------------------------------------------------------------


class TestSelectionIntegrity:
    def test_every_selected_path_exists_on_disk(self, gate: ModuleType) -> None:
        gate.check_selection_on_disk()  # raises ManifestError on any gap

    def test_the_selection_covers_the_r36_08_scopes(self, gate: ModuleType) -> None:
        selected = {path for profile in gate.PROFILES for path in profile.selection}
        assert "tests/production_entry/" in selected
        assert "tests/test_checkpoint_gc.py" in selected
        assert "tests/test_checkpoint_repository.py" in selected
        assert "tests/test_checkpoint_retry_authority.py" in selected

    def test_the_selection_covers_the_q39_08_accounting_races(self, gate: ModuleType) -> None:
        """#327: the #322 CAS-projection and #324 partial→final arms run
        on the REQUIRED selection — their files are members of a profile,
        and the critical ids are REQUIRED_TRACES (executed-ID tracked)."""
        selected = {path for profile in gate.PROFILES for path in profile.selection}
        assert "tests/test_credential_audit.py" in selected
        assert "tests/test_usage_ingestion.py" in selected
        by_profile = {profile.name: profile for profile in gate.PROFILES}
        assert "accounting-races" in by_profile
        traces = gate.traces_for_profile(by_profile["accounting-races"])
        assert {trace.profile for trace in traces} == {"accounting-races"}
        assert len(traces) == 2

    def test_profiles_own_separate_databases(self, gate: ModuleType) -> None:
        # The production-entry conftest resets the whole public schema per
        # test under FORGE_PG_TEST_URL — profiles must not share a database.
        suffixes = [profile.database_suffix for profile in gate.PROFILES]
        assert len(suffixes) == len(set(suffixes)) == len(gate.PROFILES)
        assert all(profile.selection for profile in gate.PROFILES)

    def test_required_traces_are_scoped_to_a_profile_that_selects_their_files(
        self, gate: ModuleType
    ) -> None:
        by_name = {profile.name: profile for profile in gate.PROFILES}
        assert len(by_name) == len(gate.PROFILES)
        for trace in gate.REQUIRED_TRACES:
            profile = by_name.get(trace.profile)
            assert profile is not None, f"{trace.label} names no known profile"
            path = trace.pattern.pattern.split("::")[0].replace(r"\.", ".")
            assert any(path.startswith(sel) for sel in profile.selection), (
                f"{trace.label} targets {path}, which profile {profile.name!r} does not select"
            )

    def test_a_missing_selection_file_refuses_loudly(
        self, gate: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        broken = (
            gate.GateProfile(
                name="broken",
                selection=("tests/test_pg_gate_no_such_file.py",),
                database_suffix="x",
            ),
        )
        monkeypatch.setattr(gate, "PROFILES", broken)
        with pytest.raises(gate.ManifestError, match="missing files"):
            gate.check_selection_on_disk()


# ---------------------------------------------------------------------------
# The manifest check — the marker-removal mutation detector
# ---------------------------------------------------------------------------


class TestManifestCheck:
    def test_a_complete_manifest_passes(self, gate: ModuleType) -> None:
        manifest = [
            "tests/production_entry/test_production_entry.py"
            "::TestPE4PostgresUploadRestartResume::test_new_instance_resumes",
            "tests/test_checkpoint_gc.py::TestP04ScheduleRealPostgres::"
            "test_b_reference_after_the_final_scan_survives_two_engines",
            "tests/test_checkpoint_gc.py::TestConcurrentFirstUploads"
            "::test_concurrent_first_uploads_respect_the_quota_real_postgres",
            "tests/test_checkpoint_repository.py::TestPostgresAuthorityOverRealPostgres"
            "::test_put_on_one_instance_read_on_a_fresh_one",
            "tests/test_credential_audit.py::TestRealPostgres"
            "::test_concurrent_redemptions_and_evidence_writers_survive",
            "tests/test_usage_ingestion.py::TestQ3905RealPostgres"
            "::test_concurrent_partial_and_final_reconcile_under_real_isolation",
        ]
        assert gate.missing_required(manifest) == []
        matches = gate.required_test_ids(manifest)
        assert set(matches) == {trace.label for trace in gate.REQUIRED_TRACES}
        assert all(ids for ids in matches.values())

    def test_a_missing_accounting_race_trace_refuses(self, gate: ModuleType) -> None:
        """#327: the accounting-races critical ids are REQUIRED — a
        collection missing them (a renamed class, a removed skipif marker
        that un-gates the test) refuses the gate instead of quietly
        running fewer tests."""
        manifest = [
            "tests/production_entry/test_production_entry.py"
            "::TestPE4PostgresUploadRestartResume::test_new_instance_resumes",
            "tests/test_checkpoint_gc.py::TestP04ScheduleRealPostgres::test_barrier",
            "tests/test_checkpoint_gc.py::TestConcurrentFirstUploads"
            "::test_concurrent_first_uploads_respect_the_quota_real_postgres",
            "tests/test_checkpoint_repository.py::TestPostgresAuthorityOverRealPostgres"
            "::test_put_on_one_instance_read_on_a_fresh_one",
            # the #322 trace present, the #324 trace REMOVED:
            "tests/test_credential_audit.py::TestRealPostgres"
            "::test_concurrent_redemptions_and_evidence_writers_survive",
        ]
        absent = gate.missing_required(manifest)
        assert [trace.label for trace in absent] == [
            "Q39-05 (#324) concurrent partial+final reconcile under real isolation"
        ]

    def test_a_pg_url_skip_on_the_accounting_traces_is_required(self, gate: ModuleType) -> None:
        """A missing prerequisite on the accounting races FAILS the gate
        (a REQUIRED skip), never a silent green skip."""
        node = (
            "tests/test_usage_ingestion.py::TestQ3905RealPostgres"
            "::test_concurrent_partial_and_final_reconcile_under_real_isolation"
        )
        reason = (
            "FORGE_PG_TEST_URL not set — the real-PostgreSQL isolation proofs "
            "run only against a disposable real Postgres"
        )
        assert gate.classify_skip(node, reason) == "required"

    def test_the_manifest_check_is_scoped_per_profile(self, gate: ModuleType) -> None:
        # production-entry demands ONLY PE-4; the checkpoint profile demands
        # the checkpoint traces — a PE-only manifest fails exactly those.
        pe_only = [
            "tests/production_entry/test_production_entry.py"
            "::TestPE4PostgresUploadRestartResume::test_new_instance_resumes",
            "tests/production_entry/test_production_entry.py::TestPE1::test_x",
        ]
        production_entry, checkpoint = gate.PROFILES[0], gate.PROFILES[1]
        assert gate.missing_required(pe_only, gate.traces_for_profile(production_entry)) == []
        checkpoint_traces = gate.traces_for_profile(checkpoint)
        assert gate.missing_required(pe_only, checkpoint_traces) == list(checkpoint_traces)

    def test_a_removed_required_test_refuses(self, gate: ModuleType) -> None:
        # The mutation: PE-4's test was deleted/renamed — its id no longer
        # collects, and the gate must refuse instead of running fewer tests.
        manifest = [
            "tests/test_checkpoint_gc.py::TestP04ScheduleRealPostgres::test_barrier",
            "tests/test_checkpoint_gc.py::TestConcurrentFirstUploads"
            "::test_concurrent_first_uploads_respect_the_quota_real_postgres",
            "tests/test_checkpoint_repository.py::TestPostgresAuthorityOverRealPostgres"
            "::test_put_on_one_instance_read_on_a_fresh_one",
            "tests/test_credential_audit.py::TestRealPostgres"
            "::test_concurrent_redemptions_and_evidence_writers_survive",
            "tests/test_usage_ingestion.py::TestQ3905RealPostgres"
            "::test_concurrent_partial_and_final_reconcile_under_real_isolation",
        ]
        absent = gate.missing_required(manifest)
        assert [trace.label for trace in absent] == [
            "PE-4 (AT-04) Postgres upload, restart, exact-spec resume"
        ]

    def test_the_required_patterns_match_the_live_collection(self, gate: ModuleType) -> None:
        # The detector is wired to reality: collecting the REAL selection
        # right now must capture every required trace (no PG needed —
        # collection does not execute the PG-gated skipifs).
        import subprocess

        collected = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-p",
                "no:cacheprovider",
                *(path for profile in gate.PROFILES for path in profile.selection),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert collected.returncode == 0, collected.stderr
        manifest = gate.parse_collected_manifest(collected.stdout)
        assert manifest, "the live collection must not be empty"
        assert gate.missing_required(manifest) == []

    def test_parse_collected_manifest_ignores_summary_noise(self, gate: ModuleType) -> None:
        output = "\n".join(
            [
                "tests/test_x.py::TestA::test_one",
                "tests/test_x.py::TestA::test_two[param]",
                "",
                "95 tests collected in 0.42s",
                "-- Docs: https://docs.pytest.org/en/stable/how-to/...",
                "=== warnings ===",
            ]
        )
        assert gate.parse_collected_manifest(output) == [
            "tests/test_x.py::TestA::test_one",
            "tests/test_x.py::TestA::test_two[param]",
        ]


# ---------------------------------------------------------------------------
# Skip accounting — required skips refuse, environment skips stay visible
# ---------------------------------------------------------------------------


class TestSkipAccounting:
    def test_a_pg_url_skip_is_required(self, gate: ModuleType) -> None:
        assert (
            gate.classify_skip(
                "tests/test_checkpoint_gc.py::TestP04ScheduleRealPostgres::test_x",
                "FORGE_PG_TEST_URL not set — the real-PostgreSQL P04 barrier "
                "proof runs only against a disposable real Postgres",
            )
            == "required"
        )

    def test_the_podman_lab_skip_is_environment(self, gate: ModuleType) -> None:
        assert (
            gate.classify_skip(
                "tests/test_checkpoint_retry_authority.py"
                "::TestAT04PostgresRetryAuthority::test_upload_then_fresh",
                "the forge-postgres podman container is unavailable: Error: exec: …",
            )
            == "environment"
        )
        assert (
            gate.classify_skip(
                "tests/test_checkpoint_retry_authority.py"
                "::TestAT04PostgresRetryAuthority::test_upload_then_fresh",
                "the disposable database cannot be created: …",
            )
            == "environment"
        )

    def test_an_unexplained_skip_anywhere_refuses(self, gate: ModuleType) -> None:
        # Unknown skip reasons are NEVER tolerated — a required gate must
        # not go green over a skip it cannot classify.
        assert (
            gate.classify_skip(
                "tests/test_checkpoint_gc.py::TestAnything::test_x",
                "some novel condition nobody accounted for",
            )
            == "required"
        )
        assert gate.classify_skip("tests/test_x.py::test_y", "") == "required"

    def test_a_podman_reason_outside_the_lab_refuses(self, gate: ModuleType) -> None:
        assert (
            gate.classify_skip(
                "tests/test_checkpoint_gc.py::TestP04ScheduleRealPostgres::test_x",
                "the forge-postgres podman container is unavailable: …",
            )
            == "required"
        )

    def test_a_required_skip_refuses_the_profile(self, gate: ModuleType) -> None:
        run = gate.ProfileRun(
            profile=gate.PROFILES[1],
            database_url="postgresql+asyncpg://forge:***@h/db",
            manifest=[
                "tests/test_checkpoint_gc.py::TestP04ScheduleRealPostgres::test_x",
                "tests/test_checkpoint_gc.py::TestP04ScheduleRealPostgres::test_y",
            ],
            required_matches={
                "P04 post-final-scan GC barrier (real Postgres, two engines)": [
                    "tests/test_checkpoint_gc.py::TestP04ScheduleRealPostgres::test_x",
                    "tests/test_checkpoint_gc.py::TestP04ScheduleRealPostgres::test_y",
                ]
            },
            records=[
                gate.TestRecord(
                    test_id="tests/test_checkpoint_gc.py::TestP04ScheduleRealPostgres::test_x",
                    outcome="skipped",
                    duration=0.01,
                    skip_reason="FORGE_PG_TEST_URL not set — …",
                ),
                gate.TestRecord(
                    test_id="tests/test_checkpoint_gc.py::TestP04ScheduleRealPostgres::test_y",
                    outcome="passed",
                    duration=0.4,
                ),
            ],
            skips=[
                {
                    "test_id": "tests/test_checkpoint_gc.py::TestP04ScheduleRealPostgres::test_x",
                    "reason": "FORGE_PG_TEST_URL not set — …",
                    "classification": "required",
                }
            ],
        )
        with pytest.raises(gate.RequiredSkipError, match="REQUIRED tests skipped"):
            gate.evaluate_profile(run)

    def test_all_pass_is_green(self, gate: ModuleType) -> None:
        node = "tests/test_checkpoint_gc.py::TestP04ScheduleRealPostgres::test_x"
        run = gate.ProfileRun(
            profile=gate.PROFILES[1],
            database_url="postgresql+asyncpg://forge:***@h/db",
            manifest=[node],
            required_matches={
                "P04 post-final-scan GC barrier (real Postgres, two engines)": [node]
            },
            records=[gate.TestRecord(test_id=node, outcome="passed", duration=0.4)],
        )
        gate.evaluate_profile(run)  # no refusal

    def test_a_failure_fails_the_selection(self, gate: ModuleType) -> None:
        node = "tests/test_checkpoint_gc.py::TestP04ScheduleRealPostgres::test_x"
        run = gate.ProfileRun(
            profile=gate.PROFILES[1],
            database_url="postgresql+asyncpg://forge:***@h/db",
            manifest=[node],
            required_matches={
                "P04 post-final-scan GC barrier (real Postgres, two engines)": [node]
            },
            records=[gate.TestRecord(test_id=node, outcome="failed", duration=0.4)],
        )
        with pytest.raises(gate.SelectionFailed, match="1 test failure"):
            gate.evaluate_profile(run)


# ---------------------------------------------------------------------------
# JUnit accounting — executed ids + durations
# ---------------------------------------------------------------------------

JUNIT_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="1" tests="3">
  <testcase classname="tests.test_checkpoint_gc.TestP04ScheduleRealPostgres"
            name="test_passes" time="0.417"/>
  <testcase classname="tests.test_checkpoint_gc.TestP04ScheduleRealPostgres"
            name="test_fails" time="0.020">
    <failure message="AssertionError: …"/>
  </testcase>
  <testcase classname="tests.test_checkpoint_gc.TestConcurrentFirstUploads"
            name="test_concurrent_first_uploads_respect_the_quota_real_postgres"
            time="0.003">
    <skipped type="pytest.skip" message="FORGE_PG_TEST_URL not set — …"/>
  </testcase>
</testsuite></testsuites>
"""

MANIFEST = [
    "tests/test_checkpoint_gc.py::TestP04ScheduleRealPostgres::test_passes",
    "tests/test_checkpoint_gc.py::TestP04ScheduleRealPostgres::test_fails",
    "tests/test_checkpoint_gc.py::TestConcurrentFirstUploads"
    "::test_concurrent_first_uploads_respect_the_quota_real_postgres",
]


class TestJUnitAccounting:
    def test_junit_key_maps_manifest_ids(self, gate: ModuleType) -> None:
        assert gate.junit_key(
            "tests/test_checkpoint_gc.py::TestP04ScheduleRealPostgres::test_passes"
        ) == ("tests.test_checkpoint_gc.TestP04ScheduleRealPostgres", "test_passes")
        assert gate.junit_key("tests/test_x.py::test_plain") == ("tests.test_x", "test_plain")
        assert gate.junit_key(
            "tests/production_entry/test_production_entry.py"
            "::TestPE4PostgresUploadRestartResume::test_a"
        ) == (
            "tests.production_entry.test_production_entry.TestPE4PostgresUploadRestartResume",
            "test_a",
        )

    def test_parse_junit_reads_outcomes_durations_and_skip_reasons(self, gate: ModuleType) -> None:
        records = {r.test_id: r for r in gate.parse_junit(JUNIT_XML, MANIFEST)}
        assert records[MANIFEST[0]].outcome == "passed"
        assert records[MANIFEST[0]].duration == pytest.approx(0.417)
        assert records[MANIFEST[1]].outcome == "failed"
        skipped = records[MANIFEST[2]]
        assert skipped.outcome == "skipped"
        assert "FORGE_PG_TEST_URL" in (skipped.skip_reason or "")
        assert gate.classify_skip(skipped.test_id, skipped.skip_reason or "") == "required"


# ---------------------------------------------------------------------------
# Schema-state decision — the wrong-revision / dropped-table arms
# ---------------------------------------------------------------------------


class TestSchemaState:
    def test_a_healthy_migrated_database_passes(self, gate: ModuleType) -> None:
        gate.check_schema_state(
            "027", {"flow_runs", "checkpoint_metadata"}, "027", {"flow_runs", "checkpoint_metadata"}
        )

    def test_a_database_without_alembic_version_refuses(self, gate: ModuleType) -> None:
        with pytest.raises(gate.SchemaError, match="never provisioned"):
            gate.check_schema_state(None, {"flow_runs"}, "027", {"flow_runs"})

    def test_an_early_revision_refuses(self, gate: ModuleType) -> None:
        with pytest.raises(gate.SchemaError, match="revision '026', expected the head '027'"):
            gate.check_schema_state("026", {"flow_runs"}, "027", {"flow_runs"})

    def test_a_dropped_migration_table_refuses(self, gate: ModuleType) -> None:
        with pytest.raises(gate.SchemaError, match="missing migration tables"):
            gate.check_schema_state(
                "027", {"flow_runs"}, "027", {"flow_runs", "checkpoint_metadata"}
            )

    def test_the_migration_table_derivation_covers_the_chain(self, gate: ModuleType) -> None:
        tables = gate.migration_tables()
        assert {"flow_runs", "checkpoint_metadata", "control_commands"} <= tables
        assert "alembic_version" not in tables  # the bookkeeping table is not a migration table


# ---------------------------------------------------------------------------
# The report — the qualification evidence artifact
# ---------------------------------------------------------------------------


def _green_run(gate: ModuleType) -> Any:
    node = "tests/test_checkpoint_gc.py::TestP04ScheduleRealPostgres::test_x"
    return gate.ProfileRun(
        profile=gate.PROFILES[1],
        database_url="postgresql+asyncpg://forge:***@h/db",
        manifest=[node],
        required_matches={"P04 post-final-scan GC barrier (real Postgres, two engines)": [node]},
        records=[gate.TestRecord(test_id=node, outcome="passed", duration=0.4)],
        skips=[],
        duration_seconds=1.25,
    )


class TestReport:
    def test_green_report_shape(self, gate: ModuleType) -> None:
        report = gate.build_report(
            mode="create",
            admin_url_masked="postgresql+asyncpg://forge:***@127.0.0.1:5433/forge",
            head="027",
            runs=[_green_run(gate)],
            started_iso="2026-09-24T00:00:00+00:00",
            total_duration=12.5,
            refusal=None,
            databases={
                "created": ["forge_pg_gate_ck_ab12"],
                "dropped": True,
                "provisioned_via": "alembic upgrade head (python -m forge.migrate)",
            },
        )
        qualification = report["qualification"]
        assert qualification["result"] == "green"
        assert qualification["required_skips"] == []  # MUST be empty for green
        assert qualification["environment_skips"] == []
        assert (
            "tests/test_checkpoint_gc.py::TestP04ScheduleRealPostgres::test_x"
            in qualification["executed_test_ids"]
        )
        assert qualification["durations_seconds"][
            "tests/test_checkpoint_gc.py::TestP04ScheduleRealPostgres::test_x"
        ] == pytest.approx(0.4)
        assert qualification["skip_accounting"] == {
            "collected": 1,
            "executed": 1,
            "skipped": 0,
            "required": 0,
            "environment": 0,
        }
        assert qualification["flake_attempts"] == 1
        assert report["gate"]["alembic_head"] == "027"
        assert report["gate"]["databases"]["created"] == ["forge_pg_gate_ck_ab12"]
        assert report["ci"]["profile_duration_seconds"] == {"checkpoint-lifecycle": 1.25}
        assert json.dumps(report)  # the artifact is plain JSON

    def test_environment_skips_stay_visible_in_the_report(self, gate: ModuleType) -> None:
        node = (
            "tests/test_checkpoint_retry_authority.py"
            "::TestAT04PostgresRetryAuthority::test_upload_then_fresh"
        )
        run = gate.ProfileRun(
            profile=gate.PROFILES[1],
            database_url="postgresql+asyncpg://forge:***@h/db",
            manifest=[node],
            required_matches={},
            records=[
                gate.TestRecord(
                    test_id=node,
                    outcome="skipped",
                    duration=0.0,
                    skip_reason="the forge-postgres podman container is unavailable: …",
                )
            ],
            skips=[
                {
                    "test_id": node,
                    "reason": "the forge-postgres podman container is unavailable: …",
                    "classification": "environment",
                }
            ],
        )
        report = gate.build_report(
            "create",
            "postgresql+asyncpg://forge:***@h/db",
            "027",
            [run],
            "2026-09-24T00:00:00+00:00",
            5.0,
            None,
            {"created": [], "dropped": False, "provisioned_via": "…"},
        )
        qualification = report["qualification"]
        assert qualification["result"] == "green"
        assert qualification["required_skips"] == []
        assert qualification["environment_skips"][0]["test_id"] == node
        assert qualification["skip_accounting"]["environment"] == 1


# ---------------------------------------------------------------------------
# AT-09 — the URL was removed: a typed refusal, never a silent skip
# ---------------------------------------------------------------------------


class TestPrerequisiteRefusal:
    def test_typed_refusal_hierarchy(self, gate: ModuleType) -> None:
        assert issubclass(gate.PrerequisiteError, gate.PgGateError)
        for cls, code in (
            (gate.PrerequisiteError, 2),
            (gate.SchemaError, 3),
            (gate.ManifestError, 4),
            (gate.RequiredSkipError, 5),
            (gate.SelectionFailed, 6),
        ):
            assert issubclass(cls, gate.PgGateError)
            assert cls.exit_code == code

    def test_missing_url_refuses_with_evidence_report(
        self, gate: ModuleType, tmp_path: Path
    ) -> None:
        report_path = tmp_path / "pg-gate.json"
        code = gate.main(["--report", str(report_path)], env={})
        assert code == gate.PrerequisiteError.exit_code == 2
        evidence = json.loads(report_path.read_text())
        assert evidence["qualification"]["result"] == "refused"
        refusal = evidence["qualification"]["refusal"]
        assert refusal["type"] == "PrerequisiteError"
        assert refusal["exit_code"] == 2
        assert "FORGE_PG_TEST_URL" in refusal["detail"]
        assert evidence["qualification"]["executed_test_ids"] == []
        assert evidence["qualification"]["required_skips"] == []

    def test_the_url_is_masked_in_reports(self, gate: ModuleType) -> None:
        assert (
            gate.mask_url("postgresql+asyncpg://forge:secret@127.0.0.1:5433/forge")
            == "postgresql+asyncpg://forge:***@127.0.0.1:5433/forge"
        )
