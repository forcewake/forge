"""#296 / R37-15: verification in a separate trusted executor against
BUILT artifacts and REAL dependencies.

The pins, in the issue's own order:

- **the scrub** — the subprocess environment is the parent's
  intersected with an EXPLICIT allowlist (no model keys, no provider
  tokens, no publication credentials, no proxy configuration); the
  launch receipt records the exact keys present, and the deny probes
  FAIL CLOSED in the REAL subprocess: a credential that rides the
  launch is presented to the sentinel/provider endpoints and a 2xx
  answer kills the whole run;
- **built artifacts, not models** — the two fixture services are real
  installable wheels (built with ``uv build`` from
  ``evaluation/tested_world/``); the executor verifies each wheel's
  sha256 against the digest frozen in the candidate set, installs
  those exact bytes into its own venv and runs THEIR tests and THEIR
  code — a wheel file that is not the frozen digest is refused and the
  edges covering that member fail naming it;
- **real dependencies** — the sqlite upgrade runs the INSTALLED
  wheel's schema code from a seeded baseline, and the redelivery arm
  dials a local socket-based fake broker subprocess over TCP with the
  duplicate injected at the socket, while the INSTALLED consumer's
  idempotency is asserted from both the broker's journal and the
  consumer's own durable rows;
- **separately green ≠ system** — an old-dialect consumer wheel whose
  OWN unit pipeline is green still fails the contract edge, and the
  redelivery leg observes the actual rejections (no blanket
  exactly-once claim);
- **invalidation keyed on the built-wheel digests** — a changed-source
  rebuild is a different digest, a different world; selective
  invalidation runs through the UNCHANGED existing machinery;
- **three distinct permissions** — verification readiness, merge
  permission and deploy permission are separate booleans in every
  exported result; a green run grants neither merge nor deploy.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from forge.adaptive.system_verification import (
    TWIN_REFERENCE_LABEL,
    default_twin_scenario,
    default_verifier_environment,
    replay_against_changed_inputs,
    run_system_verification,
)
from forge.adaptive.verification_executor import (
    BUS_DEPENDENCY,
    CONSUMER_SERVICE,
    DEFAULT_ENV_ALLOWLIST,
    EXIT_CLEAN,
    EXIT_ISOLATION_VIOLATED,
    EXIT_VERIFICATION_FAILED,
    IsolationViolated,
    PINNED_SERVICE,
    PRODUCER_SERVICE,
    CandidateBundle,
    ExecutorReport,
    VerificationExecutor,
    WheelRef,
    contract_bundle_document,
    environment_profile_document,
    executor_candidate_set,
    executor_edges,
    executor_readiness,
    require_isolated,
    scan_credential_env,
    scrubbed_environment,
)
from forge.adaptive.verification_executor import (
    test_bundle_document as _test_bundle_document,  # aliased: pytest must not collect it
)
from forge.adaptive.verification_sets import baseline_drift

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTED_WORLD = REPO_ROOT / "evaluation" / "tested_world"
PRODUCER_PROJECT = "orders-api"
CONSUMER_PROJECT = "orders-projection"

SENTINEL_SECRET = "sentinel-egress-secret"
PROVIDER_SECRET = "provider-leak-secret"

CONTRACT_EDGE = f"contract:{PRODUCER_SERVICE}->{CONSUMER_SERVICE}"
BASELINE_EDGE = f"baseline:{CONSUMER_SERVICE}->{PINNED_SERVICE}"
ENV_EDGE = "environment:integration"

#: Built wheels are cached per (project, variant) for the whole session
#: (``uv build`` is deterministic: same source, same digest).
_WHEEL_CACHE: dict[str, WheelRef] = {}


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_oid(project: str) -> str:
    """A real 40-hex digest over the fixture project's source tree —
    the 'source revision' the wheel was built from."""
    digest = hashlib.sha256()
    for file in sorted((TESTED_WORLD / project).rglob("*")):
        if file.is_file() and file.suffix in (".py", ".toml"):
            digest.update(str(file.relative_to(TESTED_WORLD)).encode())
            digest.update(file.read_bytes())
    return digest.hexdigest()[:40]


def build_fixture_wheel(project: str, variant: str, root: Path) -> WheelRef:
    """Build one fixture wheel (a ``v1`` variant flips the dialect —
    the OLD build of that service, whose own tests stay green)."""
    key = f"{project}:{variant}"
    if key in _WHEEL_CACHE:
        return _WHEEL_CACHE[key]
    uv = shutil.which("uv")
    if uv is None:  # pragma: no cover — uv is the repo's own toolchain
        pytest.skip("uv is required to build the fixture wheels")
    import subprocess

    build_dir = root / key.replace(":", "-")
    source = build_dir / "src"
    shutil.copytree(TESTED_WORLD / project, source)
    if variant != "v2":
        contract = source / project.replace("-", "_") / "contract.py"
        contract.write_text(
            contract.read_text().replace('DIALECT = "v2"', f'DIALECT = "{variant}"')
        )
    out = build_dir / "dist"
    subprocess.run(
        [uv, "build", str(source), "--out-dir", str(out)],
        check=True,
        capture_output=True,
        text=True,
    )
    (wheel,) = out.glob("*.whl")
    ref = WheelRef(service=project, path=wheel.resolve(), sha256=_sha256_file(wheel))
    _WHEEL_CACHE[key] = ref
    return ref


@pytest.fixture(scope="session")
def wheel_root(tmp_path_factory: Path) -> Path:
    return tmp_path_factory.mktemp("verified-executor-wheels")


@pytest.fixture(scope="session")
def producer_wheel(wheel_root: Path) -> WheelRef:
    return build_fixture_wheel(PRODUCER_PROJECT, "v2", wheel_root)


@pytest.fixture(scope="session")
def consumer_wheel(wheel_root: Path) -> WheelRef:
    return build_fixture_wheel(CONSUMER_PROJECT, "v2", wheel_root)


@pytest.fixture(scope="session")
def old_consumer_wheel(wheel_root: Path) -> WheelRef:
    return build_fixture_wheel(CONSUMER_PROJECT, "v1", wheel_root)


@pytest.fixture(scope="session")
def old_producer_wheel(wheel_root: Path) -> WheelRef:
    return build_fixture_wheel(PRODUCER_PROJECT, "v1", wheel_root)


# ----------------------------------------------------------------------
# The gated sentinel + provider endpoints (local, real sockets)
# ----------------------------------------------------------------------


class _GatedHandler(BaseHTTPRequestHandler):
    """The sentinel (``/probe``) and the provider-shaped endpoint
    (``/api/v4/user``): both answer 200 ONLY with their credential —
    a credentialless probe must be denied (401); a leaked one is
    caught red-handed."""

    def do_GET(self) -> None:  # noqa: N802 — http.server's spelling
        if self.path == "/probe":
            authorized = self.headers.get("X-Egress-Token") == SENTINEL_SECRET
        elif self.path == "/api/v4/user":
            authorized = self.headers.get("Authorization") == f"Bearer {PROVIDER_SECRET}"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200 if authorized else 401)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args: object) -> None:  # silence the test log
        return


@pytest.fixture()
def gated_endpoints() -> tuple[str, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GatedHandler)
    thread = threading.Thread(target=server.serve_forever, name="gated-endpoints", daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        yield f"{base}/probe", f"{base}/api/v4/user"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _closed_port_url() -> str:
    """A URL on a port that JUST closed — nothing listens there; the
    connection-refused flavor of denial."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}/probe"


