"""R38-08 (#309) — the .NET service-and-dependency recipe, pinned.

The issue's refusals, pinned as tests: the pinned manifest is a FROZEN
contract (immutable digests, exact package pins, one report path per
required test project — floating pins and mutable tags are refused);
the TRX inventory reconciles EVERY required report independently
through the #226 machinery (a passing first project never hides a
failing second, a missing report is ``missing_report`` — never zero
failures — and a leftover from another run is ``stale_report``); the
migration arm proves preservation by fingerprint AND the new
constraint (not just a version field), with a destructive upgrade
caught; the redelivery arm holds the exactly-once effect invariant
under duplicates and the crash-before-ack replay (the labeled
reference harness — the real RabbitMQ arm runs in the container-gated
test and in the recorded qualification run); the executor binding
composes the VerificationExecutor's OWN isolation classes (allowlist
scrub, narrowed PATH, the #308 five-outcome probe taxonomy with the
dependency reachability as the positive control); and the
honest-unavailable paths RECORD themselves — a missing dotnet SDK is
``dotnet-sdk-unavailable`` with every report missing, never a
synthetic green — while every disposable container is stopped AND
removed.
"""

from __future__ import annotations

import json
import shutil
import socket
import stat
import tempfile
from pathlib import Path

import pytest

from forge.adaptive.dotnet_recipe import (
    ARM_DIGEST_MISMATCH,
    ARM_IMAGE_UNAVAILABLE,
    ARM_REAL,
    ARM_REFERENCE,
    ARM_SDK_UNAVAILABLE,
    DOTNET_RECIPE_MANIFEST_SCHEMA,
    DOTNET_RECIPE_RUN_SCHEMA,
    REAL_COVERAGE_LABEL,
    AmqpClient,
    DedupConsumer,
    DotnetRecipeManifest,
    ManifestError,
    PinnedImage,
    PinnedPackage,
    PinnedTestProject,
    _AmqpWriter,
    _frame,
    _exactly_once,
    dependency_control_probe,
    environment_digest_of,
    expected_reports_for,
    fixture_source_digest,
    load_pinned_manifest,
    run_migration_arm_sqlite,
    run_recipe,
    run_redelivery_arm_reference,
    run_test_projects,
    write_identity_sidecar,
)
from forge.adaptive.qualification import (
    REPORT_FAILED,
    REPORT_MISSING,
    REPORT_PASSED,
    REPORT_STALE,
    reconcile_reports,
)
from forge.adaptive.verification_executor import (
    OUTCOME_AUTHORIZED_CONTROL_SUCCEEDED,
    OUTCOME_UNAVAILABLE,
    REFERENCE_COVERAGE,
)
from forge.runs.spec import canonical_json_digest

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "evaluation/tested_world/dotnet-orders"
MANIFEST_PATH = REPO / "qualification/recipes/dotnet-service-dependency/manifest.json"

_CONTAINER_RUNTIME = shutil.which("docker") or shutil.which("podman")
_DOTNET = shutil.which("dotnet")

requires_runtime = pytest.mark.skipif(
    _CONTAINER_RUNTIME is None, reason="no container runtime (docker/podman) on this machine"
)


# -- helpers -------------------------------------------------------------------


def _synthetic_manifest(**overrides: object) -> DotnetRecipeManifest:
    """A minimal valid manifest for the validation tests."""
    base = dict(
        recipe_id="synthetic/1",
        fixture_root="fixture",
        sdk_version="10.0.200",
        target_framework="net10.0",
        packages=(PinnedPackage(name="xunit", version="2.9.3"),),
        images=(
            PinnedImage(
                role="orders-db",
                image="docker.io/library/postgres:16",
                digest="sha256:" + "a" * 64,
            ),
        ),
        test_projects=(
            PinnedTestProject(
                name="Api.Tests",
                framework="net10.0",
                project="tests/Api.Tests.csproj",
                report="forge_Api.Tests_net10.0.trx",
            ),
            PinnedTestProject(
                name="Projection.Tests",
                framework="net10.0",
                project="tests/Projection.Tests.csproj",
                report="forge_Projection.Tests_net10.0.trx",
            ),
        ),
        migration_baseline="migrations/001.sql",
        migration_upgrade_postgres="migrations/002.postgres.sql",
        migration_upgrade_sqlite="migrations/002.sqlite.sql",
        preserved_columns=("id", "total", "created_at"),
        broker_queue_prefix="forge.recipe.orders",
        duplicates_per_message=2,
        seed_rows=5,
        redelivery_messages=3,
    )
    base.update(overrides)
    return DotnetRecipeManifest(**base)  # type: ignore[arg-type]


