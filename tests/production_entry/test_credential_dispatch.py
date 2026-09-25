"""The credential DELIVERY trace (issue #303 / R38-02).

The recorded defect this module closes: the resolved credential VALUE
rode ordinary dispatch inputs / template parameters / trigger variables —
channels documented as visible, precedence-trumping or log-exposed run
metadata. Every dispatch leg now carries a ``CredentialDeliveryPlan``
(REFERENCES only) through one supported transport per profile, and the
proof here is CONSUMER-SIDE: a disposable fake model endpoint asserts
WHICH sentinel the lane actually used, while the ambient fixture key in
the parent environment is never it.

Traces:

- **CD-1** — the native GitHub dispatch: the workflow inputs carry the
  credential REF (the declared ``credential_ref`` input + the redemption
  flag) and NOTHING value-bearing; the shipped template's
  ``secrets[format('FORGE_MODEL_{0}', inputs.credential_ref)]`` mapping,
  rendered and applied to a clean runner-shaped subprocess, delivers the
  provider-held secret to the fake model endpoint (never the ambient
  parent-env key); the lane's resolution snippet fails CLOSED on a
  missing secret.
- **CD-2** — runner-time redemption: the REAL ``lane_driver`` startup
  client redeems through the REAL lane-control router over real HTTP;
  exactly ONE credential variable is set (the stray same-family
  variable is scrubbed), the value is TTL-bounded, the consumer
  subprocess presents the broker-selected sentinel, and the audit row
  (no value) is durable.
- **CD-3** — the negative arms: wrong work / superseded attempt /
  revoked binding / changed reference each produce ZERO successful
  retrievals; network loss after the redemption request fails closed
  with no ambient fallback and no response bodies logged.
- **CD-4** — rotation: a NEW authorized attempt re-resolves; the old
  attempt's token can no longer redeem.
- **CD-5** — dispatch conformance across providers: no credential VALUE
  appears in ANY generated native request (GitHub inputs, GitLab trigger
  variables) — refs only — and a revoked binding means zero dispatches.
- **CD-6** — the unbound legacy lane: no plan, no credential keys on the
  payload, the run proceeds ambient — explicitly the ``ambient-legacy``
  attribution, separable from strict BYOK.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from forge.adaptive.credential_broker import (
    CONSUMER_RECEIPT_SCHEMA,
    DELIVERY_MODE_GITHUB_NATIVE,
    DELIVERY_MODE_GITLAB_PROTECTED,
    DELIVERY_MODE_RUNNER_REDEMPTION,
    DELIVERY_PLAN_SCHEMA,
    DELIVERY_ROUTE_ENV,
    DELIVERY_TEMPLATE_DIR_ENV,
    StagedBroker,
    credential_secret_segment,
)
from forge.adaptive.operator_snapshot import CanonicalSubject
from forge.adaptive.project_credentials import ProjectCredentialRegistry
from forge.durable import FlowRun, FlowStatus
from forge.lane_driver import (
    CONSUMPTION_STATUS_UNRESOLVED,
    LaneCredentialRedemptionError,
    credential_consumption_record,
    redeem_lane_credential,
)

from .conftest import (
    GL_ISSUE_DESC,
    GL_ISSUE_IID,
    GL_ISSUE_TITLE,
    GL_PROJECT_ID,
    PE_LANE_SECRET,
    gl_settings,
    start_control_plane,
)

pytestmark = pytest.mark.production_entry

#: The run's canonical subject as the service derives it from the run
#: row (gitlab family, unrecorded connection, the numeric project id).
RUN_SUBJECT = CanonicalSubject(
    provider_family="gitlab", connection="-", native_id=str(GL_PROJECT_ID)
)

#: The bound credential refs and their staged generations.
REF_V1 = "env:ANTHROPIC_AUTH_TOKEN"
REF_V2 = "vault:kv/eng#42"
SEGMENT_V1 = credential_secret_segment(REF_V1)  # ENV_ANTHROPIC_AUTH_TOKEN

#: The PROVIDER-HELD secret (what the repo secret / masked CI variable /
#: redemption broker holds) — the sentinel the consumer must receive.
SECRET_V1 = "pe-delivery-secret-generation-one"  # noqa: S105 — a test fixture value
SECRET_V2 = "pe-delivery-secret-generation-two"  # noqa: S105 — a test fixture value

#: The ambient value that must NEVER be staged, dispatched or consumed
#: while a bound credential exists (the parent env carries it; the
#: delivery contract never substitutes it).
AMBIENT_VALUE = "pe-ambient-never-delivered"  # noqa: S105 — a test fixture value

TEMPLATES_DIR = Path(__file__).parents[2] / "ci" / "templates"


# ----------------------------------------------------------------------
# The disposable fake model endpoint (the consumer-side proof surface)
# ----------------------------------------------------------------------


class _BearerRecorder(BaseHTTPRequestHandler):
    """Records the Authorization header of every request; answers 200."""

    def do_GET(self) -> None:  # noqa: N802 — the http.server contract
        bearer = self.headers.get("Authorization") or ""
        self.server.bearers.append(bearer)  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *_args: Any) -> None:  # silence the test log
        return


class FakeModelEndpoint:
    """A local HTTP server the consumer subprocess dials with whatever
    credential it was actually given — the assertion surface for WHICH
    key arrived, without publishing any value."""

    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _BearerRecorder)
        self._server.bearers = []  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1/messages"

    def bearers(self) -> list[str]:
        return list(self._server.bearers)  # type: ignore[attr-defined]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture()
def model_endpoint():
    endpoint = FakeModelEndpoint()
    try:
        yield endpoint
    finally:
        endpoint.close()


#: The consumer subprocess: presents ANTHROPIC_AUTH_TOKEN as the bearer
#: to the fake model endpoint — a clean runner-shaped process whose env
#: is EXACTLY what the delivery contract applied.
_CONSUMER = (
    "import os,urllib.request\n"
    "req=urllib.request.Request(\n"
    "    os.environ['FORGE_MODEL_ENDPOINT'],\n"
    "    headers={'Authorization':'Bearer '+os.environ.get('ANTHROPIC_AUTH_TOKEN','')},\n"
    ")\n"
    "urllib.request.urlopen(req,timeout=10)\n"
)


def run_consumer(endpoint: FakeModelEndpoint, credential_env: dict[str, str]) -> int:
    """Launch the consumer subprocess with a CLEAN env plus exactly the
    credential variables the delivery produced."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **credential_env}
    env["FORGE_MODEL_ENDPOINT"] = endpoint.url
    return subprocess.run(
        [sys.executable, "-c", _CONSUMER],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    ).returncode


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# ----------------------------------------------------------------------
# The bound world (registry + staged broker) and the service labs
# ----------------------------------------------------------------------


def _bound_lab() -> tuple[ProjectCredentialRegistry, StagedBroker]:
    registry = ProjectCredentialRegistry()
    registry.bind(RUN_SUBJECT, "anthropic-gateway", REF_V1, bound_by="ops@a")
    broker = StagedBroker()
    broker.stage(REF_V1, SECRET_V1, env_var="ANTHROPIC_AUTH_TOKEN", version="v1")
    broker.stage(REF_V2, SECRET_V2, env_var="ANTHROPIC_AUTH_TOKEN", version="v2")
    return registry, broker


def make_bound_service(session_factory, gitlab, *, registry: ProjectCredentialRegistry, broker):
    """A REAL RunService carrying the credential registry + broker (the
    constructor seams the dispatch legs resolve under)."""
    from forge.config import ForgeConfig
    from forge.runs.service import RunService
    from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer

    return RunService(
        session_factory,
        gitlab=gitlab,
        settings=gl_settings(),
        config=ForgeConfig(),
        planner=StubPlanner(),
        implementer=StubImplementer(),
        reviewer=StubReviewer(),
        credential_registry=registry,
        credential_broker=broker,
    )


async def get_run(session_factory, run_id: str) -> FlowRun:
    async with session_factory() as session:
        return await session.get(FlowRun, run_id)


