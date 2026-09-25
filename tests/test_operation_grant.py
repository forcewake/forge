"""Q39-01 (#320) — the OPERATION GRANT: redemption is authorized against
the exact dispatched operation, not project membership.

The recorded defect (external review ``6df4020``, probe P01): the
redemption endpoint validated the work/generation token, then took the
provider + credential_ref from the REQUEST and checked only the PROJECT
binding — a lane dispatched for binding A redeemed sibling binding B of
the same project; no terminal-state check; ``expires_at = now + TTL``
per request (response metadata, not a bound); no authority
re-validation across the awaited broker resolution; the runner applied
the named env_var with presence checks only.

Pinned here:

- **The grant document** (``forge.credential.operation-grant/1``):
  construction, the refs-only document codec, the idempotent per
  attempt+route+ref persistence (a re-dispatch keeps the FIRST grant's
  id and ABSOLUTE deadline — the window never re-anchors), and the
  mint-at-plan rule (redemption-mode plans carry a grant; native and
  attempt-less plans carry none);
- **The refusal matrix**, every arm ZERO broker calls (the broker
  double's call count is the proof): the P01 sibling-binding shape, a
  wrong ref under the granted route, native-only, unbound legacy,
  terminal run, and the ABSOLUTE deadline — frozen in the persisted
  evidence, identical across a simulated API restart;
- **The lost-response retry window**: a repeated request under the SAME
  grant inside the deadline is idempotent (both receipts join on the
  same ``grant_id``, the audit trail keeps separate observations);
  past the deadline the grant is expired, absolutely;
- **The authority fence**: a cancellation or a rotation that lands
  while a CONTROLLABLE broker is blocked inside the await refuses typed
  AFTER the broker resolved — no audit row, no value emitted under
  retired authority;
- **The runner's typed verification** (units; the subprocess proof
  lives in ``tests/production_entry/test_credential_dispatch.py``): a
  wrong-slot, wrong-work, wrong-generation or expired response never
  applies anything.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from forge.adaptive.credential_broker import (
    CREDENTIAL_POLICY_ENV,
    DEFAULT_OPERATION_GRANT_WINDOW_SECONDS,
    DELIVERY_MODE_GITHUB_NATIVE,
    DELIVERY_MODE_RUNNER_REDEMPTION,
    DELIVERY_PLAN_SCHEMA,
    DELIVERY_ROUTE_ENV,
    DELIVERY_TEMPLATE_DIR_ENV,
    EVIDENCE_OPERATION_GRANTS_KEY,
    OPERATION_GRANT_SCHEMA,
    OPERATION_GRANT_WINDOW_ENV,
    CredentialOperationGrant,
    ResolvedCredential,
    SecretValue,
    StagedBroker,
    attempt_delivery_mode,
    delivery_plan,
    merge_operation_grant,
    operation_grant_for_plan,
    operation_grant_key,
    operation_grant_window_seconds,
    operation_grants_for_attempt,
)
from forge.adaptive.operator_snapshot import CanonicalSubject
from forge.adaptive.project_credentials import CredentialRefusal, ProjectCredentialRegistry
from forge.api_lane_control import (
    LANE_CREDENTIAL_REDEEM_ROUTE,
    LEGACY_CREDENTIAL_ANCHOR_FILE_ENV,
    lane_control_token,
    persist_operation_grant,
)
from forge.config import Settings
from forge.database import reset_engine
from forge.durable.models import FlowRun
from forge.lane_driver import (
    expected_redemption_identity,
    verify_redemption_response,
)
from forge.main import create_app

SECRET = "grant-lane-secret"  # noqa: S105 — a test fixture value
WORK = "run-grant-1"
PROJECT_ID = 90210
SUBJECT = CanonicalSubject(provider_family="gitlab", connection="-", native_id=str(PROJECT_ID))
SUBJECT_ID = SUBJECT.subject_id()

#: The two live bindings of ONE project — the P01 world.
GRANTED_REF = "env:ANTHROPIC_AUTH_TOKEN"  # the dispatched (anthropic) route
SIBLING_REF = "env:OPENAI_API_KEY"  # the sibling (openai) route
GRANTED_SENTINEL = "sk-grant-sentinel-anthropic"  # noqa: S105 — fixture value
SIBLING_SENTINEL = "sk-grant-sentinel-openai"  # noqa: S105 — fixture value

TEMPLATES_DIR = "ci/templates"


def _settings(tmp_path, **overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test"),
        GITLAB_WEBHOOK_SECRET=SecretStr("test-secret-token"),
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/operation-grant.db",
        LITELLM_URL="http://litellm:4000",
        REDIS_URL=None,
        FORGE_CAPTURE_DIR=None,
        FORGE_BOT_TOKEN=None,
        FORGE_BOT_USERNAME="forge-bot",
        FORGE_LANE_CONTROL_SECRET=SecretStr(SECRET),
    )
    values.update(overrides)
    return Settings(**values)


@asynccontextmanager
async def _live_app(tmp_path) -> AsyncIterator:
    """One REAL app over the shared sqlite file, lifespan up and down."""
    reset_engine()
    application = create_app(settings=_settings(tmp_path))
    async with application.router.lifespan_context(application):
        yield application
    reset_engine()


@pytest.fixture(autouse=True)
def _isolated_legacy_anchor(tmp_path, monkeypatch):
    """Keep the write-once legacy anchor inside the test's own tmp dir."""
    monkeypatch.setenv(LEGACY_CREDENTIAL_ANCHOR_FILE_ENV, str(tmp_path / "legacy-anchor"))


