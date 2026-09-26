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
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import event as sa_event, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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
    PERMITTED_OPERATION_REDEMPTION,
    ConcurrentCredentialRegistry,
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
from forge.adaptive.project_credentials import (
    BINDING_REVISION_UNKNOWN,
    CredentialRefusal,
    ProjectCredentialRegistry,
)
from forge.api_lane_control import (
    LANE_CREDENTIAL_REDEEM_ROUTE,
    LEGACY_CREDENTIAL_ANCHOR_FILE_ENV,
    LaneAuthorityUnavailable,
    lane_control_token,
    persist_operation_grant,
)
from forge.config import Settings
from forge.database import reset_engine
from forge.durable.models import FlowRun, OperationGrant
from forge.models.base import Base
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
            # R40-06 (#342): the dispatched operation identity, the grant's
            # absolute deadline and the binding revision are verified axes.
            "operation": "credential-redemption",
            "redemption_deadline": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "binding_revision": 1,
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
            "binding_revision": None,
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

    # -- R40-06 (#342): the operation identity, the binding revision and
    # the authorization deadline are verified axes of the same fence. --

    def test_a_wrong_operation_answer_fails_closed(self):
        with pytest.raises(Exception, match="credential_redemption_failed.*operation"):
            verify_redemption_response(
                self._document(operation="credential-exfiltration"), expected=self._expected()
            )

    def test_a_missing_operation_answer_fails_closed(self):
        document = self._document()
        del document["operation"]
        with pytest.raises(Exception, match="credential_redemption_failed.*operation"):
            verify_redemption_response(document, expected=self._expected())

    def test_a_wrong_binding_revision_answer_fails_closed_when_the_envelope_named_one(self):
        expected = dict(self._expected(), binding_revision=1)
        with pytest.raises(Exception, match="credential_redemption_failed.*binding_revision"):
            verify_redemption_response(self._document(binding_revision=2), expected=expected)

    def test_a_malformed_binding_revision_answer_fails_closed(self):
        with pytest.raises(Exception, match="credential_redemption_failed.*binding_revision"):
            verify_redemption_response(
                self._document(binding_revision="two"), expected=self._expected()
            )

    def test_a_well_formed_binding_revision_passes_without_an_envelope_name(self):
        # The envelope named no revision: only well-formedness is demanded.
        verify_redemption_response(self._document(binding_revision=5), expected=self._expected())

    def test_an_expiry_outliving_the_grant_deadline_fails_closed(self):
        beyond = datetime.now(timezone.utc) + timedelta(hours=3)  # past the deadline
        with pytest.raises(Exception, match="credential_redemption_failed.*redemption_deadline"):
            verify_redemption_response(
                self._document(expires_at=beyond.isoformat()), expected=self._expected()
            )

    def test_a_missing_grant_deadline_fails_closed(self):
        document = self._document()
        del document["redemption_deadline"]
        with pytest.raises(Exception, match="credential_redemption_failed.*redemption_deadline"):
            verify_redemption_response(document, expected=self._expected())

    def test_the_expected_identity_reads_the_envelope_revision(self):
        identity = expected_redemption_identity(
            {
                "FORGE_WORK_ID": WORK,
                "FORGE_LANE_DRIVER": "claude",
                "FORGE_CREDENTIAL_REF": GRANTED_REF,
                "FORGE_ATTEMPT_GENERATION": "2",
                "FORGE_CREDENTIAL_BINDING_REVISION": "4",
            }
        )
        assert identity["binding_revision"] == 4
        assert (
            expected_redemption_identity({"FORGE_WORK_ID": WORK, "FORGE_LANE_DRIVER": "claude"})[
                "binding_revision"
            ]
            is None
        )


# ----------------------------------------------------------------------
# R40-05 (#341) — the keyed grant authority + the targeted projection
# ----------------------------------------------------------------------
#
# The recorded defect (review b521e1a, probe P02): ``persist_operation_grant``
# read the run row, merged the grant into the WHOLE evidence document and
# wrote the entire JSON back — no conditional version, no row-lock
# discipline. A concurrent native-handle/checkpoint commit between the
# read and the write was silently erased, and two concurrent initial
# grants could return different effective identities. These tests pin the
# replacement contract: the ``operation_grants`` KEYED ROW is the one
# transactional authority (creation races collapse at the unique index;
# replay keeps the first window; rotation replaces under a guarded
# update; corrupt and revoked are typed failures), and the evidence map
# is a DERIVED projection rewritten through a targeted CAS that touches
# ONLY the ``credential_operation_grants`` key.


def _authority_factory(tmp_path, name: str = "grant-authority.db"):
    """A SQLite factory in WAL mode — a reader never blocks the
    concurrent evidence writer the barrier schedules demand."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @sa_event.listens_for(engine.sync_engine, "connect")
    def _wal(dbapi_conn, _record):  # noqa: ANN001 — sqlite3 connection
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine


async def _seed_run(factory, work_id: str, evidence: dict | None = None) -> None:
    async with factory() as session:
        session.add(
            FlowRun(
                id=work_id,
                project_id=PROJECT_ID,
                provider="gitlab",
                status="waiting_harness",
                evidence=evidence if evidence is not None else {"harness": {"attempt": 0}},
            )
        )
        await session.commit()


async def _authority_rows(factory, work_id: str) -> list[OperationGrant]:
    async with factory() as session:
        return list(
            (await session.execute(select(OperationGrant).where(OperationGrant.work_id == work_id)))
            .scalars()
            .all()
        )


async def _evidence_of(factory, work_id: str) -> dict:
    async with factory() as session:
        run = await session.get(FlowRun, work_id)
    return dict(run.evidence or {})


def _utc(moment: datetime) -> datetime:
    """SQLite stores naive datetimes — compare grants in one timezone."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _mint(
    work_id: str = WORK,
    *,
    generation: int = 2,
    ref: str = GRANTED_REF,
    provider: str = "anthropic-gateway",
    grant_id: str | None = None,
    deadline: timedelta = timedelta(hours=1),
) -> CredentialOperationGrant:
    now = datetime.now(timezone.utc)
    return CredentialOperationGrant(
        grant_id=grant_id or uuid.uuid4().hex,
        work_id=work_id,
        subject=SUBJECT_ID,
        provider=provider,
        credential_ref=ref,
        binding_revision=1,
        attempt_generation=generation,
        delivery_mode="runner-redemption",
        redemption_deadline=now + deadline,
        created_at=now,
    )


