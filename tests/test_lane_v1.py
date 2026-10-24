"""Server routes use real JWT authorization, native SQL receipts and filesystem blobs."""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from forge_protocol import (
    ControlAck,
    ControlCommand,
    Heartbeat,
    LaneReport,
    ModelKeyGrant,
)
from sqlalchemy import select, update

from forge.api.lane_model_key_vault import VaultError
from forge.api.lane_v1_service import LaneV1Service
from forge.store.lane_v1 import EngineLaneV1Store
from forge.store.models_runs import EngineRun
from forge.store.models_lane import EngineLaneSession
from forge.runtime.app import create_app
from tests.runtime_fixture import configuration
from tests.native_lane_fixture import (
    WORKFLOW,
    exchange,
    oidc,
    upload,
    bundle,
    checkpoint,
    auth as auth,
    api as api,
    settings as settings,
    signing_key as signing_key,
)


async def test_session_brief_and_control_ack_use_exact_frozen_models(api, signing_key):
    lane, _, pending, client = api
    body = {
        **pending.identity.model_dump(),
        "provider": "github",
        "oidc_token": oidc(signing_key, pending, changes={"jti": "http-exchange"}),
    }
    response = await client.post("/lane/v1/session", json=body)
    assert response.status_code == 200
    assert response.json()["generation"] == 7
    assert (await client.get("/lane/v1/brief")).json() == pending.brief.model_dump(mode="json")
    assert (await client.get("/lane/v1/controls?after=0")).json() == []
    command = ControlCommand(
        **pending.identity.model_dump(), command_id="pause-1", sequence=1, action="pause"
    )
    await lane.store.enqueue_control(command)
    await lane.store.enqueue_control(command)
    assert (await client.get("/lane/v1/controls?after=0")).json() == [
        command.model_dump(mode="json")
    ]
    assert (await client.get("/lane/v1/controls?after=1")).json() == []
    ack = ControlAck(
        **pending.identity.model_dump(), command_id=command.command_id, sequence=1, status="applied"
    )
    for _ in range(2):
        response = await client.post(
            "/lane/v1/controls/pause-1/ack", json=ack.model_dump(mode="json")
        )
        assert response.status_code == 200 and response.json() == ack.model_dump(mode="json")
    assert (
        await client.post(
            "/lane/v1/controls/pause-1/ack",
            json={**ack.model_dump(mode="json"), "status": "refused"},
        )
    ).status_code == 409


async def test_blob_checkpoint_candidate_and_terminal_report_are_durable_idempotent_receipts(api):
    lane, factory, pending, client = api
    assert (await client.get("/lane/v1/checkpoints/latest")).status_code == 404
    digest = await upload(client)
    assert await upload(client) == digest
    assert (await client.get("/lane/v1/blobs/" + digest)).content == b"approved bytes"
    manifest = checkpoint(pending, digest)
    path = "/lane/v1/checkpoints/" + manifest.checkpoint_id
    assert (await client.put(path, json=manifest.model_dump(mode="json"))).status_code == 201
    assert (await client.put(path, json=manifest.model_dump(mode="json"))).status_code == 200
    assert (await client.get("/lane/v1/checkpoints/latest")).json() == manifest.model_dump(
        mode="json"
    )
    candidate = bundle(pending, digest)
    assert (
        await client.post("/lane/v1/candidate", json=candidate.model_dump(mode="json"))
    ).status_code == 201
    assert (
        await client.post("/lane/v1/candidate", json=candidate.model_dump(mode="json"))
    ).status_code == 200
    terminal = LaneReport(
        **pending.identity.model_dump(),
        approved_digest=pending.dispatch.approved_digest,
        status="completed",
        classification="success",
        driver=pending.dispatch.driver,
        model=pending.dispatch.model,
        terminal_reason="completed",
        candidate_diff_sha256=digest,
    )
    assert (
        await client.post("/lane/v1/report", json=terminal.model_dump(mode="json"))
    ).status_code == 201
    assert (
        await client.post("/lane/v1/report", json=terminal.model_dump(mode="json"))
    ).status_code == 200
    assert (
        await client.post(
            "/lane/v1/report",
            json={**terminal.model_dump(mode="json"), "terminal_reason": "changed"},
        )
    ).status_code == 409
    restarted = EngineLaneV1Store(factory)
    state = await restarted.access(pending.identity, lambda p, s, n: s)
    assert state["candidate"]["diff_sha256"] == digest
    assert state["report"]["status"] == "completed"
    assert "access_token" not in json.dumps(state)


