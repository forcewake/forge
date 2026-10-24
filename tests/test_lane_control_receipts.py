"""A lane restart cannot turn a settled durable control into a second vendor call."""

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import select

from forge.api.lane_v1 import router
from forge.api.lane_v1_service import LaneV1Service
from forge.store.lane_v1 import EngineLaneV1Store
from forge.store.models_lane import EngineControlCommand
from tests.lane_control_surface_fixture import control_case as control_case
from tests.native_lane_fixture import auth as auth, settings as settings, signing_key as signing_key


@pytest.mark.parametrize("status", ["applied", "refused"])
async def test_restart_same_identity_after_ack_does_not_redeliver(control_case, status):
    case = control_case
    command = await case.queue()
    assert (await case.begin(command)).status_code == 200
    ack = case.ack(command, status=status)
    first = await case.client.post(
        "/lane/v1/controls/" + command.command_id + "/ack", json=ack.model_dump(mode="json")
    )
    assert first.status_code == 200
    # Reconstruct the complete HTTP service and SQL store, with no process cursor.
    app = FastAPI()
    app.state.lane_v1_service = LaneV1Service(
        case.lane.auth, EngineLaneV1Store(case.factory), case.lane.blobs
    )
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://forge.example",
        headers={"Authorization": "Bearer " + case.token},
    ) as restarted:
        poll = await restarted.get("/lane/v1/controls?after=0")
        assert poll.status_code == 200 and poll.json() == []
        replay = await restarted.post(
            "/lane/v1/controls/" + command.command_id + "/ack", json=ack.model_dump(mode="json")
        )
        assert replay.status_code == 200 and replay.json() == first.json()
    rows = await case.rows()
    assert len(rows) == 1 and rows[0][1] == ("acknowledged" if status == "applied" else "refused")
    assert rows[0][2] is not None


async def test_poll_excludes_expired_pause_without_deleting_its_authority(control_case):
    case = control_case
    command = await case.queue("pause")
    async with case.factory.begin() as session:
        row = await session.scalar(
            select(EngineControlCommand).where(
                EngineControlCommand.command_key == command.command_id
            )
        )
        row.state = "expired"
    result = await case.client.get("/lane/v1/controls?after=0")
    assert result.status_code == 200 and result.json() == []
    rows = await case.rows()
    assert len(rows) == 1 and rows[0][1] == "expired" and rows[0][2] is None