@pytest.fixture()
async def authority(tmp_path):
    """One WAL SQLite database seeded with the run row under test."""
    engine = _authority_factory(tmp_path)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _seed_run(factory, WORK)
    yield factory
    await engine.dispose()


class TestGrantAuthority:
    async def test_exact_replay_keeps_the_first_window_and_one_active_grant(self, authority):
        first = await persist_operation_grant(
            authority, grant=_mint(deadline=timedelta(minutes=30))
        )
        # A re-dispatch mints a FRESH document with a far later deadline —
        # the replay must keep the FIRST authorization exactly.
        replay = await persist_operation_grant(authority, grant=_mint(deadline=timedelta(days=365)))

        assert replay.grant_id == first.grant_id
        assert replay.redemption_deadline == first.redemption_deadline  # never re-anchored
        rows = await _authority_rows(authority, WORK)
        assert len(rows) == 1  # ONE keyed row — no second active grant
        assert rows[0].grant_id == first.grant_id
        assert rows[0].status == "active"
        grants = (await _evidence_of(authority, WORK))[EVIDENCE_OPERATION_GRANTS_KEY]
        assert list(grants) == ["2:anthropic-gateway"]
        assert grants["2:anthropic-gateway"]["grant_id"] == first.grant_id

    async def test_a_pre_029_evidence_grant_is_adopted_not_re_anchored(self, authority):
        """The upgrade bridge: an in-flight attempt whose grant lives only
        in the (pre-029) evidence projection keeps ITS window — the first
        post-029 persist ADOPTS the standing document into the keyed
        row instead of minting a fresh one."""
        legacy = _mint(grant_id="legacy-grant-1", deadline=timedelta(minutes=10))
        async with authority() as session:
            run = await session.get(FlowRun, WORK)
            run.evidence = {
                "harness": {"attempt": 0},
                EVIDENCE_OPERATION_GRANTS_KEY: {legacy.key(): legacy.as_document()},
            }
            await session.commit()

        effective = await persist_operation_grant(
            authority, grant=_mint(deadline=timedelta(days=365))
        )

        assert effective.grant_id == "legacy-grant-1"
        assert effective.redemption_deadline == legacy.redemption_deadline
        (row,) = await _authority_rows(authority, WORK)
        assert row.grant_id == "legacy-grant-1"
        assert _utc(row.redemption_deadline) == legacy.redemption_deadline

    async def test_a_rotation_replaces_the_keyed_document_under_the_guarded_update(self, authority):
        first = await persist_operation_grant(authority, grant=_mint())
        rotated = await persist_operation_grant(authority, grant=_mint(ref="vault:kv/eng#42"))

        assert rotated.credential_ref == "vault:kv/eng#42"
        assert rotated.grant_id != first.grant_id  # a rotation IS a new grant
        rows = await _authority_rows(authority, WORK)
        assert len(rows) == 1  # still ONE keyed row
        assert rows[0].credential_ref == "vault:kv/eng#42"
        grants = (await _evidence_of(authority, WORK))[EVIDENCE_OPERATION_GRANTS_KEY]
        assert grants["2:anthropic-gateway"]["credential_ref"] == "vault:kv/eng#42"
        # the projection matches the authority — never a divergent identity
        assert grants["2:anthropic-gateway"]["grant_id"] == rows[0].grant_id

    async def test_a_corrupt_authority_document_is_a_typed_refusal_and_stands(self, authority):
        """Recovery distinguishes corrupt: a persisted authorization that
        cannot be loaded is NEVER silently replaced with a fresh window."""
        first = await persist_operation_grant(authority, grant=_mint())
        async with authority() as session:
            row = (await session.execute(select(OperationGrant))).scalar_one()
            row.document = {"schema": OPERATION_GRANT_SCHEMA, "grant_id": ""}  # corrupt
            await session.commit()

        with pytest.raises(LaneAuthorityUnavailable, match="corrupt"):
            await persist_operation_grant(authority, grant=_mint())

        # the corrupt row stands untouched — no fresh window was minted
        (row,) = await _authority_rows(authority, WORK)
        assert row.grant_id == first.grant_id
        assert row.document == {"schema": OPERATION_GRANT_SCHEMA, "grant_id": ""}
        assert (await _authority_rows(authority, WORK))[0].status == "active"

    async def test_a_revoked_key_refuses_both_replay_and_rotation(self, authority):
        await persist_operation_grant(authority, grant=_mint())
        async with authority() as session:
            await session.execute(update(OperationGrant).values(status="revoked"))
            await session.commit()

        with pytest.raises(LaneAuthorityUnavailable, match="revoked"):
            await persist_operation_grant(authority, grant=_mint())
        with pytest.raises(LaneAuthorityUnavailable, match="revoked"):
            await persist_operation_grant(authority, grant=_mint(ref="vault:kv/eng#42"))

    async def test_two_concurrent_creators_collapse_to_one_committed_identity(self, authority):
        """AC-01: two workers minting the same allowed operation return the
        SAME persisted grant id and absolute deadline — the unique index
        is the arbiter, and the loser of the insert reads the committed
        row instead of its own locally minted object."""
        barrier = asyncio.Barrier(2)

        async def creator(deadline: timedelta) -> CredentialOperationGrant:
            await barrier.wait()
            return await persist_operation_grant(authority, grant=_mint(deadline=deadline))

        early, late = await asyncio.gather(
            creator(timedelta(minutes=30)), creator(timedelta(days=365))
        )

        assert early.grant_id == late.grant_id
        assert early.redemption_deadline == late.redemption_deadline
        rows = await _authority_rows(authority, WORK)
        assert len(rows) == 1
        assert rows[0].grant_id == early.grant_id

    async def test_the_raced_rotation_keeps_one_row_and_a_matching_projection(self, authority):
        """The raced-DIFFERENT-refs policy: the key is serialized — each
        contender applies the broker's rotation rule against the row it
        read, the authority ends with EXACTLY ONE committed grant, and
        the projection never diverges from the row. Not a blind
        last-writer-wins clobber: the loser of the create race first
        LOADS the standing document and applies the merge rule to it."""
        barrier = asyncio.Barrier(2)

        async def contender(ref: str) -> CredentialOperationGrant:
            await barrier.wait()
            return await persist_operation_grant(authority, grant=_mint(ref=ref))

        first_effective, second_effective = await asyncio.gather(
            contender("vault:kv/a#1"), contender("vault:kv/b#2")
        )

        rows = await _authority_rows(authority, WORK)
        assert len(rows) == 1  # one keyed row, whatever the interleaving
        # whoever committed LAST owns the row; the projection matches it
        final = rows[0]
        assert final.grant_id in {first_effective.grant_id, second_effective.grant_id}
        grants = (await _evidence_of(authority, WORK))[EVIDENCE_OPERATION_GRANTS_KEY]
        assert grants["2:anthropic-gateway"]["grant_id"] == final.grant_id
        assert grants["2:anthropic-gateway"]["credential_ref"] == final.credential_ref

    async def test_a_death_between_commit_and_projection_recovers_on_replay(
        self, authority, monkeypatch
    ):
        """AC-6 + the projection-failure refusal: the keyed row commits,
        the projection dies (a process death between commit and
        dispatch) — persistence REFUSES typed, and the REPLAY converges
        the projection onto the standing grant with the SAME deadline."""
        import forge.api_lane_control as api

        dead = LaneAuthorityUnavailable("the projection died with the process")

        async def refusing_projection(session_factory, effective):  # noqa: ARG001
            raise dead

        monkeypatch.setattr(api, "_project_operation_grant", refusing_projection)
        with pytest.raises(LaneAuthorityUnavailable):
            await persist_operation_grant(authority, grant=_mint(deadline=timedelta(minutes=30)))
        # the authority row STANDS (committed first)…
        (row,) = await _authority_rows(authority, WORK)
        # …and the evidence carries no grant a lane could redeem against
        assert EVIDENCE_OPERATION_GRANTS_KEY not in (await _evidence_of(authority, WORK))

        monkeypatch.undo()
        replay = await persist_operation_grant(authority, grant=_mint(deadline=timedelta(days=365)))

        assert replay.grant_id == row.grant_id
        assert replay.redemption_deadline == _utc(row.redemption_deadline)  # the window stood
        grants = (await _evidence_of(authority, WORK))[EVIDENCE_OPERATION_GRANTS_KEY]
        assert grants["2:anthropic-gateway"]["grant_id"] == row.grant_id