@pytest.mark.parametrize(
    "change",
    [
        {"generation": 8},
        {"approved_digest": "0" * 64},
        {"base_sha": "0" * 40},
        {"driver": "other"},
        {"model": "other"},
        {"diff_size_bytes": 1},
    ],
)
async def test_candidate_foreign_or_false_identity_and_metadata_are_refused(api, change):
    _, _, pending, client = api
    digest = await upload(client)
    candidate = bundle(pending, digest).model_dump(mode="json")
    assert (
        await client.post("/lane/v1/candidate", json={**candidate, **change})
    ).status_code == 409


async def test_different_second_candidate_is_conflict_and_missing_blob_is_not_accepted(api):
    _, _, pending, client = api
    missing = bundle(pending, "0" * 64)
    assert (
        await client.post("/lane/v1/candidate", json=missing.model_dump(mode="json"))
    ).status_code == 404
    first = await upload(client)
    assert (
        await client.post("/lane/v1/candidate", json=bundle(pending, first).model_dump(mode="json"))
    ).status_code == 201
    other = await upload(client, b"different diff")
    assert (
        await client.post("/lane/v1/candidate", json=bundle(pending, other).model_dump(mode="json"))
    ).status_code == 409


async def test_blob_digest_limits_corruption_and_symlinks_are_refused(api, tmp_path):
    lane, _, _, client = api
    digest = hashlib.sha256(b"wrong").hexdigest()
    assert (await client.put("/lane/v1/blobs/" + digest, content=b"other")).status_code == 409
    lane.blobs.max_bytes = 8
    assert (
        await client.put("/lane/v1/blobs/" + hashlib.sha256(b"x" * 9).hexdigest(), content=b"x" * 9)
    ).status_code == 413
    assert not list(lane.blobs.root.glob(".upload-*"))
    lane.blobs.max_bytes = 16 * 1024 * 1024
    digest = await upload(client)
    (lane.blobs.root / digest).write_bytes(b"corrupt")
    assert (await client.get("/lane/v1/blobs/" + digest)).status_code == 409
    outside = tmp_path / "outside"
    outside.write_bytes(b"external")
    digest = hashlib.sha256(b"external").hexdigest()
    (lane.blobs.root / digest).symlink_to(outside)
    assert (await client.put("/lane/v1/blobs/" + digest, content=b"external")).status_code == 409
    assert outside.read_bytes() == b"external"


async def test_known_blob_hash_does_not_grant_another_dispatch_read_access(api, auth, signing_key):
    lane, _, _, client = api
    digest = await upload(client)
    pending = await auth[2](run_id="b" * 32)
    token = await exchange(
        lane.auth, pending, oidc(signing_key, pending, changes={"jti": "other-run"})
    )
    assert (
        await client.get(
            "/lane/v1/blobs/" + digest, headers={"Authorization": "Bearer " + token.access_token}
        )
    ).status_code == 404


async def test_checkpoint_exact_digest_sizes_sequence_and_resume_generation(api, signing_key):
    lane, factory, pending, client = api
    digest = await upload(client)
    manifest = checkpoint(pending, digest)
    assert (
        await client.put("/lane/v1/checkpoints/" + "0" * 64, json=manifest.model_dump(mode="json"))
    ).status_code == 409
    wrong = checkpoint(pending, digest, size=1)
    assert (
        await client.put(
            "/lane/v1/checkpoints/" + wrong.checkpoint_id, json=wrong.model_dump(mode="json")
        )
    ).status_code == 409
    assert (
        await client.put(
            "/lane/v1/checkpoints/" + manifest.checkpoint_id, json=manifest.model_dump(mode="json")
        )
    ).status_code == 201
    conflicting = manifest.model_copy(
        update={"entries": (manifest.entries[0].model_copy(update={"path": "other.txt"}),)}
    )
    assert (
        await client.put(
            "/lane/v1/checkpoints/" + conflicting.checkpoint_id,
            json=conflicting.model_dump(mode="json"),
        )
    ).status_code == 409
    async with factory.begin() as session:
        await session.execute(
            update(EngineRun)
            .where(EngineRun.id == pending.identity.forge_run_id)
            .values(generation=8)
        )
    new = await lane.auth.register_pending(
        pending.dispatch.model_copy(
            update={
                "generation": 8,
                "nonce": lane.auth.new_nonce(),
                "checkpoint_id": manifest.checkpoint_id,
            }
        ),
        pending.brief.model_copy(update={"generation": 8}),
        expires_at=pending.expires_at,
        connection_id=pending.connection_id,
        github_workflow_ref=WORKFLOW,
    )
    bearer = await exchange(
        lane.auth, new, oidc(signing_key, new, changes={"jti": "new-generation"})
    )
    assert (await client.get("/lane/v1/brief")).status_code == 401
    headers = {"Authorization": "Bearer " + bearer.access_token}
    assert (
        await client.get("/lane/v1/checkpoints/latest", headers=headers)
    ).json() == manifest.model_dump(mode="json")
    assert (
        await client.get("/lane/v1/blobs/" + digest, headers=headers)
    ).content == b"approved bytes"


