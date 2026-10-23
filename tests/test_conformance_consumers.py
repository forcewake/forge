"""Actual lane consumers refuse stale or cross-attempt authority."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from forge_lane.client import LaneClient
from forge_lane.driver import DriverEnv
from forge_lane.drivers.claude_code import ClaudeCodeDriver
from forge_lane.errors import LaneFailure
from forge_protocol import ModelKeyGrant
from tests.test_lane_runner_v1 import inputs


@pytest.mark.parametrize("axis", ["forge_run_id", "attempt", "generation", "model", "expires_at"])
async def test_wrong_or_stale_grant_never_becomes_driver_input(axis):
    dispatch, _ = inputs("a" * 40)
    data = dict(
        forge_run_id=dispatch.forge_run_id,
        attempt=dispatch.attempt,
        generation=dispatch.generation,
        gateway_url="https://gateway.example",
        key="scoped-only",
        model=dispatch.model,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
    )
    data[axis] = {
        "forge_run_id": "other",
        "attempt": 9,
        "generation": 9,
        "model": "other-model",
        "expires_at": datetime.now(timezone.utc) - timedelta(minutes=1),
    }[axis]
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=ModelKeyGrant(**data).model_dump(mode="json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = LaneClient(dispatch, http)
        with pytest.raises(LaneFailure):
            await client.model_key()
    assert len(calls) == 1


async def test_no_ambient_key_substitutes_for_a_grant(tmp_path, monkeypatch):
    _, brief = inputs("a" * 40)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-must-not-be-used")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "another-ambient-key")
    driver = ClaudeCodeDriver(sdk=SimpleNamespace())
    with pytest.raises(LaneFailure, match="gateway grant"):
        await driver.start(brief, tmp_path, DriverEnv(model="test-model"))


async def test_control_for_another_generation_is_refused():
    dispatch, _ = inputs("a" * 40)
    command = {
        "protocol": "forge.lane/1",
        "forge_run_id": dispatch.forge_run_id,
        "attempt": dispatch.attempt,
        "generation": dispatch.generation + 1,
        "command_id": "pause-1",
        "sequence": 1,
        "action": "pause",
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[command]))
    ) as http:
        with pytest.raises(LaneFailure, match="control identity"):
            await LaneClient(dispatch, http).controls(0)