# ----------------------------------------------------------------------
# The P02 interleaving — driven deterministically through the REAL entry
# ----------------------------------------------------------------------


class _InterleavingSession:
    """A delegating session proxy that holds the caller's FIRST evidence
    WRITE until the concurrent evidence writer has committed — the exact
    P02 window (read → competitor commits → write), driven through the
    real ``persist_operation_grant`` entry, not a dict-merge helper.

    The write is recognized in BOTH shapes: the targeted conditional
    UPDATE the projection issues (new code) and the ORM flush at commit
    after a whole-document read (the pre-#341 shape the mutation arm
    reverts to). Sessions that wrote a grant-row statement are exempt —
    the authority commit is not an evidence write.
    """

    def __init__(self, real, owner: "_InterleavingSessionFactory") -> None:
        self._real = real
        self._owner = owner
        self._read_flow_runs = False
        self._wrote_grant_row = False

    async def __aenter__(self):
        await self._real.__aenter__()
        return self

    async def __aexit__(self, *exc_info):
        return await self._real.__aexit__(*exc_info)

    async def get(self, entity, ident):
        result = await self._real.get(entity, ident)
        if getattr(entity, "__name__", "") == "FlowRun":
            self._read_flow_runs = True
        return result

    async def execute(self, statement, *args, **kwargs):
        text = str(statement)
        if "operation_grants" in text:
            self._wrote_grant_row = True
        if "flow_runs" in text:
            self._read_flow_runs = True
            if getattr(statement, "__visit_name__", "") == "update":
                await self._owner._interleave_once()
        return await self._real.execute(statement, *args, **kwargs)

    async def commit(self):
        if self._read_flow_runs and not self._wrote_grant_row:
            await self._owner._interleave_once()
        return await self._real.commit()

    def __getattr__(self, name):
        return getattr(self._real, name)


class _InterleavingSessionFactory:
    """Wraps a real session factory with ONE deterministic interleave."""

    def __init__(self, inner, interleave: Callable[[], Awaitable[None]]) -> None:
        self._inner = inner
        self._interleave = interleave
        self.fired = False

    async def _interleave_once(self) -> None:
        if not self.fired:
            self.fired = True
            await self._interleave()

    def __call__(self) -> _InterleavingSession:
        return _InterleavingSession(self._inner(), self)


