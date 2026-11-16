"""A controls poll uses the lane client's 15s retry window."""

import httpx
import pytest

from forge_lane.client import LaneClient
from forge_lane.control_queue import ControlQueue
from forge_lane.errors import ExitCode, LaneFailure
from forge_protocol import LaneDispatch


class Controls:
    def __init__(self, failures, commands):
        self.failures, self.commands, self.calls = list(failures), commands, 0

    async def controls(self, after):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return self.commands


def dispatch() -> LaneDispatch:
    return LaneDispatch(
        forge_run_id="run-1",
        attempt=1,
        generation=1,
        server_url="https://forge.example",
        provider="gitlab",
        repository="group/project",
        ref="forge/run",
        base_sha="b" * 40,
        approved_digest="c" * 64,
        driver="claude-code",
        model="glm",
        timeout_seconds=30,
    )


def lane(statuses: list[int]) -> tuple[LaneClient, list[float], list[int]]:
    seen: list[int] = []
    slept: list[float] = []

    def respond(_request: httpx.Request) -> httpx.Response:
        status = statuses[min(len(seen), len(statuses) - 1)]
        seen.append(status)
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json=[])

    client = LaneClient(dispatch(), httpx.AsyncClient(transport=httpx.MockTransport(respond)))

    async def record(seconds: float) -> None:
        slept.append(seconds)

    client._sleep = record
    return client, slept, seen


async def test_an_authentication_refusal_is_not_retried():
    queue = ControlQueue(
        Controls(
            [
                LaneFailure(
                    ExitCode.AUTHENTICATION,
                    "session_refused",
                    "Lane server refused GET /controls: HTTP 401",
                )
            ],
            [],
        ),
        1,
    )
    with pytest.raises(LaneFailure, match="HTTP 401"):
        await queue.poll()
    assert queue.client.calls == 1


async def test_controls_poll_survives_a_six_second_outage():
    client, slept, seen = lane([520, 520, 520, 200])
    await ControlQueue(client, 1).poll()
    assert seen == [520, 520, 520, 200]
    assert slept == [1.0, 2.0, 4.0]
    assert sum(slept) >= 6
    await client.http.aclose()


async def test_a_persistent_controls_outage_stops_after_fifteen_seconds():
    client, slept, seen = lane([520])
    with pytest.raises(LaneFailure, match="HTTP 520"):
        await ControlQueue(client, 1).poll()
    assert slept == [1.0, 2.0, 4.0, 5.0, 3.0]
    assert sum(slept) == 15 and max(slept) <= 5
    assert len(seen) == len(slept) + 1
    await client.http.aclose()
