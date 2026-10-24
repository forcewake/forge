"""Actual JWT cryptography and native durable lane identity authorization."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select, update

from forge.api.lane_auth import LaneAuthService
from forge.api.lane_auth_values import LaneAuthError
from forge.api.lane_settings import LaneAuthSettings
from forge.api.lane_jwks import JwksCache
from forge.store.lane_auth import EngineLaneAuthStore
from forge.store.lane_v1 import EngineLaneV1Store
from forge.store.inbox_models import EventInbox
from forge.store.models_runs import EngineRun
from forge.store.models_lane import EngineLaneSession
from tests.native_lane_fixture import (
    PUBLIC,
    GH_ISSUER,
    GH_KEYS,
    WORKFLOW,
    oidc,
    exchange,
    auth as auth,
    settings as settings,
    signing_key as signing_key,
)


@pytest.mark.parametrize("provider", ["github", "gitlab"])
async def test_signed_exchange_reconnect_and_replay_survive_a_new_service(
    auth, settings, signing_key, provider
):
    service, factory, register, requests = auth
    pending = await register(provider)
    first = await exchange(service, pending, oidc(signing_key, pending))
    assert await service.authenticate_bearer("Bearer " + first.access_token) == pending.identity
    restarted = LaneAuthService(EngineLaneAuthStore(factory), settings, jwks_cache=service.jwks)
    with pytest.raises(LaneAuthError, match="already been used"):
        await exchange(restarted, pending, oidc(signing_key, pending))
    second = await exchange(
        restarted, pending, oidc(signing_key, pending, changes={"jti": "jwt-2"})
    )
    assert second.expires_at > datetime.now(timezone.utc)
    assert len(requests) == 1
    async with factory() as session:
        row = await session.get(EngineRun, pending.identity.forge_run_id)
        assert row.reason == "preserve-other-owner"
        saved = await session.scalar(select(EngineLaneSession))
        assert saved.cancellation_generation == 3 and saved.nonce_consumed
        receipts = (
            await session.scalars(
                select(EventInbox).where(EventInbox.event_type == "lane.oidc.jti")
            )
        ).all()
        assert len(receipts) == 2
        assert all(
            len(r.source_event_id) == 64
            and set(r.payload) == {"expires_at"}
            and r.status == "processed"
            for r in receipts
        )
        assert "jwt-1" not in saved.first_job_json + saved.api_state_json


@pytest.mark.parametrize(
    "provider,changes",
    [
        ("github", {"iss": "https://attacker.example"}),
        ("github", {"aud": PUBLIC}),
        ("github", {"aud": [PUBLIC]}),
        ("github", {"repository": "other/repo"}),
        ("github", {"ref": "refs/heads/unapproved"}),
        ("github", {"job_workflow_ref": WORKFLOW.replace("main", "untrusted")}),
        ("github", {"job_workflow_ref": None, "workflow_ref": WORKFLOW}),
        ("github", {"run_id": None}),
        ("github", {"run_attempt": False}),
        ("github", {"jti": None}),
        ("github", {"exp": 1}),
        ("github", {"exp": None}),
        ("github", {"iat": "123"}),
        ("github", {"nbf": 9999999999}),
        ("github", {"sub": None}),
        ("gitlab", {"pipeline_id": "99"}),
        ("gitlab", {"project_path": "other/repo"}),
        ("gitlab", {"ref": "unapproved"}),
        ("gitlab", {"job_id": None}),
    ],
)
async def test_oidc_negative_claims_never_consume_dispatch(auth, signing_key, provider, changes):
    service, factory, register, _ = auth
    pending = await register(provider)
    with pytest.raises(LaneAuthError):
        await exchange(service, pending, oidc(signing_key, pending, changes=changes))
    current = await service.store.load(pending.identity)
    assert not current.nonce_consumed
    async with factory() as session:
        assert not (
            await session.scalars(
                select(EventInbox).where(EventInbox.event_type == "lane.oidc.jti")
            )
        ).all()


@pytest.mark.parametrize(
    "headers",
    [
        {"alg": "HS256"},
        {"jku": "http://metadata.invalid"},
        {"x5u": "https://attacker.invalid/key"},
        {"crit": ["custom"]},
    ],
)
async def test_algorithm_and_token_selected_key_urls_are_refused_before_http(
    auth, signing_key, headers
):
    service, _, register, requests = auth
    pending = await register()
    algorithm = headers.get("alg", "RS256")
    token = oidc(
        "attacker-key-at-least-32-bytes-long" if algorithm == "HS256" else signing_key,
        pending,
        headers=headers,
        algorithm=algorithm,
    )
    with pytest.raises(LaneAuthError):
        await exchange(service, pending, token)
    assert requests == []


@pytest.mark.parametrize(
    "provider,changed",
    [
        ("github", {"run_id": "200"}),
        ("github", {"run_attempt": "2"}),
        ("gitlab", {"job_id": "999"}),
    ],
)
async def test_reconnect_cannot_change_bound_provider_job(auth, signing_key, provider, changed):
    service, _, register, _ = auth
    pending = await register(provider)
    await exchange(service, pending, oidc(signing_key, pending))
    with pytest.raises(LaneAuthError, match="first session"):
        await exchange(
            service, pending, oidc(signing_key, pending, changes={"jti": "fresh", **changed})
        )


async def test_jti_is_global_across_different_dispatches(auth, signing_key):
    service, _, register, _ = auth
    first = await register(run_id="a" * 32)
    other = await register(run_id="b" * 32)
    await exchange(service, first, oidc(signing_key, first))
    with pytest.raises(LaneAuthError, match="already been used"):
        await exchange(service, other, oidc(signing_key, other))
    assert not (await service.store.load(other.identity)).nonce_consumed


async def test_direct_workflow_claim_is_explicit_without_missing_claim_fallback(auth, signing_key):
    service, _, register, _ = auth
    pending = await register(github_workflow_claim="workflow_ref")
    with pytest.raises(LaneAuthError):
        await exchange(service, pending, oidc(signing_key, pending))
    await exchange(
        service,
        pending,
        oidc(signing_key, pending, changes={"job_workflow_ref": None, "workflow_ref": WORKFLOW}),
    )


@pytest.mark.parametrize(
    "mutation",
    [{"cancel_requested": True}, {"cancellation_generation": 4}, {"state": "cancelled"}],
)
async def test_cancel_fences_existing_bearer_and_in_transaction_write(auth, signing_key, mutation):
    service, factory, register, _ = auth
    pending = await register()
    token = await exchange(service, pending, oidc(signing_key, pending))
    async with factory() as session, session.begin():
        await session.execute(
            update(EngineRun)
            .where(EngineRun.id == pending.identity.forge_run_id)
            .values(**mutation)
        )
    with pytest.raises(LaneAuthError):
        await service.authenticate_bearer("Bearer " + token.access_token)
    with pytest.raises(LaneAuthError):
        await service.store.load(pending.identity)
    with pytest.raises(LaneAuthError):
        await EngineLaneV1Store(factory).access(
            pending.identity,
            lambda p, state, namespace: state.update(forbidden=True),
            write=True,
        )
    async with factory() as session:
        row = await session.scalar(select(EngineLaneSession))
        assert "forbidden" not in row.api_state_json


@pytest.mark.parametrize(
    "changes",
    [
        {"forge_run_id": "b" * 32},
        {"attempt": 3},
        {"generation": 8},
        {"protocol": "other"},
        {"aud": "other"},
        {"iss": "other"},
        {"token_use": "other"},
        {"exp": 1},
        {"attempt": True},
    ],
)
async def test_bearer_exact_identity_and_type_are_verified(auth, settings, signing_key, changes):
    service, _, register, _ = auth
    pending = await register()
    token = await exchange(service, pending, oidc(signing_key, pending))
    claims = jwt.decode(
        token.access_token,
        settings.secret_key.get_secret_value(),
        algorithms=["HS256"],
        audience=PUBLIC + "/lane/v1",
    )
    forged = jwt.encode(
        {**claims, **changes},
        settings.secret_key.get_secret_value(),
        algorithm="HS256",
        headers={"typ": "forge-lane-session+jwt"},
    )
    with pytest.raises(LaneAuthError):
        await service.authenticate_bearer("Bearer " + forged)


def test_session_secret_never_falls_back_to_old_lane_control_secret(settings):
    document = settings.model_dump()
    del document["secret_key"]
    document["FORGE_LANE_CONTROL_SECRET"] = SecretStr("old-secret-that-is-also-long-enough")
    with pytest.raises(ValidationError):
        LaneAuthSettings.model_validate(document)
    with pytest.raises(ValidationError):
        LaneAuthSettings.model_validate({**settings.model_dump(), "secret_key": SecretStr("short")})


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "http://metadata.invalid"}),
        httpx.Response(200, content=b"x" * 131073),
        httpx.Response(200, json={"keys": []}),
    ],
)
async def test_jwks_redirects_large_bodies_and_empty_keys_fail_closed(response):
    requests = []

    def fetch(request):
        requests.append(str(request.url))
        return response

    cache = JwksCache({"github": (GH_ISSUER, GH_KEYS)}, transport=httpx.MockTransport(fetch))
    with pytest.raises(LaneAuthError):
        await cache.key("github", "key-1")
    assert requests == [GH_KEYS]


async def test_unknown_kid_refresh_is_bounded(auth, signing_key):
    service, _, register, requests = auth
    pending = await register()
    for number in range(5):
        with pytest.raises(LaneAuthError):
            await exchange(
                service, pending, oidc(signing_key, pending, headers={"kid": f"unknown-{number}"})
            )
    assert requests == [GH_KEYS]


async def test_expired_pending_nonce_refuses_first_session_but_bound_reconnect_survives(
    auth, signing_key
):
    service, factory, register, _ = auth
    pending = await register()
    async with factory.begin() as session:
        row = await session.scalar(
            select(EngineLaneSession).where(
                EngineLaneSession.run_id == pending.identity.forge_run_id
            )
        )
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    with pytest.raises(LaneAuthError, match="expired"):
        await exchange(service, pending, oidc(signing_key, pending))
    # A different active dispatch can reconnect after its initial admission window.
    pending = await register(run_id="b" * 32)
    await exchange(service, pending, oidc(signing_key, pending))
    async with factory.begin() as session:
        row = await session.scalar(
            select(EngineLaneSession).where(
                EngineLaneSession.run_id == pending.identity.forge_run_id
            )
        )
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await exchange(service, pending, oidc(signing_key, pending, changes={"jti": "reconnect"}))


async def test_cancellation_after_signature_validation_rolls_back_replay_claim(
    auth, signing_key, monkeypatch
):
    service, factory, register, _ = auth
    pending = await register()
    original = service.store.claim

    async def cancelled_claim(identity, **kwargs):
        async with factory() as session, session.begin():
            await session.execute(
                update(EngineRun)
                .where(EngineRun.id == identity.forge_run_id)
                .values(cancel_requested=True, cancellation_generation=4)
            )
        return await original(identity, **kwargs)

    monkeypatch.setattr(service.store, "claim", cancelled_claim)
    with pytest.raises(LaneAuthError):
        await exchange(service, pending, oidc(signing_key, pending))
    async with factory() as session:
        assert not (
            await session.scalars(
                select(EventInbox).where(EventInbox.event_type == "lane.oidc.jti")
            )
        ).all()


async def test_replay_expiry_prunes_only_expired_auth_receipts(auth, signing_key):
    service, factory, register, _ = auth
    pending = await register()
    now = int(datetime.now(timezone.utc).timestamp())
    async with factory() as session, session.begin():
        for name, kind, expiry in [
            ("e", "lane.oidc.jti", now - 1),
            ("f", "lane.oidc.jti", now + 999),
            ("d", "webhook", now - 1),
        ]:
            session.add(
                EventInbox(
                    source_event_id=name * 64,
                    project_id=1,
                    event_type=kind,
                    payload={"expires_at": expiry},
                    status="processed",
                )
            )
    await exchange(service, pending, oidc(signing_key, pending))
    async with factory() as session:
        ids = set(await session.scalars(select(EventInbox.source_event_id)))
        assert "e" * 64 not in ids
        assert {"f" * 64, "d" * 64} <= ids


async def test_registering_new_generation_fences_old_bearer_without_changing_cancel_counter(
    auth, signing_key
):
    service, factory, register, _ = auth
    pending = await register()
    bearer = await exchange(service, pending, oidc(signing_key, pending))
    dispatch = pending.dispatch.model_copy(update={"generation": 8, "nonce": service.new_nonce()})
    brief = pending.brief.model_copy(update={"generation": 8})
    async with factory.begin() as session:
        await session.execute(
            update(EngineRun)
            .where(EngineRun.id == pending.identity.forge_run_id)
            .values(generation=8)
        )
    new = await service.register_pending(
        dispatch,
        brief,
        expires_at=pending.expires_at,
        connection_id=pending.connection_id,
        github_workflow_ref=WORKFLOW,
    )
    assert new.cancellation_generation == 3
    with pytest.raises(LaneAuthError):
        await service.authenticate_bearer("Bearer " + bearer.access_token)
    with pytest.raises(LaneAuthError):
        await EngineLaneV1Store(factory).access(
            pending.identity, lambda p, state, namespace: state.update(stale=True), write=True
        )
    async with factory() as session:
        row = await session.get(EngineRun, pending.identity.forge_run_id)
        assert row.generation == 8 and row.cancellation_generation == 3


async def test_concurrent_exchange_has_one_durable_winner(auth, settings, signing_key):
    service, factory, register, _ = auth
    pending = await register()
    # Independent services and sessions share only the production database arbiter.
    other = LaneAuthService(EngineLaneAuthStore(factory), settings, jwks_cache=service.jwks)
    token = oidc(signing_key, pending)
    outcomes = await asyncio.gather(
        exchange(service, pending, token),
        exchange(other, pending, token),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert sum(isinstance(item, LaneAuthError) for item in outcomes) == 1
    async with factory() as session:
        receipts = (
            await session.scalars(
                select(EventInbox).where(EventInbox.event_type == "lane.oidc.jti")
            )
        ).all()
        assert len(receipts) == 1


async def test_reusable_workflow_pin_does_not_authorize_another_caller_ref(auth, signing_key):
    service, _, register, _ = auth
    pending = await register()
    assert pending.github_caller_ref == "refs/heads/main"
    with pytest.raises(LaneAuthError, match="caller ref"):
        await exchange(
            service, pending, oidc(signing_key, pending, changes={"ref": "refs/heads/attacker"})
        )
    assert not (await service.store.load(pending.identity)).nonce_consumed
    await exchange(service, pending, oidc(signing_key, pending))


def test_disabled_legacy_gitlab_url_cannot_break_github_only_authentication(settings):
    github_only = LaneAuthSettings(
        **{**settings.model_dump(), "authorities": (settings.authorities[0],)}
    )
    service = LaneAuthService(None, github_only)
    assert set(service.jwks.authorities) == {"connection-github"}
    with pytest.raises(ValidationError):
        LaneAuthSettings.model_validate(
            {**github_only.model_dump(), "GITLAB_URL": "http://old-disabled-gitlab.invalid"}
        )