async def test_heartbeat_sequence_and_terminal_report_cannot_be_rewritten(api):
    _, _, pending, client = api
    beat = Heartbeat(
        **pending.identity.model_dump(),
        sequence=1,
        observed_at=datetime.now(timezone.utc),
        phase="running",
    )
    for _ in range(2):
        assert (
            await client.post("/lane/v1/heartbeat", json=beat.model_dump(mode="json"))
        ).status_code == 200
    assert (
        await client.post(
            "/lane/v1/heartbeat", json={**beat.model_dump(mode="json"), "phase": "uploading"}
        )
    ).status_code == 409
    terminal = LaneReport(
        **pending.identity.model_dump(),
        approved_digest=pending.dispatch.approved_digest,
        status="failed",
        classification="driver_error",
        driver=pending.dispatch.driver,
        model=pending.dispatch.model,
        terminal_reason="failed",
    )
    assert (
        await client.post("/lane/v1/report", json=terminal.model_dump(mode="json"))
    ).status_code == 201
    assert (
        await client.post(
            "/lane/v1/heartbeat", json={**beat.model_dump(mode="json"), "sequence": 2}
        )
    ).status_code == 409
    late = b"late bytes"
    assert (
        await client.put("/lane/v1/blobs/" + hashlib.sha256(late).hexdigest(), content=late)
    ).status_code == 409


async def test_cancel_between_upload_and_reference_commit_fences_all_api_writes(api, monkeypatch):
    lane, factory, pending, client = api
    original = lane.record_blob

    async def cancelled(identity, digest, size):
        async with factory() as session, session.begin():
            await session.execute(
                update(EngineRun)
                .where(EngineRun.id == identity.forge_run_id)
                .values(cancel_requested=True, cancellation_generation=4)
            )
        return await original(identity, digest, size)

    monkeypatch.setattr(lane, "record_blob", cancelled)
    body = b"cancel race"
    assert (
        await client.put("/lane/v1/blobs/" + hashlib.sha256(body).hexdigest(), content=body)
    ).status_code == 401
    async with factory() as session:
        row = await session.scalar(select(EngineLaneSession))
        assert json.loads(row.api_state_json) == {}
    assert (await client.get("/lane/v1/brief")).status_code == 401


async def test_model_key_is_unavailable_without_injection_and_fake_redeems_once(api):
    lane, factory, pending, client = api
    assert (await client.post("/lane/v1/model-key")).status_code == 503
    calls = []

    async def fake(dispatch):
        calls.append(dispatch.identity)
        return ModelKeyGrant(
            **dispatch.identity.model_dump(),
            gateway_url="https://gateway.example",
            key="explicit-test-fake",
            model=dispatch.dispatch.model,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

    lane.model_key_provider = fake
    response = await client.post("/lane/v1/model-key")
    assert response.status_code == 200 and response.json()["key"] == "explicit-test-fake"
    duplicate = await client.post("/lane/v1/model-key")
    assert duplicate.status_code == 200 and duplicate.json() == response.json()
    restarted = LaneV1Service(lane.auth, EngineLaneV1Store(factory), lane.blobs)
    replay = await restarted.model_key(pending.identity)
    assert replay.model_dump(mode="json") == response.json()
    assert calls == [pending.identity]
    async with factory() as session:
        row = await session.scalar(select(EngineLaneSession))
        assert "explicit-test-fake" not in row.api_state_json
        encrypted = json.loads(row.api_state_json)["model_key_grant"]
        assert set(encrypted) == {"version", "nonce", "ciphertext"}


async def test_api_configuration_is_explicit_and_invalid_bodies_do_not_echo_oidc_credentials(
    api, settings
):
    _, _, _, client = api
    secret = "private-oidc-token-test-value"
    response = await client.post("/lane/v1/session", json={"oidc_token": secret})
    assert response.status_code == 422 and secret not in response.text
    response = await client.post("/lane/v1/session", content=b"x" * (1024 * 1024 + 1))
    assert response.status_code == 413
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://forge.example"
    ) as disabled:
        assert (await disabled.get("/lane/v1/brief")).status_code == 503


