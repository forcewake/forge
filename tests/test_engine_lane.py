"""Existing lane cryptographic/HTTP contracts against only the engine SQL tables."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from uuid import uuid4

from fastapi import FastAPI
from forge_protocol import LaneDispatch, ModelKeyGrant, RunIdentity
import httpx
from jwt.algorithms import RSAAlgorithm
import pytest
from sqlalchemy import inspect, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from forge.api.lane_auth import LaneAuthService
from forge.api.lane_auth_values import LaneAuthError
from forge.api.lane_jwks import JwksCache
from forge.api.lane_model_key_vault import VaultError
from forge.api.lane_v1 import router
from forge.api.lane_v1_blobs import LaneBlobStore
from forge.api.lane_v1_service import LaneV1Service
from forge.domain.approved_input import ApprovedInput
from forge.models.base import Base
from forge.store.inbox_models import EventInbox
from forge.store.lane_auth import EngineLaneAuthStore
from forge.store.lane_v1 import EngineLaneV1Store
from forge.store.models_execution import EngineCandidate
from forge.store.models_lane import EngineBlob, EngineCheckpoint, EngineLaneSession
from forge.store.models_runs import EngineApprovedInput, EngineRun, EngineRunSpec
from forge.store.records import RunRequest
from forge.store.runs import RunStore
from tests.test_domain_approved_input import spec
from tests.test_lane_auth import (
    GH_ISSUER,
    GH_KEYS,
    GL_ISSUER,
    GL_KEYS,
    PUBLIC,
    WORKFLOW,
    exchange,
    oidc,
    settings as settings,
    signing_key as signing_key,
    test_algorithm_and_token_selected_key_urls_are_refused_before_http as test_engine_algorithm_and_token_selected_key_urls,
    test_bearer_exact_identity_and_type_are_verified as test_engine_bearer_identity_and_type,
    test_direct_workflow_claim_is_explicit_without_missing_claim_fallback as test_engine_direct_workflow_pin,
    test_jti_is_global_across_different_dispatches as test_engine_jti_global,
    test_oidc_negative_claims_never_consume_dispatch as test_engine_oidc_negative_claims,
    test_reconnect_cannot_change_bound_provider_job as test_engine_reconnect_provider_job,
    test_reusable_workflow_pin_does_not_authorize_another_caller_ref as test_engine_reusable_workflow_caller_ref,
    test_unknown_kid_refresh_is_bounded as test_engine_unknown_kid_bounded,
)
from tests.test_lane_v1 import (
    bundle,
    checkpoint,
    upload,
    test_blob_digest_limits_corruption_and_symlinks_are_refused as test_engine_blob_filesystem_contract,
    test_candidate_foreign_or_false_identity_and_metadata_are_refused as test_engine_candidate_identity,
    test_concurrent_candidate_duplicates_read_one_committed_receipt as test_engine_concurrent_candidate_receipt,
    test_different_second_candidate_is_conflict_and_missing_blob_is_not_accepted as test_engine_candidate_conflicts,
    test_gitlab_lane_uses_the_same_typed_server_receipts as test_engine_gitlab_api,
    test_heartbeat_sequence_and_terminal_report_cannot_be_rewritten as test_engine_heartbeat_terminal,
    test_known_blob_hash_does_not_grant_another_dispatch_read_access as test_engine_blob_acl,
    test_lost_model_gateway_outcome_is_reserved_without_replay as test_engine_model_key_unknown_reserved,
    test_session_brief_and_control_ack_use_exact_frozen_models as test_engine_brief_controls_ack,
)

# Run the existing contract tests unchanged against this module's engine fixtures.
__all__ = [
    "test_engine_algorithm_and_token_selected_key_urls",
    "test_engine_bearer_identity_and_type",
    "test_engine_direct_workflow_pin",
    "test_engine_jti_global",
    "test_engine_oidc_negative_claims",
    "test_engine_reconnect_provider_job",
    "test_engine_reusable_workflow_caller_ref",
    "test_engine_unknown_kid_bounded",
    "test_engine_blob_filesystem_contract",
    "test_engine_candidate_identity",
    "test_engine_concurrent_candidate_receipt",
    "test_engine_candidate_conflicts",
    "test_engine_gitlab_api",
    "test_engine_heartbeat_terminal",
    "test_engine_blob_acl",
    "test_engine_model_key_unknown_reserved",
    "test_engine_brief_controls_ack",
]

DBS = ["sqlite"] + (
    [os.environ["FORGE_TEST_ENGINE_LANE_DB"]] if os.environ.get("FORGE_TEST_ENGINE_LANE_DB") else []
)


@pytest.mark.parametrize("provider", ["github", "gitlab"])
@pytest.mark.parametrize("native_id", [None, "999"])
async def test_engine_oidc_cannot_rebind_a_recreated_repository(
    auth, signing_key, provider, native_id
):
    service, factory, register, _ = auth
    pending = await register(provider)
    claim = "repository_id" if provider == "github" else "project_id"
    with pytest.raises(LaneAuthError):
        await exchange(service, pending, oidc(signing_key, pending, changes={claim: native_id}))
    async with factory() as session:
        assert not (await session.scalar(select(EngineLaneSession))).nonce_consumed
        receipts = (await session.scalars(select(EventInbox))).all()
        assert all(row.event_type != "lane.oidc.jti" for row in receipts)


async def test_engine_gitlab_dispatch_requires_an_explicit_random_nonce(auth):
    service, _, register, _ = auth
    pending = await register("gitlab")
    missing = pending.model_copy(
        update={"dispatch": pending.dispatch.model_copy(update={"nonce": None})}
    )
    with pytest.raises(LaneAuthError, match="nonce|differently"):
        await service.store.register(missing)


@pytest.fixture(params=DBS, ids=lambda item: "sqlite" if item == "sqlite" else "postgres")
async def auth(tmp_path, request, signing_key, settings):
    url = (
        f"sqlite+aiosqlite:///{tmp_path / 'lane.db'}"
        if request.param == "sqlite"
        else request.param
    )
    engine = create_async_engine(url)
    tables = [
        EventInbox.__table__,
        *[table for table in Base.metadata.sorted_tables if table.name.startswith("eng_")],
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Base.metadata.drop_all(sync, tables=tables))
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    requests = []
    public_key = json.loads(RSAAlgorithm.to_jwk(signing_key.public_key()))

    def jwks(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={"keys": [{**public_key, "kid": "key-1", "alg": "RS256"}]})

    cache = JwksCache(
        {"github": (GH_ISSUER, GH_KEYS), "gitlab": (GL_ISSUER, GL_KEYS)},
        transport=httpx.MockTransport(jwks),
    )
    service = LaneAuthService(EngineLaneAuthStore(factory), settings, jwks_cache=cache)

    async def register(provider="github", run_id="a" * 32, **kwargs):
        frozen = spec()
        approved = ApprovedInput.from_spec(frozen)
        await RunStore(factory).admit(
            RunRequest(run_id, provider, "connection", "123", "issue", run_id)
        )
        async with factory.begin() as session:
            row = await session.get(EngineRun, run_id)
            row.attempt, row.generation, row.cancellation_generation, row.state = 2, 7, 3, "running"
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


async def test_engine_session_reconnect_is_durable_and_has_no_shadow_flowrun(
    auth, settings, signing_key
):
    service, factory, register, _ = auth
    pending = await register()
    token = await exchange(service, pending, oidc(signing_key, pending))
    restarted = LaneAuthService(EngineLaneAuthStore(factory), settings, jwks_cache=service.jwks)
    assert await restarted.authenticate_bearer("Bearer " + token.access_token) == pending.identity
    with pytest.raises(LaneAuthError, match="already been used"):
        await exchange(restarted, pending, oidc(signing_key, pending))
    await exchange(restarted, pending, oidc(signing_key, pending, changes={"jti": "reconnect"}))
    async with factory() as session:
        names = await session.run_sync(lambda sync: inspect(sync.bind).get_table_names())
        assert "flow_runs" not in names
        rows = (
            await session.scalars(
                select(EventInbox).where(EventInbox.event_type == "lane.oidc.jti")
            )
        ).all()
        assert len(rows) == 2 and all(
            len(row.source_event_id) == 64 and set(row.payload) == {"expires_at"} for row in rows
        )
        saved = await session.scalar(select(EngineLaneSession))
        assert saved.nonce_consumed and saved.cancellation_generation == 3 and saved.generation == 7
        assert "jwt-1" not in saved.first_job_json + saved.api_state_json
        assert pending.brief.spec_json is not None


async def test_engine_registration_requires_exact_persisted_approval_and_current_generation(auth):
    service, factory, register, _ = auth
    pending = await register()
    wrong = pending.brief.model_copy(update={"spec_json": None})
    with pytest.raises(LaneAuthError, match="persisted"):
        await service.register_pending(
            pending.dispatch, wrong, expires_at=pending.expires_at, github_workflow_ref=WORKFLOW
        )
    future = pending.dispatch.model_copy(update={"generation": 8, "nonce": service.new_nonce()})
    with pytest.raises(LaneAuthError, match="active engine identity"):
        await service.register_pending(
            future,
            pending.brief.model_copy(update={"generation": 8}),
            expires_at=pending.expires_at,
            github_workflow_ref=WORKFLOW,
        )
    async with factory.begin() as session:
        approval = await session.scalar(select(EngineApprovedInput))
        approval.spec_digest = "0" * 64
    with pytest.raises(LaneAuthError, match="persisted approval"):
        await service.register_pending(
            pending.dispatch,
            pending.brief,
            expires_at=pending.expires_at,
            github_workflow_ref=WORKFLOW,
        )


async def test_engine_model_grant_reconnect_keeps_ciphertext_and_never_remints(api):
    lane, factory, pending, client = api
    calls = []

    async def provider(dispatch):
        calls.append(dispatch.identity)
        return ModelKeyGrant(
            **dispatch.identity.model_dump(),
            gateway_url="https://gateway.example",
            key="engine-grant-sentinel",
            model=dispatch.dispatch.model,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

    lane.model_key_provider = provider
    first = await client.post("/lane/v1/model-key")
    assert first.status_code == 200
    assert (await client.post("/lane/v1/model-key")).json() == first.json()
    restarted = LaneV1Service(lane.auth, EngineLaneV1Store(factory), lane.blobs)
    assert (await restarted.model_key(pending.identity)).model_dump(mode="json") == first.json()
    assert calls == [pending.identity]
    async with factory() as session:
        row = await session.scalar(select(EngineLaneSession))
        assert "engine-grant-sentinel" not in row.api_state_json
        encrypted = json.loads(row.api_state_json)["model_key_grant"]
    with pytest.raises(VaultError):
        lane.vault.open(pending.identity.model_copy(update={"generation": 8}), encrypted)


async def test_engine_normalized_artifact_receipts_and_explicit_prior_generation_resume(
    api, signing_key
):
    lane, factory, pending, client = api
    digest = await upload(client)
    manifest = checkpoint(pending, digest)
    path = "/lane/v1/checkpoints/" + manifest.checkpoint_id
    assert (await client.put(path, json=manifest.model_dump(mode="json"))).status_code == 201
    assert (await client.put(path, json=manifest.model_dump(mode="json"))).status_code == 200
    value = bundle(pending, digest)
    assert (
        await client.post("/lane/v1/candidate", json=value.model_dump(mode="json"))
    ).status_code == 201
    assert (
        await client.post("/lane/v1/candidate", json=value.model_dump(mode="json"))
    ).status_code == 200
    async with factory() as session:
        assert (await session.scalar(select(EngineCandidate))).state == "received"
        assert (await session.scalar(select(EngineBlob))).digest == digest
        assert (await session.scalar(select(EngineCheckpoint))).id == manifest.checkpoint_id
        row = await session.scalar(select(EngineLaneSession))
        assert all(
            key not in json.loads(row.api_state_json)
            for key in ("blobs", "candidate", "controls", "acks")
        )
    async with factory.begin() as session:
        await session.execute(
            update(EngineRun)
            .where(EngineRun.id == pending.identity.forge_run_id)
            .values(generation=8)
        )
    dispatch = pending.dispatch.model_copy(
        update={
            "generation": 8,
            "nonce": lane.auth.new_nonce(),
            "checkpoint_id": manifest.checkpoint_id,
        }
    )
    new = await lane.auth.register_pending(
        dispatch,
        pending.brief.model_copy(update={"generation": 8}),
        expires_at=pending.expires_at,
        github_workflow_ref=WORKFLOW,
    )
    token = await exchange(lane.auth, new, oidc(signing_key, new, changes={"jti": "resume"}))
    assert (await client.get("/lane/v1/brief")).status_code == 401
    client.headers["Authorization"] = "Bearer " + token.access_token
    assert (await client.get("/lane/v1/checkpoints/latest")).json() == manifest.model_dump(
        mode="json"
    )
    assert (await client.get("/lane/v1/blobs/" + digest)).content == b"approved bytes"


async def test_engine_cancellation_fences_key_delivery_and_artifact_registration(api, monkeypatch):
    lane, factory, pending, client = api

    async def provider(dispatch):
        async with factory.begin() as session:
            await session.execute(
                update(EngineRun)
                .where(EngineRun.id == pending.identity.forge_run_id)
                .values(cancel_requested=True, cancellation_generation=4)
            )
        return ModelKeyGrant(
            **dispatch.identity.model_dump(),
            gateway_url="https://gateway.example",
            key="cancelled-key",
            model=dispatch.dispatch.model,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

    lane.model_key_provider = provider
    assert (await client.post("/lane/v1/model-key")).status_code == 401
    async with factory() as session:
        row = await session.scalar(select(EngineLaneSession))
        state = json.loads(row.api_state_json)
        assert state["model_key_requested"] and "model_key_grant" not in state
    assert (await client.get("/lane/v1/brief")).status_code == 401


async def test_engine_auth_exchange_has_one_durable_winner(auth, settings, signing_key):
    service, factory, register, _ = auth
    pending = await register()
    other = LaneAuthService(EngineLaneAuthStore(factory), settings, jwks_cache=service.jwks)
    token = oidc(signing_key, pending)
    results = await asyncio.gather(
        exchange(service, pending, token), exchange(other, pending, token), return_exceptions=True
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, LaneAuthError) for result in results) == 1


async def test_engine_cancel_between_blob_write_and_reference_commit_refuses_the_acl(
    api, monkeypatch
):
    import hashlib

    lane, factory, pending, client = api
    original = lane.record_blob

    async def cancelled(identity, digest, size):
        async with factory.begin() as session:
            await session.execute(
                update(EngineRun)
                .where(EngineRun.id == identity.forge_run_id)
                .values(cancel_requested=True, cancellation_generation=4)
            )
        return await original(identity, digest, size)

    monkeypatch.setattr(lane, "record_blob", cancelled)
    body = b"cancel race"
    response = await client.put("/lane/v1/blobs/" + hashlib.sha256(body).hexdigest(), content=body)
    assert response.status_code == 401
    async with factory() as session:
        assert not (await session.scalars(select(EngineBlob))).all()
        assert json.loads((await session.scalar(select(EngineLaneSession))).api_state_json) == {}


async def test_engine_cancellation_after_jwt_validation_rolls_back_replay_receipt(
    auth, signing_key, monkeypatch
):
    service, factory, register, _ = auth
    pending = await register()
    original = service.store.claim

    async def cancelled(identity, **kwargs):
        async with factory.begin() as session:
            await session.execute(
                update(EngineRun)
                .where(EngineRun.id == identity.forge_run_id)
                .values(cancel_requested=True, cancellation_generation=4)
            )
        return await original(identity, **kwargs)

    monkeypatch.setattr(service.store, "claim", cancelled)
    with pytest.raises(LaneAuthError):
        await exchange(service, pending, oidc(signing_key, pending))
    async with factory() as session:
        assert not (
            await session.scalars(
                select(EventInbox).where(EventInbox.event_type == "lane.oidc.jti")
            )
        ).all()
        assert not (await session.scalar(select(EngineLaneSession))).nonce_consumed


async def test_engine_expired_encrypted_grant_never_remints(api, monkeypatch):
    lane, _, pending, client = api
    calls = []

    async def provider(dispatch):
        calls.append(dispatch.identity)
        return ModelKeyGrant(
            **dispatch.identity.model_dump(),
            gateway_url="https://gateway.example",
            key="expired-key",
            model=dispatch.dispatch.model,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

    lane.model_key_provider = provider
    assert (await client.post("/lane/v1/model-key")).status_code == 200

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(minutes=10)

    monkeypatch.setattr("forge.api.lane_v1_service.datetime", Later)
    assert (await client.post("/lane/v1/model-key")).status_code == 503
    assert calls == [pending.identity]


async def test_engine_replay_expiry_prunes_only_expired_auth_receipts(auth, signing_key):
    service, factory, register, _ = auth
    pending = await register()
    now = int(datetime.now(timezone.utc).timestamp())
    async with factory.begin() as session:
        for key, kind, expiry in (
            ("e", "lane.oidc.jti", now - 1),
            ("f", "lane.oidc.jti", now + 999),
            ("d", "webhook", now - 1),
        ):
            session.add(
                EventInbox(
                    source_event_id=key * 64,
                    project_id=0,
                    event_type=kind,
                    payload={"expires_at": expiry},
                    status="processed",
                )
            )
    await exchange(service, pending, oidc(signing_key, pending))
    async with factory() as session:
        keys = set(await session.scalars(select(EventInbox.source_event_id)))
        assert "e" * 64 not in keys and {"f" * 64, "d" * 64} <= keys


def test_engine_lane_import_graph_loads_no_legacy_run_owner():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import forge.api.lane_engine; import forge.api.lane_v1; "
            "assert 'forge.durable.models' not in sys.modules; "
            "assert 'forge.api.lane_auth_store' not in sys.modules; "
            "assert 'forge.api.lane_v1_store' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


async def test_engine_unknown_model_outcome_stays_reserved_after_restart(api):
    from forge.api.lane_v1_service import ModelKeyUnavailable
    from forge.api.lane_v1_values import LaneConflict

    lane, factory, pending, _ = api
    calls = []

    async def lost(dispatch):
        calls.append(dispatch.identity)
        raise TimeoutError("private provider details")

    lane.model_key_provider = lost
    with pytest.raises(ModelKeyUnavailable):
        await lane.model_key(pending.identity)
    restarted = LaneV1Service(
        lane.auth, EngineLaneV1Store(factory), lane.blobs, model_key_provider=lost
    )
    with pytest.raises(LaneConflict, match="reserved"):
        await restarted.model_key(pending.identity)
    assert calls == [pending.identity]
    async with factory() as session:
        row = await session.scalar(select(EngineLaneSession))
        state = json.loads(row.api_state_json)
        assert state["model_key_requested"] and "model_key_grant" not in state