@pytest.fixture()
async def app(tmp_path):
    async with _live_app(tmp_path) as application:
        yield application


@pytest.fixture()
async def client(app) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _gen_headers(generation: int = 2) -> dict[str, str]:
    return {"Authorization": f"Bearer {lane_control_token(SECRET, WORK, generation=generation)}"}


def _redeem_params(*, ref: str = GRANTED_REF, provider: str = "anthropic-gateway") -> dict:
    return {"work_id": WORK, "credential_ref": ref, "provider": provider}


def _frozen_clock(moment: datetime) -> type:
    """A controlled ``datetime`` pinned at *moment* (the Q35-06 trick)."""

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001 — mirrors datetime.now
            return moment if tz is None else moment.astimezone(tz)

    return _Clock


def _registry_with_sibling_bindings() -> ProjectCredentialRegistry:
    registry = ProjectCredentialRegistry()
    registry.bind(SUBJECT, "anthropic-gateway", GRANTED_REF, bound_by="ops@a")
    registry.bind(SUBJECT, "openai", SIBLING_REF, bound_by="ops@a")
    return registry


def _two_route_broker() -> StagedBroker:
    broker = StagedBroker()
    broker.stage(GRANTED_REF, GRANTED_SENTINEL, env_var="ANTHROPIC_AUTH_TOKEN", version="v1")
    broker.stage(SIBLING_REF, SIBLING_SENTINEL, env_var="OPENAI_API_KEY", version="s1")
    return broker


async def _put_run(app, *, generation: int = 2, status: str = "waiting_harness") -> None:
    async with app.state.session_factory() as session:
        session.add(
            FlowRun(
                id=WORK,
                project_id=PROJECT_ID,
                provider="gitlab",
                cancellation_generation=generation,
                status=status,
            )
        )
        await session.commit()


async def _dispatch_grant(
    app,
    *,
    generation: int = 2,
    ref: str = GRANTED_REF,
    provider: str = "anthropic-gateway",
    deadline: timedelta = timedelta(hours=1),
    created: timedelta = timedelta(0),
) -> CredentialOperationGrant:
    """Persist the attempt's grant the dispatch seam's way."""
    now = datetime.now(timezone.utc)
    grant = CredentialOperationGrant(
        grant_id=uuid.uuid4().hex,
        work_id=WORK,
        subject=SUBJECT_ID,
        provider=provider,
        credential_ref=ref,
        binding_revision=1,
        attempt_generation=generation,
        delivery_mode="runner-redemption",
        redemption_deadline=now + deadline,
        created_at=now - created,
    )
    return await persist_operation_grant(app.state.session_factory, grant=grant)


async def _redemptions(app) -> list[dict]:
    async with app.state.session_factory() as session:
        run = await session.get(FlowRun, WORK)
    return list((run.evidence or {}).get("credential_redemptions") or [])


# ----------------------------------------------------------------------
# The grant document — construction, codec, idempotent persistence
# ----------------------------------------------------------------------


