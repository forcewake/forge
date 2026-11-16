"""A single dropped controls poll does not end a lane that is otherwise healthy."""

import pytest

from forge_lane.control_queue import ControlQueue
from forge_lane.errors import ExitCode, LaneFailure


class Controls:
    def __init__(self, failures, commands):
        self.failures, self.commands, self.calls = list(failures), commands, 0

    async def controls(self, after):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return self.commands


def transport(detail):
    return LaneFailure(ExitCode.TRANSPORT, "transport_error", detail)


async def test_one_transient_controls_poll_is_retried():
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    queue = ControlQueue(
        Controls([transport("Lane request failed: GET /controls")], []),
        1,
        sleep=sleep,
    )
    await queue.poll()
    assert queue.client.calls == 2 and slept == [1.0]


async def test_a_persistent_controls_failure_is_not_hidden():
    async def sleep(_seconds):
        return None

    queue = ControlQueue(Controls([transport("down")] * 3, []), 1, sleep=sleep)
    with pytest.raises(LaneFailure, match="down"):
        await queue.poll()
    assert queue.client.calls == 3


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