class TestInterleavedEvidenceSurvives:
    async def test_native_checkpoint_and_review_fields_survive_the_grant(self, tmp_path):
        """The P02 acceptance: with barriers after the read / before the
        write / after the commit, interleaved grant + checkpoint +
        native-effect updates lose NO field — the grant projection is a
        targeted merge, never a whole-document replacement. Reverting
        the conditional update to the old whole-document write makes
        this test FAIL (the native handle is the field it loses)."""
        engine = _authority_factory(tmp_path, "interleaved.db")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        await _seed_run(factory, WORK, evidence={"harness": {"attempt": 0}})

        async def native_and_checkpoint_writer() -> None:
            # B — lands BETWEEN the projection's read and its write
            async with factory() as session:
                run = await session.get(FlowRun, WORK)
                evidence = dict(run.evidence or {})
                evidence["harness"] = {"attempt": 1, "native_handle": "gh:run:42"}
                evidence["checkpoints"] = {"cp-1": {"sha": "deadbeef", "seq": 7}}
                run.evidence = evidence
                await session.commit()

        interleave = native_and_checkpoint_writer
        stepped = _InterleavingSessionFactory(factory, interleave)

        effective = await persist_operation_grant(stepped, grant=_mint())

        # C — a checkpoint/review writer AFTER the commit: the document
        # keeps accepting concurrent writers (nothing regressed)
        async with factory() as session:
            run = await session.get(FlowRun, WORK)
            evidence = dict(run.evidence or {})
            evidence["review"] = {"verdict": "approved", "by": "ops"}
            run.evidence = evidence
            await session.commit()

        assert stepped.fired  # the barrier really placed B inside the window
        evidence = await _evidence_of(factory, WORK)
        # B's fields SURVIVED the grant write (the pre-#341 write lost
        # both — this is the mutation the test detects)
        assert evidence["harness"]["native_handle"] == "gh:run:42"
        assert evidence["checkpoints"] == {"cp-1": {"sha": "deadbeef", "seq": 7}}
        # C's post-commit field stands
        assert evidence["review"] == {"verdict": "approved", "by": "ops"}
        # the grant projection landed beside them
        grants = evidence[EVIDENCE_OPERATION_GRANTS_KEY]
        assert grants["2:anthropic-gateway"]["grant_id"] == effective.grant_id
        # the authority row agrees with the projection
        (row,) = await _authority_rows(factory, WORK)
        assert row.grant_id == effective.grant_id
        await engine.dispose()


# ----------------------------------------------------------------------
# Failed persistence — zero native starts, zero credential returns
# ----------------------------------------------------------------------


class TestFailedPersistenceParksTheDispatch:
    """AC-4: a grant that cannot be persisted parks the run BEFORE the
    provider call — the callers' ``except LaneAuthorityUnavailable``
    contract (runs/service.py and its github/azure siblings) — and the
    redemption surface returns nothing without the persisted grant."""

    @staticmethod
    def _harness_service(db, fake_gitlab, *, registry, broker):
        from forge.config import ForgeConfig
        from forge.repository import ChangesetWriter
        from forge.runs import RunService
        from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer

        values = dict(
            GITLAB_URL="https://gitlab.test",
            GITLAB_TOKEN="glpat-test",
            GITLAB_WEBHOOK_SECRET="whsec",
            FORGE_APPROVERS="alice",
            DATABASE_URL="sqlite+aiosqlite:///:memory:",
            FORGE_IMPLEMENTER_BACKEND="ci_harness",
        )
        return RunService(
            db,
            fake_gitlab,
            Settings(**values),
            ForgeConfig(),
            writer_class=ChangesetWriter,
            planner=StubPlanner(),
            implementer=StubImplementer(),
            reviewer=StubReviewer(),
            credential_registry=registry,
            credential_broker=broker,
        )

    @pytest.fixture()
    async def db(self):
        from sqlalchemy.pool import StaticPool

        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
        await engine.dispose()

    @pytest.fixture()
    def fake_gitlab(self):
        from tests.fixtures.fake_gitlab import FakeGitLab

        fake = FakeGitLab()
        fake.seed_issue(7, "Add a widget", "Widgets make the app better.")
        fake.seed_commit("main", "base-sha-1", "initial")
        return fake

    @staticmethod
    def _delivery_env(monkeypatch) -> None:
        monkeypatch.setenv(DELIVERY_ROUTE_ENV, DELIVERY_MODE_RUNNER_REDEMPTION)
        monkeypatch.setenv(DELIVERY_TEMPLATE_DIR_ENV, TEMPLATES_DIR)
        monkeypatch.setenv(CREDENTIAL_POLICY_ENV, "compat")

    @staticmethod
    def _registry_and_broker():
        registry = ProjectCredentialRegistry()
        registry.bind(
            CanonicalSubject(provider_family="gitlab", connection="-", native_id="42"),
            "anthropic-gateway",
            GRANTED_REF,
            bound_by="ops@a",
        )
        broker = StagedBroker()
        broker.stage(GRANTED_REF, GRANTED_SENTINEL, env_var="ANTHROPIC_AUTH_TOKEN", version="v1")
        return registry, broker

    async def _start_and_go(self, service) -> str:
        run_id = await service.start_run(
            42, 7, "Add a widget", "Widgets make the app better.", "alice"
        )
        await service.handle_command_note(42, f"@forge /go {run_id}", "alice", 7, author_user_id=11)
        return run_id

    async def test_the_control_dispatch_persists_the_grant_and_starts_the_pipeline(
        self, db, fake_gitlab, monkeypatch
    ):
        """The drive really reaches the grant path: a healthy persistence
        lands BOTH the keyed authority row and the projection, and the
        native pipeline starts exactly once."""
        self._delivery_env(monkeypatch)
        registry, broker = self._registry_and_broker()
        service = self._harness_service(db, fake_gitlab, registry=registry, broker=broker)
        run_id = await self._start_and_go(service)

        async with db() as session:
            run = await session.get(FlowRun, run_id)
        assert run.status == "waiting_harness"
        assert len(fake_gitlab.pipelines) == 1  # the native start happened
        (row,) = await _authority_rows(db, run_id)
        assert row.credential_ref == GRANTED_REF
        grants = (await _evidence_of(db, run_id))[EVIDENCE_OPERATION_GRANTS_KEY]
        assert grants["0:anthropic-gateway"]["grant_id"] == row.grant_id

    async def test_a_failed_persistence_parks_the_run_with_zero_native_starts(
        self, db, fake_gitlab, monkeypatch
    ):
        """The provider boundary is never reached: the typed persistence
        failure parks the run and the pipeline count stays ZERO — no
        native start, so no lane can boot against an authorization
        nobody persisted."""
        import forge.api_lane_control as api

        self._delivery_env(monkeypatch)
        registry, broker = self._registry_and_broker()
        service = self._harness_service(db, fake_gitlab, registry=registry, broker=broker)

        async def refusing_persist(session_factory, *, grant):  # noqa: ARG001
            raise LaneAuthorityUnavailable(
                "the operation grant could not be persisted (fixture injection)"
            )

        monkeypatch.setattr(api, "persist_operation_grant", refusing_persist)
        run_id = await self._start_and_go(service)

        async with db() as session:
            run = await session.get(FlowRun, run_id)
        assert run.status == "blocked"
        assert "the operation grant could not be persisted" in str(run.status_reason)
        assert fake_gitlab.pipelines == []  # ZERO native starts
        assert await _authority_rows(db, run_id) == []
        assert EVIDENCE_OPERATION_GRANTS_KEY not in (await _evidence_of(db, run_id))

    async def test_a_committed_authority_row_authorizes_even_when_the_projection_lags(
        self, app, monkeypatch
    ):
        """The redemption surface reads the ONE authoritative home — the
        KEYED row (#341's commit point), not the derived evidence
        projection. When the projection dies mid-persistence the row
        still STANDS committed, so a redemption against it succeeds with
        the grant join (R40-06/#342 moved the validation onto the row;
        under the pre-#342 evidence read this same state refused
        ``grant_absent``). The dispatch itself is still parked by the
        persist refusal — zero native starts is the RunService arm
        above; the run this fixture creates directly never parked."""
        import forge.api_lane_control as api

        await _put_run(app, generation=2)
        app.state.credential_registry = _registry_with_sibling_bindings()
        broker = _two_route_broker()
        app.state.credential_broker = broker

        async def refusing_projection(session_factory, effective):  # noqa: ARG001
            raise LaneAuthorityUnavailable("the projection died with the process")

        monkeypatch.setattr(api, "_project_operation_grant", refusing_projection)
        with pytest.raises(LaneAuthorityUnavailable):
            await _dispatch_grant(app, generation=2)
        monkeypatch.undo()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
            )
        # The COMMITTED keyed row is the authority: the redemption lands
        # with its grant id (the lagging projection converges on replay).
        assert response.status_code == 200
        async with app.state.session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(OperationGrant).where(OperationGrant.work_id == WORK)
                    )
                )
                .scalars()
                .all()
            )
        assert [row.grant_id for row in rows] == [response.json()["grant_id"]]
        assert broker.resolve_calls == [GRANTED_REF]

    async def test_a_run_with_no_persisted_grant_at_all_yields_zero_credential_returns(self, app):
        """The negative boundary that stays: when NOTHING persisted — no
        keyed row and no projection (the dispatch never got that far) —
        the lane redeems NOTHING."""
        await _put_run(app, generation=2)
        app.state.credential_registry = _registry_with_sibling_bindings()
        broker = _two_route_broker()
        app.state.credential_broker = broker

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
            )
        assert response.status_code == 403
        assert "grant_absent" in response.json()["detail"]
        assert broker.resolve_calls == []  # ZERO credential returns


