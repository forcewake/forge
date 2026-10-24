"""Real native lane admission, signed HTTP receipts and fresh-checkout checkpoints."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import FastAPI
import httpx
from sqlalchemy import select
from forge_protocol import CheckpointEntry, LaneDispatch, RunIdentity

from forge.api.lane_v1 import router
from forge.api.lane_v1_blobs import LaneBlobStore
from forge.api.lane_v1_service import LaneV1Service
from forge.domain.approved_input import ApprovedInput
from forge.store.lane_v1 import EngineLaneV1Store
from forge.store.models_lane import EngineCheckpoint
from forge.store.models_runs import EngineApprovedInput, EngineRun, EngineRunSpec
from forge.store.records import RunRequest
from forge.store.runs import RunStore
from tests.native_lane_fixture import WORKFLOW, exchange, oidc, checkpoint, upload
from tests.test_domain_approved_input import spec


async def seed(api, *, body=b"approved bytes", sequence=1):
    lane, factory, pending, client = api
    key = await upload(client, body)
    manifest = checkpoint(pending, key, size=len(body), sequence=sequence)
    response = await client.put(
        "/lane/v1/checkpoints/" + manifest.checkpoint_id, json=manifest.model_dump(mode="json")
    )
    assert response.status_code == 201, response.text
    return manifest


async def advance(api, key, signing_key, **changes):
    lane, factory, pending, client = api
    generation = pending.identity.generation + 1
    async with factory.begin() as session:
        run = await session.get(EngineRun, pending.identity.forge_run_id)
        run.generation = generation
    new = await lane.auth.register_pending(
        pending.dispatch.model_copy(
            update={
                "generation": generation,
                "nonce": lane.auth.new_nonce(),
                "checkpoint_id": key,
                **changes,
            }
        ),
        pending.brief.model_copy(update={"generation": generation}),
        expires_at=pending.expires_at,
        connection_id=pending.connection_id,
        github_workflow_ref=WORKFLOW,
    )
    bearer = await exchange(lane.auth, new, oidc(signing_key, new, changes={"jti": uuid4().hex}))
    client.headers["Authorization"] = "Bearer " + bearer.access_token
    return new


async def saved(factory):
    async with factory() as session:
        return tuple((await session.scalars(select(EngineCheckpoint))).all())


async def register_real(auth, base):
    service, factory, _, _ = auth
    run_id = uuid4().hex
    frozen = spec(base_sha=base)
    approved = ApprovedInput.from_spec(frozen)
    await RunStore(factory).admit(
        RunRequest(run_id, "github", "connection-github", "123", "issue", run_id)
    )
    async with factory.begin() as session:
        row = await session.get(EngineRun, run_id)
        row.state = "running"
        session.add(
            EngineRunSpec(
                id=uuid4().hex, run_id=run_id, digest=frozen.digest, document_json=frozen.spec_json
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
    identity = RunIdentity(forge_run_id=run_id, attempt=1, generation=1)
    dispatch = LaneDispatch(
        **identity.model_dump(),
        server_url="https://forge.example",
        provider="github",
        repository="owner/repo",
        ref="main",
        base_sha=base,
        approved_digest=approved.approved_digest,
        driver=frozen.driver,
        model=frozen.model,
        timeout_seconds=600,
        nonce=service.new_nonce(),
    )
    return await service.register_pending(
        dispatch,
        approved.to_brief(identity, frozen),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        connection_id="connection-github",
        github_workflow_ref=WORKFLOW,
    )


@asynccontextmanager
async def real_http(auth, key, root, base):
    pending = await register_real(auth, base)
    service, factory, _, _ = auth
    lane = LaneV1Service(service, EngineLaneV1Store(factory), LaneBlobStore(root / "blobs"))
    app = FastAPI()
    app.state.lane_v1_service = lane
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://forge.example"
    ) as client:
        response = await client.post(
            "/lane/v1/session",
            json={
                **pending.identity.model_dump(),
                "provider": "github",
                "oidc_token": oidc(key, pending),
            },
        )
        assert response.status_code == 200, response.text
        client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        yield lane, factory, pending, client


def entry(path, data=b"checkpoint bytes", *, state="new", mode="100644"):
    from hashlib import sha256

    return CheckpointEntry(
        path=path,
        mode=mode,
        state=state,
        sha256=sha256(data).hexdigest() if state != "deleted" else None,
        size_bytes=len(data) if state != "deleted" else 0,
    )
