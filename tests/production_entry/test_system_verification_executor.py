"""The R37-15 / #296 production-entry trace (AT-12's verification half).

The unit file (``tests/test_verification_executor.py``) pins the RULES;
this file proves them at the level a customer drives. Everything the
customer's verification touches is REAL:

- the executor is a REAL SUBPROCESS under the scrubbed environment
  (``python -m forge.adaptive.verification_executor --candidate-set …
  --out …``), and its isolation is proved FROM THE LAUNCHED PROCESS:
  the sentinel and provider-shaped endpoints are real local HTTP
  servers, the deny probes run inside the subprocess, and a leaked
  credential actually reaches the endpoint and is caught;
- the tested services are REAL BUILT WHEELS (``uv build`` over
  ``evaluation/tested_world/``) installed by sha256 into the
  executor's own venv — the unit pipelines are the wheels' own tests,
  executed in that venv;
- the broker arm is a REAL network round trip: the fake broker is its
  own subprocess (asyncio over TCP), the consumer is the installed
  wheel's own subprocess, and the duplicate delivery is injected at
  the socket.

Honest scope (the same evidence classes the evaluation README names):
the two services are FIXTURE wheels, not customer images; the broker
is a LOCAL fake with real sockets, not the customer's broker; the
sentinel is a local gated endpoint, not a production egress.

This trace is env-clean by construction: nothing here needs
GITLAB_*/model credentials, and the scrub proves the launched process
holds none even when the surrounding environment does.
"""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from forge.adaptive.verification_executor import (
    CONSUMER_SERVICE,
    EXIT_CLEAN,
    EXIT_ISOLATION_VIOLATED,
    EXIT_VERIFICATION_FAILED,
    PINNED_SERVICE,
    PRODUCER_SERVICE,
    CandidateBundle,
    VerificationExecutor,
    WheelRef,
    contract_bundle_document,
    environment_profile_document,
    executor_candidate_set,
    executor_readiness,
    test_bundle_document as _test_bundle_document,  # aliased: pytest must not collect it
)
from forge.adaptive.system_verification import replay_against_changed_inputs
from tests.test_verification_executor import (
    SENTINEL_SECRET,
    _GatedHandler,
    _credential_shaped,
    _source_oid,
    build_fixture_wheel,
)

pytestmark = pytest.mark.production_entry

CONTRACT_EDGE = f"contract:{PRODUCER_SERVICE}->{CONSUMER_SERVICE}"
BASELINE_EDGE = f"baseline:{CONSUMER_SERVICE}->{PINNED_SERVICE}"
ENV_EDGE = "environment:integration"