class TestGrantDocument:
    def _grant(self, **overrides) -> CredentialOperationGrant:
        now = datetime.now(timezone.utc)
        fields = dict(
            grant_id="g1",
            work_id=WORK,
            subject=SUBJECT_ID,
            provider="anthropic-gateway",
            credential_ref=GRANTED_REF,
            binding_revision=3,
            attempt_generation=2,
            delivery_mode="runner-redemption",
            redemption_deadline=now + timedelta(hours=1),
            created_at=now,
        )
        fields.update(overrides)
        return CredentialOperationGrant(**fields)

    def test_the_document_carries_the_operation_tuple_refs_only(self):
        grant = self._grant()
        document = grant.as_document()
        assert document["schema"] == OPERATION_GRANT_SCHEMA
        assert OPERATION_GRANT_SCHEMA == "forge.credential.operation-grant/1"
        assert document["grant_id"] == "g1"
        assert document["work_id"] == WORK
        assert document["subject"] == SUBJECT_ID
        assert document["provider"] == "anthropic-gateway"
        assert document["credential_ref"] == GRANTED_REF
        assert document["binding_revision"] == 3
        assert document["attempt_generation"] == 2
        assert document["delivery_mode"] == "runner-redemption"
        assert document["operation"] == "credential-redemption"
        # The ABSOLUTE deadline rides as a fixed ISO instant.
        assert datetime.fromisoformat(document["redemption_deadline"]) == (
            grant.redemption_deadline
        )
        assert datetime.fromisoformat(document["created_at"]) == grant.created_at
        # No value slot exists at all.
        assert "value" not in document
        # Round trip: from_document rebuilds the same grant.
        assert CredentialOperationGrant.from_document(document) == grant

    def test_the_deadline_boundary_itself_refuses(self):
        boundary = datetime.now(timezone.utc) + timedelta(seconds=1)
        grant = self._grant(redemption_deadline=boundary)
        assert grant.expired_at(boundary - timedelta(seconds=1)) is False
        assert grant.expired_at(boundary) is True  # inclusive: AT is past

    def test_a_naive_persisted_instant_reads_as_utc(self):
        grant = self._grant()
        document = grant.as_document()
        document["redemption_deadline"] = grant.redemption_deadline.replace(tzinfo=None).isoformat()
        loaded = CredentialOperationGrant.from_document(document)
        assert loaded.redemption_deadline == grant.redemption_deadline

    def test_a_malformed_document_is_a_typed_refusal(self):
        document = self._grant().as_document()
        for mutation in (
            {"grant_id": ""},
            {"credential_ref": ""},
            {"redemption_deadline": "not-a-time"},
            {"attempt_generation": "two"},
        ):
            corrupt = dict(document, **mutation)
            with pytest.raises(CredentialRefusal, match="operation_grant_invalid"):
                CredentialOperationGrant.from_document(corrupt)

    def test_the_evidence_key_is_attempt_plus_route(self):
        assert operation_grant_key(2, "anthropic-gateway") == "2:anthropic-gateway"
        grant = self._grant()
        assert grant.key() == "2:anthropic-gateway"

    def test_the_window_is_operator_state_never_silently_redefaulted(self):
        assert operation_grant_window_seconds({}) == DEFAULT_OPERATION_GRANT_WINDOW_SECONDS
        assert operation_grant_window_seconds({OPERATION_GRANT_WINDOW_ENV: "120"}) == 120.0
        for bad in ("soon", "-5", "0"):
            with pytest.raises(CredentialRefusal, match="operation_grant_window_invalid"):
                operation_grant_window_seconds({OPERATION_GRANT_WINDOW_ENV: bad})

    def test_merging_keeps_the_first_grant_for_the_same_attempt_route_ref(self):
        """Idempotency per attempt+route+ref: the FIRST grant's id and
        ABSOLUTE deadline survive a re-dispatch — the window never
        re-anchors, not even by the dispatch itself."""
        first = self._grant(
            grant_id="first", redemption_deadline=datetime(2030, 1, 1, tzinfo=timezone.utc)
        )
        evidence, effective = merge_operation_grant({}, first)
        assert effective is first
        assert evidence[EVIDENCE_OPERATION_GRANTS_KEY]["2:anthropic-gateway"] == first.as_document()

        later = self._grant(
            grant_id="second",
            redemption_deadline=datetime(2099, 1, 1, tzinfo=timezone.utc),  # would widen
            created_at=datetime.now(timezone.utc) + timedelta(hours=2),
        )
        evidence, effective = merge_operation_grant(evidence, later)
        assert effective.grant_id == "first"  # the original authorization stands
        assert effective.redemption_deadline == first.redemption_deadline

        # A DIFFERENT ref at the same key (a rotation) replaces — the
        # evidence stays honest about what is authorized NOW.
        rotated = self._grant(grant_id="third", credential_ref="vault:kv/eng#9")
        evidence, effective = merge_operation_grant(evidence, rotated)
        assert effective.grant_id == "third"
        assert effective.credential_ref == "vault:kv/eng#9"

    async def test_persisting_through_the_seam_is_idempotent_per_attempt(self, app):
        await _put_run(app)
        first = await _dispatch_grant(app)
        # A re-dispatch of the SAME attempt under the SAME route+ref
        # mints a fresh document — the seam keeps the FIRST grant.
        second = await _dispatch_grant(app, deadline=timedelta(days=365))
        assert second.grant_id == first.grant_id
        assert second.redemption_deadline == first.redemption_deadline
        async with app.state.session_factory() as session:
            run_row = await session.get(FlowRun, WORK)
        stored = (run_row.evidence or {})[EVIDENCE_OPERATION_GRANTS_KEY]
        assert len(stored) == 1
        assert operation_grants_for_attempt(run_row.evidence or {}, 2)[0].grant_id == first.grant_id
        # Other attempts' grants are untouched.
        assert operation_grants_for_attempt(run_row.evidence or {}, 3) == []

    def test_the_attempt_grant_load_skips_unreadable_documents(self):
        good = self._grant()
        evidence = {
            EVIDENCE_OPERATION_GRANTS_KEY: {
                "2:anthropic-gateway": good.as_document(),
                "2:openai": {"schema": OPERATION_GRANT_SCHEMA, "grant_id": ""},  # corrupt
                "1:anthropic-gateway": self._grant(attempt_generation=1).as_document(),
            }
        }
        grants = operation_grants_for_attempt(evidence, 2)
        assert [grant.provider for grant in grants] == ["anthropic-gateway"]

    def test_the_attempt_delivery_mode_reads_the_latest_plan(self):
        evidence = {
            "harness": {
                "dispatch_credential": {
                    "mode": DELIVERY_MODE_GITHUB_NATIVE,
                    "attempt_generation": 2,
                }
            }
        }
        assert attempt_delivery_mode(evidence, 2) == DELIVERY_MODE_GITHUB_NATIVE
        assert attempt_delivery_mode(evidence, 3) == ""  # another attempt
        assert attempt_delivery_mode({}, 2) == ""


