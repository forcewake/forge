"""#296 / R37-15 + #307 / R38-07: verification in a separate trusted
executor against BUILT artifacts and REAL dependencies, with isolation
claims that match observable containment.

The pins, in the issues' own order:

- **the scrub** — the subprocess environment is the parent's
  intersected with an EXPLICIT allowlist (no model keys, no provider
  tokens, no publication credentials, no proxy configuration); the
  launch receipt records the exact keys present, and the controlled
  probes FAIL CLOSED in the REAL subprocess: an ungated endpoint (2xx
  to the synthetic probe credential) or any credential-shaped env
  survivor kills the whole run;
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

R38-07 (#308) pins — the isolation claims match the containment
actually observable:

- **the five-outcome taxonomy** — every controlled probe lands in
  ``authorized_control_succeeded`` / ``expected_denial_observed`` /
  ``unavailable`` / ``inconclusive`` / ``violation``; each outcome is
  reachable from REAL sockets, and the P04 shapes are pinned BOTH at
  the function level and in the REAL subprocess: a dead endpoint →
  ``unavailable`` (never a denial), 503 → ``unavailable``, timeout →
  ``inconclusive``, 401-with-control-green → the only denial that
  counts, 2xx-to-credential-material → ``violation`` (exit 3);
- **the positive-control gate** — each probe class first dials a
  no-credential 200 route on the same server; a control that is dead,
  missing or slow means the deny probe NEVER RUNS (``deny is None``)
  and the isolation is ``unproven`` (exit 5) — a 503/DNS failure
  alone can NEVER yield a pass;
- **clean HOME, narrowed PATH** — the child's HOME is an isolated
  empty home the launcher provisions; a planted credential file under
  the PARENT home (``.netrc``, ``.config/git/credentials`` shapes) is
  unreadable from the verifier, asserted by the launched process
  reading its OWN HOME shapes; the child PATH is the system minimum
  (a planted bin directory in the parent PATH never reaches it); a
  credential shape readable under the verifier's own HOME fails the
  run closed;
- **the enforcement-profile record** — the launcher records the
  isolation configuration (env allowlist version, HOME/PATH policy,
  network policy class) as ``verification.enforcement_profile_digest``,
  the launched process validates the declaration against what it
  observes, and the report carries THREE SEPARATE verdicts
  (environment hygiene / credential non-disclosure / network
  enforcement) plus the tri-state isolation — never one
  ``isolation: true``;
- **no first-surviving-key heuristics** — the probes present the
  explicit SYNTHETIC qualification credential only
  (``presented_from_env`` stays empty even when a credential-shaped
  key rides the launch); the env scan is the absence assertion whose
  survivor fails the run closed (PE-8d's teeth);
- **reference-coverage labels** — the fixture TCP-broker and sqlite
  legs are labeled ``reference-coverage`` in the frozen environment
  profile and the report (NOT production RabbitMQ/PostgreSQL proof).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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
    CREDENTIAL_HOME_SHAPES,
    CREDENTIAL_SOURCE_SYNTHETIC,
    CONSUMER_SERVICE,
    DB_DEPENDENCY,
    DEFAULT_ENV_ALLOWLIST,
    EXIT_CLEAN,
    EXIT_ISOLATION_UNPROVEN,
    EXIT_ISOLATION_VIOLATED,
    EXIT_VERIFICATION_FAILED,
    ISOLATION_PROVEN,
    ISOLATION_UNPROVEN,
    ISOLATION_VIOLATED,
    OUTCOME_AUTHORIZED_CONTROL_SUCCEEDED,
    OUTCOME_EXPECTED_DENIAL_OBSERVED,
    OUTCOME_INCONCLUSIVE,
    OUTCOME_UNAVAILABLE,
    OUTCOME_VIOLATION,
    PINNED_SERVICE,
    PRODUCER_SERVICE,
    AuthorityReceipt,
    CandidateBundle,
    EnforcementProfile,
    ExecutorReport,
    ExecutorRun,
    IsolationUnproven,
    IsolationViolated,
    ProbeTarget,
    SYSTEM_PATH_ENTRIES,
    VerificationExecutor,
    WheelRef,
    contract_bundle_document,
    environment_profile_document,
    executor_candidate_set,
    executor_edges,
    executor_readiness,
    require_isolated,
    run_deny_probe,
    run_positive_control,
    run_probe_class,
    scan_credential_env,
    scan_home_credentials,
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
# The gated endpoint server: one HTTP server, the probe routes + the
# positive-control route (R38-07)
# ----------------------------------------------------------------------


class _GatedHandler(BaseHTTPRequestHandler):
    """One HTTP server with every route the probe classes need:

    - ``/control`` — the POSITIVE CONTROL: 200 WITHOUT credentials
      (liveness; each class's deny probe runs only while this is
      green);
    - ``/probe`` — the sentinel class's gated route: 200 only with
      ``X-Egress-Token: SENTINEL_SECRET``, 401 otherwise;
    - ``/api/v4/user`` — the provider class's gated route: 200 only
      with ``Authorization: Bearer PROVIDER_SECRET``, 401 otherwise.

    Failure-shape knobs (class attributes, set by :func:`gated_server`):
    ``deny_status`` (answer ``/probe`` with this status instead of
    gating — the 503 shape), ``deny_delay`` (seconds to stall ONLY the
    gated routes — the timeout shape, the control stays fast) and
    ``accept_any`` (answer 200 to ANY presented credential — the
    ungated-endpoint violation shape)."""

    deny_status: int | None = None
    deny_delay: float = 0.0
    accept_any: bool = False

    def do_GET(self) -> None:  # noqa: N802 — http.server's spelling
        if self.path == "/control":
            self._answer(200)
            return
        if self.path == "/probe":
            if self.deny_delay:
                time.sleep(self.deny_delay)
            if self.deny_status is not None:
                self._answer(self.deny_status)
                return
            presented = self.headers.get("X-Egress-Token", "")
            authorized = presented == SENTINEL_SECRET or (self.accept_any and bool(presented))
            self._answer(200 if authorized else 401)
            return
        if self.path == "/api/v4/user":
            if self.deny_delay:
                time.sleep(self.deny_delay)
            if self.deny_status is not None:
                self._answer(self.deny_status)
                return
            presented = self.headers.get("Authorization", "")
            authorized = presented == f"Bearer {PROVIDER_SECRET}" or (
                self.accept_any and bool(presented)
            )
            self._answer(200 if authorized else 401)
            return
        self._answer(404)

    def _answer(self, status: int) -> None:
        self.send_response(status)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args: object) -> None:  # silence the test log
        return


@contextmanager
def gated_server(**knobs: Any) -> Iterator[tuple[str, str, str]]:
    """One gated HTTP server with the given failure-shape knobs; yields
    ``(sentinel_url, provider_url, control_url)`` — the two gated deny
    routes and the no-credential positive-control route."""
    handler = type("_ShapedGatedHandler", (_GatedHandler,), dict(knobs))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, name="gated-server", daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        yield f"{base}/probe", f"{base}/api/v4/user", f"{base}/control"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def gated_endpoints() -> Iterator[tuple[str, str, str]]:
    with gated_server() as urls:
        yield urls


def _closed_port_url() -> str:
    """A URL on a port that JUST closed — nothing listens there; the
    dead-endpoint shape (unreachability, not denial)."""
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


def _make_bundle(
    work_dir: Path,
    wheels: tuple[WheelRef, ...],
    sentinel_url: str,
    provider_url: str,
    *,
    sentinel_control_url: str = "",
    provider_control_url: str = "",
    probe_timeout: float = 10.0,
    candidate_set=None,
) -> CandidateBundle:
    by_service = {wheel.service: wheel for wheel in wheels}
    frozen = candidate_set or executor_candidate_set(
        producer_wheel_sha256=by_service[PRODUCER_SERVICE].sha256,
        consumer_wheel_sha256=by_service[CONSUMER_SERVICE].sha256,
        producer_source_oid=_source_oid(PRODUCER_PROJECT),
        consumer_source_oid=_source_oid(CONSUMER_PROJECT),
    )
    return CandidateBundle(
        candidate_set=frozen,
        wheels=wheels,
        sentinel_url=sentinel_url,
        provider_url=provider_url,
        work_dir=work_dir,
        sentinel_control_url=sentinel_control_url,
        provider_control_url=provider_control_url,
        probe_timeout=probe_timeout,
        contract_document=contract_bundle_document(),
        test_document=_test_bundle_document(),
        environment_document=environment_profile_document(),
    )


def run_executor(
    work_dir: Path,
    wheels: tuple[WheelRef, ...],
    sentinel_url: str,
    provider_url: str,
    *,
    sentinel_control_url: str = "",
    provider_control_url: str = "",
    probe_timeout: float = 10.0,
    extra_env: dict[str, str] | None = None,
    probes_only: bool = False,
    candidate_set=None,
) -> tuple[object, ExecutorReport, CandidateBundle]:
    """Launch the REAL executor subprocess and parse its report."""
    bundle = _make_bundle(
        work_dir,
        wheels,
        sentinel_url,
        provider_url,
        sentinel_control_url=sentinel_control_url,
        provider_control_url=provider_control_url,
        probe_timeout=probe_timeout,
        candidate_set=candidate_set,
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
    with gated_server() as (sentinel_url, provider_url, control_url):
        root = tmp_path_factory.mktemp("verified-executor-happy")
        run, report, bundle = run_executor(
            root / "work",
            (producer_wheel, consumer_wheel),
            sentinel_url,
            provider_url,
            sentinel_control_url=control_url,
            provider_control_url=control_url,
        )
        assert run.exit_code == EXIT_CLEAN
        return run, report, bundle


@pytest.fixture(scope="module")
def incompatible_run(
    tmp_path_factory: Path,
    producer_wheel: WheelRef,
    old_consumer_wheel: WheelRef,
):
    """ONE full run of the separately-green-but-incompatible world
    (new producer + OLD consumer), shared by the assertion classes."""
    with gated_server() as (sentinel_url, provider_url, control_url):
        root = tmp_path_factory.mktemp("verified-executor-incompatible")
        run, report, bundle = run_executor(
            root / "work",
            (producer_wheel, old_consumer_wheel),
            sentinel_url,
            provider_url,
            sentinel_control_url=control_url,
            provider_control_url=control_url,
        )
        assert run.exit_code == EXIT_VERIFICATION_FAILED
        return run, report, bundle


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
# The five-outcome taxonomy — each outcome reachable, from real
# sockets, at the function level (the P04 shapes)
# ----------------------------------------------------------------------


class TestProbeOutcomeTaxonomy:
    def test_authorized_control_succeeded_is_reachable(self):
        with gated_server() as (_sentinel, _provider, control_url):
            attempt = run_positive_control("sentinel-egress", control_url, timeout=5.0)
        assert attempt.outcome == OUTCOME_AUTHORIZED_CONTROL_SUCCEEDED
        assert attempt.status_code == 200

    def test_the_expected_denial_is_observed_only_with_the_control_green(self):
        with gated_server() as (sentinel_url, _provider, control_url):
            probe = run_probe_class(
                ProbeTarget("sentinel-egress", sentinel_url, control_url, "X-Egress-Token"),
                timeout=5.0,
            )
        assert probe.control.outcome == OUTCOME_AUTHORIZED_CONTROL_SUCCEEDED
        assert probe.deny is not None
        assert probe.deny.outcome == OUTCOME_EXPECTED_DENIAL_OBSERVED
        assert probe.deny.status_code == 401
        assert probe.outcome == OUTCOME_EXPECTED_DENIAL_OBSERVED
        assert probe.blocker == ""

    def test_a_denial_without_a_green_control_is_inconclusive(self):
        with gated_server() as (sentinel_url, _provider, _control):
            attempt = run_deny_probe(
                "sentinel-egress", sentinel_url, "X-Egress-Token", timeout=5.0, control_green=False
            )
        assert attempt.outcome == OUTCOME_INCONCLUSIVE
        assert attempt.status_code == 401

    def test_a_dead_endpoint_is_unavailable_and_claims_nothing(self):
        dead = _closed_port_url()
        probe = run_probe_class(
            ProbeTarget("sentinel-egress", dead, dead, "X-Egress-Token"), timeout=5.0
        )
        assert probe.control.outcome == OUTCOME_UNAVAILABLE
        assert probe.deny is None  # the deny probe never ran
        assert probe.outcome == OUTCOME_UNAVAILABLE
        assert "positive control" in probe.blocker

    def test_a_503_answer_is_unavailable_not_a_denial(self):
        with gated_server(deny_status=503) as (sentinel_url, _provider, control_url):
            probe = run_probe_class(
                ProbeTarget("sentinel-egress", sentinel_url, control_url, "X-Egress-Token"),
                timeout=5.0,
            )
        assert probe.control.outcome == OUTCOME_AUTHORIZED_CONTROL_SUCCEEDED
        assert probe.deny is not None
        assert probe.deny.outcome == OUTCOME_UNAVAILABLE
        assert probe.deny.status_code == 503
        assert probe.outcome == OUTCOME_UNAVAILABLE

    def test_a_timeout_is_inconclusive(self):
        with gated_server(deny_delay=3.0) as (sentinel_url, _provider, control_url):
            probe = run_probe_class(
                ProbeTarget("sentinel-egress", sentinel_url, control_url, "X-Egress-Token"),
                timeout=1.0,
            )
        assert probe.control.outcome == OUTCOME_AUTHORIZED_CONTROL_SUCCEEDED
        assert probe.deny is not None
        assert probe.deny.outcome == OUTCOME_INCONCLUSIVE
        assert probe.deny.status_code is None
        assert probe.outcome == OUTCOME_INCONCLUSIVE

    def test_a_2xx_answer_to_credential_material_is_a_violation(self):
        with gated_server(accept_any=True) as (sentinel_url, _provider, control_url):
            probe = run_probe_class(
                ProbeTarget("sentinel-egress", sentinel_url, control_url, "X-Egress-Token"),
                timeout=5.0,
            )
        assert probe.deny is not None
        assert probe.deny.outcome == OUTCOME_VIOLATION
        assert probe.deny.status_code == 200
        assert probe.outcome == OUTCOME_VIOLATION

    def test_the_probe_presents_the_synthetic_credential_never_an_ambient_key(self, monkeypatch):
        """No first-surviving-key heuristics: ambient credential-shaped
        keys in the surrounding environment NEVER become the presented
        credential — the explicit synthetic sentinel is presented."""
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-ambient")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-ambient")
        with gated_server() as (sentinel_url, _provider, control_url):
            probe = run_probe_class(
                ProbeTarget("sentinel-egress", sentinel_url, control_url, "X-Egress-Token"),
                timeout=5.0,
            )
        assert probe.credential_source == CREDENTIAL_SOURCE_SYNTHETIC
        assert probe.presented_from_env == ""
        assert probe.outcome == OUTCOME_EXPECTED_DENIAL_OBSERVED


# ----------------------------------------------------------------------
# The controlled probes — in the REAL subprocess
# ----------------------------------------------------------------------


class TestControlledProbesInSubprocess:
    def test_a_clean_launch_observes_the_expected_denial_with_the_control_green(
        self, tmp_path: Path, producer_wheel, consumer_wheel, gated_endpoints
    ):
        sentinel_url, provider_url, control_url = gated_endpoints
        run, report, _bundle = run_executor(
            tmp_path / "work",
            (producer_wheel, consumer_wheel),
            sentinel_url,
            provider_url,
            sentinel_control_url=control_url,
            provider_control_url=control_url,
            probes_only=True,
        )
        assert run.exit_code == EXIT_CLEAN
        authority = report.authority_receipt
        assert authority.isolation == ISOLATION_PROVEN
        assert authority.violations == ()
        assert authority.blockers == ()
        assert authority.credential_shaped_keys == ()
        assert authority.environment_hygiene == "passed"
        assert authority.credential_non_disclosure == "passed"
        assert authority.network_enforcement == "demonstrated"
        by_name = {probe.name: probe for probe in authority.probes}
        for name in ("sentinel-egress", "provider-api"):
            probe = by_name[name]
            assert probe.control.outcome == OUTCOME_AUTHORIZED_CONTROL_SUCCEEDED
            assert probe.deny is not None
            assert probe.deny.outcome == OUTCOME_EXPECTED_DENIAL_OBSERVED
            assert probe.deny.status_code in (401, 403, 407)
            assert probe.presented_from_env == ""  # never an ambient key
            assert probe.credential_source == CREDENTIAL_SOURCE_SYNTHETIC
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
        sentinel_url, provider_url, control_url = gated_endpoints
        run, report, _bundle = run_executor(
            tmp_path / "work",
            (producer_wheel, consumer_wheel),
            sentinel_url,
            provider_url,
            sentinel_control_url=control_url,
            provider_control_url=control_url,
            probes_only=True,
        )
        assert run.exit_code == EXIT_CLEAN
        authority = report.authority_receipt
        assert "GITLAB_TOKEN" not in authority.env_keys
        assert "OPENAI_API_KEY" not in authority.env_keys
        assert authority.isolation == ISOLATION_PROVEN

    def test_a_dead_endpoint_reports_unavailable_and_never_yields_a_pass(
        self, tmp_path: Path, producer_wheel, consumer_wheel, gated_endpoints
    ):
        """The P04 defect, pinned: unreachability is NOT a denial. One
        probe class dead (deny + control on a port that just closed)
        while the other is healthy still leaves the isolation unproven
        — never a pass."""
        _sentinel, provider_url, control_url = gated_endpoints
        dead = _closed_port_url()
        run, report, _bundle = run_executor(
            tmp_path / "work",
            (producer_wheel, consumer_wheel),
            dead,
            provider_url,
            sentinel_control_url=dead,
            provider_control_url=control_url,
            probes_only=True,
        )
        assert run.exit_code == EXIT_ISOLATION_UNPROVEN
        authority = report.authority_receipt
        assert authority.isolation == ISOLATION_UNPROVEN
        assert report.isolation_unproven is True
        assert report.isolation_violated is False
        assert authority.network_enforcement == ISOLATION_UNPROVEN
        by_name = {probe.name: probe for probe in authority.probes}
        assert by_name["sentinel-egress"].outcome == OUTCOME_UNAVAILABLE
        assert by_name["sentinel-egress"].deny is None  # no probe claimed
        assert by_name["provider-api"].outcome == OUTCOME_EXPECTED_DENIAL_OBSERVED
        assert any(
            "sentinel-egress" in blocker and "positive control" in blocker
            for blocker in authority.blockers
        )
        # nothing verified: no edges, no evidence, not ready
        assert report.edge_results == ()
        verdict = executor_readiness(report, _bundle.candidate_set)
        assert verdict.verification_ready is False

    def test_the_whole_sentinel_server_offline_never_turns_qualification_green(
        self, tmp_path: Path, producer_wheel, consumer_wheel
    ):
        """The issue's negative test 1: BOTH probe classes offline —
        the qualification must not turn green."""
        dead = _closed_port_url()
        run, report, _bundle = run_executor(
            tmp_path / "work",
            (producer_wheel, consumer_wheel),
            dead,
            dead,
            sentinel_control_url=dead,
            provider_control_url=dead,
            probes_only=True,
        )
        assert run.exit_code == EXIT_ISOLATION_UNPROVEN
        authority = report.authority_receipt
        assert authority.isolation == ISOLATION_UNPROVEN
        assert authority.violations == ()  # nothing leaked — just nothing proven
        assert all(
            probe.outcome == OUTCOME_UNAVAILABLE and probe.deny is None
            for probe in authority.probes
        )
        assert len(authority.blockers) == len(authority.probes)
        assert report.system_ready is False

    def test_a_503_sentinel_is_unavailable_and_blocks_the_pass(
        self, tmp_path: Path, producer_wheel, consumer_wheel
    ):
        """A failing service is not a denial: the control is green, the
        gated route answers 503, and the run still cannot pass."""
        with gated_server(deny_status=503) as (sentinel_url, provider_url, control_url):
            run, report, _bundle = run_executor(
                tmp_path / "work",
                (producer_wheel, consumer_wheel),
                sentinel_url,
                provider_url,
                sentinel_control_url=control_url,
                provider_control_url=control_url,
                probes_only=True,
            )
        assert run.exit_code == EXIT_ISOLATION_UNPROVEN
        authority = report.authority_receipt
        (sentinel,) = [p for p in authority.probes if p.name == "sentinel-egress"]
        assert sentinel.control.outcome == OUTCOME_AUTHORIZED_CONTROL_SUCCEEDED  # alive…
        assert sentinel.deny is not None
        assert sentinel.deny.outcome == OUTCOME_UNAVAILABLE  # …and STILL no pass
        assert sentinel.deny.status_code == 503
        assert authority.isolation == ISOLATION_UNPROVEN

    def test_a_slow_sentinel_is_inconclusive(self, tmp_path: Path, producer_wheel, consumer_wheel):
        with gated_server(deny_delay=3.0) as (sentinel_url, provider_url, control_url):
            run, report, _bundle = run_executor(
                tmp_path / "work",
                (producer_wheel, consumer_wheel),
                sentinel_url,
                provider_url,
                sentinel_control_url=control_url,
                provider_control_url=control_url,
                probe_timeout=1.0,
                probes_only=True,
            )
        assert run.exit_code == EXIT_ISOLATION_UNPROVEN
        authority = report.authority_receipt
        (sentinel,) = [p for p in authority.probes if p.name == "sentinel-egress"]
        assert sentinel.deny is not None
        assert sentinel.deny.outcome == OUTCOME_INCONCLUSIVE
        assert sentinel.deny.status_code is None
        assert authority.isolation == ISOLATION_UNPROVEN

    def test_an_ungated_sentinel_is_a_violation_and_fails_the_run_closed(
        self, tmp_path: Path, producer_wheel, consumer_wheel
    ):
        """The probes keep their teeth: an endpoint that answers 200 to
        the synthetic probe credential does not gate — credential
        material reached something, and the whole run dies closed."""
        with gated_server(accept_any=True) as (sentinel_url, provider_url, control_url):
            run, report, _bundle = run_executor(
                tmp_path / "work",
                (producer_wheel, consumer_wheel),
                sentinel_url,
                provider_url,
                sentinel_control_url=control_url,
                provider_control_url=control_url,
                probes_only=True,
            )
        assert run.exit_code == EXIT_ISOLATION_VIOLATED
        assert report.isolation_violated is True
        authority = report.authority_receipt
        assert authority.credential_non_disclosure == "violated"
        assert authority.isolation == ISOLATION_VIOLATED
        (sentinel,) = [p for p in authority.probes if p.name == "sentinel-egress"]
        assert sentinel.deny is not None
        assert sentinel.deny.outcome == OUTCOME_VIOLATION
        assert sentinel.deny.status_code == 200
        assert report.edge_results == ()
        assert report.report_coverage["edges_missing"] == sorted(
            edge.edge_id for edge in executor_edges()
        )

    def test_a_missing_control_url_never_lets_a_probe_claim(
        self, tmp_path: Path, producer_wheel, consumer_wheel, gated_endpoints
    ):
        sentinel_url, provider_url, control_url = gated_endpoints
        run, report, _bundle = run_executor(
            tmp_path / "work",
            (producer_wheel, consumer_wheel),
            sentinel_url,
            provider_url,
            provider_control_url=control_url,  # the sentinel control is MISSING
            probes_only=True,
        )
        assert run.exit_code == EXIT_ISOLATION_UNPROVEN
        (sentinel,) = [p for p in report.authority_receipt.probes if p.name == "sentinel-egress"]
        assert sentinel.control.outcome == OUTCOME_UNAVAILABLE
        assert sentinel.deny is None  # a blocked class claims NOTHING
        assert report.authority_receipt.isolation == ISOLATION_UNPROVEN

    def test_a_leaked_egress_credential_fails_the_whole_run_closed(
        self, tmp_path: Path, producer_wheel, consumer_wheel, gated_endpoints
    ):
        """PE-8d's teeth, under the R38-07 contract: a deliberately
        leaked token riding the launch is caught by the ABSENCE scan
        from the launched process (hygiene violation) — while the deny
        probe itself still presents only the synthetic credential."""
        sentinel_url, provider_url, control_url = gated_endpoints
        run, report, _bundle = run_executor(
            tmp_path / "work",
            (producer_wheel, consumer_wheel),
            sentinel_url,
            provider_url,
            sentinel_control_url=control_url,
            provider_control_url=control_url,
            probes_only=True,
            extra_env={"EGRESS_TOKEN": SENTINEL_SECRET},
        )
        assert run.exit_code == EXIT_ISOLATION_VIOLATED
        assert "EGRESS_TOKEN" in run.launch_env_keys  # the launch receipt admits it…
        assert report.isolation_violated is True
        authority = report.authority_receipt
        assert authority.credential_shaped_keys == ("EGRESS_TOKEN",)
        assert authority.environment_hygiene == "failed"
        assert authority.isolation == ISOLATION_VIOLATED
        assert any("EGRESS_TOKEN" in violation for violation in authority.violations)
        # …the LAUNCHED process caught it, and the probe never picked it
        (sentinel,) = [p for p in authority.probes if p.name == "sentinel-egress"]
        assert sentinel.presented_from_env == ""
        assert sentinel.deny.outcome == OUTCOME_EXPECTED_DENIAL_OBSERVED
        # fail closed: no edge ran at all
        assert report.edge_results == ()
        assert report.report_coverage["edges_missing"] == sorted(
            edge.edge_id for edge in executor_edges()
        )

    def test_a_leaked_provider_token_is_caught_by_the_absence_scan(
        self, tmp_path: Path, producer_wheel, consumer_wheel, gated_endpoints
    ):
        sentinel_url, provider_url, control_url = gated_endpoints
        run, report, _bundle = run_executor(
            tmp_path / "work",
            (producer_wheel, consumer_wheel),
            sentinel_url,
            provider_url,
            sentinel_control_url=control_url,
            provider_control_url=control_url,
            probes_only=True,
            extra_env={"GITLAB_TOKEN": PROVIDER_SECRET},
        )
        assert run.exit_code == EXIT_ISOLATION_VIOLATED
        authority = report.authority_receipt
        assert authority.credential_shaped_keys == ("GITLAB_TOKEN",)
        assert authority.environment_hygiene == "failed"
        assert authority.isolation == ISOLATION_VIOLATED
        (provider,) = [p for p in authority.probes if p.name == "provider-api"]
        assert provider.presented_from_env == ""  # the heuristic is gone

    def test_require_isolated_raises_for_violated_unproven_or_missing_reports(self, happy_run):
        _run, report, _bundle = happy_run
        clean = require_isolated(_run)
        assert clean is _run

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
        unproven = ExecutorRun(
            argv=(),
            allowlist=(),
            extra_env_keys=(),
            launch_env_keys=(),
            exit_code=EXIT_ISOLATION_UNPROVEN,
            timed_out=False,
            stdout_sha256="",
            stderr_sha256="",
            report=ExecutorReport(
                isolation_unproven=True,
                authority_receipt=AuthorityReceipt(
                    isolation=ISOLATION_UNPROVEN,
                    blockers=("the positive control did not succeed",),
                ),
            ),
        )
        with pytest.raises(IsolationUnproven, match="unproven"):
            require_isolated(unproven)
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
# Clean HOME + narrowed PATH — observed from the launched process
# ----------------------------------------------------------------------


class TestCleanHomeAndNarrowedPath:
    def test_scan_home_credentials_finds_planted_shapes_under_a_dirty_home(self, tmp_path):
        (tmp_path / ".netrc").write_text("machine corp login probe password p\n")
        git = tmp_path / ".config" / "git"
        git.mkdir(parents=True)
        (git / "credentials").write_text("https://probe:p@corp.example\n")
        found = scan_home_credentials(str(tmp_path))
        assert set(found) == {".netrc", ".config/git/credentials"}

    def test_scan_home_credentials_finds_nothing_under_an_empty_home(self, tmp_path):
        assert scan_home_credentials(str(tmp_path)) == {}

    def test_a_planted_parent_home_credential_is_unreadable_from_the_verifier(
        self, tmp_path: Path, monkeypatch, producer_wheel, consumer_wheel, gated_endpoints
    ):
        """The subprocess does NOT inherit the parent HOME: credential
        files planted under the parent home (the ~/.netrc and
        ~/.config/... shapes) are UNREADABLE from the verifier, proved
        by the launched process reading its OWN HOME shapes."""
        parent_home = tmp_path / "parent-home"
        parent_home.mkdir()
        (parent_home / ".netrc").write_text(
            "machine corporate login probe password parent-secret\n"
        )
        git = parent_home / ".config" / "git"
        git.mkdir(parents=True)
        (git / "credentials").write_text("https://probe:parent-secret@corp.example\n")
        monkeypatch.setenv("HOME", str(parent_home))
        sentinel_url, provider_url, control_url = gated_endpoints
        run, report, _bundle = run_executor(
            tmp_path / "work",
            (producer_wheel, consumer_wheel),
            sentinel_url,
            provider_url,
            sentinel_control_url=control_url,
            provider_control_url=control_url,
            probes_only=True,
        )
        assert run.exit_code == EXIT_CLEAN
        authority = report.authority_receipt
        assert authority.home != str(parent_home)  # the parent home never arrived
        assert (parent_home / ".netrc").is_file()  # …while still existing on disk
        assert authority.home_shapes_found == ()  # nothing readable
        assert set(authority.home_shapes_checked) == set(CREDENTIAL_HOME_SHAPES)
        assert authority.environment_hygiene == "passed"
        assert authority.isolation == ISOLATION_PROVEN

    def test_a_readable_credential_shape_under_the_verifier_home_fails_closed(
        self, tmp_path: Path, producer_wheel, consumer_wheel, gated_endpoints
    ):
        """The HOME probe has teeth: launch the module DIRECTLY with a
        dirty HOME (bypassing the launcher's provisioning) — the home
        scan must fail hygiene and kill the run."""
        dirty = tmp_path / "dirty-home"
        dirty.mkdir()
        (dirty / ".netrc").write_text("machine corp login probe password p\n")
        sentinel_url, provider_url, control_url = gated_endpoints
        bundle = _make_bundle(
            tmp_path / "work",
            (producer_wheel, consumer_wheel),
            sentinel_url,
            provider_url,
            sentinel_control_url=control_url,
            provider_control_url=control_url,
        )
        bundle_path = bundle.write(tmp_path / "bundle.json")
        out_path = tmp_path / "report.json"
        env = scrubbed_environment(os.environ) | {"HOME": str(dirty)}
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "forge.adaptive.verification_executor",
                "--candidate-set",
                str(bundle_path),
                "--out",
                str(out_path),
                "--probes-only",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert completed.returncode == EXIT_ISOLATION_VIOLATED
        report = ExecutorReport.from_document(json.loads(out_path.read_text()))
        authority = report.authority_receipt
        assert authority.home == str(dirty)
        assert ".netrc" in authority.home_shapes_found
        assert authority.environment_hygiene == "failed"
        assert authority.isolation == ISOLATION_VIOLATED

    def test_the_child_path_is_narrowed_and_a_planted_bin_dir_is_excluded(
        self, tmp_path: Path, monkeypatch, producer_wheel, consumer_wheel, gated_endpoints
    ):
        evil_bin = tmp_path / "evil-bin"
        evil_bin.mkdir()
        monkeypatch.setenv("PATH", f"{evil_bin}{os.pathsep}{os.environ.get('PATH', '')}")
        sentinel_url, provider_url, control_url = gated_endpoints
        run, report, _bundle = run_executor(
            tmp_path / "work",
            (producer_wheel, consumer_wheel),
            sentinel_url,
            provider_url,
            sentinel_control_url=control_url,
            provider_control_url=control_url,
            probes_only=True,
        )
        assert run.exit_code == EXIT_CLEAN
        authority = report.authority_receipt
        assert str(evil_bin) not in authority.path_entries
        uv = shutil.which("uv")
        allowed = set(SYSTEM_PATH_ENTRIES) | ({str(Path(uv).parent)} if uv else set())
        assert set(authority.path_entries) <= allowed
        # observed == declared: the launched process validated the profile
        assert list(authority.path_entries) == report.enforcement_profile["path_entries"]
        assert authority.environment_hygiene == "passed"


# ----------------------------------------------------------------------
# The enforcement profile record + the three separated verdicts
# ----------------------------------------------------------------------


class TestEnforcementProfile:
    def test_the_launch_records_the_enforcement_profile_and_its_digest(
        self, tmp_path: Path, producer_wheel, consumer_wheel, gated_endpoints
    ):
        sentinel_url, provider_url, control_url = gated_endpoints
        run, report, _bundle = run_executor(
            tmp_path / "work",
            (producer_wheel, consumer_wheel),
            sentinel_url,
            provider_url,
            sentinel_control_url=control_url,
            provider_control_url=control_url,
            probes_only=True,
        )
        assert run.exit_code == EXIT_CLEAN
        assert run.enforcement_profile_digest == report.enforcement_profile_digest
        assert len(report.enforcement_profile_digest) == 64
        profile = report.enforcement_profile
        assert profile["env_allowlist_version"] == "env-allowlist/1"
        assert profile["home_policy"] == "clean-home/1"
        assert profile["path_policy"] == "system-minimum/1"
        assert profile["network_policy_class"] == "probe-gated-deny-default/1"
        assert profile["home_layout"] == []  # the clean home is EMPTY
        assert Path(profile["home_path"]).is_dir()
        assert profile["env_allowlist"] == sorted(DEFAULT_ENV_ALLOWLIST)
        # the digest binds the declared content exactly
        assert (
            EnforcementProfile.from_document(profile).digest() == report.enforcement_profile_digest
        )

    def test_the_launched_process_observes_exactly_the_declared_profile(
        self, tmp_path: Path, producer_wheel, consumer_wheel, gated_endpoints
    ):
        sentinel_url, provider_url, control_url = gated_endpoints
        run, report, _bundle = run_executor(
            tmp_path / "work",
            (producer_wheel, consumer_wheel),
            sentinel_url,
            provider_url,
            sentinel_control_url=control_url,
            provider_control_url=control_url,
            probes_only=True,
        )
        assert run.exit_code == EXIT_CLEAN
        authority = report.authority_receipt
        assert authority.home == report.enforcement_profile["home_path"]
        assert list(authority.path_entries) == report.enforcement_profile["path_entries"]
        assert authority.environment_hygiene == "passed"
        assert authority.isolation == ISOLATION_PROVEN

    def test_the_digest_binds_the_profile_content(self):
        base = EnforcementProfile()
        drifted = EnforcementProfile(home_path="/elsewhere")
        assert base.digest() != drifted.digest()
        assert len(base.digest()) == 64

    def test_the_report_carries_three_separate_verdicts_never_one_boolean(self, happy_run):
        _run, report, _bundle = happy_run
        document = json.loads(json.dumps(report.to_document()))
        receipt = document["authority_receipt"]
        assert receipt["environment_hygiene"] == "passed"
        assert receipt["credential_non_disclosure"] == "passed"
        assert receipt["network_enforcement"] == "demonstrated"
        assert receipt["isolation"] == ISOLATION_PROVEN
        assert "isolated" not in receipt  # never one isolation boolean
        assert document["enforcement_profile_digest"]
        assert document["isolation"] == ISOLATION_PROVEN


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
        assert authority.isolation == ISOLATION_PROVEN
        assert authority.credential_shaped_keys == ()
        assert authority.home_shapes_found == ()
        assert authority.environment_hygiene == "passed"
        assert authority.credential_non_disclosure == "passed"
        assert authority.network_enforcement == "demonstrated"
        for probe in authority.probes:
            assert probe.control.outcome == OUTCOME_AUTHORIZED_CONTROL_SUCCEEDED
            assert probe.outcome == OUTCOME_EXPECTED_DENIAL_OBSERVED

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

    def test_fixture_dependencies_are_labeled_reference_coverage(self, happy_run):
        """R38-07: the fixture TCP-broker and sqlite legs are labeled
        reference coverage — an explicit note, never a removal, and
        never readable as production RabbitMQ/PostgreSQL proof."""
        _run, report, _bundle = happy_run
        assert set(report.dependency_coverage) == {DB_DEPENDENCY, BUS_DEPENDENCY}
        assert report.dependency_coverage[DB_DEPENDENCY].startswith("reference-coverage")
        assert "PostgreSQL" in report.dependency_coverage[DB_DEPENDENCY]
        assert report.dependency_coverage[BUS_DEPENDENCY].startswith("reference-coverage")
        assert "RabbitMQ" in report.dependency_coverage[BUS_DEPENDENCY]
        assert report.redelivery_outcome["coverage"] == "reference-coverage"
        (environment,) = [e for e in report.edge_results if e.kind == "environment"]
        upgrade = [c for c in environment.checks if c["check"] == "db-upgrade"][0]
        assert upgrade["coverage"] == "reference-coverage"
        # the frozen environment profile document carries the same labels
        assert environment_profile_document()["coverage_labels"] == report.dependency_coverage

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

    def test_an_unproven_isolation_blocks_every_edge_as_a_prerequisite_not_a_defect(
        self, happy_run
    ):
        """A security-prerequisite failure BLOCKS verification; it is
        never misclassified as a code defect on some member."""
        _run, report, bundle = happy_run
        unproven = ExecutorReport(
            tested_world_digest=report.tested_world_digest,
            isolation_unproven=True,
            authority_receipt=AuthorityReceipt(isolation=ISOLATION_UNPROVEN),
        )
        verdict = executor_readiness(unproven, bundle.candidate_set)
        assert verdict.verification_ready is False
        assert {edge.edge_id for edge in executor_edges()} == {
            b.edge_id for b in verdict.blocked_edges
        }


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
        sentinel_url, provider_url, control_url = gated_endpoints
        run, report, _bundle = run_executor(
            tmp_path / "work",
            (old_producer_wheel, consumer_wheel),
            sentinel_url,
            provider_url,
            sentinel_control_url=control_url,
            provider_control_url=control_url,
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
        sentinel_url, provider_url, control_url = gated_endpoints
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
            sentinel_control_url=control_url,
            provider_control_url=control_url,
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
        sentinel_url, provider_url, control_url = gated_endpoints
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
            sentinel_control_url=control_url,
            provider_control_url=control_url,
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
