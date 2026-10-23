"""The shipped planner boundary, with a recording model and no paid calls.

Cohort arms, preregistration and blind grading are retired. Real planning
still bounds prompt input, records errors and never grants its requested budget.
"""

import json
import pytest

from forge.factory.llm import LLMResponseError
from forge.factory.planner import LLMPlanner, PLANNER_MAX_INPUT_CHARS
from forge.runs.harness_selection import compile_harness_selection
from tests.fixtures.fake_llm import FakeLLM


async def test_normal_planner_carries_scope_and_bounded_budget_request():
    model = FakeLLM(
        script=[
            json.dumps(
                {
                    "summary": "Fix widget",
                    "steps": ["fix"],
                    "risks": [],
                    "files_hint": ["src/widget.py"],
                    "budget_class": "heavy",
                    "budget_reason": "parser change",
                }
            )
        ]
    )
    planner = LLMPlanner(model)
    plan = await planner.plan("widget", "repair", path_scope=["src/**"], flow_run_id="run-1")
    assert "Fix widget" in plan
    assert planner.budget_assessment() == "heavy"
    assert "src/**" in model.calls[0]["user"]
    assert model.calls[0]["flow_run_id"] == "run-1"
    assert model.calls[0]["json_mode"] is True
    selection = compile_harness_selection(
        ["claude-code"], "ci_harness:claude-code", {"claude-code"}, planner.last_plan
    )
    assert selection.budget_class == "heavy"


async def test_invalid_json_refuses_and_does_not_reuse_previous_plan():
    model = FakeLLM(script=['{"summary":"old"}', "invalid JSON"])
    planner = LLMPlanner(model)
    await planner.plan("first", "ok")
    assert planner.last_plan is not None
    with pytest.raises(LLMResponseError):
        await planner.plan("second", "bad")
    assert planner.last_plan is None
    assert planner.files_hint() == []


async def test_provider_failure_is_not_an_empty_successful_plan():
    model = FakeLLM(script=[RuntimeError("gateway unavailable")])
    planner = LLMPlanner(model)
    with pytest.raises(RuntimeError, match="gateway unavailable"):
        await planner.plan("issue", "details")
    assert planner.last_plan is None
    assert len(model.calls) == 1


async def test_large_issue_prompt_is_bounded_before_model_call():
    model = FakeLLM(script=['{"summary":"bounded"}'])
    await LLMPlanner(model).plan("title", "x" * (PLANNER_MAX_INPUT_CHARS * 2))
    assert len(model.calls[0]["user"]) <= PLANNER_MAX_INPUT_CHARS