class TestGrantMinting:
    """The dispatch seam mints the grant beside redemption-mode plans."""

    @staticmethod
    def _env(delivery: str) -> dict[str, str]:
        return {
            DELIVERY_ROUTE_ENV: delivery,
            DELIVERY_TEMPLATE_DIR_ENV: TEMPLATES_DIR,
            CREDENTIAL_POLICY_ENV: "compat",
        }

    async def test_a_redemption_plan_carries_the_grant(self):
        registry = _registry_with_sibling_bindings()
        plan = await delivery_plan(
            registry,
            StagedBroker(),
            subject=SUBJECT,
            provider_route="anthropic-gateway",
            profile="github",
            work_id=WORK,
            attempt_generation=4,
            environ=self._env(DELIVERY_MODE_RUNNER_REDEMPTION),
        )
        assert plan is not None
        grant = plan.operation_grant
        assert grant is not None
        assert grant.work_id == WORK
        assert grant.attempt_generation == 4
        assert grant.provider == plan.provider == "anthropic-gateway"
        assert grant.credential_ref == plan.credential_ref == GRANTED_REF
        assert grant.binding_revision == plan.binding_revision
        assert grant.delivery_mode == DELIVERY_MODE_RUNNER_REDEMPTION
        assert grant.operation == "credential-redemption"
        # The deadline is ABSOLUTE: fixed at mint, now + window, once.
        deadline = datetime.now(timezone.utc) + timedelta(
            seconds=DEFAULT_OPERATION_GRANT_WINDOW_SECONDS
        )
        assert grant.redemption_deadline - timedelta(seconds=5) <= deadline
        assert grant.created_at <= datetime.now(timezone.utc)

    async def test_a_native_plan_carries_no_grant(self):
        plan = await delivery_plan(
            _registry_with_sibling_bindings(),
            StagedBroker(),
            subject=SUBJECT,
            provider_route="anthropic-gateway",
            profile="github",
            work_id=WORK,
            attempt_generation=4,
            environ=self._env(DELIVERY_MODE_GITHUB_NATIVE),
        )
        assert plan is not None
        assert plan.mode == DELIVERY_MODE_GITHUB_NATIVE
        assert plan.operation_grant is None

    async def test_a_plan_without_an_attempt_identity_carries_no_grant(self):
        """Legacy callers (no work/attempt) keep the pre-Q39-01 shape."""
        plan = await delivery_plan(
            _registry_with_sibling_bindings(),
            StagedBroker(),
            subject=SUBJECT,
            provider_route="anthropic-gateway",
            profile="github",
            environ=self._env(DELIVERY_MODE_RUNNER_REDEMPTION),
        )
        assert plan is not None
        assert plan.as_document()["schema"] == DELIVERY_PLAN_SCHEMA
        assert plan.operation_grant is None

    def test_minting_honors_the_configured_window(self):
        grant = operation_grant_for_plan(
            _plan_stub(),
            work_id=WORK,
            attempt_generation=7,
            environ={OPERATION_GRANT_WINDOW_ENV: "60"},
            now=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        assert grant.redemption_deadline == datetime(2030, 1, 1, tzinfo=timezone.utc) + (
            timedelta(seconds=60)
        )
        assert grant.attempt_generation == 7


def _plan_stub():
    """A minimal redemption-mode plan for grant minting (the fields the
    mint reads)."""
    from forge.adaptive.credential_broker import CredentialDeliveryPlan

    return CredentialDeliveryPlan(
        subject=SUBJECT_ID,
        provider="anthropic-gateway",
        profile="github",
        credential_ref=GRANTED_REF,
        env_var="ANTHROPIC_AUTH_TOKEN",
        binding_revision=1,
        mode=DELIVERY_MODE_RUNNER_REDEMPTION,
        transport_ref="/lane/credentials/redeem",
        dispatch_ref=GRANTED_REF,
        redemption=True,
    )


# ----------------------------------------------------------------------
# The refusal matrix — every arm ZERO broker calls
# ----------------------------------------------------------------------


class TestRefusalMatrix:
    @pytest.fixture()
    async def granted(self, app):
        """The P01 world: one project, TWO live bindings, the dispatch
        granted the anthropic route; a broker double whose call count is
        the zero-broker-I/O proof."""
        await _put_run(app, generation=2)
        app.state.credential_registry = _registry_with_sibling_bindings()
        broker = _two_route_broker()
        app.state.credential_broker = broker
        grant = await _dispatch_grant(app, generation=2)
        return broker, grant

    async def test_the_p01_sibling_binding_shape(self, app, client, granted):
        """THE probe: the granted route redeems; the SIBLING route of the
        same project refuses with ZERO broker calls — project membership
        is not operation authorization."""
        broker, grant = granted

        dispatched = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
        )
        assert dispatched.status_code == 200
        assert dispatched.json()["value"] == GRANTED_SENTINEL
        assert dispatched.json()["grant_id"] == grant.grant_id
        assert broker.resolve_calls == [GRANTED_REF]

        sibling = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE,
            params=_redeem_params(ref=SIBLING_REF, provider="openai"),
            headers=_gen_headers(),
        )
        assert sibling.status_code == 403
        assert "grant_route_mismatch" in sibling.json()["detail"]
        # ZERO broker calls for the refusal — the check precedes all I/O.
        assert broker.resolve_calls == [GRANTED_REF]
        assert await _redemptions(app) != []
        rows = await _redemptions(app)
        assert all(row["credential_ref"] == GRANTED_REF for row in rows)

    async def test_a_wrong_ref_under_the_granted_route_refuses_zero_broker(
        self, app, client, granted
    ):
        broker, _grant = granted
        response = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE,
            params=_redeem_params(ref="vault:kv/other#1"),
            headers=_gen_headers(),
        )
        assert response.status_code == 403
        assert "grant_ref_mismatch" in response.json()["detail"]
        assert broker.resolve_calls == []

    async def test_a_native_only_attempt_never_redeems(self, app, client):
        """AC-02: a valid lane token for a NATIVE-only dispatch cannot
        redeem a model credential merely because the registry has one."""
        await _put_run(app, generation=2)
        app.state.credential_registry = _registry_with_sibling_bindings()
        broker = _two_route_broker()
        app.state.credential_broker = broker
        # The dispatch evidence names a NATIVE plan for THIS attempt.
        async with app.state.session_factory() as session:
            run = await session.get(FlowRun, WORK)
            evidence = dict(run.evidence or {})
            evidence["harness"] = {
                "dispatch_credential": {
                    "schema": DELIVERY_PLAN_SCHEMA,
                    "mode": DELIVERY_MODE_GITHUB_NATIVE,
                    "attempt_generation": 2,
                }
            }
            run.evidence = evidence
            await session.commit()

        response = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
        )

        assert response.status_code == 403
        assert "grant_absent_native_only" in response.json()["detail"]
        assert broker.resolve_calls == []  # the registry entry alone granted nothing

    async def test_an_unbound_legacy_attempt_never_redeems(self, app, client):
        """No grant, no delivery plan — the labeled legacy boundary."""
        await _put_run(app, generation=2)
        app.state.credential_registry = _registry_with_sibling_bindings()
        broker = _two_route_broker()
        app.state.credential_broker = broker

        response = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
        )

        assert response.status_code == 403
        assert "grant_absent_legacy" in response.json()["detail"]
        assert broker.resolve_calls == []

    async def test_a_terminal_run_refuses_even_with_a_live_grant(self, app, client):
        await _put_run(app, generation=2)
        app.state.credential_registry = _registry_with_sibling_bindings()
        broker = _two_route_broker()
        app.state.credential_broker = broker
        await _dispatch_grant(app, generation=2)
        for status in ("ready_for_human", "blocked", "failed", "cancelled"):
            async with app.state.session_factory() as session:
                run = await session.get(FlowRun, WORK)
                run.status = status
                await session.commit()

            response = await client.get(
                LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
            )

            assert response.status_code == 403
            assert "attempt_terminal" in response.json()["detail"]
            assert broker.resolve_calls == []
            assert await _redemptions(app) == []