async def test_concurrent_candidate_duplicates_read_one_committed_receipt(api):
    _, _, pending, client = api
    digest = await upload(client)
    body = bundle(pending, digest).model_dump(mode="json")
    responses = await asyncio.gather(
        client.post("/lane/v1/candidate", json=body),
        client.post("/lane/v1/candidate", json=body),
    )
    assert sorted(response.status_code for response in responses) == [200, 201]
    assert all(response.json() == body for response in responses)


async def test_lost_model_gateway_outcome_is_reserved_without_replay(api):
    lane, _, _, client = api
    calls = []

    async def lost(pending):
        calls.append(pending.identity)
        raise TimeoutError("provider detail must not escape")

    lane.model_key_provider = lost
    response = await client.post("/lane/v1/model-key")
    assert response.status_code == 503 and "provider detail" not in response.text
    assert (await client.post("/lane/v1/model-key")).status_code == 409
    assert len(calls) == 1


async def test_gitlab_lane_uses_the_same_typed_server_receipts(api, auth, signing_key):
    _, _, _, client = api
    pending = await auth[2](provider="gitlab", run_id="b" * 32)
    response = await client.post(
        "/lane/v1/session",
        json={
            **pending.identity.model_dump(),
            "provider": "gitlab",
            "oidc_token": oidc(signing_key, pending, changes={"jti": "gitlab-http"}),
        },
    )
    assert response.status_code == 200
    client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
    assert (await client.get("/lane/v1/brief")).json() == pending.brief.model_dump(mode="json")
    digest = await upload(client)
    value = bundle(pending, digest)
    assert (
        await client.post("/lane/v1/candidate", json=value.model_dump(mode="json"))
    ).status_code == 201


async def test_configured_application_composes_database_auth_and_clears_service_when_disabled(
    api, settings, tmp_path
):
    lane, _, pending, client = api
    config, _ = configuration(tmp_path)

    @asynccontextmanager
    async def opener(settings):
        assert settings == config
        yield SimpleNamespace(lane=lane)

    app = create_app(config, opener=opener)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://forge.example",
            headers=client.headers,
        ) as configured:
            assert (await configured.get("/lane/v1/brief")).json() == pending.brief.model_dump(
                mode="json"
            )
            assert app.state.lane_v1_service is lane
    assert app.state.lane_v1_service is None and app.state.runtime is None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://forge.example",
        headers=client.headers,
    ) as disabled:
        assert (await disabled.get("/lane/v1/brief")).status_code == 503


async def test_encrypted_model_grant_is_identity_bound_and_expired_cache_never_remints(
    api, monkeypatch
):
    lane, factory, pending, client = api
    calls = []

    async def fake(dispatch):
        calls.append(dispatch.identity)
        return ModelKeyGrant(
            **dispatch.identity.model_dump(),
            gateway_url="https://gateway.example",
            key="vault-expiry-sentinel",
            model=dispatch.dispatch.model,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

    lane.model_key_provider = fake
    assert (await client.post("/lane/v1/model-key")).status_code == 200
    async with factory() as session:
        row = await session.scalar(select(EngineLaneSession))
        encrypted = json.loads(row.api_state_json)["model_key_grant"]
        assert "vault-expiry-sentinel" not in row.api_state_json
    with pytest.raises(VaultError):
        lane.vault.open(pending.identity.model_copy(update={"generation": 8}), encrypted)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(minutes=10)

    monkeypatch.setattr("forge.api.lane_v1_service.datetime", Later)
    assert (await client.post("/lane/v1/model-key")).status_code == 503
    assert calls == [pending.identity]