def _write_trx(path: Path, *, total: int, executed: int, passed: int, failed: int) -> None:
    """A minimal TRX with the real TeamTest namespace + counters (the
    exact shape the qualification parser reads)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<TestRun id="00000000-0000-4000-8000-000000000001"'
        ' name="synthetic"'
        ' xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010">\n'
        '  <ResultSummary outcome="Completed">\n'
        f'    <Counters total="{total}" executed="{executed}" passed="{passed}"'
        f' failed="{failed}" error="0" timeout="0" aborted="0" inconclusive="0" />\n'
        "  </ResultSummary>\n"
        "</TestRun>\n",
        encoding="utf-8",
    )


def _write_reports(
    results_dir: Path,
    *,
    candidate_id: str,
    bundle_digest: str,
    reports: dict[str, tuple[int, int, int, int] | None],
) -> None:
    """Write synthetic TRX files + identity sidecars; a None counter
    tuple means 'this project produced no report this run'."""
    results_dir.mkdir(parents=True, exist_ok=True)
    for report, counters in reports.items():
        if counters is None:
            continue
        _write_trx(
            results_dir / report,
            total=counters[0],
            executed=counters[1],
            passed=counters[2],
            failed=counters[3],
        )
        write_identity_sidecar(
            results_dir, report, candidate_id=candidate_id, bundle_digest=bundle_digest
        )


def _fake_dotnet_script(path: Path, *, sdk: str = "10.0.200") -> Path:
    """A deterministic offline fake of the dotnet CLI: answers --version
    and, for ``test``, writes the canned TRX the test wants (a FAILING
    report + exit 1 for the Projection project — the failing-second
    scenario) into the requested results directory."""
    script = path / "fake-dotnet"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        f"if args[:1] == ['--version']:\n"
        f"    print({sdk!r})\n"
        "    raise SystemExit(0)\n"
        "report = next(a.split('LogFileName=')[1] for a in args"
        " if a.startswith('trx;LogFileName='))\n"
        "results = Path(next(args[i + 1] for i, a in enumerate(args)"
        " if a == '--results-directory'))\n"
        "results.mkdir(parents=True, exist_ok=True)\n"
        "project = next(a for a in args if a.endswith('.csproj'))\n"
        "if 'Projection' in project:\n"
        "    total, executed, passed, failed, exit_code = 4, 4, 3, 1, 1\n"
        "else:\n"
        "    total, executed, passed, failed, exit_code = 3, 3, 3, 0, 0\n"
        "report_path = results / report\n"
        "report_path.write_text(\n"
        '    \'<?xml version="1.0" encoding="utf-8"?>\\n\'\n'
        '    \'<TestRun id="00000000-0000-4000-8000-00000000000f"'
        ' name="fake" xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010">\\n\'\n'
        "    '  <ResultSummary outcome=\"Completed\">\\n'\n"
        '    f\'    <Counters total="{total}" executed="{executed}"'
        ' passed="{passed}" failed="{failed}" error="0" timeout="0"'
        ' aborted="0" inconclusive="0" />\\n\'\n'
        "    '  </ResultSummary>\\n'\n"
        "    '</TestRun>\\n', encoding='utf-8')\n"
        "raise SystemExit(exit_code)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


class TestPinnedManifest:
    """The pinned manifest is a frozen, reproducible contract."""

    def test_the_pinned_manifest_loads_and_validates(self):
        manifest = load_pinned_manifest(MANIFEST_PATH)
        assert manifest.recipe_id == "dotnet-service-dependency/1"
        assert manifest.sdk_version and manifest.target_framework
        assert len(manifest.test_projects) == 2
        assert {project.name for project in manifest.test_projects} == {
            "DotnetOrders.Api.Tests",
            "DotnetOrders.Projection.Tests",
        }
        roles = {image.role for image in manifest.images}
        assert {"orders-db", "orders-bus"} <= roles
        for image in manifest.images:
            assert image.digest.startswith("sha256:") and len(image.digest) == 7 + 64
        # the pinned test projects exist in the frozen fixture
        for project in manifest.test_projects:
            assert (FIXTURE / project.project).is_file(), project.project

    def test_manifest_digest_is_content_bound(self):
        manifest = _synthetic_manifest()
        other = _synthetic_manifest(seed_rows=manifest.seed_rows + 1)
        assert manifest.manifest_digest != other.manifest_digest
        # and the digest is the canonical-JSON digest of the body
        assert manifest.manifest_digest == canonical_json_digest(manifest.body_document)

    def test_floating_package_pins_are_refused(self):
        with pytest.raises(ManifestError, match="floating"):
            _synthetic_manifest(packages=(PinnedPackage(name="xunit", version="*"),))
        with pytest.raises(ManifestError, match="floating"):
            _synthetic_manifest(packages=(PinnedPackage(name="xunit", version="9.*"),))
        with pytest.raises(ManifestError, match="floating"):
            _synthetic_manifest(packages=(PinnedPackage(name="xunit", version="latest"),))

    def test_mutable_image_pins_are_refused(self):
        bad_digest = "sha256:" + "z" * 64  # not hex
        with pytest.raises(ManifestError):
            _synthetic_manifest(
                images=(PinnedImage(role="db", image="postgres:16", digest=bad_digest),)
            )
        with pytest.raises(ManifestError):
            _synthetic_manifest(
                images=(PinnedImage(role="db", image="postgres:16", digest="latest"),)
            )

    def test_duplicate_reports_or_names_are_refused(self):
        duplicate = (
            PinnedTestProject(
                name="A", framework="net10.0", project="a.csproj", report="forge_A.trx"
            ),
            PinnedTestProject(
                name="B", framework="net10.0", project="b.csproj", report="forge_A.trx"
            ),
        )
        with pytest.raises(ManifestError, match="one report path and one name"):
            _synthetic_manifest(test_projects=duplicate)

    def test_foreign_schema_is_refused(self):
        with pytest.raises(ManifestError, match="unknown manifest schema"):
            DotnetRecipeManifest.from_document({"schema": "other/1"})

    def test_a_changed_baseline_image_moves_the_environment_digest(self):
        """The negative: the baseline image changed without a source
        change — the recorded verification must no longer apply."""
        base = environment_digest_of(
            manifest_digest="m1", sdk_version="10.0.200", image_digests={"orders-db": "d1"}
        )
        moved = environment_digest_of(
            manifest_digest="m1", sdk_version="10.0.200", image_digests={"orders-db": "d2"}
        )
        assert base != moved
        # unchanged world: identical digest
        same = environment_digest_of(
            manifest_digest="m1", sdk_version="10.0.200", image_digests={"orders-db": "d1"}
        )
        assert base == same

    def test_fixture_source_digest_binds_every_source_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp)
            (fixture / "src").mkdir()
            (fixture / "src/A.cs").write_text("class A {}", encoding="utf-8")
            (fixture / "src/B.csproj").write_text("<Project />", encoding="utf-8")
            (fixture / "src/obj").mkdir()
            (fixture / "src/obj/C.cs").write_text("build noise", encoding="utf-8")
            first = fixture_source_digest(fixture)
            # bin/obj noise never moves the digest
            (fixture / "src/obj/C.cs").write_text("changed noise", encoding="utf-8")
            assert fixture_source_digest(fixture) == first
            # a real source edit does
            (fixture / "src/A.cs").write_text("class A { changed }", encoding="utf-8")
            assert fixture_source_digest(fixture) != first


class TestTrxInventory:
    """The #226 reconciliation over synthetic TRX fixtures — every
    required project judged independently."""

    def _manifest(self) -> DotnetRecipeManifest:
        return load_pinned_manifest(MANIFEST_PATH)

    def _reconcile(self, results_dir: Path, reports: dict) -> dict:
        manifest = self._manifest()
        expected = expected_reports_for(manifest, candidate_id="cand-1", bundle_digest="bundle-1")
        _write_reports(
            results_dir, candidate_id="cand-1", bundle_digest="bundle-1", reports=reports
        )
        return reconcile_reports(expected, results_dir).to_document()

    def test_both_projects_pass_green(self, tmp_path):
        document = self._reconcile(
            tmp_path,
            {
                "forge_DotnetOrders.Api.Tests_net10.0.trx": (3, 3, 3, 0),
                "forge_DotnetOrders.Projection.Tests_net10.0.trx": (6, 6, 6, 0),
            },
        )
        assert document["aggregate"] == "all_passed"
        assert document["problems"] == []

    def test_a_failing_second_project_never_hides_behind_a_passing_first(self, tmp_path):
        document = self._reconcile(
            tmp_path,
            {
                "forge_DotnetOrders.Api.Tests_net10.0.trx": (3, 3, 3, 0),
                "forge_DotnetOrders.Projection.Tests_net10.0.trx": (6, 6, 5, 1),
            },
        )
        assert document["aggregate"] == "problems"
        verdicts = {v["test_project"]: v["verdict"] for v in document["verdicts"]}
        assert verdicts["DotnetOrders.Api.Tests"] == REPORT_PASSED
        assert verdicts["DotnetOrders.Projection.Tests"] == REPORT_FAILED
        assert any("1 failing test" in problem for problem in document["problems"])

    def test_an_omitted_trx_is_missing_report_never_zero_failures(self, tmp_path):
        document = self._reconcile(
            tmp_path,
            {"forge_DotnetOrders.Api.Tests_net10.0.trx": (3, 3, 3, 0)},
        )
        assert document["aggregate"] == "problems"
        verdicts = {v["test_project"]: v["verdict"] for v in document["verdicts"]}
        assert verdicts["DotnetOrders.Projection.Tests"] == REPORT_MISSING
        assert any("missing_report" in problem for problem in document["problems"])

    def test_an_old_leftover_report_is_stale(self, tmp_path):
        manifest = self._manifest()
        expected = expected_reports_for(manifest, candidate_id="cand-2", bundle_digest="bundle-2")
        # a report bound to a DIFFERENT run identity (a leftover)
        _write_reports(
            tmp_path,
            candidate_id="cand-OLD",
            bundle_digest="bundle-OLD",
            reports={
                "forge_DotnetOrders.Api.Tests_net10.0.trx": (3, 3, 3, 0),
                "forge_DotnetOrders.Projection.Tests_net10.0.trx": (6, 6, 6, 0),
            },
        )
        document = reconcile_reports(expected, tmp_path).to_document()
        assert document["aggregate"] == "problems"
        assert all(verdict["verdict"] == REPORT_STALE for verdict in document["verdicts"])
        # and a sidecar-less report is a leftover too
        (tmp_path / "forge_DotnetOrders.Api.Tests_net10.0.trx.identity.json").unlink()
        document = reconcile_reports(expected, tmp_path).to_document()
        verdicts = {v["test_project"]: v["verdict"] for v in document["verdicts"]}
        assert verdicts["DotnetOrders.Api.Tests"] == REPORT_STALE

    def test_a_report_that_executed_zero_tests_proves_nothing(self, tmp_path):
        document = self._reconcile(
            tmp_path,
            {
                "forge_DotnetOrders.Api.Tests_net10.0.trx": (0, 0, 0, 0),
                "forge_DotnetOrders.Projection.Tests_net10.0.trx": (6, 6, 6, 0),
            },
        )
        verdicts = {v["test_project"]: v["verdict"] for v in document["verdicts"]}
        assert verdicts["DotnetOrders.Api.Tests"] == REPORT_FAILED

    def test_the_expected_reports_freeze_carries_the_manifest_digest(self, tmp_path):
        manifest = self._manifest()
        expected = expected_reports_for(manifest, candidate_id="cand-1", bundle_digest="bundle-1")
        from forge.adaptive.qualification import freeze_report_inventory

        frozen = freeze_report_inventory(expected, contract_digest=manifest.manifest_digest)
        assert frozen["contract_digest"] == manifest.manifest_digest
        assert len(frozen["reports"]) == len(manifest.test_projects)


class TestTrxArmThroughTheRunner:
    """``run_test_projects`` end to end against a deterministic fake
    dotnet CLI (offline) — and the honest SDK-unavailable path."""

    def _with_fake_dotnet(self, tmp_path, monkeypatch, *, sdk="10.0.200"):
        fake = _fake_dotnet_script(tmp_path, sdk=sdk)
        real_which = shutil.which

        def fake_which(name, *args, **kwargs):
            return str(fake) if name == "dotnet" else real_which(name, *args, **kwargs)

        monkeypatch.setattr("forge.adaptive.dotnet_recipe.shutil.which", fake_which)

    def test_the_failing_second_project_surfaces_through_the_runner(self, tmp_path, monkeypatch):
        from forge.adaptive.dotnet_recipe import CommandRun

        self._with_fake_dotnet(tmp_path, monkeypatch)
        manifest = load_pinned_manifest(MANIFEST_PATH)
        commands: list[CommandRun] = []
        arm = run_test_projects(
            manifest,
            FIXTURE,
            tmp_path / "results",
            candidate_id="cand-1",
            bundle_digest="bundle-1",
            commands=commands,
        )
        assert arm["status"] == ARM_REAL
        assert arm["sdk_observed"] == "10.0.200"
        exits = {project["project"]: project["exit_code"] for project in arm["projects"]}
        assert exits["DotnetOrders.Api.Tests"] == 0
        assert exits["DotnetOrders.Projection.Tests"] == 1  # the failing second
        reconciliation = arm["report_reconciliation"]
        assert reconciliation["aggregate"] == "problems"
        verdicts = {v["test_project"]: v["verdict"] for v in reconciliation["verdicts"]}
        assert verdicts["DotnetOrders.Api.Tests"] == REPORT_PASSED
        assert verdicts["DotnetOrders.Projection.Tests"] == REPORT_FAILED

    def test_a_missing_sdk_is_recorded_never_faked_green(self, tmp_path, monkeypatch):
        monkeypatch.setattr("forge.adaptive.dotnet_recipe.shutil.which", lambda name: None)
        from forge.adaptive.dotnet_recipe import CommandRun

        manifest = load_pinned_manifest(MANIFEST_PATH)
        commands: list[CommandRun] = []
        arm = run_test_projects(
            manifest,
            FIXTURE,
            tmp_path / "results",
            candidate_id="cand-1",
            bundle_digest="bundle-1",
            commands=commands,
        )
        assert arm["status"] == ARM_SDK_UNAVAILABLE
        assert arm["projects"] == []
        verdicts = {v["verdict"] for v in arm["report_reconciliation"]["verdicts"]}
        assert verdicts == {REPORT_MISSING}  # every report missing — never zero failures
        assert arm["report_reconciliation"]["aggregate"] == "problems"


class TestMigrationArm:
    """The sqlite reference dialect of the N-1 -> N upgrade:
    preservation by fingerprint, the constraint probe, the backfill."""

    def test_the_upgrade_preserves_and_enforces(self, tmp_path):
        manifest = load_pinned_manifest(MANIFEST_PATH)
        arm = run_migration_arm_sqlite(manifest, FIXTURE, tmp_path / "m.db")
        assert arm["status"] == ARM_REFERENCE
        assert arm["coverage"] == REFERENCE_COVERAGE
        assert arm["preserved"] is True
        assert arm["baseline_fingerprint"]["rows"] == manifest.seed_rows
        assert arm["baseline_fingerprint"] == arm["after_fingerprint"]
        assert arm["constraint_probe"]["pre_upgrade_negative_insert"] == "accepted"
        assert arm["constraint_probe"]["post_upgrade_negative_insert"] == "rejected"
        assert arm["region_backfill_complete"] is True
        assert "preserved every seeded row" in arm["detail"]

    def test_a_destructive_upgrade_is_caught(self, tmp_path):
        """A migration that loses rows is NOT preservation — the
        fingerprints disagree and the arm says so."""
        manifest = load_pinned_manifest(MANIFEST_PATH)
        broken_fixture = tmp_path / "fixture"
        (broken_fixture / "migrations").mkdir(parents=True)
        shutil.copy(
            FIXTURE / manifest.migration_baseline, broken_fixture / manifest.migration_baseline
        )
        (broken_fixture / manifest.migration_upgrade_sqlite).write_text(
            (FIXTURE / manifest.migration_upgrade_sqlite).read_text(encoding="utf-8")
            + "\nDELETE FROM orders WHERE id = 'seed-0001';\n",
            encoding="utf-8",
        )
        arm = run_migration_arm_sqlite(manifest, broken_fixture, tmp_path / "m.db")
        assert arm["preserved"] is False
        assert arm["baseline_fingerprint"]["rows"] == manifest.seed_rows
        assert arm["after_fingerprint"]["rows"] == manifest.seed_rows - 1
        assert "did NOT satisfy" in arm["detail"]


class TestRedeliveryReferenceArm:
    """The labeled reference harness: exactly-once side effects under
    duplicates + the crash-before-ack replay, asserted from the DB."""

    def test_exactly_once_under_duplicates_and_redelivery(self, tmp_path):
        manifest = load_pinned_manifest(MANIFEST_PATH)
        arm = run_redelivery_arm_reference(manifest, tmp_path / "r.db")
        assert arm["status"] == ARM_REFERENCE
        assert arm["coverage"] == REFERENCE_COVERAGE
        assert arm["exactly_once_all"] is True
        assert arm["crash_before_ack_redelivered"] is True
        # every message saw the duplicate deliveries AND one effect row
        for message_id, entry in arm["effects_per_message"].items():
            assert entry["deliveries_seen"] >= manifest.duplicates_per_message
            assert entry["effect"].startswith(f"projection:{message_id}")
        # nothing claimed a real broker
        assert arm["broker"] == "reference-in-process"
        assert REFERENCE_COVERAGE in arm["detail"] or "reference" in arm["detail"].lower()

    def test_effects_without_duplicates_do_not_prove_idempotency(self):
        """The invariant itself: a world where every message arrived
        exactly once proves NOTHING about idempotency."""
        single_delivery = {
            f"order-{i:04d}": {"effect": f"e{i}", "deliveries_seen": 1} for i in range(1, 4)
        }
        assert _exactly_once(single_delivery, 3) is False
        with_duplicates = {
            key: {**value, "deliveries_seen": 2} for key, value in single_delivery.items()
        }
        assert _exactly_once(with_duplicates, 3) is True

    def test_a_duplicate_publish_produces_one_durable_row(self, tmp_path):
        consumer = DedupConsumer(tmp_path / "d.db")
        message = {"id": "order-0001", "total": "10", "region": "emea"}
        first = consumer.apply(message, redelivered=False, acked=True)
        crash = consumer.apply(message, redelivered=True, acked=True)
        duplicate = consumer.apply(message, redelivered=False, acked=True)
        effects = consumer.effects()
        consumer.close()
        assert (first, crash, duplicate) == ("applied", "duplicate", "duplicate")
        assert list(effects) == ["order-0001"]
        assert effects["order-0001"]["deliveries_seen"] == 3


class TestAmqpWireFormat:
    """Deterministic byte-level pins of the minimal AMQP 0-9-1 client —
    the exact bytes the real-broker handshake depends on."""

    def test_field_tables_carry_byte_length_not_entry_count(self):
        writer = _AmqpWriter()
        writer.table({"product": "forge"})
        payload = bytes(writer.buf)
        # u32 length prefix covers the entries that follow
        length = int.from_bytes(payload[:4], "big")
        assert length == len(payload) - 4

    def test_frames_carry_type_channel_size_and_end_marker(self):
        frame = _frame(1, 7, b"abc")
        assert frame == b"\x01\x00\x07\x00\x00\x00\x03abc\xce"

    def test_publish_header_sets_the_delivery_mode_property_flag(self):
        # bit 12 (0x1000) is delivery_mode — 0x8000 is content-type and
        # crashed a real rabbit (the pinned lesson from the lab run).
        writer = _AmqpWriter()
        writer.u16(0x1000)
        writer.u8(2)
        assert bytes(writer.buf) == b"\x10\x00\x02"

    def test_the_client_exists_for_the_real_arm(self):
        assert AmqpClient is not None


class TestExecutorBinding:
    """The recipe composes the VerificationExecutor's OWN isolation
    machinery — same classes, same scrub, same probe taxonomy."""

    def test_the_enforcement_profile_is_the_executors_shape(self):
        from forge.adaptive.dotnet_recipe import recipe_enforcement_profile

        profile = recipe_enforcement_profile(extra_env_keys=["NUGET_PACKAGES"])
        document = profile.as_document()
        assert document["env_allowlist"]  # the executor's allowlist, intersected
        assert "NUGET_PACKAGES" in document["extra_env_keys"]
        assert document["path_policy"] == "system-minimum/1"
        # every kept PATH entry exists and is the system minimum + tool dirs
        for entry in document["path_entries"]:
            assert Path(entry).exists()

    def test_the_scrub_drops_credential_shaped_keys(self, monkeypatch):
        from forge.adaptive.dotnet_recipe import recipe_scrubbed_env

        monkeypatch.setenv("FORGE_TEST_MODEL_KEY", "sk-not-a-real-key")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("RECIPE_EXTRA", "kept")
        env = recipe_scrubbed_env({"RECIPE_EXTRA": "kept"})
        assert "FORGE_TEST_MODEL_KEY" not in env
        assert "HTTPS_PROXY" not in env
        assert env["RECIPE_EXTRA"] == "kept"

    def test_dependency_reachability_is_the_five_outcome_positive_control(self):
        # a LIVE listener is the control-succeeded outcome
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            attempt = dependency_control_probe("orders-db", "127.0.0.1", port)
        assert attempt.outcome == OUTCOME_AUTHORIZED_CONTROL_SUCCEEDED
        assert attempt.status_code is None
        # a dead port is UNAVAILABLE — it demonstrated nothing
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead_port = probe.getsockname()[1]
        attempt = dependency_control_probe("orders-db", "127.0.0.1", dead_port)
        assert attempt.outcome == OUTCOME_UNAVAILABLE
        assert "not coverage" in attempt.detail


class TestHonestUnavailablePaths:
    """A full recipe run with NO SDK and NO runtime: every unavailable
    arm records its typed status; nothing turns green."""

    def test_run_recipe_without_sdk_or_runtime_is_honestly_not_verifying(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("forge.adaptive.dotnet_recipe.shutil.which", lambda name: None)
        monkeypatch.setattr("forge.adaptive.dotnet_recipe.container_runtime_command", lambda: None)
        run = run_recipe(
            load_pinned_manifest(MANIFEST_PATH),
            workspace=tmp_path / "run",
            root=REPO,
            enforce_isolation=False,
        )
        document = run.to_document()
        assert document["schema"] == DOTNET_RECIPE_RUN_SCHEMA
        assert document["arms"]["build_test"]["status"] == ARM_SDK_UNAVAILABLE
        assert document["sdk"]["observed"] == ""
        assert document["sdk"]["matches"] is False
        verdicts = {
            v["test_project"]: v["verdict"]
            for v in document["arms"]["build_test"]["report_reconciliation"]["verdicts"]
        }
        assert set(verdicts.values()) == {REPORT_MISSING}
        assert document["arms"]["migration"]["status"] == ARM_REFERENCE
        assert document["arms"]["migration"]["coverage"] == REFERENCE_COVERAGE
        assert document["arms"]["redelivery"]["status"] == ARM_REFERENCE
        assert document["arms"]["redelivery"]["coverage"] == REFERENCE_COVERAGE
        assert run.verifies is False
        assert run.real_coverage_complete is False
        assert any("no dotnet SDK" in problem for problem in run.problems)
        # the honest statuses are named, never buried
        degradations = " ".join(document["degradations"])
        assert "no container runtime" in degradations
        assert "REFERENCE" in degradations
        # the run document binds its world and grants nothing
        assert document["environment_digest"]
        assert document["enforcement_profile_digest"]
        assert "grants neither merge nor deployment" in document["authority"]
        # the run document round-trips
        from forge.adaptive.dotnet_recipe import DotnetRecipeRun

        restored = DotnetRecipeRun.from_document(json.loads(json.dumps(document)))
        assert restored.manifest.manifest_digest == run.manifest.manifest_digest
        assert restored.verifies is False

    def test_the_run_document_records_the_container_status_vocabulary(self, tmp_path):
        """The typed container statuses exist and are distinct — the
        honest vocabulary the lab run reports through."""
        for status in (
            ARM_REAL,
            ARM_REFERENCE,
            ARM_SDK_UNAVAILABLE,
            ARM_IMAGE_UNAVAILABLE,
            ARM_DIGEST_MISMATCH,
        ):
            assert status and isinstance(status, str)
        assert REAL_COVERAGE_LABEL == "real-dependency"
        assert REFERENCE_COVERAGE == "reference-coverage"
        assert REAL_COVERAGE_LABEL != REFERENCE_COVERAGE


@requires_runtime
class TestRealDependencyArms:
    """The REAL arms against the pulled dependency images (skipped on a
    machine with no container runtime): the postgres migration upgrade
    inside the container and the RabbitMQ redelivery — disposable
    containers, stopped AND removed on every path."""

    def test_postgres_migration_and_rabbitmq_redelivery(self, tmp_path):
        from forge.adaptive.dotnet_recipe import (
            DependencyContainer,
            run_migration_arm_postgres,
            run_redelivery_arm_real,
        )

        manifest = load_pinned_manifest(MANIFEST_PATH)
        runtime = _CONTAINER_RUNTIME
        run_id = "t309"
        pg = DependencyContainer(
            runtime=runtime,
            pinned=manifest.image_for_role("orders-db"),
            name=f"forge-dotnet-recipe-test-pg-{run_id}",
        )
        rabbit = DependencyContainer(
            runtime=runtime,
            pinned=manifest.image_for_role("orders-bus"),
            name=f"forge-dotnet-recipe-test-rabbit-{run_id}",
        )
        with pg, rabbit:
            pg_status = pg.start(
                {
                    "POSTGRES_USER": "forge",
                    "POSTGRES_PASSWORD": "forge-recipe-local",
                    "POSTGRES_DB": "orders",
                },
                (5432,),
            )
            assert pg_status == ""
            assert pg.wait_postgres_ready()
            migration = run_migration_arm_postgres(manifest, FIXTURE, pg)
            assert migration["status"] == ARM_REAL
            assert migration["coverage"] == REAL_COVERAGE_LABEL
            assert migration["preserved"] is True
            assert migration["constraint_probe"]["post_upgrade_negative_insert"] == "rejected"

            rabbit_status = rabbit.start(
                {
                    "RABBITMQ_DEFAULT_USER": "forge",
                    "RABBITMQ_DEFAULT_PASS": "forge-recipe-local",
                },
                (5672,),
            )
            assert rabbit_status == ""
            from forge.adaptive.dotnet_recipe import _wait_amqp_ready

            _attempt, ready = _wait_amqp_ready(rabbit)
            assert ready, "the rabbitmq container never answered the AMQP handshake"
            redelivery = run_redelivery_arm_real(
                manifest,
                host="127.0.0.1",
                port=rabbit.port,
                db_path=tmp_path / "redelivery.db",
                candidate_id=run_id,
                image_digest=rabbit.resolved_digest,
            )
            assert redelivery["status"] == ARM_REAL
            assert redelivery["coverage"] == REAL_COVERAGE_LABEL
            assert redelivery["crash_before_ack_redelivered"] is True
            assert redelivery["exactly_once_all"] is True
        # the context managers stopped AND removed both containers
        for name in (pg.name, rabbit.name):
            listed = subprocess_run([runtime, "ps", "-a", "--format", "{{.Names}}"])
            assert name not in listed

    def test_the_pinned_digest_actually_pulls(self):
        import subprocess

        pinned = load_pinned_manifest(MANIFEST_PATH).image_for_role("orders-db")
        completed = subprocess.run(
            [_CONTAINER_RUNTIME, "pull", f"{pinned.image}@{pinned.digest}"],
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert completed.returncode == 0, completed.stderr[-400:]


def subprocess_run(argv: list[str]) -> str:
    import subprocess

    return subprocess.run(argv, capture_output=True, text=True, timeout=60).stdout


def test_the_frozen_fixture_carries_the_compat_negative():
    """The old/new producer-consumer negative combination is pinned in
    the fixture's OWN acceptance tests (executed under the pinned SDK
    in the recorded recipe run; here we pin that the frozen source
    actually carries it)."""
    projection_tests = (
        FIXTURE / "tests/DotnetOrders.Projection.Tests/ProjectionTests.cs"
    ).read_text(encoding="utf-8")
    assert "V1OnlyGate" in projection_tests
    assert "RejectionReason" in projection_tests
    contract = (FIXTURE / "src/DotnetOrders.Common/Contract.cs").read_text(encoding="utf-8")
    assert "dialect v1 does not admit the field 'region'" in contract


def test_the_manifest_document_is_loadable_from_disk_shape():
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert document["schema"] == DOTNET_RECIPE_MANIFEST_SCHEMA
    manifest = DotnetRecipeManifest.from_document(document)
    assert manifest.as_document()["manifest_digest"] == manifest.manifest_digest