# ----------------------------------------------------------------------
# The absolute deadline — frozen in evidence, identical across restarts
# ----------------------------------------------------------------------


class TestAbsoluteDeadline:
    async def test_the_deadline_is_frozen_across_a_simulated_restart(self, tmp_path, monkeypatch):
        """The deadline lives in the PERSISTED grant document: a FRESH
        app (a restarted control plane over the same database) reads the
        SAME instant — and a controlled clock past it refuses, with no
        re-derivation from any TTL."""
        import forge.api_lane_control as api

        async with _live_app(tmp_path) as first_app:
            await _put_run(first_app, generation=2)
            first_app.state.credential_registry = _registry_with_sibling_bindings()
            first_app.state.credential_broker = _two_route_broker()
            grant = await _dispatch_grant(first_app, generation=2, deadline=timedelta(minutes=30))
            transport = ASGITransport(app=first_app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                original = await client.get(
                    LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
                )
            assert original.status_code == 200

        # The RESTARTED control plane: a fresh process over the same DB.
        async with _live_app(tmp_path) as second_app:
            second_app.state.credential_registry = _registry_with_sibling_bindings()
            second_app.state.credential_broker = _two_route_broker()
            transport = ASGITransport(app=second_app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                within = await client.get(
                    LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
                )
                assert within.status_code == 200  # the SAME grant answers
                assert within.json()["grant_id"] == grant.grant_id

                # Advance a controlled clock PAST the persisted deadline
                # (still inside the old now+TTL semantics — that is the
                # hole: a response TTL would run to the full hour).
                real_datetime = api.datetime
                monkeypatch.setattr(
                    api,
                    "datetime",
                    _frozen_clock(real_datetime.now(timezone.utc) + timedelta(minutes=45)),
                )
                try:
                    past = await client.get(
                        LANE_CREDENTIAL_REDEEM_ROUTE,
                        params=_redeem_params(),
                        headers=_gen_headers(),
                    )
                finally:
                    monkeypatch.setattr(api, "datetime", real_datetime)
            assert past.status_code == 403
            assert "grant_expired" in past.json()["detail"]

    async def test_the_response_expiry_never_outlives_the_grant(self, app, client):
        """The TTL metadata is CAPPED at the grant's absolute deadline —
        the value is never presented as current beyond the window."""
        await _put_run(app, generation=2)
        app.state.credential_registry = _registry_with_sibling_bindings()
        app.state.credential_broker = _two_route_broker()
        await _dispatch_grant(app, generation=2, deadline=timedelta(seconds=90))

        response = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
        )

        assert response.status_code == 200
        expires = datetime.fromisoformat(response.json()["expires_at"])
        assert expires - datetime.now(timezone.utc) <= timedelta(seconds=95)


# ----------------------------------------------------------------------
# The lost-response retry window — idempotent under the SAME grant
# ----------------------------------------------------------------------


class TestLostResponseRetry:
    async def test_a_repeated_request_under_the_same_grant_is_idempotent(self, app, client):
        """The acknowledged-but-lost bootstrap response retries INSIDE the
        grant's own lifetime: 200 again, the SAME logical authorization
        (the grant_id join), and separate audit observations."""
        await _put_run(app, generation=2)
        app.state.credential_registry = _registry_with_sibling_bindings()
        app.state.credential_broker = _two_route_broker()
        grant = await _dispatch_grant(app, generation=2, deadline=timedelta(minutes=10))

        first = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
        )
        retry = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
        )

        assert first.status_code == retry.status_code == 200
        assert first.json()["value"] == retry.json()["value"] == GRANTED_SENTINEL
        # The SAME logical redemption: both receipts join on the grant id
        # (distinct observation ids, per-request, as ever).
        assert first.json()["grant_id"] == retry.json()["grant_id"] == grant.grant_id
        assert first.json()["redemption_id"] != retry.json()["redemption_id"]
        rows = await _redemptions(app)
        assert len(rows) == 2
        assert {row["grant_id"] for row in rows} == {grant.grant_id}
        assert rows[0]["grant_retry_observation"] == 0
        assert rows[1]["grant_retry_observation"] == 1
        assert GRANTED_SENTINEL not in json.dumps(rows)  # value-free audit

    async def test_the_retry_window_closes_at_the_absolute_deadline(self, app, client, monkeypatch):
        import forge.api_lane_control as api

        await _put_run(app, generation=2)
        app.state.credential_registry = _registry_with_sibling_bindings()
        app.state.credential_broker = _two_route_broker()
        await _dispatch_grant(app, generation=2, deadline=timedelta(minutes=5))

        within = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
        )
        assert within.status_code == 200

        real_datetime = api.datetime
        monkeypatch.setattr(
            api,
            "datetime",
            _frozen_clock(real_datetime.now(timezone.utc) + timedelta(minutes=6)),
        )
        try:
            past = await client.get(
                LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
            )
        finally:
            monkeypatch.setattr(api, "datetime", real_datetime)

        assert past.status_code == 403
        assert "grant_expired" in past.json()["detail"]
        # No new observation landed after expiry.
        rows = await _redemptions(app)
        assert len(rows) == 1


