"""Native control intent is durable before SDK I/O and unknown delivery never recalls it."""

import asyncio
from datetime import timedelta
import json
from uuid import uuid4

import httpx
import pytest
from forge_lane.client import LaneClient
from forge_lane.control_queue import ControlQueue
from forge_lane.driver import DriverEnv
from forge_lane.errors import LaneFailure
from forge_lane.drivers.claude_code import ClaudeCodeDriver
from forge_lane.supervisor import Supervisor, Timings
from forge_protocol import ControlDelivery
from sqlalchemy import select

from forge.store.models_lane import EngineControlCommand
from forge.store.operator_evidence import controls
from forge.store.runs import RunStore
from tests.native_driver_fixture import Case
from tests.lane_control_surface_fixture import control_case as control_case, now
from tests.native_lane_fixture import auth as auth, settings as settings, signing_key as signing_key


async def test_matching_delivery_is_required_before_terminal_ack(control_case):
    case = control_case
    command = await case.queue()
    body = case.ack(command).model_dump(mode="json")
    endpoint = "/lane/v1/controls/" + command.command_id
    old = {key: value for key, value in body.items() if key != "delivery_id"}
    assert (await case.client.post(endpoint + "/ack", json=old)).status_code == 422
    assert (await case.client.post(endpoint + "/ack", json=body)).status_code == 409
    assert (await case.begin(command)).status_code == 200
    wrong = {**case.ack(command).model_dump(mode="json"), "delivery_id": uuid4().hex}
    assert (await case.client.post(endpoint + "/ack", json=wrong)).status_code == 409
    assert (await case.rows())[0][1:3] == ("inflight", None)
    assert (await case.acknowledge(command)).status_code == 200


async def test_duplicate_begin_never_authorizes_another_delivery(control_case):
    case = control_case
    command = await case.queue("pause")
    first = await case.begin(command, "a" * 32)
    assert first.status_code == 200
    exact_replay = await case.begin(command, "a" * 32)
    changed_replay = await case.begin(command, "b" * 32)
    assert exact_replay.status_code == changed_replay.status_code == 409
    assert (await case.client.get("/lane/v1/controls?after=0")).json() == []
    async with case.factory() as session:
        row = await session.scalar(select(EngineControlCommand))
        assert row.state == "inflight" and row.acknowledgement_json is None
        assert json.loads(row.delivery_json)["delivery_id"] == "a" * 32


async def test_concurrent_begin_has_exactly_one_winner(control_case):
    case = control_case
    command = await case.queue()
    responses = await asyncio.gather(case.begin(command, "a" * 32), case.begin(command, "b" * 32))
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert (await case.rows())[0][1] == "inflight"


async def test_expired_pending_command_never_starts(control_case):
    case = control_case
    command = await case.queue()
    async with case.factory.begin() as session:
        row = await session.scalar(select(EngineControlCommand))
        row.deadline_at = now() - timedelta(seconds=1)
    assert (await case.begin(command)).status_code == 409
    assert (await case.rows())[0][1:3] == ("pending", None)


async def test_begin_path_sequence_and_signed_identity_are_bound(control_case):
    case = control_case
    command = await case.queue()
    body = ControlDelivery(
        **case.identity.model_dump(),
        command_id=command.command_id,
        sequence=2,
        delivery_id="a" * 32,
    ).model_dump(mode="json")
    endpoint = "/lane/v1/controls/" + command.command_id + "/begin"
    assert (await case.client.post(endpoint, json=body)).status_code == 409
    body["sequence"] = 1
    body["forge_run_id"] = "b" * 32
    assert (await case.client.post(endpoint, json=body)).status_code == 409
    body["forge_run_id"] = case.identity.forge_run_id
    assert (await case.client.post("/lane/v1/controls/foreign/begin", json=body)).status_code == 409
    assert (await case.rows())[0][1] == "pending"


