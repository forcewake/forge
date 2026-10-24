"""Current signed lane HTTP and accepted SQL control receipts for retained lessons."""

from datetime import datetime, timezone
import json
from uuid import uuid4

from fastapi import FastAPI
from forge_protocol import (
    ControlAck,
    ControlCommand,
    ControlDelivery,
    ModelKeyGrant,
    SessionRequest,
)
import httpx
import pytest
from sqlalchemy import select

from forge.api.lane_v1 import router
from forge.api.lane_v1_blobs import LaneBlobStore
from forge.api.lane_v1_service import LaneV1Service
from forge.command_values import CommandEnvelope
from forge.domain.plan import RepositoryIdentity
from forge.ingress_values import Command
from forge.port_values import Actor, RepoRef, SubjectRef
from forge.store.commands import CommandStore
from forge.store.controls import ControlStore
from forge.store.lane_v1 import EngineLaneV1Store
from forge.store.models_lane import EngineControlCommand, EngineLaneSession
from forge.store.models_runs import EngineRun
from forge.store.records import RunRequest
from forge.store.runs import subject_key
from tests.native_lane_fixture import PUBLIC, oidc


def now():
    return datetime.now(timezone.utc)


class LaneControlCase:
    def __init__(self, lane, factory, pending, app, client, token, key):
        self.lane, self.factory, self.pending = lane, factory, pending
        self.app, self.client, self.token, self.key = app, client, token, key
        self.identity = pending.identity
        self.deliveries = {}

    async def queue(self, action="steer", text="bounded operator guidance"):
        provider = self.pending.dispatch.provider
        connection = "connection-" + provider
        repo = RepoRef(
            identity=RepositoryIdentity(
                provider=provider, connection_id=connection, repository_id="123"
            ),
            full_name="owner/repo",
        )
        envelope = CommandEnvelope(
            source="api",
            native_id=uuid4().hex,
            actor=Actor(
                provider=provider,
                connection_id=connection,
                native_id="42",
                login="owner",
                is_bot=False,
            ),
            target=SubjectRef(repo=repo, number=7),
            command=Command(kind=action, argument=text if action == "steer" else ""),
        )
        commands = CommandStore(self.factory)
        record = await commands.accept(envelope, now=now())
        lease = await commands.claim("control-test", now=now(), command_id=record.id)
        assert lease is not None
        result = await ControlStore(
            self.factory, {self.pending.dispatch.driver: frozenset({"steer", "interrupt"})}
        ).control(lease, self.identity.forge_run_id, now=now())
        assert result.status == "accepted", result
        async with self.factory() as session:
            row = await session.scalar(
                select(EngineControlCommand).where(EngineControlCommand.command_key == record.id)
            )
            assert row is not None
            return ControlCommand.model_validate_json(row.payload_json)

    async def begin(self, command, delivery_id=None):
        delivery = ControlDelivery(
            **self.identity.model_dump(),
            command_id=command.command_id,
            sequence=command.sequence,
            delivery_id=delivery_id or uuid4().hex,
        )
        response = await self.client.post(
            "/lane/v1/controls/" + command.command_id + "/begin",
            json=delivery.model_dump(mode="json"),
        )
        if response.status_code == 200:
            self.deliveries[command.command_id] = delivery.delivery_id
        return response

    def ack(self, command, **changes):
        return ControlAck(
            **{
                **self.identity.model_dump(),
                "command_id": command.command_id,
                "sequence": command.sequence,
                "delivery_id": self.deliveries.get(command.command_id, "0" * 32),
                "status": "applied",
                **changes,
            }
        )

    async def acknowledge(self, command, **changes):
        if command.command_id not in self.deliveries:
            begun = await self.begin(command)
            if begun.status_code != 200:
                return begun
        return await self.client.post(
            "/lane/v1/controls/" + command.command_id + "/ack",
            json=self.ack(command, **changes).model_dump(mode="json"),
        )

    async def rows(self):
        async with self.factory() as session:
            rows = (
                await session.scalars(
                    select(EngineControlCommand)
                    .where(EngineControlCommand.run_id == self.identity.forge_run_id)
                    .order_by(EngineControlCommand.sequence)
                )
            ).all()
            return [(r.command_key, r.state, r.acknowledgement_json, r.payload_json) for r in rows]

    async def mutate_run(self, **changes):
        async with self.factory.begin() as session:
            row = await session.get(EngineRun, self.identity.forge_run_id)
            for key, value in changes.items():
                setattr(row, key, value)

    async def state(self):
        async with self.factory() as session:
            row = await session.scalar(
                select(EngineLaneSession).where(
                    EngineLaneSession.run_id == self.identity.forge_run_id
                )
            )
            return json.loads(row.api_state_json)

    def grant(self, **changes):
        from datetime import timedelta

        values = dict(
            **self.identity.model_dump(),
            model=self.pending.dispatch.model,
            gateway_url="https://gateway.example",
            key="synthetic-scoped-model-key",
            expires_at=now() + timedelta(minutes=3),
        )
        values.update(changes)
        return ModelKeyGrant(**values)


@pytest.fixture(params=["github", "gitlab"])
async def control_case(auth, signing_key, tmp_path, request):
    service, factory, register, _ = auth
    pending = await register(request.param)
    async with factory.begin() as session:
        row = await session.get(EngineRun, pending.identity.forge_run_id)
        row.subject_id = "7"
        row.subject_key = subject_key(
            RunRequest(row.id, row.provider, row.connection_id, row.repository_id, "issue", "7")
        )
    lane = LaneV1Service(service, EngineLaneV1Store(factory), LaneBlobStore(tmp_path / "blobs"))
    app = FastAPI()
    app.state.lane_v1_service = lane
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC) as client:
        exchange = await client.post(
            "/lane/v1/session",
            json=SessionRequest(
                **pending.identity.model_dump(),
                provider=request.param,
                oidc_token=oidc(signing_key, pending),
            ).model_dump(mode="json"),
        )
        assert exchange.status_code == 200, exchange.text
        token = exchange.json()["access_token"]
        client.headers["Authorization"] = "Bearer " + token
        yield LaneControlCase(lane, factory, pending, app, client, token, signing_key)
