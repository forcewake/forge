"""Shared native lane RSA, frozen admission and SQL/HTTP receipt fixtures."""

import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import hashlib

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
import httpx
import jwt
from jwt.algorithms import RSAAlgorithm
from pydantic import SecretStr
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.engine import make_url
from forge_protocol import (
    LaneDispatch,
    RunIdentity,
    SessionRequest,
    CandidateBundle,
    CheckpointManifest,
    CheckpointEntry,
)

from forge.api.lane_auth import LaneAuthService
from forge.api.lane_jwks import JwksCache
from forge.api.lane_settings import LaneAuthSettings, OidcAuthority
from forge.api.lane_v1 import router
from forge.api.lane_v1_blobs import LaneBlobStore
from forge.api.lane_v1_service import LaneV1Service
from forge.domain.approved_input import ApprovedInput
from forge.store.base import Base
from forge.store.lane_auth import EngineLaneAuthStore
from forge.store.lane_v1 import EngineLaneV1Store
from forge.store.models_runs import EngineRun, EngineRunSpec, EngineApprovedInput
from forge.store.records import RunRequest
from forge.store.runs import RunStore
from forge.store import models
from tests.test_domain_approved_input import spec

PUBLIC = "https://forge.example"
GH_ISSUER = "https://token.actions.githubusercontent.com"
GH_KEYS = GH_ISSUER + "/.well-known/jwks"
GL_ISSUER = "https://gitlab.example"
GL_KEYS = GL_ISSUER + "/oauth/discovery/keys"
WORKFLOW = "owner/repo/.github/workflows/lane.yml@refs/heads/main"
DBS = ["sqlite"] + (
    [os.environ["FORGE_TEST_ENGINE_LANE_DB"]] if os.environ.get("FORGE_TEST_ENGINE_LANE_DB") else []
)


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def settings():
    return LaneAuthSettings(
        secret_key=SecretStr("test-session-signing-key-32-bytes-long"),
        public_url=PUBLIC,
        authorities=tuple(
            OidcAuthority(
                connection_id="connection-" + provider,
                provider=provider,
                issuer=issuer,
                jwks_url=keys,
            )
            for provider, issuer, keys in (
                ("github", GH_ISSUER, GH_KEYS),
                ("gitlab", GL_ISSUER, GL_KEYS),
            )
        ),
    )


def jwks_cache(signing_key):
    requests = []
    public_key = json.loads(RSAAlgorithm.to_jwk(signing_key.public_key()))

    def jwks(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={"keys": [{**public_key, "kid": "key-1", "alg": "RS256"}]})

    cache = JwksCache(
        {"connection-github": (GH_ISSUER, GH_KEYS), "connection-gitlab": (GL_ISSUER, GL_KEYS)},
        transport=httpx.MockTransport(jwks),
    )
    return cache, requests


@pytest.fixture(params=DBS, ids=lambda item: "sqlite" if item == "sqlite" else "postgres")
async def auth(tmp_path, request, signing_key, settings):
    url = (
        f"sqlite+aiosqlite:///{tmp_path / 'lane.db'}"
        if request.param == "sqlite"
        else request.param
    )
    if request.param != "sqlite" and make_url(url).get_backend_name() != "postgresql":
        raise ValueError("configured lane SQL proof requires PostgreSQL")
    engine = create_async_engine(url)
    owned = {getattr(models, name).__table__ for name in models.__all__}
    tables = [table for table in Base.metadata.sorted_tables if table in owned]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Base.metadata.drop_all(sync, tables=tables))
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    cache, requests = jwks_cache(signing_key)
    service = LaneAuthService(EngineLaneAuthStore(factory), settings, jwks_cache=cache)

    async def register(provider="github", run_id="a" * 32, **kwargs):
        frozen = spec()
        approved = ApprovedInput.from_spec(frozen)
        await RunStore(factory).admit(
            RunRequest(run_id, provider, "connection-" + provider, "123", "issue", run_id)
        )
        async with factory.begin() as session:
            row = await session.get(EngineRun, run_id)
            row.attempt, row.generation, row.cancellation_generation, row.state = 2, 7, 3, "running"
            row.reason = "preserve-other-owner"
            session.add(
                EngineRunSpec(
                    id=uuid4().hex,
                    run_id=run_id,
                    digest=frozen.digest,
                    document_json=frozen.spec_json,
                )
            )
            session.add(
                EngineApprovedInput(
                    id=uuid4().hex,
                    run_id=run_id,
                    revision=approved.revision,
                    digest=approved.approved_digest,
                    spec_digest=approved.spec_digest,
                    base_sha=approved.base_sha,
                    document_json=approved.canonical_bytes.decode(),
                )
            )
        identity = RunIdentity(forge_run_id=run_id, attempt=2, generation=7)
        brief = approved.to_brief(identity, frozen)
        dispatch = LaneDispatch(
            **identity.model_dump(),
            server_url=PUBLIC,
            provider=provider,
            repository="owner/repo",
            ref="main",
            base_sha=brief.base_sha,
            approved_digest=brief.approved_digest,
            driver=frozen.driver,
            model=frozen.model,
            timeout_seconds=600,
            nonce=service.new_nonce(),
        )
        return await service.register_pending(
            dispatch,
            brief,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            connection_id="connection-" + provider,
            github_workflow_ref=WORKFLOW if provider == "github" else None,
            gitlab_pipeline_id="42" if provider == "gitlab" else None,
            **kwargs,
        )

    try:
        yield service, factory, register, requests
    finally:
        await engine.dispose()


@pytest.fixture
async def api(auth, signing_key, tmp_path):
    service, factory, register, _ = auth
    pending = await register()
    token = await exchange(service, pending, oidc(signing_key, pending))
    lane = LaneV1Service(service, EngineLaneV1Store(factory), LaneBlobStore(tmp_path / "blobs"))
    app = FastAPI()
    app.state.lane_v1_service = lane
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=PUBLIC,
        headers={"Authorization": "Bearer " + token.access_token},
    ) as client:
        yield lane, factory, pending, client


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
    if pending.repository_id is not None:
        claims["repository_id" if is_gh else "project_id"] = pending.repository_id
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


async def upload(client, body=b"approved bytes"):
    digest = hashlib.sha256(body).hexdigest()
    response = await client.put("/lane/v1/blobs/" + digest, content=body)
    assert response.status_code == 204, response.text
    return digest


def bundle(pending, digest, size=14):
    return CandidateBundle(
        **pending.identity.model_dump(),
        diff_sha256=digest,
        diff_size_bytes=size,
        base_sha=pending.dispatch.base_sha,
        approved_digest=pending.dispatch.approved_digest,
        driver=pending.dispatch.driver,
        model=pending.dispatch.model,
        terminal_reason="completed",
    )


def checkpoint(pending, digest, size=14, sequence=1):
    return CheckpointManifest(
        **pending.identity.model_dump(),
        base_sha=pending.dispatch.base_sha,
        approved_digest=pending.dispatch.approved_digest,
        driver=pending.dispatch.driver,
        sequence=sequence,
        entries=(
            CheckpointEntry(
                path="src/file.txt", mode="100644", sha256=digest, state="modified", size_bytes=size
            ),
        ),
    )