# ----------------------------------------------------------------------
# The PG-gated variant — real independent sessions, real isolation
# ----------------------------------------------------------------------


class TestRealPostgres:
    @pytest.mark.skipif(
        not os.environ.get("FORGE_PG_TEST_URL"),
        reason=(
            "FORGE_PG_TEST_URL not set — the real-PostgreSQL two-session "
            "grant-authority proofs run only against a disposable real Postgres"
        ),
    )
    async def test_concurrent_creators_and_interleaved_evidence_writers_survive(self):
        """The full R40-05 scenario on real PostgreSQL: two real sessions
        race the grant creation through the ACTUAL entry point (one
        committed identity), a concurrent evidence writer interleaves
        inside the projection's window (no field lost), and the exact
        replay neither extends the window nor adds a second grant."""
        engine = create_async_engine(os.environ["FORGE_PG_TEST_URL"])
        try:
            from forge.durable.models import CredentialRedemption

            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                # the shared disposable database may hold rows for this run
                # id from sibling PG-gated proofs — clear them FK-first.
                await conn.execute(
                    OperationGrant.__table__.delete().where(OperationGrant.work_id == WORK)
                )
                await conn.execute(
                    CredentialRedemption.__table__.delete().where(
                        CredentialRedemption.work_id == WORK
                    )
                )
                await conn.execute(FlowRun.__table__.delete().where(FlowRun.id == WORK))
                await conn.execute(
                    FlowRun.__table__.insert().values(
                        [
                            {
                                "id": WORK,
                                "project_id": PROJECT_ID,
                                "provider": "gitlab",
                                "status": "waiting_harness",
                                "evidence": {"harness": {"attempt": 0}},
                            }
                        ]
                    )
                )
            factory = async_sessionmaker(engine, expire_on_commit=False)

            # 1. two concurrent creators through the REAL entry → ONE identity
            barrier = asyncio.Barrier(2)

            async def creator(deadline: timedelta) -> CredentialOperationGrant:
                await barrier.wait()
                return await persist_operation_grant(factory, grant=_mint(deadline=deadline))

            early, late = await asyncio.gather(
                creator(timedelta(minutes=30)), creator(timedelta(days=365))
            )
            assert early.grant_id == late.grant_id
            assert early.redemption_deadline == late.redemption_deadline
            rows = await _authority_rows(factory, WORK)
            assert len(rows) == 1

            # 2. the interleaved evidence writer INSIDE the projection window
            async def native_and_checkpoint_writer() -> None:
                async with factory() as session:
                    run = await session.get(FlowRun, WORK)
                    evidence = dict(run.evidence or {})
                    evidence["harness"] = {"attempt": 1, "native_handle": "gl:pipe:77"}
                    evidence["checkpoints"] = {"cp-1": {"sha": "deadbeef", "seq": 7}}
                    run.evidence = evidence
                    await session.commit()

            stepped = _InterleavingSessionFactory(factory, native_and_checkpoint_writer)
            replay = await persist_operation_grant(stepped, grant=_mint())

            assert stepped.fired  # the barrier really interleaved the writer
            assert replay.grant_id == rows[0].grant_id  # the replay kept the row
            assert replay.redemption_deadline == rows[0].redemption_deadline
            rows_after = await _authority_rows(factory, WORK)
            assert len(rows_after) == 1  # no second active grant

            # 3. a post-commit review writer — and EVERY field survives
            async with factory() as session:
                run = await session.get(FlowRun, WORK)
                evidence = dict(run.evidence or {})
                evidence["review"] = {"verdict": "approved", "by": "ops"}
                run.evidence = evidence
                await session.commit()

            evidence = await _evidence_of(factory, WORK)
            assert evidence["harness"]["native_handle"] == "gl:pipe:77"
            assert evidence["checkpoints"] == {"cp-1": {"sha": "deadbeef", "seq": 7}}
            assert evidence["review"] == {"verdict": "approved", "by": "ops"}
            grants = evidence[EVIDENCE_OPERATION_GRANTS_KEY]
            assert grants["2:anthropic-gateway"]["grant_id"] == rows_after[0].grant_id
        finally:
            await engine.dispose()


