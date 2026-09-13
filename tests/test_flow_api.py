"""F30: the legacy ``/flows/{id}`` endpoint is removed from the router.

It served raw legacy Redis flow state without any auth; the durable run
read model lives at ``/runs``. The legacy FlowRunner itself still executes
in the worker — only the leaking read route is gone, for every caller.
"""

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


async def test_legacy_flow_route_removed(client, app, flow_state_mgr):
    """GET /flows/{id} answers 404 even when the flow EXISTS in Redis."""
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
    assert resp.status_code == 404
    # No legacy state leaks through the 404 body either.
    assert "state" not in resp.json()


async def test_legacy_flow_route_removed_without_redis(client, app):
    """Without Redis there is no 503 fallback either — the route is gone."""
    app.state.flow_state_mgr = None

    resp = await client.get("/flows/some-id")
    assert resp.status_code == 404