@pytest.mark.parametrize("path", ["apply", "steer"])
@pytest.mark.parametrize("failure", ["lost_begin", "lost_ack", "sdk_error"])
async def test_actual_primary_driver_never_repeats_unknown_delivery(
    control_case, tmp_path, monkeypatch, failure, path
):
    case = control_case
    command = await case.queue(text="unique native delivery witness")
    process = Case(tmp_path / "primary-driver")
    driver = ClaudeCodeDriver(process.sdk)
    session = await driver.start(
        case.pending.brief, process.workspace, DriverEnv(case.pending.dispatch.model, case.grant())
    )
    client = LaneClient(case.pending.dispatch, case.client)
    native = process.sdk.clients[0]
    real_query = native.query
    observed = []

    async def query(text):
        # Independent SQL read at actual SDK boundary: intent must precede vendor I/O.
        observed.append((await case.rows())[0][1])
        if failure == "sdk_error":
            raise TimeoutError("synthetic native response lost")
        await real_query(text)

    monkeypatch.setattr(native, "query", query)
    if failure == "lost_begin":
        real_begin = client.begin

        async def lost_begin(value):
            await real_begin(value)
            raise httpx.ReadError("synthetic begin response lost")

        monkeypatch.setattr(client, "begin", lost_begin)
    else:

        async def lost_ack(value):
            raise httpx.ReadError("synthetic acknowledgment response lost")

        monkeypatch.setattr(client, "ack", lost_ack)
    supervisor = Supervisor(client, driver, session, None, Timings(controls=0.005))
    try:
        with pytest.raises((httpx.ReadError, TimeoutError, LaneFailure)):
            if path == "apply":
                await supervisor.apply(command)
            else:
                await supervisor.steer(command, ControlQueue(client, 0.005))
        assert observed == ([] if failure == "lost_begin" else ["inflight"])
        assert len([p for p in native.prompts if "unique native delivery witness" in p]) == (
            1 if failure == "lost_ack" else 0
        )
        # New client has no in-memory cursor or delivery state.
        restarted = LaneClient(case.pending.dispatch, case.client)
        assert await restarted.controls(0) == []
        with pytest.raises(LaneFailure, match="HTTP 409"):
            await restarted.begin(command)
        assert (await case.rows())[0][1:3] == ("inflight", None)
    finally:
        await session.interrupt()


async def test_operator_view_keeps_inflight_outcome_unknown(control_case):
    case = control_case
    command = await case.queue("pause")
    assert (await case.begin(command)).status_code == 200
    run = await RunStore(case.factory).load(case.identity.forge_run_id)
    assert run is not None
    async with case.factory() as session:
        snapshot = await controls(session, run)
    row = snapshot["latest"][0]
    assert row["state"] == "inflight" and row["delivery_outcome"] == "unknown"
    assert row["delivery_intent"] == "recorded" and row["acknowledged_status"] is None
    assert (await case.acknowledge(command, status="refused")).status_code == 200
    assert run is not None
    async with case.factory() as session:
        snapshot = await controls(session, run)
    assert snapshot["latest"][0]["delivery_outcome"] == "refused"


async def test_unknown_interrupt_is_not_recalled_during_cleanup():
    from types import SimpleNamespace

    calls = []

    async def interrupt():
        calls.append("interrupt")
        raise TimeoutError("synthetic stop response lost")

    session = SimpleNamespace(interrupt=interrupt)
    supervisor = Supervisor(None, None, session, None, Timings(shutdown=0.01))
    for _ in range(2):
        with pytest.raises(LaneFailure, match="Driver"):
            await supervisor.stop()
    assert calls == ["interrupt"] and supervisor.stopped is False


@pytest.mark.parametrize("state", ["pending", "inflight", "acknowledged", "refused", "expired"])
async def test_public_poll_never_releases_accepted_pause_authority(control_case, state):
    from forge.store.models_runs import EngineRun
    from forge.store.pause import PublicationPaused, require_unpaused

    case = control_case
    command = await case.queue("pause")
    if state in {"inflight", "acknowledged", "refused"}:
        assert (await case.begin(command)).status_code == 200
    if state in {"acknowledged", "refused"}:
        status = "applied" if state == "acknowledged" else "refused"
        assert (await case.acknowledge(command, status=status)).status_code == 200
    if state == "expired":
        async with case.factory.begin() as session:
            row = await session.scalar(select(EngineControlCommand))
            row.state = state
    response = await case.client.get("/lane/v1/controls?after=0")
    assert response.status_code == 200
    assert len(response.json()) == int(state == "pending")
    async with case.factory.begin() as session:
        run = await session.scalar(select(EngineRun).with_for_update())
        with pytest.raises(PublicationPaused, match="publication_paused"):
            await require_unpaused(session, run)