# ----------------------------------------------------------------------
# R40-06 (#342) — the COMPLETE grant identity validated BEFORE the broker
# is invoked; the binding revision compared at redemption; the authority
# row re-read across the awaited broker resolution. Every refusal arm
# proves ZERO emitted credential values: the broker double's call count
# and the absence of the sentinel everywhere.
# ----------------------------------------------------------------------


async def _standing_row(app):
    async with app.state.session_factory() as session:
        return (
            await session.execute(select(OperationGrant).where(OperationGrant.work_id == WORK))
        ).scalar_one()


async def _mutate_standing_document(app, **fields):
    """Rewrite fields INSIDE the standing authority row's document (the
    tampering surface the identity validation guards)."""
    async with app.state.session_factory() as session:
        row = (
            await session.execute(select(OperationGrant).where(OperationGrant.work_id == WORK))
        ).scalar_one()
        document = dict(row.document or {})
        for name, value in fields.items():
            if value is _DELETE:
                document.pop(name, None)
            else:
                document[name] = value
        row.document = document
        await session.commit()


class _DELETE:
    """Sentinel for 'remove the field from the document'."""


class TestGrantIdentityRefusedPreBroker:
    """Wrong schema / operation / delivery mode / work / subject /
    internal generation — each a typed ``grant_invalid_field`` refusal
    BEFORE the broker is invoked; plus the revoked authority row and the
    row↔document key drift."""

    @pytest.fixture()
    async def granted(self, app):
        await _put_run(app, generation=2)
        app.state.credential_registry = _registry_with_sibling_bindings()
        broker = _two_route_broker()
        app.state.credential_broker = broker
        await _dispatch_grant(app, generation=2)
        return broker

    @pytest.mark.parametrize(
        ("field", "fields"),
        (
            ("schema", {"schema": "forge.credential.operation-grant/2"}),
            ("operation", {"operation": "credential-exfiltration"}),
            ("delivery_mode", {"delivery_mode": DELIVERY_MODE_GITHUB_NATIVE}),
            ("work_id", {"work_id": "run-other"}),
            ("subject", {"subject": "gitlab/gitlab.other/90210"}),
            ("attempt_generation", {"attempt_generation": 7}),
        ),
    )
    async def test_each_wrong_identity_field_refuses_pre_broker(
        self, app, client, granted, field, fields
    ):
        broker = granted
        await _mutate_standing_document(app, **fields)
        response = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
        )
        assert response.status_code == 403
        assert "grant_invalid_field" in response.json()["detail"]
        assert field in response.json()["detail"]
        # ZERO broker calls and ZERO emissions for the refusal.
        assert broker.resolve_calls == []
        assert await _redemptions(app) == []
        assert GRANTED_SENTINEL not in response.text

    async def test_a_revoked_authority_row_refuses_pre_broker(self, app, client, granted):
        broker = granted
        async with app.state.session_factory() as session:
            await session.execute(update(OperationGrant).values(status="revoked"))
            await session.commit()
        response = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
        )
        assert response.status_code == 403
        assert "grant_revoked" in response.json()["detail"]
        assert broker.resolve_calls == []
        assert await _redemptions(app) == []
        assert GRANTED_SENTINEL not in response.text

    async def test_a_row_document_diverging_from_its_key_columns_refuses(
        self, app, client, granted
    ):
        """KEY CONSISTENCY: the authority row's columns and the document
        inside it disagree (grant_id drift) — typed, pre-broker."""
        broker = granted
        async with app.state.session_factory() as session:
            row = (
                await session.execute(select(OperationGrant).where(OperationGrant.work_id == WORK))
            ).scalar_one()
            row.grant_id = "a-different-grant-id"
            await session.commit()
        response = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
        )
        assert response.status_code == 403
        assert "grant_invalid_field" in response.json()["detail"]
        assert "authority_row.grant_id" in response.json()["detail"]
        assert broker.resolve_calls == []
        assert await _redemptions(app) == []