@pytest.fixture(scope="module")
def pe_gated_endpoints():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GatedHandler)
    thread = threading.Thread(target=server.serve_forever, name="pe-gated-endpoints", daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        yield f"{base}/probe", f"{base}/api/v4/user"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _pe_run(
    root: Path,
    wheels: tuple[WheelRef, ...],
    sentinel_url: str,
    provider_url: str,
    *,
    extra_env: dict[str, str] | None = None,
):
    by_service = {wheel.service: wheel for wheel in wheels}
    frozen = executor_candidate_set(
        producer_wheel_sha256=by_service[PRODUCER_SERVICE].sha256,
        consumer_wheel_sha256=by_service[CONSUMER_SERVICE].sha256,
        producer_source_oid=_source_oid(PRODUCER_SERVICE),
        consumer_source_oid=_source_oid(CONSUMER_SERVICE),
    )
    bundle = CandidateBundle(
        candidate_set=frozen,
        wheels=wheels,
        sentinel_url=sentinel_url,
        provider_url=provider_url,
        work_dir=root / "work",
        contract_document=contract_bundle_document(),
        test_document=_test_bundle_document(),
        environment_document=environment_profile_document(),
    )
    bundle_path = bundle.write(root / "bundle.json")
    run = VerificationExecutor(extra_env=extra_env).run(bundle_path, out_path=root / "report.json")
    assert run.report is not None
    return run, run.report, bundle


class TestVerifiedExecutorCustomerTrace:
    def test_pe8a_the_green_candidate_set_verifies_as_a_system(
        self,
        tmp_path: Path,
        pe_gated_endpoints,
    ):
        """The exact two-service candidate set — the BUILT wheels —
        verifies against its required database and broker behavior,
        separately from any permission to merge or deploy."""
        sentinel_url, provider_url = pe_gated_endpoints
        producer = build_fixture_wheel(PRODUCER_SERVICE, "v2", tmp_path / "w")
        consumer = build_fixture_wheel(CONSUMER_SERVICE, "v2", tmp_path / "w")
        run, report, bundle = _pe_run(
            tmp_path / "green", (producer, consumer), sentinel_url, provider_url
        )
        assert run.exit_code == EXIT_CLEAN
        assert report.system_ready is True
        # the launch receipt and the launched process's receipt agree:
        # nothing credential-shaped was passed OR survived (modulo the
        # __CF_USER_TEXT_ENCODING key macOS's spawn adds to any child)
        assert run.launch_env_keys == tuple(
            sorted(set(report.authority_receipt.env_keys) - {"__CF_USER_TEXT_ENCODING"})
        )
        assert not any(name for name in run.launch_env_keys if _credential_shaped(name))
        assert report.authority_receipt.isolated is True
        # the exact wheel bytes are what the venv ran
        installed = {wheel.service: wheel for wheel in report.executor_receipt.installed}
        assert installed[PRODUCER_SERVICE].installed_sha256 == producer.sha256
        assert installed[CONSUMER_SERVICE].installed_sha256 == consumer.sha256
        # the dependency behaviors: seeded upgrade + socket redelivery
        upgrade = [
            check
            for edge in report.edge_results
            if edge.kind == "environment"
            for check in edge.checks
            if check["check"] == "db-upgrade"
        ][0]
        assert upgrade["preserved"] and upgrade["post_upgrade_write"]
        assert report.redelivery_outcome["exactly_once_all"] is True
        assert report.redelivery_outcome["deliveries"] == 2 * 8
        # three DISTINCT permissions in the exported result
        assert (
            report.readiness["verification_ready"],
            report.readiness["merge_permitted"],
            report.readiness["deploy_permitted"],
        ) == (True, False, False)

    def test_pe8a_the_scrub_proves_absence_even_in_a_loaded_dev_environment(
        self, tmp_path: Path, pe_gated_endpoints, monkeypatch
    ):
        """A surrounding environment FULL of credentials must not leak
        one into the launched verifier (the isolation never depends on
        the dev environment being clean)."""
        for name, value in (
            ("GITLAB_TOKEN", "glpat-dev"),
            ("GITLAB_WEBHOOK_SECRET", "whsec-dev"),
            ("OPENAI_API_KEY", "sk-dev"),
            ("ANTHROPIC_API_KEY", "sk-dev2"),
            ("HTTP_PROXY", "http://corp-proxy"),
        ):
            monkeypatch.setenv(name, value)
        sentinel_url, provider_url = pe_gated_endpoints
        producer = build_fixture_wheel(PRODUCER_SERVICE, "v2", tmp_path / "w")
        consumer = build_fixture_wheel(CONSUMER_SERVICE, "v2", tmp_path / "w")
        run, report, _bundle = _pe_run(
            tmp_path / "loaded", (producer, consumer), sentinel_url, provider_url
        )
        assert run.exit_code == EXIT_CLEAN
        leaked = {name for name in run.launch_env_keys if _credential_shaped(name)}
        assert leaked == set(), leaked
        assert report.authority_receipt.isolated is True
        assert all(probe.denied for probe in report.authority_receipt.deny_probes)
        assert report.system_ready is True

    def test_pe8b_separately_green_pipelines_do_not_compose(
        self, tmp_path: Path, pe_gated_endpoints
    ):
        """The old consumer wheel's OWN pipeline is green; the SYSTEM
        is not. The failed redelivery scenario blocks system readiness
        even though both unit pipelines passed — and the trace records
        the ACTUAL outcome (rejections), never a blanket claim."""
        sentinel_url, provider_url = pe_gated_endpoints
        producer = build_fixture_wheel(PRODUCER_SERVICE, "v2", tmp_path / "w")
        old_consumer = build_fixture_wheel(CONSUMER_SERVICE, "v1", tmp_path / "w")
        run, report, bundle = _pe_run(
            tmp_path / "incompatible", (producer, old_consumer), sentinel_url, provider_url
        )
        assert run.exit_code == EXIT_VERIFICATION_FAILED
        (contract,) = [edge for edge in report.edge_results if edge.kind == "contract"]
        assert contract.status == "failed"
        assert contract.failed_member == CONSUMER_SERVICE
        selftests = {
            check["service"]: check
            for check in contract.checks
            if check["check"] == "wheel-selftest"
        }
        assert all(check["status"] == "passed" for check in selftests.values())
        assert report.redelivery_outcome["exactly_once_all"] is False
        verdict = executor_readiness(report, bundle.candidate_set)
        assert verdict.verification_ready is False
        assert verdict.merge_permitted is False
        assert verdict.deploy_permitted is False

    def test_pe8c_a_switched_baseline_image_under_the_same_source_sha_invalidates(
        self, tmp_path: Path, pe_gated_endpoints
    ):
        """The issue's negative test 1: run the (incompatible) world,
        then switch ONLY the ledger baseline's image under an unchanged
        source SHA — the affected proofs invalidate, the rest stand."""
        sentinel_url, provider_url = pe_gated_endpoints
        producer = build_fixture_wheel(PRODUCER_SERVICE, "v2", tmp_path / "w")
        consumer = build_fixture_wheel(CONSUMER_SERVICE, "v2", tmp_path / "w")
        run, report, bundle = _pe_run(
            tmp_path / "switched", (producer, consumer), sentinel_url, provider_url
        )
        assert run.exit_code == EXIT_CLEAN
        mutated = executor_candidate_set(
            producer_wheel_sha256=producer.sha256,
            consumer_wheel_sha256=consumer.sha256,
            producer_source_oid=_source_oid(PRODUCER_SERVICE),
            consumer_source_oid=_source_oid(CONSUMER_SERVICE),
            ledger_baseline_digest=f"sha256:{'aa' * 32}",
        )
        members_before = {m.repository_id: m for m in bundle.candidate_set.members}
        members_after = {m.repository_id: m for m in mutated.members}
        assert (
            members_after[PINNED_SERVICE].candidate_oid
            == members_before[PINNED_SERVICE].candidate_oid
        )  # the source SHA never moved
        assert (
            members_after[PINNED_SERVICE].image_digest
            != members_before[PINNED_SERVICE].image_digest
        )  # only the artifact did
        replay = replay_against_changed_inputs(report.ledger(), bundle.candidate_set, mutated)
        assert set(replay.invalidated_evidence_ids) == {
            f"ex-{BASELINE_EDGE}",
            f"ex-{ENV_EDGE}",
        }
        assert replay.retained_evidence_ids == (f"ex-{CONTRACT_EDGE}",)
        (drift,) = [
            d for d in replay.drift if d.input_path == f"member/{PINNED_SERVICE}/image_digest"
        ]
        assert drift.source_sha_unchanged is True

    def test_pe8d_a_leaked_credential_kills_the_trace_from_the_launched_process(
        self, tmp_path: Path, pe_gated_endpoints
    ):
        """The safe sentinel check from the issue's negative tests: a
        forbidden reach attempted from the verifier environment — with
        a credential deliberately riding the launch — is observed by
        the endpoint and fails the whole verification closed."""
        sentinel_url, provider_url = pe_gated_endpoints
        producer = build_fixture_wheel(PRODUCER_SERVICE, "v2", tmp_path / "w")
        consumer = build_fixture_wheel(CONSUMER_SERVICE, "v2", tmp_path / "w")
        run, report, _bundle = _pe_run(
            tmp_path / "leaked",
            (producer, consumer),
            sentinel_url,
            provider_url,
            extra_env={"EGRESS_TOKEN": SENTINEL_SECRET},
        )
        assert run.exit_code == EXIT_ISOLATION_VIOLATED
        assert report.isolation_violated is True
        (sentinel,) = [
            probe
            for probe in report.authority_receipt.deny_probes
            if probe.name == "sentinel-egress"
        ]
        assert sentinel.outcome == "violated"
        assert sentinel.presented_from_env == "EGRESS_TOKEN"
        # nothing verified: no edge results, no evidence, no readiness
        assert report.edge_results == ()
        assert report.evidence_records == ()
        assert report.system_ready is False