# ----------------------------------------------------------------------
# The authority fence across the awaited broker resolution
# ----------------------------------------------------------------------


class GatedBroker:
    """The CONTROLLABLE broker double: resolve() blocks until released —
    the await window the fence guards."""

    resolver_identity = "gated"

    def __init__(self, value: str = GRANTED_SENTINEL) -> None:
        self.resolve_calls: list[str] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self._value = value

    async def resolve(
        self, credential_ref: str, *, grant: Mapping | None = None
    ) -> ResolvedCredential:
        self.resolve_calls.append(str(credential_ref))
        self.entered.set()
        await self.release.wait()
        return ResolvedCredential(
            version="v1",
            staged_env={"ANTHROPIC_AUTH_TOKEN": SecretValue(self._value)},
            receipt={"receipt_id": "gated-1"},
            resolver_identity=self.resolver_identity,
            version_kind="fixture",
        )


class TestAuthorityFence:
    async def test_a_cancellation_during_the_await_emits_nothing(self, app, client):
        """AC-04: cancel while the broker is blocked → the resolution
        completes, the fence refuses AFTER it, and NO value leaves (no
        audit row, no 200) under the retired authority."""
        await _put_run(app, generation=2)
        app.state.credential_registry = _registry_with_sibling_bindings()
        broker = GatedBroker()
        app.state.credential_broker = broker
        await _dispatch_grant(app, generation=2)

        request = asyncio.create_task(
            client.get(
                LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
            )
        )
        await broker.entered.wait()
        # The authority retires WHILE the broker is resolving.
        async with app.state.session_factory() as session:
            run = await session.get(FlowRun, WORK)
            run.status = "cancelled"
            await session.commit()
        broker.release.set()
        response = await request

        assert response.status_code == 403
        assert "attempt_terminal" in response.json()["detail"]
        assert broker.resolve_calls == [GRANTED_REF]  # the broker ran…
        assert await _redemptions(app) == []  # …but nothing was emitted

    async def test_a_superseding_generation_during_the_await_emits_nothing(self, app, client):
        await _put_run(app, generation=2)
        app.state.credential_registry = _registry_with_sibling_bindings()
        broker = GatedBroker()
        app.state.credential_broker = broker
        await _dispatch_grant(app, generation=2)

        request = asyncio.create_task(
            client.get(
                LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
            )
        )
        await broker.entered.wait()
        async with app.state.session_factory() as session:
            run = await session.get(FlowRun, WORK)
            run.cancellation_generation = 3  # the attempt moved on
            await session.commit()
        broker.release.set()
        response = await request

        assert response.status_code == 403
        assert "attempt_superseded" in response.json()["detail"]
        assert await _redemptions(app) == []

    async def test_a_rotation_during_the_await_emits_nothing(self, app, client):
        await _put_run(app, generation=2)
        registry = _registry_with_sibling_bindings()
        app.state.credential_registry = registry
        broker = GatedBroker()
        app.state.credential_broker = broker
        await _dispatch_grant(app, generation=2)

        request = asyncio.create_task(
            client.get(
                LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
            )
        )
        await broker.entered.wait()
        registry.bind(SUBJECT, "anthropic-gateway", "vault:kv/eng#42", bound_by="ops@a")
        broker.release.set()
        response = await request

        assert response.status_code == 403
        assert "rotated" in response.json()["detail"]
        assert await _redemptions(app) == []

    async def test_a_quiet_await_publishes_normally(self, app, client):
        """The fence is not a refusal machine: nothing retiring during
        the await → the redemption publishes with the grant join."""
        await _put_run(app, generation=2)
        app.state.credential_registry = _registry_with_sibling_bindings()
        broker = GatedBroker()
        app.state.credential_broker = broker
        grant = await _dispatch_grant(app, generation=2)

        request = asyncio.create_task(
            client.get(
                LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
            )
        )
        await broker.entered.wait()
        broker.release.set()
        response = await request

        assert response.status_code == 200
        assert response.json()["grant_id"] == grant.grant_id
        assert response.json()["value"] == GRANTED_SENTINEL
        rows = await _redemptions(app)
        assert len(rows) == 1 and rows[0]["grant_id"] == grant.grant_id


