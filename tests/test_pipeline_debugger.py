from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.agents.models import PipelineDebugResult
from forge.agents.pipeline_debugger import PipelineDebuggerAgent
from forge.agents.registry import AgentDefinition, TriggerSpec
from forge.context.engine import AgentContext
from forge.gitlab.schemas import Job, Pipeline


def _make_definition() -> AgentDefinition:
    return AgentDefinition(
        name="pipeline-debugger",
        type="pipeline-debugger",
        description="Debug pipelines",
        triggers=[TriggerSpec(event="pipeline", actions=["failed"])],
        model_alias="strong",
        system_prompt="You are a CI debugger for {project_path}.",
        actions={"summary_note": True},
    )


def _make_context(
    *,
    with_mr: bool = False,
    ci_config: str = "",
) -> AgentContext:
    return AgentContext(
        event_type="pipeline",
        project_id=42,
        project_path="group/project",
        pipeline=Pipeline(id=10, status="failed", ref="main", sha="abc123"),
        failed_jobs=[
            Job(
                id=101,
                name="test-unit",
                stage="test",
                status="failed",
                failure_reason="script_failure",
            ),
        ],
        job_logs={
            101: "$ pytest\nFAILED tests/test_foo.py::test_bar\nAssertionError: 1 != 2\n=== 1 failed ===",
        },
        ci_config=ci_config,
    )


class TestPipelineDebuggerAgent:
    def test_output_schema(self):
        agent = PipelineDebuggerAgent(
            definition=_make_definition(),
            model=MagicMock(),
            context=_make_context(),
            project_config=MagicMock(review_rules=[]),
            gitlab=MagicMock(),
        )
        assert agent._output_schema() is PipelineDebugResult

    def test_build_user_message_includes_pipeline_info(self):
        agent = PipelineDebuggerAgent(
            definition=_make_definition(),
            model=MagicMock(),
            context=_make_context(),
            project_config=MagicMock(review_rules=[]),
            gitlab=MagicMock(),
        )
        msg = agent._build_user_message()
        assert "failed" in msg
        assert "test-unit" in msg
        assert "AssertionError" in msg

    def test_build_user_message_includes_ci_config(self):
        agent = PipelineDebuggerAgent(
            definition=_make_definition(),
            model=MagicMock(),
            context=_make_context(ci_config="stages:\n  - test\n  - deploy"),
            project_config=MagicMock(review_rules=[]),
            gitlab=MagicMock(),
        )
        msg = agent._build_user_message()
        assert ".gitlab-ci.yml" in msg
        assert "stages:" in msg

    def test_system_prompt_includes_project_path(self):
        agent = PipelineDebuggerAgent(
            definition=_make_definition(),
            model=MagicMock(),
            context=_make_context(),
            project_config=MagicMock(review_rules=[]),
            gitlab=MagicMock(),
        )
        prompt = agent._render_system_prompt()
        assert "group/project" in prompt

    @pytest.mark.asyncio
    async def test_run_returns_pipeline_debug_result(self):
        agent = PipelineDebuggerAgent(
            definition=_make_definition(),
            model=MagicMock(),
            context=_make_context(),
            project_config=MagicMock(review_rules=[]),
            gitlab=MagicMock(),
        )

        mock_result = PipelineDebugResult(
            summary="Test failure in test-unit",
            is_flaky=False,
            jobs=[],
            suggested_actions=["Fix the assertion"],
        )

        response = MagicMock()
        response.content = mock_result
        response.metrics = None

        with patch("forge.agents.base.Agent") as MockAgent:
            instance = MockAgent.return_value
            instance.arun = AsyncMock(return_value=response)
            result = await agent.run()

        assert result.success is True
        assert result.pipeline_debug is not None
        assert result.pipeline_debug.summary == "Test failure in test-unit"
        assert result.review is None