class TestBindingRevisionAtRedemption:
    """The CURRENT binding revision vs the grant's recorded revision:
    same-ref rebind rev1→rev2 does NOT silently redeem under the rev1
    grant; exact replay within the same revision does; the changed-ref
    case is kept SEPARATE (the rotation reasons own it)."""

    @pytest.fixture()
    async def granted(self, app):
        await _put_run(app, generation=2)
        registry = _registry_with_sibling_bindings()
        app.state.credential_registry = registry
        broker = _two_route_broker()
        app.state.credential_broker = broker
        await _dispatch_grant(app, generation=2)  # the grant records revision 1
        return registry, broker

    async def test_a_same_ref_rebind_refuses_typed_with_zero_emissions(self, app, client, granted):
        """AC-02: ``bind()`` at the SAME ref is still a NEW decision
        (revision 2) — the rev1 grant does not silently redeem."""
        registry, broker = granted
        registry.bind(SUBJECT, "anthropic-gateway", GRANTED_REF, bound_by="ops@a")
        assert registry.binding_for(SUBJECT, "anthropic-gateway").revision == 2

        response = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
        )
        assert response.status_code == 403
        assert "binding_revision_mismatch" in response.json()["detail"]
        assert "authorized_binding_revision" in response.json()["detail"]
        # ZERO emitted credential values: no broker call, no audit row, no
        # sentinel anywhere in the refusal.
        assert broker.resolve_calls == []
        assert await _redemptions(app) == []
        assert GRANTED_SENTINEL not in response.text
        assert SIBLING_SENTINEL not in response.text

    async def test_a_revoke_then_regrant_at_the_same_locator_refuses(self, app, client, granted):
        """AC-03: revoke-then-regrant at the same locator — a NEW decision
        or a typed refusal, never transparent reuse of the old grant."""
        registry, broker = granted
        registry.revoke(SUBJECT, "anthropic-gateway", revoked_by="ops@a")
        registry.bind(SUBJECT, "anthropic-gateway", GRANTED_REF, bound_by="ops@a")

        response = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
        )
        assert response.status_code == 403
        assert "binding_revision_mismatch" in response.json()["detail"]
        assert broker.resolve_calls == []
        assert await _redemptions(app) == []

    async def test_a_changed_ref_rotation_is_the_separate_case_old_ref(self, app, client, granted):
        """CHANGED-ref, request still naming the OLD ref: the registry's
        rotation refusal owns it (never the revision axis)."""
        registry, broker = granted
        registry.bind(SUBJECT, "anthropic-gateway", "vault:kv/eng#42", bound_by="ops@a")
        response = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
        )
        assert response.status_code == 403
        assert "rotated" in response.json()["detail"]
        assert broker.resolve_calls == []

    async def test_a_changed_ref_rotation_is_the_separate_case_new_ref(self, app, client, granted):
        """CHANGED-ref, request naming the NEW ref: the grant's EXACT ref
        mismatch owns it (the grant still authorizes the old ref)."""
        registry, broker = granted
        registry.bind(SUBJECT, "anthropic-gateway", "vault:kv/eng#42", bound_by="ops@a")
        broker.stage("vault:kv/eng#42", "v-material", env_var="ANTHROPIC_AUTH_TOKEN")
        response = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE,
            params=_redeem_params(ref="vault:kv/eng#42"),
            headers=_gen_headers(),
        )
        assert response.status_code == 403
        assert "grant_ref_mismatch" in response.json()["detail"]
        assert broker.resolve_calls == []
        assert await _redemptions(app) == []

    async def test_exact_replay_within_the_same_revision_does_not_extend_the_deadline(
        self, app, client, granted
    ):
        """AC-07: the exact request redeems idempotently inside the SAME
        revision — and the persisted deadline never moves."""
        _registry, broker = granted
        row_before = await _standing_row(app)
        first = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
        )
        again = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
        )
        assert first.status_code == again.status_code == 200
        assert first.json()["grant_id"] == again.json()["grant_id"] == row_before.grant_id
        # The response now names the operation + the grant's absolute
        # deadline; the presented expiry is capped at it.
        assert first.json()["operation"] == PERMITTED_OPERATION_REDEMPTION
        assert datetime.fromisoformat(first.json()["redemption_deadline"]) == _utc(
            row_before.redemption_deadline
        )
        assert first.json()["expires_at"] <= first.json()["redemption_deadline"]
        row_after = await _standing_row(app)
        assert row_after.redemption_deadline == row_before.redemption_deadline
        assert broker.resolve_calls == [GRANTED_REF, GRANTED_REF]
        rows = await _redemptions(app)
        assert len(rows) == 2
        assert all(row["grant_binding_revision"] == 1 for row in rows)
        assert all(row["binding_revision"] == 1 for row in rows)

    async def test_a_grandfathered_pre_revision_grant_redeems_explicitly(
        self, app, client, granted
    ):
        """The version adapter: a grant document persisted BEFORE the
        revision axis (the slot absent) loads as the EXPLICIT unknown
        marker and grandfathers through the comparison — the audit names
        both the live revision and the grant's marker."""
        _registry, broker = granted
        await _mutate_standing_document(app, binding_revision=_DELETE)
        response = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
        )
        assert response.status_code == 200
        assert response.json()["binding_revision"] == 1  # the LIVE revision, honestly
        rows = await _redemptions(app)
        assert rows[0]["grant_binding_revision"] == BINDING_REVISION_UNKNOWN
        assert rows[0]["binding_revision"] == 1
        assert broker.resolve_calls == [GRANTED_REF]


