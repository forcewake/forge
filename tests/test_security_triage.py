from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.agents.models import SecurityTriageResult
from forge.agents.security_triage import SecurityTriageAgent
from forge.agents.registry import AgentDefinition, TriggerSpec
from forge.context.engine import AgentContext
from forge.context.security_report import SecurityFinding, SecurityReport
from forge.gitlab.schemas import Pipeline


def _make_definition() -> AgentDefinition:
    return AgentDefinition(
        name="security-triage",
        type="security-triage",
        description="Triage security findings",
        triggers=[TriggerSpec(event="build", actions=["success"], job_names=["semgrep-sast"])],
        model_alias="strong",
        system_prompt="You are a security engineer for {project_path}.",
        actions={"summary_note": True, "labels": True},
    )


def _make_context(*, with_reports: bool = True) -> AgentContext:
    reports = []
    if with_reports:
        reports = [
            SecurityReport(
                findings=[
                    SecurityFinding(
                        id="v1",
                        name="SQL Injection",
                        description="User input in query",
                        severity="High",
                        scanner="semgrep",
                        file="app/db.py",
                        start_line=42,
                    ),
                    SecurityFinding(
                        id="v2",
                        name="Hardcoded secret",
                        description="Secret in code",
                        severity="Low",
                        scanner="semgrep",
                        file="config.py",
                        start_line=10,
                    ),
                ],
                scan_type="sast",
                scanner_name="semgrep",
            )
        ]
    return AgentContext(
        event_type="build",
        project_id=42,
        project_path="group/project",
        pipeline=Pipeline(id=10, status="success", ref="main", sha="abc123"),
        security_reports=reports,
        job_logs={200: "Scan complete: 2 findings"} if not with_reports else {},
    )


class TestSecurityTriageAgent:
    def test_output_schema(self):
        agent = SecurityTriageAgent(
            definition=_make_definition(),
            model=MagicMock(),
            context=_make_context(),
            project_config=MagicMock(review_rules=[]),
            gitlab=MagicMock(),
        )
        assert agent._output_schema() is SecurityTriageResult

    def test_build_user_message_with_reports(self):
        agent = SecurityTriageAgent(
            definition=_make_definition(),
            model=MagicMock(),
            context=_make_context(with_reports=True),
            project_config=MagicMock(review_rules=[]),
            gitlab=MagicMock(),
        )
        msg = agent._build_user_message()
        assert "SQL Injection" in msg
        assert "app/db.py:42" in msg
        assert "sast" in msg

    def test_build_user_message_falls_back_to_logs(self):
        agent = SecurityTriageAgent(
            definition=_make_definition(),
            model=MagicMock(),
            context=_make_context(with_reports=False),
            project_config=MagicMock(review_rules=[]),
            gitlab=MagicMock(),
        )
        msg = agent._build_user_message()
        assert "Scan complete" in msg

    def test_system_prompt_includes_project_path(self):
        agent = SecurityTriageAgent(
            definition=_make_definition(),
            model=MagicMock(),
            context=_make_context(),
            project_config=MagicMock(review_rules=[]),
            gitlab=MagicMock(),
        )
        prompt = agent._render_system_prompt()
        assert "group/project" in prompt

    @pytest.mark.asyncio
    async def test_run_returns_security_triage_result(self):
        agent = SecurityTriageAgent(
            definition=_make_definition(),
            model=MagicMock(),
            context=_make_context(),
            project_config=MagicMock(review_rules=[]),
            gitlab=MagicMock(),
        )

        mock_result = SecurityTriageResult(
            summary="1 confirmed finding, 1 false positive",
            risk_level="high",
            findings=[],
            false_positive_count=1,
        )

        response = MagicMock()
        response.content = mock_result
        response.metrics = None

        with patch("forge.agents.base.Agent") as MockAgent:
            instance = MockAgent.return_value
            instance.arun = AsyncMock(return_value=response)
            result = await agent.run()

        assert result.success is True
        assert result.security_triage is not None
        assert result.security_triage.risk_level == "high"
        assert result.review is None
        assert result.pipeline_debug is None
