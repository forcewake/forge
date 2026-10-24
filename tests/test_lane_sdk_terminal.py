"""Pinned real SDK result subtype/reason/origin cannot invent a successful candidate."""

import asyncio
from dataclasses import replace

import pytest
from claude_agent_sdk import ResultMessage

from forge_lane.errors import ExitCode
from tests.native_driver_fixture import Case, collect, completed_case
from tests.test_lane_sdk_usage import result


@pytest.mark.parametrize(
    "reason,origin",
    [
        ("completed", None),
        (None, None),
        ("completed", {"kind": "human"}),
    ],
)
async def test_actual_sdk_successful_own_prompt_result_can_complete(tmp_path, reason, origin):
    message = replace(
        result({"input_tokens": 1, "output_tokens": 2}), terminal_reason=reason, origin=origin
    )
    case = await completed_case(
        tmp_path,
        frames=[message],
        mutate=lambda case: setattr(case.sdk, "ResultMessage", ResultMessage),
    )
    assert case.result.exit_code == ExitCode.COMPLETED and case.server.candidates
    assert case.result.report.status == "completed" and case.result.report_accepted


@pytest.mark.parametrize(
    "reason,subtype,error,classification",
    [
        ("aborted_tools", "success", False, "driver_result_incomplete"),
        ("aborted_streaming", "success", False, "driver_result_incomplete"),
        ("max_turns", "success", False, "driver_result_incomplete"),
        ("future_unconfirmed", "success", False, "driver_result_incomplete"),
        ("completed", "error_during_execution", False, "driver_result_error"),
        ("completed", "error_max_turns", False, "driver_result_error"),
        ("completed", "success", True, "driver_result_error"),
        ("aborted_tools", "error_during_execution", True, "driver_result_error"),
    ],
)
async def test_actual_sdk_incomplete_or_error_result_cannot_submit_candidate(
    tmp_path, reason, subtype, error, classification
):
    message = replace(
        result({"input_tokens": 1, "output_tokens": 2}),
        terminal_reason=reason,
        subtype=subtype,
        is_error=error,
    )
    case = await completed_case(
        tmp_path,
        frames=[message],
        mutate=lambda case: setattr(case.sdk, "ResultMessage", ResultMessage),
    )
    assert case.result.exit_code == ExitCode.DRIVER and not case.server.candidates
    assert (
        case.result.report.classification == classification
        and case.result.report.status == "failed"
    )
    assert case.sdk.clients[0].disconnected


@pytest.mark.parametrize("kind", ["task-notification", "scheduled-task", "unknown-future-origin"])
async def test_actual_sdk_injected_background_result_does_not_complete_or_count_usage(
    tmp_path, kind
):
    case = Case(tmp_path)
    case.sdk.ResultMessage = ResultMessage
    session = await case.claude()
    observed = asyncio.create_task(collect(session))
    background = replace(
        result({"input_tokens": 999, "output_tokens": 999}, 99), origin={"kind": kind}
    )
    case.sdk.clients[0].messages.put_nowait(background)
    await asyncio.sleep(0.01)
    assert not observed.done() and session.received == 0 and session.usage is None
    own = replace(result({"input_tokens": 1, "output_tokens": 2}), origin={"kind": "human"})
    case.sdk.clients[0].messages.put_nowait(own)
    events = await asyncio.wait_for(observed, 2)
    assert len([event for event in events if event.kind == "completed"]) == 1
    assert events[-1].usage.input_tokens == 1 and events[-1].usage.output_tokens == 2
    assert session.received == 1 and case.sdk.clients[0].disconnected