async def persist_grant(
    session_factory,
    work_id: str,
    *,
    generation: int,
    ref: str,
    subject: CanonicalSubject | None = None,
    provider: str = "anthropic-gateway",
) -> str:
    """Persist the attempt's operation grant the way the DISPATCH seam
    does (Q39-01) — through the production :func:`persist_operation_grant`,
    so the lab's evidence shape is the production one. Returns the id."""
    from datetime import datetime, timedelta, timezone

    from forge.adaptive.credential_broker import CredentialOperationGrant
    from forge.api_lane_control import persist_operation_grant

    now = datetime.now(timezone.utc)
    grant = CredentialOperationGrant(
        grant_id=uuid.uuid4().hex,
        work_id=work_id,
        subject=(subject or RUN_SUBJECT).subject_id(),
        provider=provider,
        credential_ref=ref,
        binding_revision=1,
        attempt_generation=generation,
        delivery_mode="runner-redemption",
        redemption_deadline=now + timedelta(hours=1),
        created_at=now,
    )
    effective = await persist_operation_grant(session_factory, grant=grant)
    return effective.grant_id


async def get_run_evidence(session_factory, run_id: str) -> dict[str, Any]:
    run = await get_run(session_factory, run_id)
    return dict(run.evidence or {})


async def drive_go(service, run_id: str) -> None:
    await service.handle_command_note(
        GL_PROJECT_ID, f"@forge /go {run_id}", "alice", GL_ISSUE_IID, author_user_id=11
    )


def dispatched_variables(gitlab_native, index: int) -> dict[str, str]:
    dispatch = gitlab_native.dispatches()[index]
    return {v["key"]: v["value"] for v in dispatch["variables"]}


async def _start_run(
    pe_db,
    gitlab_native,
    gitlab_client,
    *,
    registry,
    broker,
    monkeypatch,
    delivery: str,
):
    """issue → plan → the parked gate (the REAL planning leg, stubbed
    models). Returns (service, factory, run_id)."""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", AMBIENT_VALUE)
    monkeypatch.setenv(DELIVERY_ROUTE_ENV, delivery)
    monkeypatch.setenv(DELIVERY_TEMPLATE_DIR_ENV, str(TEMPLATES_DIR))
    gitlab_native.seed_issue(GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC)
    factory = pe_db.worker_factory()
    service = make_bound_service(factory, gitlab_client, registry=registry, broker=broker)
    run_id = await service.start_run(
        GL_PROJECT_ID, GL_ISSUE_IID, GL_ISSUE_TITLE, GL_ISSUE_DESC, "alice"
    )
    return service, factory, run_id


# ----------------------------------------------------------------------
# CD-1 — the native GitHub dispatch + the consumer-side proof
# ----------------------------------------------------------------------


