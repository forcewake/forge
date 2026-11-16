"""Unit tests for the required PostgreSQL qualification gate.

The runner loads ``scripts/pg_gate.py`` directly and pins its decision logic:
the selection must exist, a removed required id refuses, a PostgreSQL-URL skip
on a required trace is a missing prerequisite, a wrong schema revision refuses,
and the installer is the packaged schema command.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_SCRIPT = REPO_ROOT / "scripts" / "pg_gate.py"

CRASH = (
    "tests/test_failure_injection.py::TestS3CrashAfterCommit::"
    "test_s3_recovery_adopts_the_existing_commit_and_never_commits_twice"
)
BASELINE = (
    "tests/test_schema_baseline.py::"
    "test_one_fresh_baseline_is_complete_unprefixed_and_preserves_exact_resource_values"
)
RACE = (
    "tests/test_budget_amendment.py::TestRealPostgresRaces::"
    "test_two_amendments_and_a_reservation_raced_across_sessions"
)
ONCE = (
    "tests/test_budget_amendment.py::TestRealPostgresRaces::"
    "test_one_command_raced_from_two_sessions_applies_once"
)
COMPLETE = [CRASH, BASELINE, RACE, ONCE]


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


class TestSelectionIntegrity:
    def test_every_selected_path_exists_on_disk(self, gate: ModuleType) -> None:
        gate.check_selection_on_disk()

    def test_profiles_name_the_current_runtime_suites(self, gate: ModuleType) -> None:
        selected = {path for profile in gate.PROFILES for path in profile.selection}
        assert selected == {
            "tests/test_failure_injection.py",
            "tests/test_schema_baseline.py",
            "tests/test_budget_amendment.py",
        }
        assert [profile.name for profile in gate.PROFILES] == [
            "crash-recovery",
            "schema-baseline",
            "accounting-races",
        ]

    def test_profiles_own_separate_databases_and_pass_their_url(self, gate: ModuleType) -> None:
        suffixes = [profile.database_suffix for profile in gate.PROFILES]
        assert len(suffixes) == len(set(suffixes)) == len(gate.PROFILES)
        by_name = {profile.name: profile for profile in gate.PROFILES}
        assert "FORGE_ENGINE_TEST_DATABASE_URL" in by_name["crash-recovery"].url_env
        assert "FORGE_SCHEMA_TEST_DATABASE_URL" in by_name["schema-baseline"].url_env
        assert by_name["accounting-races"].url_env == ("FORGE_PG_TEST_URL",)

    def test_required_traces_belong_to_the_profile_that_selects_their_file(
        self, gate: ModuleType
    ) -> None:
        by_name = {profile.name: profile for profile in gate.PROFILES}
        for trace in gate.REQUIRED_TRACES:
            profile = by_name[trace.profile]
            path = trace.pattern.pattern.split("::", 1)[0].replace(r"\.", ".")
            assert any(path.startswith(item) for item in profile.selection)

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


class TestManifestCheck:
    def test_a_complete_manifest_passes(self, gate: ModuleType) -> None:
        assert gate.missing_required(COMPLETE) == []
        matches = gate.required_test_ids(COMPLETE)
        assert set(matches) == {trace.label for trace in gate.REQUIRED_TRACES}
        assert all(matches.values())

    def test_a_removed_required_test_refuses(self, gate: ModuleType) -> None:
        absent = gate.missing_required([node for node in COMPLETE if node != CRASH])
        assert [trace.label for trace in absent] == [
            "crash recovery adopts one commit after worker death"
        ]

    def test_the_manifest_check_is_scoped_per_profile(self, gate: ModuleType) -> None:
        crash, schema = gate.PROFILES[0], gate.PROFILES[1]
        assert gate.missing_required([CRASH], gate.traces_for_profile(crash)) == []
        assert gate.missing_required([CRASH], gate.traces_for_profile(schema)) == list(
            gate.traces_for_profile(schema)
        )

    def test_accounting_races_require_both_ids(self, gate: ModuleType) -> None:
        profile = next(item for item in gate.PROFILES if item.name == "accounting-races")
        traces = gate.traces_for_profile(profile)
        assert len(traces) == 2
        absent = gate.missing_required([RACE], traces)
        assert len(absent) == 1
        assert absent[0].pattern.search(ONCE)

    def test_the_required_patterns_match_the_live_collection(self, gate: ModuleType) -> None:
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
            timeout=180,
        )
        assert collected.returncode == 0, collected.stderr
        manifest = gate.parse_collected_manifest(collected.stdout)
        assert gate.missing_required(manifest) == []

    def test_parse_collected_manifest_ignores_summary_noise(self, gate: ModuleType) -> None:
        output = "\n".join(
            [
                "tests/test_x.py::TestA::test_one",
                "tests/test_x.py::TestA::test_two[param]",
                "",
                "95 tests collected in 0.42s",
            ]
        )
        assert gate.parse_collected_manifest(output) == [
            "tests/test_x.py::TestA::test_one",
            "tests/test_x.py::TestA::test_two[param]",
        ]


class TestSkipAccounting:
    def test_a_required_url_skip_is_a_missing_prerequisite(self, gate: ModuleType) -> None:
        reason = "FORGE_ENGINE_TEST_DATABASE_URL is not configured"
        assert gate.classify_skip(CRASH, reason) == "prerequisite_missing"

    def test_a_url_skip_outside_the_required_set_still_refuses(self, gate: ModuleType) -> None:
        node = "tests/test_budget_amendment.py::TestRecovery::test_other"
        assert gate.classify_skip(node, "FORGE_PG_TEST_URL not set") == "required"

    def test_a_lab_only_skip_stays_an_environment_skip(self, gate: ModuleType) -> None:
        node = "tests/test_failure_injection.py::TestLabOnly::test_live_gitlab"
        reason = "the forge-postgres podman container is unavailable"
        assert gate.classify_skip(node, reason) == "environment"

    def test_any_other_skip_refuses(self, gate: ModuleType) -> None:
        assert gate.classify_skip(CRASH, "unrelated fixture missing") == "required"


class TestSchemaState:
    def test_a_healthy_installed_database_passes(self, gate: ModuleType) -> None:
        gate.check_schema_state("10000001", {"runs"}, "10000001", {"runs"})

    def test_a_database_without_alembic_version_refuses(self, gate: ModuleType) -> None:
        with pytest.raises(gate.SchemaError, match="never provisioned"):
            gate.check_schema_state(None, {"runs"}, "10000001", {"runs"})

    def test_an_old_revision_refuses(self, gate: ModuleType) -> None:
        with pytest.raises(gate.SchemaError, match="revision '032', expected the head '10000001'"):
            gate.check_schema_state("032", {"runs"}, "10000001", {"runs"})

    def test_a_dropped_table_refuses(self, gate: ModuleType) -> None:
        with pytest.raises(gate.SchemaError, match="missing migration tables"):
            gate.check_schema_state("10000001", {"runs"}, "10000001", {"runs", "checkpoints"})

    def test_the_packaged_baseline_is_the_only_head(self, gate: ModuleType) -> None:
        assert gate.alembic_head() == "10000001"
        tables = gate.migration_tables()
        assert {"runs", "checkpoints", "schema_installation", "effects"} <= tables
        assert "alembic_version" not in tables
        assert "flow_runs" not in tables

    def test_schema_init_provisions_sqlite_and_a_missing_url_refuses(self, gate, tmp_path) -> None:
        url = "sqlite+aiosqlite:///" + str(tmp_path / "gate.db")
        gate.run_migrations(url)
        report_path = tmp_path / "pg-gate.json"
        code = gate.main(["--report", str(report_path)], env={})
        assert code == 2
        body = json.loads(report_path.read_text())
        assert body["gate"]["databases"]["provisioned_via"] == "python -m forge.store.schema init"
        assert body["qualification"]["result"] == "refused"

    def test_the_installer_is_the_packaged_schema_command(self, gate: ModuleType) -> None:
        command = gate.schema_install_command("postgresql+asyncpg://forge:forge@127.0.0.1/forge")
        assert command[1:4] == ["-m", "forge.store.schema", "init"]
        assert "forge.migrate" not in command
        assert "--database-url" not in command
        assert "forge:forge" not in " ".join(command)


class TestReport:
    def test_green_report_names_the_executed_trace_and_its_source(self, gate: ModuleType) -> None:
        run = gate.ProfileRun(
            profile=gate.PROFILES[0],
            database_url="postgresql+asyncpg://forge:***@h/db",
            manifest=[CRASH],
            required_matches={gate.REQUIRED_TRACES[0].label: [CRASH]},
            records=[gate.TestRecord(test_id=CRASH, outcome="passed", duration=0.4)],
        )
        report = gate.build_report(
            mode="create",
            admin_url_masked="postgresql+asyncpg://forge:***@127.0.0.1/forge",
            head="10000001",
            runs=[run],
            started_iso="2026-11-15T00:00:00+00:00",
            total_duration=1.5,
            refusal=None,
            databases={
                "created": ["forge_pg_gate_fi_ab12"],
                "dropped": True,
                "provisioned_via": "python -m forge.store.schema init",
            },
            environ={gate.TRACE_RECORD_DIR_ENV: "", gate.RELEASE_ARTIFACT_SHA_ENV: ""},
        )
        assert report["qualification"]["result"] == "green"
        assert CRASH in report["qualification"]["executed_test_ids"]
        assert report["gate"]["alembic_head"] == "10000001"
        assert report["gate"]["databases"]["provisioned_via"] == "python -m forge.store.schema init"
        source = report["qualification"]["critical_test_sources"][CRASH]
        assert source["file"] == "tests/test_failure_injection.py"
        assert len(source["sha256"]) == 64
        assert json.dumps(report)

    def test_an_environment_skip_stays_visible(self, gate: ModuleType) -> None:
        node = "tests/test_failure_injection.py::TestLabOnly::test_live_gitlab"
        run = gate.ProfileRun(
            profile=gate.PROFILES[0],
            database_url="postgresql+asyncpg://forge:***@h/db",
            manifest=[node],
            records=[
                gate.TestRecord(
                    test_id=node,
                    outcome="skipped",
                    duration=0.0,
                    skip_reason="podman is unavailable",
                )
            ],
            skips=[
                {
                    "test_id": node,
                    "reason": "podman is unavailable",
                    "classification": "environment",
                }
            ],
        )
        report = gate.build_report(
            "create",
            "postgresql+asyncpg://forge:***@h/db",
            "10000001",
            [run],
            "2026-11-15T00:00:00+00:00",
            1.0,
            None,
            {
                "created": [],
                "dropped": False,
                "provisioned_via": "python -m forge.store.schema init",
            },
        )
        assert report["qualification"]["result"] == "green"
        assert report["qualification"]["environment_skips"][0]["test_id"] == node