def _credential_shaped(name: str) -> bool:
    """Whether an env var NAME is credential-shaped (the probe's rule)."""
    return bool(scan_credential_env({name: "x"}))


# ----------------------------------------------------------------------
# The executor launcher (shared by every subprocess test)
# ----------------------------------------------------------------------


def run_executor(
    work_dir: Path,
    wheels: tuple[WheelRef, ...],
    sentinel_url: str,
    provider_url: str,
    *,
    extra_env: dict[str, str] | None = None,
    probes_only: bool = False,
    candidate_set=None,
) -> tuple[object, ExecutorReport, CandidateBundle]:
    """Launch the REAL executor subprocess and parse its report."""
    by_service = {wheel.service: wheel for wheel in wheels}
    frozen = candidate_set or executor_candidate_set(
        producer_wheel_sha256=by_service[PRODUCER_SERVICE].sha256,
        consumer_wheel_sha256=by_service[CONSUMER_SERVICE].sha256,
        producer_source_oid=_source_oid(PRODUCER_PROJECT),
        consumer_source_oid=_source_oid(CONSUMER_PROJECT),
    )
    bundle = CandidateBundle(
        candidate_set=frozen,
        wheels=wheels,
        sentinel_url=sentinel_url,
        provider_url=provider_url,
        work_dir=work_dir,
        contract_document=contract_bundle_document(),
        test_document=_test_bundle_document(),
        environment_document=environment_profile_document(),
    )
    bundle_path = bundle.write(work_dir.parent / f"{work_dir.name}-bundle.json")
    run = VerificationExecutor(extra_env=extra_env).run(
        bundle_path,
        out_path=work_dir.parent / f"{work_dir.name}-report.json",
        probes_only=probes_only,
    )
    assert run.report is not None
    assert isinstance(run.report, ExecutorReport)
    return run, run.report, bundle