class TestAuthorityFenceR40:
    """The fence re-reads the AUTHORITY ROW + the binding across the
    awaited broker resolution (the documented linearization contract):
    whatever committed before the audit+publish refuses with zero
    emission; the broker resolving is not publication."""

    @staticmethod
    async def _granted_with_gated_broker(app):
        await _put_run(app, generation=2)
        registry = _registry_with_sibling_bindings()
        app.state.credential_registry = registry
        broker = GatedBroker()
        app.state.credential_broker = broker
        await _dispatch_grant(app, generation=2)
        return registry, broker

    async def _redeem_while_blocked(self, client, broker) -> object:
        request = asyncio.create_task(
            client.get(
                LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
            )
        )
        await broker.entered.wait()
        return request

    async def test_a_pre_broker_refusal_never_wakes_the_paused_broker(self, app, client):
        """The broker stays PAUSED and is never entered: the binding is
        re-decided before the request, so the refusal happens entirely
        pre-broker — zero emitted credential values by construction."""
        registry, broker = await self._granted_with_gated_broker(app)
        registry.bind(SUBJECT, "anthropic-gateway", GRANTED_REF, bound_by="ops@a")  # rev 2
        response = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
        )
        assert response.status_code == 403
        assert "binding_revision_mismatch" in response.json()["detail"]
        assert broker.resolve_calls == []  # the paused broker NEVER woke
        assert not broker.entered.is_set()
        assert await _redemptions(app) == []
        assert GRANTED_SENTINEL not in response.text

    async def test_a_same_ref_rebind_during_the_await_emits_nothing(self, app, client):
        """Rotation during a slow broker call, SAME-ref shape: the fence's
        binding re-check compares revisions and refuses AFTER the broker
        resolved — no audit row, no value under the new decision."""
        registry, broker = await self._granted_with_gated_broker(app)
        request = await self._redeem_while_blocked(client, broker)
        registry.bind(SUBJECT, "anthropic-gateway", GRANTED_REF, bound_by="ops@a")
        broker.release.set()
        response = await request

        assert response.status_code == 403
        assert "binding_revision_mismatch" in response.json()["detail"]
        assert broker.resolve_calls == [GRANTED_REF]  # the broker ran…
        assert await _redemptions(app) == []  # …but nothing was emitted
        assert GRANTED_SENTINEL not in response.text

    async def test_a_grant_revocation_during_the_await_emits_nothing(self, app, client):
        _registry, broker = await self._granted_with_gated_broker(app)
        request = await self._redeem_while_blocked(client, broker)
        async with app.state.session_factory() as session:
            await session.execute(update(OperationGrant).values(status="revoked"))
            await session.commit()
        broker.release.set()
        response = await request

        assert response.status_code == 403
        assert "grant_revoked" in response.json()["detail"]
        assert await _redemptions(app) == []
        assert GRANTED_SENTINEL not in response.text

    async def test_a_grant_replacement_during_the_await_emits_nothing(self, app, client):
        """A rotation that REPLACED the keyed row while the broker was
        resolving: the in-memory grant cannot make the new row live."""
        _registry, broker = await self._granted_with_gated_broker(app)
        request = await self._redeem_while_blocked(client, broker)
        # The rotation's own path: a re-dispatch under a different ref
        # replaces the keyed document through the guarded update.
        await _dispatch_grant(app, generation=2, ref="vault:kv/eng#42")
        broker.release.set()
        response = await request

        assert response.status_code == 403
        assert "grant_superseded" in response.json()["detail"]
        assert await _redemptions(app) == []
        assert GRANTED_SENTINEL not in response.text


class TestRegistryReaderProcess:
    """The MULTI-PROCESS arm: a SEPARATE registry-writer process rebinds
    the same ref (revision 2); the endpoint's registry reader observes
    the new generation within its documented stat TTL and refuses the
    rev1 grant — the revision axis holds across process boundaries."""

    @staticmethod
    def _writer_script() -> str:
        return (
            "import pathlib, sys\n"
            "from forge.adaptive.operator_snapshot import CanonicalSubject\n"
            "from forge.adaptive.project_credentials import ProjectCredentialRegistry\n"
            "path, family, connection, native_id, provider, ref = sys.argv[1:7]\n"
            "registry = ProjectCredentialRegistry(path=pathlib.Path(path))\n"
            "subject = CanonicalSubject(provider_family=family, connection=connection, "
            "native_id=native_id)\n"
            "binding = registry.bind(subject, provider, ref, bound_by='ops@process')\n"
            "print(binding.revision)\n"
        )

    async def test_another_process_rebind_observed_by_the_endpoint_reader(
        self, app, client, tmp_path
    ):
        import time as _time

        registry_path = tmp_path / "reader-process-bindings.json"
        registry = ConcurrentCredentialRegistry(path=registry_path, ttl_seconds=0.05)
        registry.bind(SUBJECT, "anthropic-gateway", GRANTED_REF, bound_by="ops@a")
        registry.bind(SUBJECT, "openai", SIBLING_REF, bound_by="ops@a")
        app.state.credential_registry = registry
        broker = _two_route_broker()
        app.state.credential_broker = broker
        await _put_run(app, generation=2)
        await _dispatch_grant(app, generation=2)  # the grant records revision 1

        # The reader's own view, first: the exact request redeems.
        first = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
        )
        assert first.status_code == 200

        # A SEPARATE PROCESS rebinds the SAME ref — a new decision at the
        # same locator, revision 2, on the shared document.
        writer = subprocess.run(
            [
                sys.executable,
                "-c",
                self._writer_script(),
                str(registry_path),
                "gitlab",
                "-",
                str(PROJECT_ID),
                "anthropic-gateway",
                GRANTED_REF,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(Path(__file__).parents[1]),
        )
        assert writer.returncode == 0, writer.stderr
        assert writer.stdout.strip() == "2"  # the WRITER's own reader view

        # Past the registry's stat TTL the endpoint's reader observes the
        # rebind: the rev1 grant no longer redeems (typed, zero emissions).
        _time.sleep(0.1)
        second = await client.get(
            LANE_CREDENTIAL_REDEEM_ROUTE, params=_redeem_params(), headers=_gen_headers()
        )
        assert second.status_code == 403
        assert "binding_revision_mismatch" in second.json()["detail"]
        assert broker.resolve_calls == [GRANTED_REF]  # only the FIRST (allowed) request
        assert GRANTED_SENTINEL not in second.text