class TestCD1NativeGitHubDelivery:
    @staticmethod
    def _github_service(session_factory, client, reader, *, registry, broker):
        from forge.config import ForgeConfig
        from forge.integrations.github_flow import GitHubAgents, GitHubPublishFlow
        from forge.runs.github_service import GitHubRunService
        from forge.runs.stubs import StubImplementer, StubPlanner
        from .conftest import PE_BASE_BRANCH, PE_REPO, StubPRReviewer, pe_settings

        stack = GitHubAgents(
            client=client,
            reader=reader,
            planner=StubPlanner(),
            implementer=StubImplementer(),
            reviewer=StubPRReviewer(),
            flow=GitHubPublishFlow(client, proposer=StubImplementer(), base_branch=PE_BASE_BRANCH),
        )
        return GitHubRunService(
            session_factory,
            pe_settings(),
            ForgeConfig(),
            stack=stack,
            repo_full_name=PE_REPO,
            credential_registry=registry,
            credential_broker=broker,
        )

    async def _drive(self, pe_db, native, native_client, monkeypatch, *, delivery, revoke):
        from .conftest import PE_REPO

        GH_SUBJECT = CanonicalSubject(provider_family="github", connection="-", native_id=PE_REPO)
        registry = ProjectCredentialRegistry()
        registry.bind(GH_SUBJECT, "anthropic-gateway", REF_V1, bound_by="ops@gh")
        if revoke:
            registry.revoke(GH_SUBJECT, "anthropic-gateway", revoked_by="ops@gh")
        broker = StagedBroker()
        broker.stage(REF_V1, SECRET_V1, env_var="ANTHROPIC_AUTH_TOKEN", version="v1")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", AMBIENT_VALUE)
        monkeypatch.setenv(DELIVERY_ROUTE_ENV, delivery)
        monkeypatch.setenv(DELIVERY_TEMPLATE_DIR_ENV, str(TEMPLATES_DIR))
        native.seed_issue(42, "Add the widget", "Body")
        client, reader = native_client
        factory = pe_db.worker_factory()
        service = self._github_service(factory, client, reader, registry=registry, broker=broker)
        run_id = await service.start_run(
            project_id=42,
            issue_number=42,
            issue_title="Add the widget",
            issue_description="Body",
            author_username="alice",
        )
        await service.handle_go(
            project_id=42,
            issue_number=42,
            note_text=f"@forge /go {run_id}",
            author_username="alice",
        )
        return service, factory, run_id

    async def test_the_dispatch_carries_the_ref_only_never_a_value(
        self, pe_db, native, native_client, monkeypatch
    ):
        service, factory, run_id = await self._drive(
            pe_db,
            native,
            native_client,
            monkeypatch,
            delivery=DELIVERY_MODE_GITHUB_NATIVE,
            revoke=False,
        )
        del service
        run = await get_run(factory, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value
        assert len(native.dispatches()) == 1
        branch = native.dispatches()[0]["ref"]
        (inputs,) = native.dispatch_inputs(ref=branch)
        # The payload carries the declared REF + the redemption flag ONLY.
        assert inputs["credential_ref"] == SEGMENT_V1
        assert inputs["credential_redeem"] == ""
        assert "ANTHROPIC_AUTH_TOKEN" not in inputs
        # No credential VALUE anywhere in the captured native request.
        payload = json.dumps(native.dispatches())
        assert SECRET_V1 not in payload and AMBIENT_VALUE not in payload
        # The evidence carries the delivery plan (refs/metadata only).
        proof = (await get_run_evidence(factory, run_id))["harness"]["dispatch_credential"]
        assert proof["schema"] == DELIVERY_PLAN_SCHEMA
        assert proof["mode"] == DELIVERY_MODE_GITHUB_NATIVE
        assert proof["transport_ref"] == f"FORGE_MODEL_{SEGMENT_V1}"
        assert proof["attribution"] == "bound-delivery"
        assert SECRET_V1 not in json.dumps(proof)
        # Q39-01: a NATIVE dispatch mints NO operation grant — redemption
        # was never authorized for this attempt.
        assert not (await get_run_evidence(factory, run_id)).get("credential_operation_grants")
        assert native.unknown_paths() == []

    async def test_a_revoked_binding_means_zero_workflow_dispatches(
        self, pe_db, native, native_client, monkeypatch
    ):
        service, factory, run_id = await self._drive(
            pe_db,
            native,
            native_client,
            monkeypatch,
            delivery=DELIVERY_MODE_GITHUB_NATIVE,
            revoke=True,
        )
        del service
        run = await get_run(factory, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "credential_refused: revoked" in str(run.status_reason)
        assert native.dispatches() == []
        assert native.unknown_paths() == []

    async def test_the_redemption_mode_dispatch_carries_the_raw_ref_and_flag(
        self, pe_db, native, native_client, monkeypatch
    ):
        service, factory, run_id = await self._drive(
            pe_db,
            native,
            native_client,
            monkeypatch,
            delivery=DELIVERY_MODE_RUNNER_REDEMPTION,
            revoke=False,
        )
        del service
        run = await get_run(factory, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value
        (inputs,) = native.dispatch_inputs(ref=native.dispatches()[0]["ref"])
        assert inputs["credential_ref"] == REF_V1  # the RAW ref — the lane re-checks it
        assert inputs["credential_redeem"] == "1"
        assert SECRET_V1 not in json.dumps(native.dispatches())
        # Q39-01: the dispatch PERSISTED the attempt's operation grant
        # (redemption mode only) — the exact ref/route the lane may redeem,
        # with its absolute deadline, refs/metadata only.
        grants = (await get_run_evidence(factory, run_id)).get("credential_operation_grants")
        assert grants and "0:anthropic-gateway" in grants
        grant = grants["0:anthropic-gateway"]
        assert grant["schema"] == "forge.credential.operation-grant/1"
        assert grant["credential_ref"] == REF_V1
        assert grant["provider"] == "anthropic-gateway"
        assert grant["attempt_generation"] == 0
        assert grant["operation"] == "credential-redemption"
        assert datetime.fromisoformat(grant["redemption_deadline"]).tzinfo is not None
        assert SECRET_V1 not in json.dumps(grant)


class TestCD1NativeConsumerProof:
    """The shipped template's env mapping, rendered and applied to a
    clean runner-shaped subprocess — the fake model endpoint asserts
    WHICH key arrived (the provider-held secret, never the ambient
    parent-env value)."""

    GITHUB_TEMPLATE = (TEMPLATES_DIR / "forge-harness.github.yml").read_text()

    def test_the_rendered_mapping_delivers_the_secret_not_the_ambient_key(self, model_endpoint):
        # The ACTUAL shipped template carries the mapping this renders.
        mapping = "secrets[format('FORGE_MODEL_{0}', inputs.credential_ref)]"
        assert mapping in self.GITHUB_TEMPLATE
        # The rendered env: the repo secret FORGE_MODEL_<ref> (what the CI
        # provider delivers runner-side), keyed by the dispatched ref.
        repo_secrets = {f"FORGE_MODEL_{SEGMENT_V1}": SECRET_V1}
        rendered = {
            "FORGE_CREDENTIAL_REF": SEGMENT_V1,
            "FORGE_CREDENTIAL_REDEEM": "",
            "FORGE_MODEL_CREDENTIAL": repo_secrets.get(f"FORGE_MODEL_{SEGMENT_V1}", ""),
        }
        # The template's resolution: export exactly ONE credential var.
        # (The ambient parent env — which carries AMBIENT_VALUE — is not
        # part of the runner-shaped env at all.)
        lane_env = {"ANTHROPIC_AUTH_TOKEN": rendered["FORGE_MODEL_CREDENTIAL"]}
        assert lane_env["ANTHROPIC_AUTH_TOKEN"] == SECRET_V1

        assert run_consumer(model_endpoint, lane_env) == 0
        assert model_endpoint.bearers() == [f"Bearer {SECRET_V1}"]
        assert AMBIENT_VALUE not in model_endpoint.bearers()[0]

    def test_the_resolution_snippet_fails_closed_on_a_missing_secret(self, model_endpoint):
        """The shipped snippet's semantics, executed in bash: a BOUND
        dispatch with an empty FORGE_MODEL secret refuses (exit 1, the
        bootstrap marker) instead of falling back to an ambient key."""
        snippet = (
            'if [ -n "${FORGE_CREDENTIAL_REF:-}" ] && [ "${FORGE_CREDENTIAL_REDEEM:-}" != "1" ]; then\n'
            '  if [ -z "${FORGE_MODEL_CREDENTIAL:-}" ]; then\n'
            '    echo "FORGE_BOOTSTRAP_FAILED: the bound credential secret FORGE_MODEL_${FORGE_CREDENTIAL_REF} is empty or missing"\n'
            "    exit 1\n"
            "  fi\n"
            '  export ANTHROPIC_AUTH_TOKEN="$FORGE_MODEL_CREDENTIAL"\n'
            "  unset ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN 2>/dev/null || true\n"
            "fi\n"
        )
        # The shipped template carries this exact posture.
        assert "FORGE_BOOTSTRAP_FAILED: the bound credential secret" in self.GITHUB_TEMPLATE

        bound_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "FORGE_CREDENTIAL_REF": SEGMENT_V1,
            "FORGE_CREDENTIAL_REDEEM": "",
            "FORGE_MODEL_CREDENTIAL": "",
            "ANTHROPIC_AUTH_TOKEN": AMBIENT_VALUE,  # the stray ambient key
        }
        probe = '\nprintf "%s" "${ANTHROPIC_AUTH_TOKEN:-}"\n'
        missing = subprocess.run(
            ["bash", "-c", snippet + probe],
            env=bound_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert missing.returncode == 1
        assert "FORGE_BOOTSTRAP_FAILED" in missing.stdout
        assert AMBIENT_VALUE not in missing.stdout  # never the fallback

        delivered = dict(bound_env, FORGE_MODEL_CREDENTIAL=SECRET_V1)
        ok = subprocess.run(
            ["bash", "-c", snippet + probe],
            env=delivered,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert ok.returncode == 0
        assert ok.stdout == SECRET_V1  # exactly the provider-held secret


# ----------------------------------------------------------------------
# CD-2 — runner-time redemption through the REAL lane driver client
# ----------------------------------------------------------------------


class TestCD2RunnerRedemption:
    @staticmethod
    def _lane_env(
        control_url: str, token: str, *, ref: str = REF_V1, generation: str = "0"
    ) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "FORGE_WORK_ID": "run-redeem-1",
            "FORGE_RUN_ID": "run-redeem-1",
            "FORGE_LANE_DRIVER": "claude",
            "FORGE_CREDENTIAL_REF": ref,
            # Q39-01: the dispatched attempt generation the bootstrap
            # verifies the redemption response against.
            "FORGE_ATTEMPT_GENERATION": generation,
            "FORGE_LANE_CONTROL_URL": control_url,
            "FORGE_LANE_CONTROL_TOKEN": token,
            # The stray same-family variable a runner image may carry —
            # the delivery must SCRUB it (precedence, doc 05 §2).
            "ANTHROPIC_API_KEY": "pe-stray-ambient-api-key",
            "ANTHROPIC_AUTH_TOKEN": AMBIENT_VALUE,
        }

    @staticmethod
    async def _lab(pe_db, monkeypatch, *, revoked=False):
        """The control plane over real HTTP + the run row (with its
        attempt-0 operation grant persisted the dispatch seam's way) +
        the registry/broker the redemption endpoint resolves through."""
        control = await start_control_plane(pe_db.url, secret=PE_LANE_SECRET)
        monkeypatch.setenv("FORGE_LANE_LEGACY_TOKEN_DEADLINE", "2020-01-01T00:00:00+00:00")
        from forge.durable.models import FlowRun as FlowRunRow

        async with control._session_factory() as session:
            session.add(
                FlowRunRow(
                    id="run-redeem-1",
                    project_id=GL_PROJECT_ID,
                    provider="gitlab",
                    cancellation_generation=0,
                )
            )
            await session.commit()
        await persist_grant(control._session_factory, "run-redeem-1", generation=0, ref=REF_V1)
        registry, broker = _bound_lab()
        if revoked:
            registry.revoke(RUN_SUBJECT, "anthropic-gateway", revoked_by="ops@a")
        # The redemption endpoint resolves through app.state-or-default:
        # pin the DEFAULTS to this lab's registry/broker (the endpoint's
        # function-level imports pick the patched names up).
        import forge.adaptive.credential_broker as broker_module
        import forge.adaptive.project_credentials as credentials_module

        monkeypatch.setattr(credentials_module, "registry_from_env", lambda *a, **k: registry)
        monkeypatch.setattr(broker_module, "EnvBroker", lambda *a, **k: broker, raising=True)
        return control, registry, broker

    async def test_the_lane_bootstrap_redeems_sets_exactly_one_variable_and_consumes_it(
        self, pe_db, monkeypatch, model_endpoint
    ):
        control, _registry, _broker = await self._lab(pe_db, monkeypatch)
        try:
            from forge.api_lane_control import lane_control_token

            env = self._lane_env(
                control.base_url, lane_control_token(PE_LANE_SECRET, "run-redeem-1", generation=0)
            )
            record = redeem_lane_credential(env)

            # Exactly ONE credential variable is set; the stray is gone.
            assert env["ANTHROPIC_AUTH_TOKEN"] == SECRET_V1
            assert "ANTHROPIC_API_KEY" not in env
            assert record["env_var"] == "ANTHROPIC_AUTH_TOKEN"
            assert record["scrubbed_env_vars"] == ["ANTHROPIC_API_KEY"]
            assert record["credential_ref"] == REF_V1
            # TTL-bounded to the attempt.
            from datetime import datetime, timedelta, timezone

            expires = datetime.fromisoformat(record["expires_at"])
            assert (
                datetime.now(timezone.utc)
                < expires
                <= (datetime.now(timezone.utc) + timedelta(hours=2))
            )
            # The value NEVER rides the record (it uploads with the lane).
            assert SECRET_V1 not in json.dumps(record)
            # The consumer: a clean runner-shaped subprocess presenting
            # the delivered credential — the BROKER-selected sentinel.
            assert (
                run_consumer(model_endpoint, {"ANTHROPIC_AUTH_TOKEN": env["ANTHROPIC_AUTH_TOKEN"]})
                == 0
            )
            assert model_endpoint.bearers() == [f"Bearer {SECRET_V1}"]
        finally:
            control.stop()

    async def test_the_audit_row_is_durable_and_value_free(self, pe_db, monkeypatch):
        control, _registry, _broker = await self._lab(pe_db, monkeypatch)
        try:
            from forge.api_lane_control import lane_control_token

            env = self._lane_env(
                control.base_url, lane_control_token(PE_LANE_SECRET, "run-redeem-1", generation=0)
            )
            record = redeem_lane_credential(env)
            async with control._session_factory() as session:
                run = await session.get(FlowRun, "run-redeem-1")
            rows = (run.evidence or {}).get("credential_redemptions")
            assert rows and rows[0]["redemption_id"] == record["redemption_id"]
            assert rows[0]["binding_revision"] == 1
            assert SECRET_V1 not in json.dumps(rows)
        finally:
            control.stop()

    async def test_network_loss_fails_closed_with_no_ambient_fallback(self, pe_db, monkeypatch):
        control, _registry, _broker = await self._lab(pe_db, monkeypatch)
        try:
            dead_port = _free_port()
            env = self._lane_env(f"http://127.0.0.1:{dead_port}", "any-token")
            with pytest.raises(LaneCredentialRedemptionError) as caught:
                redeem_lane_credential(env)
            # No fallback, no response body, no credential applied.
            assert "unreachable" in str(caught.value)
            assert env["ANTHROPIC_AUTH_TOKEN"] == AMBIENT_VALUE  # untouched
            async with control._session_factory() as session:
                run = await session.get(FlowRun, "run-redeem-1")
            assert not (run.evidence or {}).get("credential_redemptions")
        finally:
            control.stop()


# ----------------------------------------------------------------------
# CD-3 — the negative arms: zero successful retrievals
# ----------------------------------------------------------------------


class TestCD3RedemptionRefusals:
    async def _refuse(self, pe_db, monkeypatch, *, token_work_id="run-redeem-1", generation=0):
        control, _registry, _broker = await TestCD2RunnerRedemption._lab(pe_db, monkeypatch)
        return control

    async def _attempt(self, control, *, token: str, ref: str = REF_V1, work: str = "run-redeem-1"):
        env = TestCD2RunnerRedemption._lane_env(control.base_url, token, ref=ref)
        env["FORGE_WORK_ID"] = work
        env["FORGE_RUN_ID"] = work
        try:
            redeem_lane_credential(env)
            return None  # a retrieval SUCCEEDED — the caller asserts not-None fails
        except LaneCredentialRedemptionError as exc:
            return str(exc)

    async def test_a_wrong_works_token_retrieves_nothing(self, pe_db, monkeypatch):
        from forge.api_lane_control import lane_control_token

        control = await self._refuse(pe_db, monkeypatch)
        try:
            wrong = lane_control_token(PE_LANE_SECRET, "run-other", generation=0)
            message = await self._attempt(control, token=wrong)
            assert message is not None and "HTTP 403" in message
        finally:
            control.stop()

    async def test_a_superseded_attempt_retrieves_nothing(self, pe_db, monkeypatch):
        from forge.api_lane_control import lane_control_token

        control = await self._refuse(pe_db, monkeypatch)
        try:
            # The attempt moved on (generation 1); the retired lane holds
            # its generation-0 token.
            async with control._session_factory() as session:
                run = await session.get(FlowRun, "run-redeem-1")
                run.cancellation_generation = 1
                await session.commit()
            stale = lane_control_token(PE_LANE_SECRET, "run-redeem-1", generation=0)
            message = await self._attempt(control, token=stale)
            assert message is not None and "HTTP 403" in message
            async with control._session_factory() as session:
                run = await session.get(FlowRun, "run-redeem-1")
            assert not (run.evidence or {}).get("credential_redemptions")
        finally:
            control.stop()

    async def test_a_revoked_binding_retrieves_nothing(self, pe_db, monkeypatch):
        from forge.api_lane_control import lane_control_token

        control, _registry, _broker = await TestCD2RunnerRedemption._lab(
            pe_db, monkeypatch, revoked=True
        )
        try:
            token = lane_control_token(PE_LANE_SECRET, "run-redeem-1", generation=0)
            message = await self._attempt(control, token=token)
            assert message is not None and "HTTP 403" in message
        finally:
            control.stop()

    async def test_a_changed_reference_retrieves_nothing(self, pe_db, monkeypatch):
        from forge.api_lane_control import lane_control_token

        control = await self._refuse(pe_db, monkeypatch)
        try:
            token = lane_control_token(PE_LANE_SECRET, "run-redeem-1", generation=0)
            message = await self._attempt(control, token=token, ref="vault:kv/other#1")
            assert message is not None and "HTTP 403" in message
            async with control._session_factory() as session:
                run = await session.get(FlowRun, "run-redeem-1")
            assert not (run.evidence or {}).get("credential_redemptions")
        finally:
            control.stop()


# ----------------------------------------------------------------------
# CD-4 — rotation: the new attempt re-resolves, the old token retires
# ----------------------------------------------------------------------


class TestCD4Rotation:
    async def test_the_new_attempt_re_resolves_and_the_old_token_retires(self, pe_db, monkeypatch):
        from forge.api_lane_control import lane_control_token

        control, registry, broker = await TestCD2RunnerRedemption._lab(pe_db, monkeypatch)
        try:
            old = lane_control_token(PE_LANE_SECRET, "run-redeem-1", generation=0)
            first = redeem_lane_credential(TestCD2RunnerRedemption._lane_env(control.base_url, old))
            assert first["credential_ref"] == REF_V1

            # The operator rotates the binding (a NEW live generation of
            # the credential world) and the attempt moves on.
            registry.bind(RUN_SUBJECT, "anthropic-gateway", REF_V2, bound_by="ops@a")
            async with control._session_factory() as session:
                run = await session.get(FlowRun, "run-redeem-1")
                run.cancellation_generation = 1
                await session.commit()
            # The re-dispatch of the new attempt persists ITS grant (the
            # dispatch seam's own step — Q39-01).
            await persist_grant(control._session_factory, "run-redeem-1", generation=1, ref=REF_V2)

            # The OLD attempt's token is retired — zero retrievals.
            with pytest.raises(LaneCredentialRedemptionError, match="HTTP 403"):
                redeem_lane_credential(
                    TestCD2RunnerRedemption._lane_env(
                        control.base_url, old, ref=REF_V2, generation="0"
                    )
                )
            # The NEW authorized attempt re-resolves the rotated-in ref.
            new = lane_control_token(PE_LANE_SECRET, "run-redeem-1", generation=1)
            second = redeem_lane_credential(
                TestCD2RunnerRedemption._lane_env(control.base_url, new, ref=REF_V2, generation="1")
            )
            assert second["credential_ref"] == REF_V2
            assert second["binding_revision"] == 2
            assert second["grant_id"]
            assert broker.resolve_calls.count(REF_V2) >= 1
        finally:
            control.stop()


# ----------------------------------------------------------------------
# CD-5 — dispatch conformance across providers: refs only, no value
# ----------------------------------------------------------------------


class TestCD5DispatchConformance:
    async def test_the_gitlab_envelope_carries_the_ref_never_the_value(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch
    ):
        registry, broker = _bound_lab()
        service, factory, run_id = await _start_run(
            pe_db,
            gitlab_native,
            gitlab_client,
            registry=registry,
            broker=broker,
            monkeypatch=monkeypatch,
            delivery=DELIVERY_MODE_GITLAB_PROTECTED,
        )
        await drive_go(service, run_id)

        run = await get_run(factory, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value
        assert len(gitlab_native.dispatches()) == 1
        variables = dispatched_variables(gitlab_native, 0)
        # The envelope carries the non-secret REF variable (the protected
        # + masked project variable FORGE_MODEL_<ref> holds the VALUE
        # runner-side); NEVER a value-bearing trigger variable.
        assert variables["FORGE_CREDENTIAL_REF"] == SEGMENT_V1
        assert variables["FORGE_CREDENTIAL_REDEEM"] == ""
        assert "ANTHROPIC_AUTH_TOKEN" not in variables
        every_value = [v["value"] for d in gitlab_native.dispatches() for v in d["variables"]]
        for secret_value in (SECRET_V1, SECRET_V2, AMBIENT_VALUE, "glpat-test", "whsec"):
            assert secret_value not in every_value

        evidence = await get_run_evidence(factory, run_id)
        proof = evidence["harness"]["dispatch_credential"]
        assert proof["schema"] == DELIVERY_PLAN_SCHEMA
        assert proof["mode"] == DELIVERY_MODE_GITLAB_PROTECTED
        assert proof["transport_ref"] == f"FORGE_MODEL_{SEGMENT_V1}"
        assert SECRET_V1 not in json.dumps(evidence)
        envelope = evidence["harness"]["dispatch_envelope"]
        assert envelope["credential_ref"] == REF_V1
        assert envelope["credential_delivery_mode"] == DELIVERY_MODE_GITLAB_PROTECTED
        # Q39-01: a NATIVE dispatch mints NO operation grant.
        assert not evidence.get("credential_operation_grants")
        assert gitlab_native.unknown_paths() == []

    async def test_a_revoked_binding_parks_the_run_with_zero_provider_dispatches(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch
    ):
        registry, broker = _bound_lab()
        registry.revoke(RUN_SUBJECT, "anthropic-gateway", revoked_by="ops@a")
        service, factory, run_id = await _start_run(
            pe_db,
            gitlab_native,
            gitlab_client,
            registry=registry,
            broker=broker,
            monkeypatch=monkeypatch,
            delivery=DELIVERY_MODE_GITLAB_PROTECTED,
        )
        await drive_go(service, run_id)

        run = await get_run(factory, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "credential_refused: revoked" in str(run.status_reason)
        assert gitlab_native.dispatches() == []
        assert gitlab_native.unknown_paths() == []

    async def test_an_undeclared_delivery_route_for_a_bound_subject_refuses(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch
    ):
        """A bound subject with no declared route NEVER falls back to an
        ambient credential — the typed refusal parks the run pre-paid."""
        registry, broker = _bound_lab()
        service, factory, run_id = await _start_run(
            pe_db,
            gitlab_native,
            gitlab_client,
            registry=registry,
            broker=broker,
            monkeypatch=monkeypatch,
            delivery="",  # nothing declared
        )
        await drive_go(service, run_id)

        run = await get_run(factory, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "credential_refused: delivery_route_unsupported" in str(run.status_reason)
        assert gitlab_native.dispatches() == []

    async def test_a_redemption_mode_dispatch_persists_the_attempt_grant(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch
    ):
        """Q39-01: the GitLab dispatch leg under runner-redemption mints
        and persists the attempt's OPERATION GRANT beside the delivery
        plan — the lane that boots may redeem exactly this ref/route,
        inside the persisted absolute window, nothing else."""
        registry, broker = _bound_lab()
        service, factory, run_id = await _start_run(
            pe_db,
            gitlab_native,
            gitlab_client,
            registry=registry,
            broker=broker,
            monkeypatch=monkeypatch,
            delivery=DELIVERY_MODE_RUNNER_REDEMPTION,
        )
        await drive_go(service, run_id)

        run = await get_run(factory, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value
        variables = dispatched_variables(gitlab_native, 0)
        assert variables["FORGE_CREDENTIAL_REDEEM"] == "1"
        evidence = await get_run_evidence(factory, run_id)
        grants = evidence.get("credential_operation_grants")
        assert grants and set(grants) == {"0:anthropic-gateway"}
        grant = grants["0:anthropic-gateway"]
        assert grant["schema"] == "forge.credential.operation-grant/1"
        assert grant["credential_ref"] == REF_V1
        assert grant["work_id"] == run_id
        assert grant["operation"] == "credential-redemption"
        assert grant["delivery_mode"] == DELIVERY_MODE_RUNNER_REDEMPTION
        deadline = datetime.fromisoformat(grant["redemption_deadline"])
        assert deadline > datetime.now(deadline.tzinfo)
        # Refs/metadata only — no value anywhere in the grant evidence.
        assert SECRET_V1 not in json.dumps(grant)
        assert SECRET_V1 not in json.dumps(evidence)


# ----------------------------------------------------------------------
# CD-6 — the unbound legacy lane: ambient, explicitly separable
# ----------------------------------------------------------------------


class TestCD6UnboundLegacy:
    async def test_an_unbound_dispatch_rides_no_credential_keys_and_stays_ambient(
        self, pe_db, gitlab_native, gitlab_client, monkeypatch
    ):
        registry = ProjectCredentialRegistry()  # NO binding decision
        broker = StagedBroker()
        service, factory, run_id = await _start_run(
            pe_db,
            gitlab_native,
            gitlab_client,
            registry=registry,
            broker=broker,
            monkeypatch=monkeypatch,
            delivery=DELIVERY_MODE_GITLAB_PROTECTED,
        )
        await drive_go(service, run_id)

        run = await get_run(factory, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value  # ambient legacy proceeds
        variables = dispatched_variables(gitlab_native, 0)
        assert "FORGE_CREDENTIAL_REF" not in variables  # no plan, no ref
        assert "ANTHROPIC_AUTH_TOKEN" not in variables
        every_value = [v["value"] for d in gitlab_native.dispatches() for v in d["variables"]]
        assert AMBIENT_VALUE not in every_value  # and still no value
        evidence = await get_run_evidence(factory, run_id)
        assert "dispatch_credential" not in (evidence.get("harness") or {})
        # Q39-01: an unbound legacy dispatch mints NO operation grant —
        # this lane could never redeem at the endpoint.
        assert not evidence.get("credential_operation_grants")
        # The broker was never consulted — the attribution stays honestly
        # ambient-legacy, never promoted to a claim.
        assert broker.resolve_calls == []


# ----------------------------------------------------------------------
# CD-7 / CD-8 (R38-04, issue #305) — the RUNNER BOUNDARY: a disposable
# lane-job subprocess executing the REAL lane bootstrap
# (``python -m forge.lane_driver``) against the REAL control plane and a
# fake model endpoint, plus rotation AT the boundary.
# ----------------------------------------------------------------------

#: A disposable claude_agent_sdk stand-in the lane-job subprocess loads
#: from PYTHONPATH: its one driven turn presents whatever credential the
#: bootstrap staged to the fake model endpoint — the consumer action the
#: whole proof turns on. It carries no vendor logic of its own.
_FAKE_SDK_STUB = '''
"""Disposable claude_agent_sdk stand-in (R38-04 PE runner boundary).

The message classes are DATACLASSES because the driver serializes every
drained piece with dataclasses.asdict (the vendor dataclass shape)."""
import asyncio
import dataclasses
import os
import urllib.request


class ClaudeAgentOptions:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


@dataclasses.dataclass
class _InitMessage:
    subtype: str
    data: dict


@dataclasses.dataclass
class ResultMessage:
    subtype: str
    duration_ms: int
    duration_api_ms: int
    is_error: bool
    num_turns: int
    session_id: str
    terminal_reason: str = "completed"
    usage: dict | None = None
    total_cost_usd: float | None = None


class ClaudeSDKClient:
    def __init__(self, options=None, transport=None):
        self.options = options
        self.transport = transport
        self.connected = False
        self._session_id = "pe-stub-session-1"
        self._inbox = []

    async def connect(self, prompt=None):
        self.connected = True
        self._inbox.append(_InitMessage("init", {"session_id": self._session_id}))

    async def receive_messages(self):
        while True:
            if self._inbox:
                yield self._inbox.pop(0)
                continue
            await asyncio.sleep(0.01)

    async def query(self, prompt, session_id="default"):
        # THE consumer action: whatever credential the bootstrap staged
        # is presented to the (fake) model endpoint.
        request = urllib.request.Request(
            os.environ.get("FORGE_MODEL_ENDPOINT", ""),
            headers={"Authorization": "Bearer " + os.environ.get("ANTHROPIC_AUTH_TOKEN", "")},
        )
        urllib.request.urlopen(request, timeout=10).read()
        self._inbox.append(
            ResultMessage(
                subtype="success",
                duration_ms=5,
                duration_api_ms=4,
                is_error=False,
                num_turns=1,
                session_id=self._session_id,
                terminal_reason="completed",
                usage={"input_tokens": 10, "output_tokens": 5},
                total_cost_usd=0.01,
            )
        )

    async def interrupt(self):
        return None

    async def disconnect(self):
        self.connected = False
'''


LANE_WORK_ID = "run-lane-1"
LANE_JOB_ID = "907001"  # the CI_JOB_ID the consumer receipt joins on


async def _lane_lab(pe_db, monkeypatch):
    """The control plane over real HTTP with the lane's run row (its
    attempt-0 operation grant persisted the dispatch seam's way) and the
    bound registry/broker its redemption endpoint resolves through."""
    control = await start_control_plane(pe_db.url, secret=PE_LANE_SECRET)
    monkeypatch.setenv("FORGE_LANE_LEGACY_TOKEN_DEADLINE", "2020-01-01T00:00:00+00:00")
    from forge.durable.models import FlowRun as FlowRunRow

    async with control._session_factory() as session:
        session.add(
            FlowRunRow(
                id=LANE_WORK_ID,
                project_id=GL_PROJECT_ID,
                provider="gitlab",
                cancellation_generation=0,
            )
        )
        await session.commit()
    await persist_grant(control._session_factory, LANE_WORK_ID, generation=0, ref=REF_V1)
    registry, broker = _bound_lab()
    import forge.adaptive.credential_broker as broker_module
    import forge.adaptive.project_credentials as credentials_module

    monkeypatch.setattr(credentials_module, "registry_from_env", lambda *a, **k: registry)
    monkeypatch.setattr(broker_module, "EnvBroker", lambda *a, **k: broker, raising=True)
    return control, registry, broker


def _run_lane_job(workdir: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """The disposable lane job: the REAL ``python -m forge.lane_driver``
    bootstrap in a clean runner-shaped workspace."""
    (workdir / ".forge").mkdir(parents=True, exist_ok=True)
    (workdir / ".forge" / "brief.md").write_text("PLAN: prove the credential consumption boundary")
    return subprocess.run(
        [sys.executable, "-m", "forge.lane_driver"],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _lane_job_env(
    control_url: str, token: str, *, endpoint_url: str, sdk_dir: Path
) -> dict[str, str]:
    """The lane job's env: redemption mode, the ambient never-delivered
    key, the stray same-family variable, and the fake SDK on PYTHONPATH."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(sdk_dir),
        "PYTHONDONTWRITEBYTECODE": "1",
        "FORGE_WORK_ID": LANE_WORK_ID,
        "FORGE_RUN_ID": LANE_WORK_ID,
        "FORGE_ATTEMPT_BASE": "a" * 40,
        "FORGE_ISSUE_IID": "42",
        "FORGE_LANE_DRIVER": "claude",
        "FORGE_LANE_BUDGET_SECONDS": "60",
        "FORGE_LANE_POLL_SECONDS": "0.05",
        "FORGE_CREDENTIAL_REF": REF_V1,
        "FORGE_ATTEMPT_GENERATION": "0",
        "FORGE_CREDENTIAL_REDEEM": "1",
        "FORGE_LANE_CONTROL_URL": control_url,
        "FORGE_LANE_CONTROL_TOKEN": token,
        "FORGE_MODEL_ENDPOINT": endpoint_url,
        "CI_JOB_ID": LANE_JOB_ID,
        "GITLAB_CI": "true",
        # The ambient key that must NEVER be consumed, and the stray.
        "ANTHROPIC_AUTH_TOKEN": AMBIENT_VALUE,
        "ANTHROPIC_API_KEY": "pe-stray-ambient-api-key",
    }


async def _lane_audit_rows(control) -> list[dict[str, Any]]:
    async with control._session_factory() as session:
        run = await session.get(FlowRun, LANE_WORK_ID)
    return list((run.evidence or {}).get("credential_redemptions") or [])


class TestCD7RunnerBoundary:
    """The REAL lane bootstrap: redeem over real HTTP → drive one turn →
    the fake model endpoint receives the BROKER sentinel; the consumer
    receipt joins broker id ↔ consumer id ↔ attempt against the durable
    audit row; a failed consumer bootstrap preserves an UNRESOLVED
    delivery record; the mutation arm fails the proof though the ledger
    looks correct."""

    async def test_the_real_lane_job_consumes_the_broker_sentinel_and_joins_the_receipts(
        self, pe_db, monkeypatch, model_endpoint, tmp_path
    ):
        control, _registry, _broker = await _lane_lab(pe_db, monkeypatch)
        try:
            from forge.api_lane_control import lane_control_token

            sdk_dir = tmp_path / "sdk"
            sdk_dir.mkdir()
            (sdk_dir / "claude_agent_sdk.py").write_text(_FAKE_SDK_STUB)
            token = lane_control_token(PE_LANE_SECRET, LANE_WORK_ID, generation=0)
            result = _run_lane_job(
                tmp_path / "lane-job",
                _lane_job_env(
                    control.base_url, token, endpoint_url=model_endpoint.url, sdk_dir=sdk_dir
                ),
            )
            assert result.returncode == 0, result.stderr

            # THE consumer-side proof: the endpoint received the BROKER's
            # selection — never the ambient parent-env key.
            assert model_endpoint.bearers() == [f"Bearer {SECRET_V1}"]

            meta = json.loads(
                ((tmp_path / "lane-job") / ".forge" / "candidate.meta.json").read_text()
            )
            assert meta["exit"] == "completed"
            consumption = meta["credential_consumption"]
            assert consumption["schema"] == CONSUMER_RECEIPT_SCHEMA
            assert consumption["consumer_status"] == "consumed"  # a completed turn earned it
            assert consumption["env_var"] == "ANTHROPIC_AUTH_TOKEN"  # the slot NAME only
            assert consumption["consumer_identity"] == {
                "kind": "ci-job",
                "id": LANE_JOB_ID,
                "via": "CI_JOB_ID",
            }
            assert consumption["binding_revision"] == 1
            assert consumption["attempt_generation"] == 0
            assert consumption["credential_policy"] == "compat"

            # The JOIN: broker id ↔ redemption id ↔ attempt ↔ consumer,
            # against the control plane's durable audit row.
            rows = await _lane_audit_rows(control)
            assert rows and rows[0]["redemption_id"] == consumption["redemption_id"]
            assert rows[0]["broker_receipt_id"] == consumption["broker_receipt_id"]
            assert rows[0]["broker_receipt_id"]
            assert rows[0]["attempt_generation"] == consumption["attempt_generation"]
            # Presence-version honesty rides the consumer receipt too.
            assert consumption["resolved_version_kind"] == "fixture"
            assert "v1" == consumption["resolved_version"]

            # Both durable journals carry the record (the meta the
            # collector consumes and the steering sidecar), value-free.
            sidecar = json.loads(((tmp_path / "lane-job") / ".forge" / "steering.json").read_text())
            assert (
                sidecar["credential_consumption"]["redemption_id"] == consumption["redemption_id"]
            )
            assert (
                sidecar["credential_redemption"]["redemption_id"] == (consumption["redemption_id"])
            )
            for document in (json.dumps(meta), json.dumps(sidecar), json.dumps(rows)):
                assert SECRET_V1 not in document
                assert AMBIENT_VALUE not in document
        finally:
            control.stop()

    async def test_a_failed_consumer_bootstrap_preserves_an_unresolved_delivery_record(
        self, pe_db, monkeypatch, model_endpoint, tmp_path
    ):
        """The bootstrap staged the credential (the broker returned, the
        audit row landed) — then the consumer's model endpoint went
        DOWN. The delivery record stays UNRESOLVED: no model-usage claim,
        no usage receipt, the endpoint never dialed."""
        control, _registry, _broker = await _lane_lab(pe_db, monkeypatch)
        try:
            from forge.api_lane_control import lane_control_token

            sdk_dir = tmp_path / "sdk"
            sdk_dir.mkdir()
            (sdk_dir / "claude_agent_sdk.py").write_text(_FAKE_SDK_STUB)
            token = lane_control_token(PE_LANE_SECRET, LANE_WORK_ID, generation=0)
            dead_endpoint = f"http://127.0.0.1:{_free_port()}/v1/messages"
            result = _run_lane_job(
                tmp_path / "lane-job",
                _lane_job_env(control.base_url, token, endpoint_url=dead_endpoint, sdk_dir=sdk_dir),
            )
            assert result.returncode == 1  # the lane fails; the artifacts still land
            meta = json.loads(
                ((tmp_path / "lane-job") / ".forge" / "candidate.meta.json").read_text()
            )
            assert meta["exit"] == "failed"
            consumption = meta["credential_consumption"]
            assert consumption["schema"] == CONSUMER_RECEIPT_SCHEMA
            assert consumption["consumer_status"] == CONSUMPTION_STATUS_UNRESOLVED
            assert consumption["redemption_id"]
            # NO model-usage claim merely because the broker returned:
            # zero turns reached the endpoint, the usage stays unknown.
            assert model_endpoint.bearers() == []
            assert meta["usage"] is None
            # The broker-side delivery record is preserved unresolved —
            # the ledger row stands, the claim does not.
            rows = await _lane_audit_rows(control)
            assert rows and rows[0]["redemption_id"] == consumption["redemption_id"]
            assert SECRET_V1 not in json.dumps(meta)
        finally:
            control.stop()

    async def test_the_deliberate_consumer_mapping_omission_fails_the_proof(
        self, pe_db, monkeypatch, model_endpoint
    ):
        """The mutation arm: the broker call STAYS (the redemption
        succeeds, the durable ledger looks correct) but the consumer
        MAPPING — the delivered value's application to the env slot — is
        dropped. The proof's own assertion surface (the exact predicate
        that passes in CD-2/CD-7) then FAILS: the endpoint never sees
        the broker's sentinel. Encoded here as the detector firing."""
        control, _registry, _broker = await _lane_lab(pe_db, monkeypatch)
        try:
            from forge.api_lane_control import lane_control_token

            token = lane_control_token(PE_LANE_SECRET, LANE_WORK_ID, generation=0)
            env = TestCD2RunnerRedemption._lane_env(control.base_url, token)
            env["FORGE_WORK_ID"] = LANE_WORK_ID
            env["FORGE_RUN_ID"] = LANE_WORK_ID
            # The broker call stays — the redemption and its audit row land.
            record = redeem_lane_credential(env)
            rows = await _lane_audit_rows(control)
            assert rows and rows[0]["redemption_id"] == record["redemption_id"]

            # THE MUTATION: the consumer mapping is dropped — the lane
            # never applies the delivered value to its env slot.
            del env[record["env_var"]]

            # The happy-path proof predicate (CD-2/CD-7's own surface):
            # a clean consumer presenting the staged credential. Under
            # the mutation it evaluates FALSE — the proof would fail even
            # though the request ledger above looks correct.
            consumer_exit = run_consumer(
                model_endpoint, {"ANTHROPIC_AUTH_TOKEN": env.get("ANTHROPIC_AUTH_TOKEN", "")}
            )
            proof_predicate = consumer_exit == 0 and model_endpoint.bearers() == [
                f"Bearer {SECRET_V1}"
            ]
            assert proof_predicate is False
            # Nothing was silently substituted in the sentinel's place —
            # not the ambient key, not the stray.
            assert AMBIENT_VALUE not in model_endpoint.bearers()[0]
            assert model_endpoint.bearers() == ["Bearer "]
        finally:
            control.stop()


class TestCD8RotationAtTheBoundary:
    """Rotation BETWEEN approval and dispatch (typed refusal, never a
    silent substitution) and rotation AFTER dispatch (the launched env
    unchanged; the NEW attempt resolves the new revision)."""

    async def test_rotation_between_approval_and_dispatch_refuses_typed(self, pe_db, monkeypatch):
        control, registry, _broker = await _lane_lab(pe_db, monkeypatch)
        try:
            from forge.api_lane_control import lane_control_token

            # The approval recorded the OLD ref; the operator rotates the
            # binding before the dispatch's lane redeems it.
            registry.bind(RUN_SUBJECT, "anthropic-gateway", REF_V2, bound_by="ops@a")
            token = lane_control_token(PE_LANE_SECRET, LANE_WORK_ID, generation=0)
            rotated_env = TestCD2RunnerRedemption._lane_env(control.base_url, token, ref=REF_V1)
            rotated_env["FORGE_WORK_ID"] = LANE_WORK_ID
            rotated_env["FORGE_RUN_ID"] = LANE_WORK_ID
            message: str | None
            try:
                redeem_lane_credential(rotated_env)
                message = None  # a retrieval SUCCEEDED — the caller asserts not-None fails
            except LaneCredentialRedemptionError as exc:
                message = str(exc)
            # Documented, tested outcome: a typed refusal — never a
            # silent substitution of the rotated-in credential. The lane
            # sees the status class only (never the body); the typed
            # reason rides the endpoint's refusal detail.
            assert message is not None and "HTTP 403" in message
            assert "no credential was redeemed and none is substituted" in message
            import httpx

            from forge.api_lane_control import LANE_CREDENTIAL_REDEEM_ROUTE

            refusal = httpx.get(
                control.base_url + LANE_CREDENTIAL_REDEEM_ROUTE,
                params={
                    "work_id": LANE_WORK_ID,
                    "credential_ref": REF_V1,
                    "provider": "anthropic-gateway",
                },
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            assert refusal.status_code == 403
            assert "rotated" in refusal.json()["detail"]
            assert await _lane_audit_rows(control) == []  # zero successful retrievals
        finally:
            control.stop()

    async def test_rotation_after_dispatch_keeps_the_launched_env_and_renews_on_the_new_attempt(
        self, pe_db, monkeypatch
    ):
        control, registry, _broker = await _lane_lab(pe_db, monkeypatch)
        try:
            from forge.api_lane_control import lane_control_token

            old = lane_control_token(PE_LANE_SECRET, LANE_WORK_ID, generation=0)
            lane_env = TestCD2RunnerRedemption._lane_env(control.base_url, old)
            lane_env["FORGE_WORK_ID"] = LANE_WORK_ID
            lane_env["FORGE_RUN_ID"] = LANE_WORK_ID
            first = redeem_lane_credential(lane_env)
            assert lane_env["ANTHROPIC_AUTH_TOKEN"] == SECRET_V1

            # Rotation AFTER dispatch: the LAUNCHED environment is
            # unchanged — the staged snapshot is this attempt's
            # generation until terminal.
            registry.bind(RUN_SUBJECT, "anthropic-gateway", REF_V2, bound_by="ops@a")
            assert lane_env["ANTHROPIC_AUTH_TOKEN"] == SECRET_V1
            assert first["binding_revision"] == 1

            # The new authorized attempt resolves the rotated-in revision.
            async with control._session_factory() as session:
                run = await session.get(FlowRun, LANE_WORK_ID)
                run.cancellation_generation = 1
                await session.commit()
            # The re-dispatch of the new attempt persists ITS grant (the
            # dispatch seam's own step — Q39-01).
            await persist_grant(control._session_factory, LANE_WORK_ID, generation=1, ref=REF_V2)
            new = lane_control_token(PE_LANE_SECRET, LANE_WORK_ID, generation=1)
            renewed_env = TestCD2RunnerRedemption._lane_env(
                control.base_url, new, ref=REF_V2, generation="1"
            )
            renewed_env["FORGE_WORK_ID"] = LANE_WORK_ID
            renewed_env["FORGE_RUN_ID"] = LANE_WORK_ID
            second = redeem_lane_credential(renewed_env)
            assert second["credential_ref"] == REF_V2
            assert second["binding_revision"] == 2
            assert renewed_env["ANTHROPIC_AUTH_TOKEN"] == SECRET_V2

            # Each attempt's consumer receipt names ITS revision — the
            # operator can tell which binding revision paid for which.
            first_receipt = credential_consumption_record(
                redemption_record=first,
                env={"FORGE_WORK_ID": LANE_WORK_ID, "CI_JOB_ID": LANE_JOB_ID},
            )
            second_receipt = credential_consumption_record(
                redemption_record=second,
                env={"FORGE_WORK_ID": LANE_WORK_ID, "CI_JOB_ID": LANE_JOB_ID},
            )
            assert first_receipt["binding_revision"] == 1
            assert second_receipt["binding_revision"] == 2
            assert first_receipt["redemption_id"] != second_receipt["redemption_id"]
            assert first_receipt["broker_receipt_id"] != second_receipt["broker_receipt_id"]
            assert second_receipt["resolved_version_kind"] == "fixture"  # presence honesty
        finally:
            control.stop()


# ----------------------------------------------------------------------
# CD-9 (Q39-01 / #320) — the RUNNER's typed verification: a wrong-slot
# or expired redemption ANSWER (HTTP 200!) never constructs the vendor
# client — the fake model endpoint asserts ZERO calls, and the ambient
# key never substitutes.
# ----------------------------------------------------------------------


class _CannedRedemptionHandler(BaseHTTPRequestHandler):
    """Serves ONE canned redemption document (200) — a lying endpoint."""

    def do_GET(self) -> None:  # noqa: N802 — the http.server contract
        body = json.dumps(self.server.document).encode("utf-8")  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:  # silence the test log
        return


class CannedRedemptionEndpoint:
    """A local HTTP server impersonating the redemption route."""

    def __init__(self, document: dict[str, Any]) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _CannedRedemptionHandler)
        self._server.document = document  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _lying_document(**overrides: Any) -> dict[str, Any]:
    """A redemption answer for THIS lane — every field correct except the
    axes each CD-9 case mutates (the 200 is deliberate: the verification,
    not the transport, is the fence under test)."""
    from datetime import datetime, timedelta, timezone

    document: dict[str, Any] = {
        "redemption_id": "lying-1",
        "grant_id": "lying-grant-1",
        "work_id": LANE_WORK_ID,
        "provider": "anthropic-gateway",
        "credential_ref": REF_V1,
        "env_var": "ANTHROPIC_AUTH_TOKEN",
        "value": SECRET_V2,  # a DIFFERENT sentinel — must never be applied
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "binding_revision": 1,
        "resolver_identity": "canned",
        "resolved_version": "v2",
        "resolved_version_kind": "fixture",
        "attempt_generation": 0,
        "credential_policy": "compat",
    }
    document.update(overrides)
    return document


class TestCD9RunnerTypedVerification:
    """AC-06: the runner rejects expired or wrong-slot ANSWERS before any
    vendor client is constructed — with an ambient key present."""

    @staticmethod
    def _lane_env_against(lying: CannedRedemptionEndpoint, sdk_dir: Path, endpoint_url: str):
        env = _lane_job_env(
            lying.base_url, "any-token-works-here", endpoint_url=endpoint_url, sdk_dir=sdk_dir
        )
        return env

    async def test_a_wrong_slot_answer_never_constructs_the_vendor_client(
        self, model_endpoint, tmp_path
    ):
        """HTTP 200 with the value bound to the WRONG env slot: the
        bootstrap refuses before the SDK session exists — zero model
        calls, the ambient key never presented."""
        lying = CannedRedemptionEndpoint(_lying_document(env_var="OPENAI_API_KEY"))
        try:
            sdk_dir = tmp_path / "sdk"
            sdk_dir.mkdir()
            (sdk_dir / "claude_agent_sdk.py").write_text(_FAKE_SDK_STUB)
            env = self._lane_env_against(lying, sdk_dir, model_endpoint.url)
            env["FORGE_ATTEMPT_GENERATION"] = "0"
            result = _run_lane_job(tmp_path / "lane-job", env)

            assert result.returncode == 1  # the lane halts; artifacts land
            meta = json.loads(
                ((tmp_path / "lane-job") / ".forge" / "candidate.meta.json").read_text()
            )
            assert meta["exit"] == "failed"
            assert meta["terminal_reason"] == "credential_redemption_failed"
            assert "env_var" in meta["error"]
            # THE vendor-client proof: the fake model endpoint saw NOTHING
            # (the SDK session never existed), and no ambient fallback.
            assert model_endpoint.bearers() == []
            assert AMBIENT_VALUE not in result.stdout + result.stderr
        finally:
            lying.stop()

    async def test_an_expired_answer_never_constructs_the_vendor_client(
        self, model_endpoint, tmp_path
    ):
        from datetime import datetime, timedelta, timezone

        stale = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        lying = CannedRedemptionEndpoint(_lying_document(expires_at=stale))
        try:
            sdk_dir = tmp_path / "sdk"
            sdk_dir.mkdir()
            (sdk_dir / "claude_agent_sdk.py").write_text(_FAKE_SDK_STUB)
            env = self._lane_env_against(lying, sdk_dir, model_endpoint.url)
            env["FORGE_ATTEMPT_GENERATION"] = "0"
            result = _run_lane_job(tmp_path / "lane-job", env)

            assert result.returncode == 1
            meta = json.loads(
                ((tmp_path / "lane-job") / ".forge" / "candidate.meta.json").read_text()
            )
            assert meta["terminal_reason"] == "credential_redemption_failed"
            assert "expires_at" in meta["error"]
            assert model_endpoint.bearers() == []
            assert AMBIENT_VALUE not in result.stdout + result.stderr
        finally:
            lying.stop()

    async def test_a_wrong_generation_answer_never_constructs_the_vendor_client(
        self, model_endpoint, tmp_path
    ):
        """The envelope names attempt 0; the answer claims attempt 9 —
        a response for a DIFFERENT attempt is not this lane's."""
        lying = CannedRedemptionEndpoint(_lying_document(attempt_generation=9))
        try:
            sdk_dir = tmp_path / "sdk"
            sdk_dir.mkdir()
            (sdk_dir / "claude_agent_sdk.py").write_text(_FAKE_SDK_STUB)
            env = self._lane_env_against(lying, sdk_dir, model_endpoint.url)
            env["FORGE_ATTEMPT_GENERATION"] = "0"
            result = _run_lane_job(tmp_path / "lane-job", env)

            assert result.returncode == 1
            meta = json.loads(
                ((tmp_path / "lane-job") / ".forge" / "candidate.meta.json").read_text()
            )
            assert meta["terminal_reason"] == "credential_redemption_failed"
            assert "attempt_generation" in meta["error"]
            assert model_endpoint.bearers() == []
        finally:
            lying.stop()
