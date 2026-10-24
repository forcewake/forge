"""The pinned real SDK parser permits unchecked usage; lane receipts do not."""

import asyncio
from decimal import Decimal

import pytest
from claude_agent_sdk import ResultMessage
from claude_agent_sdk._internal.message_parser import parse_message

from forge_lane.errors import ExitCode, LaneFailure
from tests.native_driver_fixture import Case, collect, completed_case


def result(usage, cost=None, uuid="result-1"):
    # Actual SDK0.2.157 parser copies these optional fields without validation.
    return parse_message(
        {
            "type": "result",
            "subtype": "success",
            "duration_ms": 1,
            "duration_api_ms": 1,
            "is_error": False,
            "num_turns": 1,
            "session_id": "native-session",
            "terminal_reason": "completed",
            "uuid": uuid,
            "usage": usage,
            "total_cost_usd": cost,
        }
    )


@pytest.mark.parametrize(
    "usage,cost",
    [
        (None, None),
        ({"input_tokens": 0, "output_tokens": 0}, 0),
        ({"input_tokens": 10, "output_tokens": 20}, None),
        (
            {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": 5,
                "cache_creation_input_tokens": 7,
            },
            0.2,
        ),
    ],
)
async def test_real_sdk_valid_or_wholly_unknown_usage_is_preserved(tmp_path, usage, cost):
    message = result(usage, cost)
    assert isinstance(message, ResultMessage) and message.usage == usage
    case = await completed_case(
        tmp_path,
        frames=[message],
        mutate=lambda case: setattr(case.sdk, "ResultMessage", ResultMessage),
    )
    assert case.result.exit_code == ExitCode.COMPLETED
    assert case.server.candidates and case.result.report_accepted
    if usage is None:
        assert case.result.report.usage is None and case.server.candidates[0].usage is None
    else:
        observed = case.result.report.usage
        assert observed.input_tokens == usage["input_tokens"] + usage.get(
            "cache_read_input_tokens", 0
        ) + usage.get("cache_creation_input_tokens", 0)
        assert observed.output_tokens == usage["output_tokens"]
        assert observed.cost_usd == (Decimal(str(cost)) if cost is not None else None)


@pytest.mark.parametrize(
    "usage,cost",
    [
        ({}, None),
        ({"input_tokens": 1}, None),
        ({"output_tokens": 1}, None),
        ({"input_tokens": -1, "output_tokens": 1}, None),
        ({"input_tokens": 1, "output_tokens": -1}, None),
        ({"input_tokens": True, "output_tokens": 1}, None),
        ({"input_tokens": 1, "output_tokens": False}, None),
        ({"input_tokens": 1.5, "output_tokens": 1}, None),
        ({"input_tokens": "1", "output_tokens": 1}, None),
        ({"input_tokens": float("nan"), "output_tokens": 1}, None),
        ({"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": True}, None),
        ({"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": -1}, None),
        ({"input_tokens": 1, "output_tokens": 1}, float("nan")),
        ({"input_tokens": 1, "output_tokens": 1}, float("inf")),
        ({"input_tokens": 1, "output_tokens": 1}, -1),
        ({"input_tokens": 1, "output_tokens": 1}, True),
        ({"input_tokens": 1, "output_tokens": 1}, "0.1"),
        ([], None),
    ],
)
async def test_real_sdk_partial_or_invalid_usage_refuses_before_candidate(tmp_path, usage, cost):
    message = result(usage, cost)
    assert isinstance(message, ResultMessage) and message.usage is usage
    case = await completed_case(
        tmp_path,
        frames=[message],
        mutate=lambda case: setattr(case.sdk, "ResultMessage", ResultMessage),
    )
    assert case.result.exit_code == ExitCode.DRIVER
    assert case.result.report.classification == "driver_usage_invalid"
    assert case.result.report.usage is None and not case.server.candidates
    assert case.sdk.clients[0].disconnected


@pytest.mark.parametrize(
    "first,second",
    [
        (result(None), result({"input_tokens": 1, "output_tokens": 1}, uuid="second")),
        (result({"input_tokens": 1, "output_tokens": 1}), result(None, uuid="second")),
        (
            result({"input_tokens": 1, "output_tokens": 1}, 0.2),
            result({"input_tokens": 1, "output_tokens": 1}, 0.1, uuid="second"),
        ),
    ],
)
async def test_real_sdk_mixed_unknown_or_regressing_cumulative_usage_never_completes(
    tmp_path, first, second
):
    case = Case(tmp_path)
    case.sdk.ResultMessage = ResultMessage
    session = await case.claude()
    await session.steer("approved bounded correction")
    case.sdk.clients[0].messages.put_nowait(first)
    case.sdk.clients[0].messages.put_nowait(second)
    with pytest.raises(LaneFailure) as failure:
        await asyncio.wait_for(collect(session), 2)
    assert failure.value.classification == "driver_usage_invalid"
    assert session.done.is_set() and case.sdk.clients[0].disconnected
