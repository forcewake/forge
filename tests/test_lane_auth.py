"""Actual JWT cryptography and durable lane identity authorization."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa
from forge_protocol import Brief, LaneDispatch, SessionRequest, approved_input_digest
from jwt.algorithms import RSAAlgorithm
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from forge.api.lane_auth import LaneAuthService
from forge.api.lane_auth_store import (
    AUTH_NAMESPACE,
    LaneAuthError,
    SqlLaneAuthStore,
    check_current,
    current_conditions,
    identity_of,
)
from forge.api.lane_jwks import JwksCache
from forge.config import Settings
from forge.durable.models import EventInbox, FlowRun
from forge.models.base import Base

PUBLIC = "https://forge.example"
GH_ISSUER = "https://token.actions.githubusercontent.com"
GH_KEYS = GH_ISSUER + "/.well-known/jwks"
GL_ISSUER = "https://gitlab.example"
GL_KEYS = GL_ISSUER + "/oauth/discovery/keys"
WORKFLOW = "owner/repo/.github/workflows/lane.yml@refs/heads/main"


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        FORGE_GITLAB_ENABLED=False,
        FORGE_SECRET_KEY=SecretStr("test-session-signing-key-32-bytes-long"),
        FORGE_PUBLIC_URL=PUBLIC,
        GITLAB_URL=GL_ISSUER,
        FORGE_GITLAB_OIDC_ISSUER=GL_ISSUER,
    )


@pytest_asyncio.fixture
async def auth(tmp_path, signing_key, settings):
    engine = create_async_engine(
        os.environ.get("FORGE_TEST_LANE_AUTH_DB", f"sqlite+aiosqlite:///{tmp_path}/auth.db")
    )
    tables = [FlowRun.__table__, EventInbox.__table__]
    async with engine.begin() as connection:
        await connection.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    public_key = json.loads(RSAAlgorithm.to_jwk(signing_key.public_key()))
    requests = []

    def jwks(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={"keys": [{**public_key, "kid": "key-1", "alg": "RS256"}]})

    cache = JwksCache(
        {"github": (GH_ISSUER, GH_KEYS), "gitlab": (GL_ISSUER, GL_KEYS)},
        transport=httpx.MockTransport(jwks),
    )
    service = LaneAuthService(SqlLaneAuthStore(factory), settings, jwks_cache=cache)

    async def register(provider="github", run_id="a" * 32, **kwargs):
        data = dict(
            forge_run_id=run_id,
            attempt=2,
            generation=7,
            brief_text="Implement the approved input.",
            plan_digest="1" * 64,
            spec_digest="2" * 64,
            base_sha="3" * 40,
            revision=1,
        )
        approved = approved_input_digest(
            **{
                k: data[k]
                for k in (
                    "brief_text",
                    "plan_digest",
                    "spec_digest",
                    "base_sha",
                    "revision",
                )
            }
        )
        brief = Brief(**data, approved_digest=approved)
        dispatch = LaneDispatch(
            **identity_of(brief).model_dump(),
            server_url=PUBLIC,
            provider=provider,
            repository="owner/repo",
            ref="main",
            base_sha=brief.base_sha,
            approved_digest=approved,
            driver="claude-sdk",
            model="model",
            timeout_seconds=600,
            nonce=service.new_nonce() if provider == "github" else None,
        )
        async with factory() as session, session.begin():
            session.add(
                FlowRun(
                    id=run_id,
                    provider=provider,
                    project_id=1,
                    issue_iid=None,
                    cancellation_generation=3,
                    evidence={"preserve": "other-owner"},
                )
            )
        pending = await service.register_pending(
            dispatch,
            brief,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            github_workflow_ref=WORKFLOW if provider == "github" else None,
            gitlab_pipeline_id="42" if provider == "gitlab" else None,
            **kwargs,
        )
        return pending

    yield service, factory, register, requests
    async with engine.begin() as connection:
        await connection.run_sync(lambda c: Base.metadata.drop_all(c, tables=tables))
    await engine.dispose()


def oidc(key, pending, *, changes=None, headers=None, algorithm="RS256"):
    now = int(datetime.now(timezone.utc).timestamp())
    is_gh = pending.dispatch.provider == "github"
    claims = {
        "iss": GH_ISSUER if is_gh else GL_ISSUER,
        "aud": PUBLIC + "/lane/" + pending.dispatch.nonce if is_gh else PUBLIC,
        "sub": "provider-signed-subject",
        "jti": "jwt-1",
        "iat": now - 2,
        "nbf": now - 2,
        "exp": now + 300,
        **(
            {
                "repository": "owner/repo",
                "ref": "refs/heads/main",
                "job_workflow_ref": WORKFLOW,
                "run_id": "100",
                "run_attempt": "1",
            }
            if is_gh
            else {"project_path": "owner/repo", "ref": "main", "pipeline_id": "42", "job_id": "123"}
        ),
    }
    for name, value in (changes or {}).items():
        if value is None:
            claims.pop(name, None)
        else:
            claims[name] = value
    return jwt.encode(claims, key, algorithm=algorithm, headers={"kid": "key-1", **(headers or {})})


async def exchange(service, pending, token):
    return await service.exchange(
        SessionRequest(
            **pending.identity.model_dump(),
            provider=pending.dispatch.provider,
            oidc_token=token,
        )
    )


@pytest.mark.parametrize("provider", ["github", "gitlab"])
async def test_signed_exchange_reconnect_and_replay_survive_a_new_service(
    auth, settings, signing_key, provider
):
    service, factory, register, requests = auth
    pending = await register(provider)
    first = await exchange(service, pending, oidc(signing_key, pending))
    assert await service.authenticate_bearer("Bearer " + first.access_token) == pending.identity
    restarted = LaneAuthService(SqlLaneAuthStore(factory), settings, jwks_cache=service.jwks)
    with pytest.raises(LaneAuthError, match="already been used"):
        await exchange(restarted, pending, oidc(signing_key, pending))
    second = await exchange(
        restarted, pending, oidc(signing_key, pending, changes={"jti": "jwt-2"})
    )
    assert second.expires_at > datetime.now(timezone.utc)
    assert len(requests) == 1
    async with factory() as session:
        row = await session.get(FlowRun, pending.identity.forge_run_id)
        assert row.evidence["preserve"] == "other-owner"
        assert row.evidence[AUTH_NAMESPACE]["cancellation_generation"] == 3
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
        assert "jwt-1" not in json.dumps(row.evidence)


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
    [{"cancel_requested": True}, {"cancellation_generation": 4}, {"status": "cancelled"}],
)
async def test_cancel_fences_existing_bearer_and_in_transaction_write(auth, signing_key, mutation):
    service, factory, register, _ = auth
    pending = await register()
    token = await exchange(service, pending, oidc(signing_key, pending))
    async with factory() as session, session.begin():
        await session.execute(
            update(FlowRun).where(FlowRun.id == pending.identity.forge_run_id).values(**mutation)
        )
    with pytest.raises(LaneAuthError):
        await service.authenticate_bearer("Bearer " + token.access_token)
    async with factory() as session, session.begin():
        row = await session.get(FlowRun, pending.identity.forge_run_id)
        with pytest.raises(LaneAuthError):
            check_current(row, pending.identity)
        result = await session.execute(
            update(FlowRun)
            .where(*current_conditions(pending.identity, 3))
            .values(status_reason="forbidden write")
        )
        assert result.rowcount == 0


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
        settings.FORGE_SECRET_KEY.get_secret_value(),
        algorithms=["HS256"],
        audience=PUBLIC + "/lane/v1",
    )
    forged = jwt.encode(
        {**claims, **changes},
        settings.FORGE_SECRET_KEY.get_secret_value(),
        algorithm="HS256",
        headers={"typ": "forge-lane-session+jwt"},
    )
    with pytest.raises(LaneAuthError):
        await service.authenticate_bearer("Bearer " + forged)


def test_session_secret_never_falls_back_to_old_lane_control_secret(settings):
    settings.FORGE_SECRET_KEY = None
    settings.FORGE_LANE_CONTROL_SECRET = SecretStr("old-secret-that-is-also-long-enough")
    with pytest.raises(ValueError, match="FORGE_SECRET_KEY"):
        LaneAuthService(None, settings)


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
    async with factory() as session, session.begin():
        row = await session.get(FlowRun, pending.identity.forge_run_id)
        evidence = {**row.evidence}
        evidence[AUTH_NAMESPACE] = {
            **evidence[AUTH_NAMESPACE],
            "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        }
        row.evidence = evidence
    with pytest.raises(LaneAuthError, match="expired"):
        await exchange(service, pending, oidc(signing_key, pending))
    # A different active dispatch can reconnect after its initial admission window.
    pending = await register(run_id="b" * 32)
    await exchange(service, pending, oidc(signing_key, pending))
    async with factory() as session, session.begin():
        row = await session.get(FlowRun, pending.identity.forge_run_id)
        row.evidence = {
            **row.evidence,
            AUTH_NAMESPACE: {
                **row.evidence[AUTH_NAMESPACE],
                "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
            },
        }
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
                update(FlowRun)
                .where(FlowRun.id == identity.forge_run_id)
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
    new = await service.register_pending(
        dispatch, brief, expires_at=pending.expires_at, github_workflow_ref=WORKFLOW
    )
    assert new.cancellation_generation == 3
    with pytest.raises(LaneAuthError):
        await service.authenticate_bearer("Bearer " + bearer.access_token)
    async with factory() as session, session.begin():
        result = await session.execute(
            update(FlowRun)
            .where(*current_conditions(pending.identity, 3))
            .values(status_reason="stale")
        )
        assert result.rowcount == 0


async def test_concurrent_exchange_has_one_durable_winner(auth, settings, signing_key):
    service, factory, register, _ = auth
    pending = await register()
    # Independent services and sessions share only the production database arbiter.
    other = LaneAuthService(SqlLaneAuthStore(factory), settings, jwks_cache=service.jwks)
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
    settings.FORGE_GITLAB_OIDC_ISSUER = ""
    settings.GITLAB_URL = "http://old-disabled-gitlab.invalid"
    service = LaneAuthService(None, settings)
    assert set(service.jwks.authorities) == {"github"}
