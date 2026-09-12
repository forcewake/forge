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
def flow_state_mgr(fake_redis):
    return FlowStateManager(fake_redis)


async def test_flow_status_endpoint(client, app, flow_state_mgr):
    """GET /flows/{id} returns flow status."""
    app.state.flow_state_mgr = flow_state_mgr

    flow = FlowInstance(
        id="abc123",
        flow_name="test-flow",
        project_id=42,
        trigger_event={},
        current_step=1,
        state={"review": {"severity": "info"}},
        status="running",
        started_at="2026-03-28T12:00:00+00:00",
        updated_at="2026-03-28T12:01:00+00:00",
    )
    await flow_state_mgr.create(flow)

    resp = await client.get("/flows/abc123")
    assert resp.status_code == 200

    data = resp.json()
    assert data["id"] == "abc123"
    assert data["name"] == "test-flow"
    assert data["status"] == "running"
    assert data["current_step"] == 1
    assert data["state"]["review"]["severity"] == "info"


async def test_flow_status_not_found(client, app, flow_state_mgr):
    """GET /flows/{id} returns 404 for unknown flow."""
    app.state.flow_state_mgr = flow_state_mgr

    resp = await client.get("/flows/nonexistent")
    assert resp.status_code == 404


async def test_flow_status_no_redis(client, app):
    """GET /flows/{id} returns 503 when Redis is not configured."""
    app.state.flow_state_mgr = None

    resp = await client.get("/flows/some-id")
    assert resp.status_code == 503