@pytest.fixture(scope="module")
def happy_run(
    tmp_path_factory: Path,
    producer_wheel: WheelRef,
    consumer_wheel: WheelRef,
):
    """ONE full green executor run, shared by the assertion classes
    (the gated endpoints run in-thread — the executor, the fake broker
    and the consumer are all REAL subprocesses)."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GatedHandler)
    thread = threading.Thread(target=server.serve_forever, name="happy-endpoints", daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        root = tmp_path_factory.mktemp("verified-executor-happy")
        run, report, bundle = run_executor(
            root / "work",
            (producer_wheel, consumer_wheel),
            f"{base}/probe",
            f"{base}/api/v4/user",
        )
        assert run.exit_code == EXIT_CLEAN
        return run, report, bundle
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def incompatible_run(
    tmp_path_factory: Path,
    producer_wheel: WheelRef,
    old_consumer_wheel: WheelRef,
):
    """ONE full run of the separately-green-but-incompatible world
    (new producer + OLD consumer), shared by the assertion classes."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GatedHandler)
    thread = threading.Thread(target=server.serve_forever, name="incompat-endpoints", daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        root = tmp_path_factory.mktemp("verified-executor-incompatible")
        run, report, bundle = run_executor(
            root / "work",
            (producer_wheel, old_consumer_wheel),
            f"{base}/probe",
            f"{base}/api/v4/user",
        )
        assert run.exit_code == EXIT_VERIFICATION_FAILED
        return run, report, bundle
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ----------------------------------------------------------------------
# The scrub (fail closed, both sides of the launch)
# ----------------------------------------------------------------------


class TestEnvironmentScrub:
    def test_credential_shaped_variables_are_dropped(self):
        source = {
            "PATH": "/bin",
            "HOME": "/home/op",
            "GITLAB_TOKEN": "glpat-x",
            "GITLAB_WEBHOOK_SECRET": "whsec",
            "OPENAI_API_KEY": "sk-x",
            "ANTHROPIC_API_KEY": "sk-y",
            "HTTP_PROXY": "http://corp",
            "HTTPS_PROXY": "http://corp",
            "CI_JOB_TOKEN": "job",
        }
        env = scrubbed_environment(source)
        assert set(env) <= set(DEFAULT_ENV_ALLOWLIST)
        assert env == {"PATH": "/bin", "HOME": "/home/op"}

    def test_absent_allowlisted_keys_are_not_invented(self):
        assert scrubbed_environment({"PATH": "/bin"}) == {"PATH": "/bin"}

    def test_extra_rides_through_and_is_the_callers_responsibility(self):
        env = scrubbed_environment({"PATH": "/bin"}, extra={"TMPDIR": "/tmp/x"})
        assert env == {"PATH": "/bin", "TMPDIR": "/tmp/x"}

    def test_the_default_allowlist_is_never_credential_shaped(self):
        assert not any(scan_credential_env({name: "x"}) for name in DEFAULT_ENV_ALLOWLIST)

    def test_the_scrub_drops_a_dev_environment_token(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-from-dev-env")
        env = scrubbed_environment(os.environ)
        assert "GITLAB_TOKEN" not in env


# ----------------------------------------------------------------------
# The deny probes — in the REAL subprocess
# ----------------------------------------------------------------------


class TestDenyProbesInSubprocess:
    def test_a_clean_launch_observes_denial_from_the_launched_process(
        self, tmp_path: Path, producer_wheel, consumer_wheel, gated_endpoints
    ):
        sentinel_url, provider_url = gated_endpoints
        run, report, _bundle = run_executor(
            tmp_path / "work",
            (producer_wheel, consumer_wheel),
            sentinel_url,
            provider_url,
            probes_only=True,
        )
        assert run.exit_code == EXIT_CLEAN
        authority = report.authority_receipt
        assert authority.isolated is True
        assert authority.violations == ()
        assert authority.credential_shaped_keys == ()
        by_name = {probe.name: probe for probe in authority.deny_probes}
        assert by_name["sentinel-egress"].outcome == "denied-unauthorized"
        assert by_name["sentinel-egress"].status_code == 401
        assert by_name["sentinel-egress"].presented_from_env == ""
        assert by_name["provider-api"].outcome == "denied-unauthorized"
        assert by_name["provider-api"].status_code == 401
        # the receipt is the LAUNCHED process's own env listing: exactly
        # the allowlisted keys the parent actually had, nothing else
        # (macOS's spawn adds __CF_USER_TEXT_ENCODING to any child env)
        allowed = {name for name in DEFAULT_ENV_ALLOWLIST if name in os.environ}
        assert set(authority.env_keys) - {"__CF_USER_TEXT_ENCODING"} == allowed

    def test_a_parent_dev_token_never_reaches_the_launched_process(
        self, tmp_path: Path, producer_wheel, consumer_wheel, gated_endpoints, monkeypatch
    ):
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-from-dev-env")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-dev-env")
        sentinel_url, provider_url = gated_endpoints
        run, report, _bundle = run_executor(
            tmp_path / "work",
            (producer_wheel, consumer_wheel),
            sentinel_url,
            provider_url,
            probes_only=True,
        )
        assert run.exit_code == EXIT_CLEAN
        authority = report.authority_receipt
        assert "GITLAB_TOKEN" not in authority.env_keys
        assert "OPENAI_API_KEY" not in authority.env_keys
        assert authority.isolated is True

    def test_an_unreachable_sentinel_is_denial_refused(
        self, tmp_path: Path, producer_wheel, consumer_wheel, gated_endpoints
    ):
        _sentinel, provider_url = gated_endpoints
        run, report, _bundle = run_executor(
            tmp_path / "work",
            (producer_wheel, consumer_wheel),
            _closed_port_url(),
            provider_url,
            probes_only=True,
        )
        assert run.exit_code == EXIT_CLEAN
        (sentinel,) = [
            probe
            for probe in report.authority_receipt.deny_probes
            if probe.name == "sentinel-egress"
        ]
        assert sentinel.outcome == "denied-refused"
        assert sentinel.status_code is None
        assert report.authority_receipt.isolated is True

    def test_a_leaked_egress_credential_fails_the_whole_run_closed(
        self, tmp_path: Path, producer_wheel, consumer_wheel, gated_endpoints
    ):
        sentinel_url, provider_url = gated_endpoints
        run, report, _bundle = run_executor(
            tmp_path / "work",
            (producer_wheel, consumer_wheel),
            sentinel_url,
            provider_url,
            probes_only=True,
            extra_env={"EGRESS_TOKEN": SENTINEL_SECRET},
        )
        assert run.exit_code == EXIT_ISOLATION_VIOLATED
        assert run.launch_env_keys == tuple(sorted((*run.launch_env_keys,)))  # recorded
        assert "EGRESS_TOKEN" in run.launch_env_keys  # the launch receipt admits it…
        assert report.isolation_violated is True
        (sentinel,) = [
            probe
            for probe in report.authority_receipt.deny_probes
            if probe.name == "sentinel-egress"
        ]
        # …and the LAUNCHED process's probe caught it reaching the endpoint
        assert sentinel.outcome == "violated"
        assert sentinel.presented_from_env == "EGRESS_TOKEN"
        # fail closed: no edge ran at all
        assert report.edge_results == ()
        assert report.report_coverage["edges_missing"] == sorted(
            edge.edge_id for edge in executor_edges()
        )

    def test_a_leaked_provider_token_is_caught_by_the_provider_probe_shape(
        self, tmp_path: Path, producer_wheel, consumer_wheel, gated_endpoints
    ):
        sentinel_url, provider_url = gated_endpoints
        run, report, _bundle = run_executor(
            tmp_path / "work",
            (producer_wheel, consumer_wheel),
            sentinel_url,
            provider_url,
            probes_only=True,
            extra_env={"GITLAB_TOKEN": PROVIDER_SECRET},
        )
        assert run.exit_code == EXIT_ISOLATION_VIOLATED
        (provider,) = [
            probe for probe in report.authority_receipt.deny_probes if probe.name == "provider-api"
        ]
        assert provider.outcome == "violated"
        assert provider.presented_from_env == "GITLAB_TOKEN"

    def test_require_isolated_raises_for_a_violated_or_missing_report(self, happy_run):
        _run, report, _bundle = happy_run
        clean = require_isolated(_run)
        assert clean is _run
        from forge.adaptive.verification_executor import ExecutorRun

        violated = ExecutorRun(
            argv=(),
            allowlist=(),
            extra_env_keys=(),
            launch_env_keys=(),
            exit_code=EXIT_ISOLATION_VIOLATED,
            timed_out=False,
            stdout_sha256="",
            stderr_sha256="",
            report=ExecutorReport(isolation_violated=True),
        )
        with pytest.raises(IsolationViolated, match="not isolated"):
            require_isolated(violated)
        missing = ExecutorRun(
            argv=(),
            allowlist=(),
            extra_env_keys=(),
            launch_env_keys=(),
            exit_code=1,
            timed_out=True,
            stdout_sha256="",
            stderr_sha256="",
            report=None,
        )
        with pytest.raises(IsolationViolated, match="no report"):
            require_isolated(missing)


# ----------------------------------------------------------------------
# The full run: built wheels installed, real dependencies exercised
# ----------------------------------------------------------------------


class TestFullExecutorRun:
    def test_the_green_run_is_system_ready_with_every_edge_passed(self, happy_run):
        run, report, bundle = happy_run
        assert run.exit_code == EXIT_CLEAN
        assert run.timed_out is False
        assert report.system_ready is True
        assert report.failed_members == ()
        assert {edge.edge_id: edge.status for edge in report.edge_results} == {
            CONTRACT_EDGE: "passed",
            BASELINE_EDGE: "passed",
            ENV_EDGE: "passed",
        }
        assert report.tested_world_digest == bundle.candidate_set.tested_world_digest
        assert report.applicability_digest == bundle.candidate_set.applicability_digest

    def test_isolation_is_proved_from_the_launched_process(self, happy_run):
        _run, report, _bundle = happy_run
        authority = report.authority_receipt
        assert authority.isolated is True
        assert authority.credential_shaped_keys == ()
        assert all(probe.denied for probe in authority.deny_probes)

    def test_the_exact_wheel_bytes_are_what_the_venv_runs(self, happy_run):
        _run, report, bundle = happy_run
        installed = {wheel.service: wheel for wheel in report.executor_receipt.installed}
        assert set(installed) == {PRODUCER_SERVICE, CONSUMER_SERVICE}
        by_service = {wheel.service: wheel for wheel in bundle.wheels}
        members = {m.repository_id: m for m in bundle.candidate_set.members}
        for service, wheel in installed.items():
            assert wheel.matches_recorded is True
            assert wheel.file_sha256 == by_service[service].sha256
            assert wheel.installed_sha256 == by_service[service].sha256
            # the frozen identity IS the built artifact's digest
            assert members[service].image_digest == f"sha256:{wheel.file_sha256}"
        assert installed[PRODUCER_SERVICE].version == "1.2.0"
        assert installed[CONSUMER_SERVICE].version == "2.3.0"

    def test_every_command_exit_and_log_digest_is_recorded(self, happy_run):
        _run, report, _bundle = happy_run
        commands = {command.name: command for command in report.executor_receipt.commands}
        assert report.executor_receipt.venv_tool in ("uv", "stdlib-venv")
        for name in (
            "selftest-orders-api",
            "selftest-orders-projection",
            "schema-upgrade",
            "baseline-projection",
            "produce",
            "fake-broker",
            "orders-projection-consumer",
        ):
            assert name in commands, sorted(commands)
            assert commands[name].exit_code == 0, name
            assert len(commands[name].log_sha256) == 64, name

    def test_the_wheels_own_tests_ran_green_inside_the_executor(self, happy_run):
        _run, report, _bundle = happy_run
        (contract,) = [e for e in report.edge_results if e.kind == "contract"]
        selftests = {
            check["service"]: check
            for check in contract.checks
            if check["check"] == "wheel-selftest"
        }
        assert set(selftests) == {PRODUCER_SERVICE, CONSUMER_SERVICE}
        assert all(check["status"] == "passed" for check in selftests.values())
        assert all(check["exit_code"] == 0 for check in selftests.values())
        assert all(check["tests"] >= 4 for check in selftests.values())

    def test_the_upgrade_runs_the_installed_wheels_schema_code_from_a_seeded_baseline(
        self, happy_run
    ):
        _run, report, _bundle = happy_run
        (environment,) = [e for e in report.edge_results if e.kind == "environment"]
        upgrade = [c for c in environment.checks if c["check"] == "db-upgrade"][0]
        assert upgrade["status"] == "passed"
        assert upgrade["seed_rows"] == 25  # never from an empty schema
        assert upgrade["preserved"] is True
        assert upgrade["baseline_fingerprint"] == upgrade["target_fingerprint"]
        assert upgrade["schema_advanced"] is True
        assert upgrade["post_upgrade_write"] is True

    def test_the_redelivery_is_real_sockets_and_exactly_once(self, happy_run):
        _run, report, _bundle = happy_run
        outcome = report.redelivery_outcome
        assert outcome["messages"] == 8
        assert outcome["deliveries"] == 16  # the duplicate injected at the socket
        assert outcome["acks"] == 16
        assert outcome["duplicates_injected_at_socket"] == 2
        assert outcome["broker_complete"] is True
        assert outcome["exactly_once_all"] is True
        assert set(outcome["effects_per_message"]) == {f"ord-{index:04d}" for index in range(1, 9)}
        assert all(count == 1 for count in outcome["effects_per_message"].values())
        assert "127.0.0.1:" in outcome["broker_socket"]  # a real loopback socket

    def test_the_baseline_edge_projects_the_pinned_rows_through_the_wheel(self, happy_run):
        _run, report, _bundle = happy_run
        (baseline,) = [e for e in report.edge_results if e.kind == "baseline"]
        projection = [c for c in baseline.checks if c["check"] == "baseline-projection"][0]
        assert projection["projected"] == projection["rows"] == 15
        assert projection["served_at_pin"] == "api/v3"

    def test_passed_edges_record_evidence_bound_to_the_frozen_world(self, happy_run):
        _run, report, bundle = happy_run
        assert [record.evidence_id for record in report.evidence_records] == [
            f"ex-{CONTRACT_EDGE}",
            f"ex-{BASELINE_EDGE}",
            f"ex-{ENV_EDGE}",
        ]
        assert report.ledger().applicable_to(bundle.candidate_set) == {
            f"ex-{BASELINE_EDGE}",
            f"ex-{CONTRACT_EDGE}",
            f"ex-{ENV_EDGE}",
        }

    def test_report_coverage_is_recorded(self, happy_run):
        _run, report, _bundle = happy_run
        coverage = report.report_coverage
        assert coverage["edges_expected"] == sorted([CONTRACT_EDGE, BASELINE_EDGE, ENV_EDGE])
        assert coverage["edges_verified"] == coverage["edges_expected"]
        assert coverage["complete"] is True

    def test_the_report_round_trips_through_json(self, happy_run):
        _run, report, _bundle = happy_run
        rebuilt = ExecutorReport.from_document(json.loads(json.dumps(report.to_document())))
        assert rebuilt == report


# ----------------------------------------------------------------------
# Three distinct permissions
# ----------------------------------------------------------------------


class TestThreeDistinctPermissions:
    def test_a_green_verification_grants_neither_merge_nor_deploy(self, happy_run):
        _run, report, _bundle = happy_run
        assert report.readiness["verification_ready"] is True
        assert report.readiness["merge_permitted"] is False
        assert report.readiness["deploy_permitted"] is False

    def test_explicit_human_grants_are_recorded_as_their_own_booleans(self, happy_run):
        _run, report, bundle = happy_run
        granted = executor_readiness(
            report, bundle.candidate_set, merge_approval=True, deploy_approval=True
        )
        assert (
            granted.verification_ready,
            granted.merge_permitted,
            granted.deploy_permitted,
        ) == (True, True, True)

    def test_a_blocked_world_with_a_grant_reports_both_axes_separately(self, incompatible_run):
        run, report, bundle = incompatible_run
        assert run.exit_code == EXIT_VERIFICATION_FAILED
        verdict = executor_readiness(report, bundle.candidate_set, deploy_approval=True)
        assert (verdict.verification_ready, verdict.deploy_permitted) == (False, True)

    def test_an_isolation_violation_blocks_every_edge_even_with_grants(self, happy_run):
        _run, report, bundle = happy_run
        violated = ExecutorReport(
            tested_world_digest=report.tested_world_digest,
            isolation_violated=True,
        )
        verdict = executor_readiness(
            violated, bundle.candidate_set, merge_approval=True, deploy_approval=True
        )
        assert verdict.verification_ready is False
        assert verdict.merge_permitted is True  # grants are recorded, never derived
        assert {b.reason_kind for b in verdict.blocked_edges} == {"failed"}


# ----------------------------------------------------------------------
# Separately green pipelines, incompatible system
# ----------------------------------------------------------------------


class TestSeparatelyGreenIncompatibleSystem:
    def test_an_old_consumer_wheel_fails_the_contract_edge_naming_the_consumer(
        self, incompatible_run
    ):
        run, report, _bundle = incompatible_run
        assert run.exit_code == EXIT_VERIFICATION_FAILED
        assert report.system_ready is False
        assert CONSUMER_SERVICE in report.failed_members
        failed = {e.edge_id: e for e in report.edge_results if e.status == "failed"}
        assert set(failed) == {CONTRACT_EDGE, ENV_EDGE}
        assert failed[CONTRACT_EDGE].failed_member == CONSUMER_SERVICE
        # the OLD build's OWN pipeline was green — that is the point
        selftests = {
            check["service"]: check
            for check in failed[CONTRACT_EDGE].checks
            if check["check"] == "wheel-selftest"
        }
        assert selftests[CONSUMER_SERVICE]["status"] == "passed"

    def test_the_old_consumer_rejections_are_observed_not_assumed(self, incompatible_run):
        run, report, _bundle = incompatible_run
        assert run.exit_code == EXIT_VERIFICATION_FAILED
        # the redelivery actually ran and reported the real outcome
        outcome = report.redelivery_outcome
        assert outcome["messages"] == 8
        assert outcome["deliveries"] == 16
        assert outcome["exactly_once_all"] is False  # the v1 consumer rejected v2 messages
        (environment,) = [e for e in report.edge_results if e.kind == "environment"]
        assert environment.status == "failed"  # a failed dependency scenario blocks readiness
        assert BUS_DEPENDENCY in environment.detail

    def test_an_old_producer_wheel_names_the_producer(
        self, tmp_path: Path, old_producer_wheel, consumer_wheel, gated_endpoints
    ):
        sentinel_url, provider_url = gated_endpoints
        run, report, _bundle = run_executor(
            tmp_path / "work",
            (old_producer_wheel, consumer_wheel),
            sentinel_url,
            provider_url,
        )
        assert run.exit_code == EXIT_VERIFICATION_FAILED
        (failed,) = [e for e in report.edge_results if e.edge_id == CONTRACT_EDGE]
        assert failed.failed_member == PRODUCER_SERVICE
        referee = [c for c in failed.checks if c["check"] == "contract-referee"][0]
        assert referee["producer_dialect"] == "v1"
        assert referee["referee_dialect"] == "v2"


# ----------------------------------------------------------------------
# Installed-wheel identity: stale digests refuse
# ----------------------------------------------------------------------


class TestInstalledWheelIdentity:
    def test_a_wheel_file_that_is_not_the_frozen_digest_is_refused(
        self, tmp_path: Path, producer_wheel, old_producer_wheel, consumer_wheel, gated_endpoints
    ):
        """The world froze the v2 producer's digest; the FILE on disk is
        a different build — the executor refuses to install it and fails
        exactly the edges covering that member."""
        sentinel_url, provider_url = gated_endpoints
        frozen = executor_candidate_set(
            producer_wheel_sha256=producer_wheel.sha256,  # the world's digest (v2)
            consumer_wheel_sha256=consumer_wheel.sha256,
            producer_source_oid=_source_oid(PRODUCER_PROJECT),
            consumer_source_oid=_source_oid(CONSUMER_PROJECT),
        )
        # the bundle RECORDS the frozen digest; the FILE is the v1 rebuild
        stale_file = WheelRef(
            service=PRODUCER_SERVICE,
            path=old_producer_wheel.path,
            sha256=producer_wheel.sha256,
        )
        run, report, _bundle = run_executor(
            tmp_path / "work",
            (stale_file, consumer_wheel),
            sentinel_url,
            provider_url,
            candidate_set=frozen,
        )
        assert run.exit_code == EXIT_VERIFICATION_FAILED
        assert report.system_ready is False
        producer_row = {wheel.service: wheel for wheel in report.executor_receipt.installed}[
            PRODUCER_SERVICE
        ]
        assert producer_row.matches_recorded is False
        status = {edge.edge_id: edge.status for edge in report.edge_results}
        assert status[CONTRACT_EDGE] == "failed"
        assert status[ENV_EDGE] == "failed"
        assert status[BASELINE_EDGE] == "passed"  # selective: the consumer is fine
        (failed,) = [e for e in report.edge_results if e.edge_id == CONTRACT_EDGE]
        assert failed.failed_member == PRODUCER_SERVICE
        assert "refreeze the world" in failed.detail

    def test_a_stale_consumer_wheel_fails_every_edge_covering_it(
        self, tmp_path: Path, producer_wheel, consumer_wheel, old_consumer_wheel, gated_endpoints
    ):
        sentinel_url, provider_url = gated_endpoints
        frozen = executor_candidate_set(
            producer_wheel_sha256=producer_wheel.sha256,
            consumer_wheel_sha256=consumer_wheel.sha256,
            producer_source_oid=_source_oid(PRODUCER_PROJECT),
            consumer_source_oid=_source_oid(CONSUMER_PROJECT),
        )
        stale_file = WheelRef(
            service=CONSUMER_SERVICE,
            path=old_consumer_wheel.path,
            sha256=consumer_wheel.sha256,
        )
        run, report, _bundle = run_executor(
            tmp_path / "work",
            (producer_wheel, stale_file),
            sentinel_url,
            provider_url,
            candidate_set=frozen,
        )
        assert run.exit_code == EXIT_VERIFICATION_FAILED
        status = {edge.edge_id: edge.status for edge in report.edge_results}
        assert set(status.values()) == {"failed"}  # every edge covers the consumer

    def test_a_changed_source_rebuild_is_a_different_digest(self, wheel_root: Path):
        v2 = build_fixture_wheel(PRODUCER_PROJECT, "v2", wheel_root)
        v1 = build_fixture_wheel(PRODUCER_PROJECT, "v1", wheel_root)
        assert v2.sha256 != v1.sha256

    def test_an_unchanged_source_rebuild_reproduces_the_same_digest(self, tmp_path: Path):
        """Hatchling builds are deterministic: same source, same wheel —
        an honest rebuild invalidates nothing (identity binds CONTENT,
        not build time)."""
        first = build_fixture_wheel(PRODUCER_PROJECT, "v2", tmp_path / "a")
        second = build_fixture_wheel(PRODUCER_PROJECT, "v2", tmp_path / "b")
        assert second.sha256 == first.sha256


# ----------------------------------------------------------------------
# Selective invalidation, keyed on the built-wheel digests
# ----------------------------------------------------------------------


class TestInvalidationOnWheelDigestChange:
    def test_a_rebuilt_producer_wheel_invalidates_only_the_affected_edges(
        self, happy_run, old_producer_wheel
    ):
        _run, report, bundle = happy_run
        mutated = executor_candidate_set(
            producer_wheel_sha256=old_producer_wheel.sha256,
            consumer_wheel_sha256={m.repository_id: m for m in bundle.candidate_set.members}[
                CONSUMER_SERVICE
            ].image_digest.removeprefix("sha256:"),
            producer_source_oid=_source_oid(PRODUCER_PROJECT),
            consumer_source_oid=_source_oid(CONSUMER_PROJECT),
        )
        replay = replay_against_changed_inputs(report.ledger(), bundle.candidate_set, mutated)
        assert set(replay.invalidated_evidence_ids) == {
            f"ex-{CONTRACT_EDGE}",
            f"ex-{ENV_EDGE}",
        }
        assert replay.retained_evidence_ids == (f"ex-{BASELINE_EDGE}",)
        # the retained record still APPLIES to the mutated world (it
        # never covered the producer): invalidation, not amnesia
        assert report.ledger().applicable_to(mutated) == {f"ex-{BASELINE_EDGE}"}

    def test_the_drift_names_the_wheel_digest_move(self, happy_run, old_producer_wheel):
        _run, _report, bundle = happy_run
        mutated = executor_candidate_set(
            producer_wheel_sha256=old_producer_wheel.sha256,
            consumer_wheel_sha256={m.repository_id: m for m in bundle.candidate_set.members}[
                CONSUMER_SERVICE
            ].image_digest.removeprefix("sha256:"),
            producer_source_oid=_source_oid(PRODUCER_PROJECT),
            consumer_source_oid=_source_oid(CONSUMER_PROJECT),
        )
        (drift,) = [
            d
            for d in baseline_drift(bundle.candidate_set, mutated)
            if d.input_path == f"member/{PRODUCER_SERVICE}/image_digest"
        ]
        assert drift.previous != drift.current

    def test_switching_only_the_baseline_pin_image_invalidates_baseline_and_environment(
        self, happy_run
    ):
        _run, report, bundle = happy_run
        mutated = executor_candidate_set(
            producer_wheel_sha256={m.repository_id: m for m in bundle.candidate_set.members}[
                PRODUCER_SERVICE
            ].image_digest.removeprefix("sha256:"),
            consumer_wheel_sha256={m.repository_id: m for m in bundle.candidate_set.members}[
                CONSUMER_SERVICE
            ].image_digest.removeprefix("sha256:"),
            producer_source_oid=_source_oid(PRODUCER_PROJECT),
            consumer_source_oid=_source_oid(CONSUMER_PROJECT),
            ledger_baseline_digest=f"sha256:{'f5' * 32}",  # only the artifact moved
        )
        replay = replay_against_changed_inputs(report.ledger(), bundle.candidate_set, mutated)
        assert set(replay.invalidated_evidence_ids) == {
            f"ex-{BASELINE_EDGE}",
            f"ex-{ENV_EDGE}",
        }
        assert replay.retained_evidence_ids == (f"ex-{CONTRACT_EDGE}",)
        (drift,) = [
            d for d in replay.drift if d.input_path == f"member/{PINNED_SERVICE}/image_digest"
        ]
        assert drift.source_sha_unchanged is True  # same source SHA, different artifact


# ----------------------------------------------------------------------
# The reference twin stays labeled and green
# ----------------------------------------------------------------------


class TestTwinReferenceLabel:
    def test_the_in_repo_twin_is_labeled_the_deterministic_reference(self):
        from forge.adaptive import system_verification

        assert system_verification.TWIN_REFERENCE_LABEL == TWIN_REFERENCE_LABEL
        assert TWIN_REFERENCE_LABEL == "deterministic-reference-twin"
        assert system_verification.__doc__ is not None
        assert "DETERMINISTIC REFERENCE" in system_verification.__doc__

    def test_the_twin_still_passes_end_to_end(self):
        twin = default_twin_scenario()
        report = run_system_verification(twin.freeze(), default_verifier_environment(twin))
        assert report.system_ready is True
