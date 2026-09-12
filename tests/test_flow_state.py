import fakeredis
import pytest

from forge.flows.models import FlowInstance
from forge.flows.state import FlowStateManager
from forge.utils.redis_client import RedisManager


@pytest.fixture()
def fake_redis():
    manager = RedisManager.__new__(RedisManager)
    server = fakeredis.FakeServer()
    manager._pool = None
    manager._client = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    return manager


@pytest.fixture()
def state_mgr(fake_redis):
    return FlowStateManager(fake_redis)


def _make_flow(**overrides) -> FlowInstance:
    defaults = {
        "id": "abc123def456",
        "flow_name": "test-flow",
        "project_id": 42,
        "trigger_event": {"object_kind": "note"},
        "current_step": 0,
        "state": {},
        "status": "running",
        "started_at": "2026-03-28T12:00:00+00:00",
        "updated_at": "2026-03-28T12:00:00+00:00",
    }
    defaults.update(overrides)
    return FlowInstance(**defaults)


async def test_create_and_get(state_mgr):
    flow = _make_flow()
    await state_mgr.create(flow)

    retrieved = await state_mgr.get(flow.id)
    assert retrieved is not None
    assert retrieved.id == flow.id
    assert retrieved.flow_name == "test-flow"
    assert retrieved.status == "running"


async def test_get_missing_returns_none(state_mgr):
    result = await state_mgr.get("nonexistent-id")
    assert result is None


async def test_update_step(state_mgr):
    flow = _make_flow()
    await state_mgr.create(flow)

    await state_mgr.update_step(
        flow.id,
        step_index=0,
        step_name="analyze",
        output={"summary": "looks good"},
        status="running",
    )

    updated = await state_mgr.get(flow.id)
    assert updated.current_step == 1
    assert updated.state["analyze"]["summary"] == "looks good"


async def test_step_output_accumulates(state_mgr):
    flow = _make_flow()
    await state_mgr.create(flow)

    await state_mgr.update_step(flow.id, 0, "step_a", {"result": "a"})
    await state_mgr.update_step(flow.id, 1, "step_b", {"result": "b"})

    updated = await state_mgr.get(flow.id)
    assert updated.current_step == 2
    assert updated.state["step_a"]["result"] == "a"
    assert updated.state["step_b"]["result"] == "b"


async def test_set_status(state_mgr):
    flow = _make_flow()
    await state_mgr.create(flow)

    await state_mgr.set_status(flow.id, "completed")
    updated = await state_mgr.get(flow.id)
    assert updated.status == "completed"
    assert updated.error is None


async def test_set_status_with_error(state_mgr):
    flow = _make_flow()
    await state_mgr.create(flow)

    await state_mgr.set_status(flow.id, "failed", "Agent crashed")
    updated = await state_mgr.get(flow.id)
    assert updated.status == "failed"
    assert updated.error == "Agent crashed"


async def test_get_step_output(state_mgr):
    flow = _make_flow()
    await state_mgr.create(flow)

    await state_mgr.update_step(flow.id, 0, "review", {"severity": "warning"})

    output = await state_mgr.get_step_output(flow.id, "review")
    assert output == {"severity": "warning"}


async def test_get_step_output_missing(state_mgr):
    flow = _make_flow()
    await state_mgr.create(flow)

    output = await state_mgr.get_step_output(flow.id, "nonexistent")
    assert output is None


async def test_get_step_output_missing_flow(state_mgr):
    output = await state_mgr.get_step_output("nonexistent", "step")
    assert output is None