# ----------------------------------------------------------------------
# The runner's typed verification (units; the subprocess proof is PE)
# ----------------------------------------------------------------------


class TestRunnerVerification:
    @staticmethod
    def _document(**overrides) -> dict:
        fields: dict = {
            "value": "delivered",
            "env_var": "ANTHROPIC_AUTH_TOKEN",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "redemption_id": "r1",
            "grant_id": "g1",
            "work_id": WORK,
            "provider": "anthropic-gateway",
            "credential_ref": GRANTED_REF,
            "attempt_generation": 2,
        }
        fields.update(overrides)
        return fields

    @staticmethod
    def _expected() -> dict:
        return {
            "work_id": WORK,
            "provider": "anthropic-gateway",
            "credential_ref": GRANTED_REF,
            "env_var": "ANTHROPIC_AUTH_TOKEN",
            "attempt_generation": 2,
        }

    def test_the_expected_identity_comes_from_the_envelope(self):
        identity = expected_redemption_identity(
            {
                "FORGE_WORK_ID": WORK,
                "FORGE_LANE_DRIVER": "claude",
                "FORGE_CREDENTIAL_REF": GRANTED_REF,
                "FORGE_ATTEMPT_GENERATION": "2",
            }
        )
        assert identity == self._expected()
        # A lane whose envelope never named the generation expects None —
        # the verification then only demands the response CARRY one.
        assert (
            expected_redemption_identity({"FORGE_WORK_ID": WORK, "FORGE_LANE_DRIVER": "claude"})[
                "attempt_generation"
            ]
            is None
        )

    def test_a_matching_document_passes(self):
        verify_redemption_response(self._document(), expected=self._expected())

    def test_a_wrong_slot_document_fails_closed(self):
        with pytest.raises(Exception, match="credential_redemption_failed.*env_var"):
            verify_redemption_response(
                self._document(env_var="OPENAI_API_KEY"), expected=self._expected()
            )

    def test_an_expired_document_fails_closed(self):
        stale = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with pytest.raises(Exception, match="credential_redemption_failed.*expires_at"):
            verify_redemption_response(self._document(expires_at=stale), expected=self._expected())

    def test_a_wrong_work_route_ref_or_generation_fails_closed(self):
        for axis, mutation in (
            ("work_id", {"work_id": "run-other"}),
            ("provider", {"provider": "openai"}),
            ("credential_ref", {"credential_ref": SIBLING_REF}),
            ("attempt_generation", {"attempt_generation": 9}),
        ):
            with pytest.raises(Exception, match=f"credential_redemption_failed.*{axis}"):
                verify_redemption_response(self._document(**mutation), expected=self._expected())

    def test_a_missing_grant_id_fails_closed(self):
        with pytest.raises(Exception, match="credential_redemption_failed.*grant_id"):
            verify_redemption_response(self._document(grant_id=""), expected=self._expected())
