import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from forge.agents.base import ForgeAgent, _classify_llm_error
from forge.agents.models import AgentResult
from forge.agents.registry import AgentDefinition, TriggerSpec
from forge.config import ForgeConfig, Settings
from forge.models.agent_run import AgentRun
from forge.models.base import Base
from forge.orchestrator.orchestrator import Orchestrator, _format_error_note


class TestClassifyLLMError:
    def test_timeout_by_message(self):
        assert _classify_llm_error(Exception("Request timeout after 30s")) == "timeout"

    def test_timeout_by_type(self):
        assert _classify_llm_error(asyncio.TimeoutError()) == "timeout"

    def test_rate_limit_by_429(self):
        assert _classify_llm_error(Exception("Error 429: Too many requests")) == "rate_limit"

    def test_rate_limit_by_message(self):
        assert _classify_llm_error(Exception("Rate limit exceeded")) == "rate_limit"

    def test_generic_error(self):
        assert _classify_llm_error(Exception("Something went wrong")) == "error"

    def test_connection_error(self):
        assert _classify_llm_error(ConnectionError("refused")) == "error"


class TestFormatErrorNote:
    def test_timeout_note(self):
        result = AgentResult(success=False, error="Timed out", status_hint="timeout")
        note = _format_error_note("code-reviewer", result)
        assert "timed out" in note
        assert "skip_paths" in note

    def test_rate_limit_note(self):
        result = AgentResult(success=False, error="429", status_hint="rate_limit")
        note = _format_error_note("code-reviewer", result)
        assert "rate limit" in note
        assert "retry automatically" in note

    def test_generic_error_note(self):
        result = AgentResult(success=False, error="Connection refused")
        note = _format_error_note("code-reviewer", result)
        assert "Connection refused" in note
        assert "error" in note.lower()


class TestAgentTimeout:
    async def test_timeout_returns_status_hint(self):
        """Agent that times out returns status_hint='timeout'."""
        defn = AgentDefinition(
            name="test-agent",
            triggers=[TriggerSpec(event="merge_request", actions=["open"])],
            model_alias="code",
            system_prompt="Test",
            settings={"timeout": 1},  # 1 second timeout
            actions={},
        )

        mock_model = MagicMock()
        mock_context = MagicMock()
        mock_context.mr = None
        mock_gitlab = AsyncMock()
        mock_project_config = MagicMock()
        mock_project_config.review_rules = []

        agent = ForgeAgent(
            definition=defn,
            model=mock_model,
            context=mock_context,
            project_config=mock_project_config,
            gitlab=mock_gitlab,
        )
        # Override abstract method
        agent._build_user_message = lambda: "Review this code"

        # Mock the Agno Agent to simulate a slow LLM call
        with patch("forge.agents.base.Agent") as MockAgnoAgent:

            async def slow_run(*args, **kwargs):
                await asyncio.sleep(10)

            mock_agno = MagicMock()
            mock_agno.arun = slow_run
            MockAgnoAgent.return_value = mock_agno

            result = await agent.run()

        assert result.success is False
        assert result.status_hint == "timeout"
        assert "Timed out" in result.error


class TestRecordRunWithStatusHint:
    @pytest.fixture()
    async def session_factory(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        yield factory
        await engine.dispose()

    async def test_timeout_status_recorded(self, session_factory):
        settings = Settings(
            GITLAB_URL="https://gitlab.test",
            GITLAB_TOKEN=SecretStr("glpat-test"),
            GITLAB_WEBHOOK_SECRET=SecretStr("secret"),
            DATABASE_URL="sqlite+aiosqlite:///:memory:",
        )
        forge_config = ForgeConfig(path="nonexistent.yml")
        from forge.agents.registry import AgentRegistry

        registry = AgentRegistry("/nonexistent")
        orch = Orchestrator(settings, forge_config, session_factory, registry)

        result = AgentResult(
            success=False,
            error="Timed out after 120s",
            duration_ms=120000,
            status_hint="timeout",
        )

        await orch._record_run(
            project_id=1,
            user_id=10,
            event_type="merge_request",
            target_iid=42,
            agent_name="code-reviewer",
            model_used="code",
            result=result,
        )

        async with session_factory() as session:
            rows = (await session.execute(select(AgentRun))).scalars().all()
            assert len(rows) == 1
            assert rows[0].status == "timeout"

    async def test_rate_limit_status_recorded(self, session_factory):
        settings = Settings(
            GITLAB_URL="https://gitlab.test",
            GITLAB_TOKEN=SecretStr("glpat-test"),
            GITLAB_WEBHOOK_SECRET=SecretStr("secret"),
            DATABASE_URL="sqlite+aiosqlite:///:memory:",
        )
        forge_config = ForgeConfig(path="nonexistent.yml")
        from forge.agents.registry import AgentRegistry

        registry = AgentRegistry("/nonexistent")
        orch = Orchestrator(settings, forge_config, session_factory, registry)

        result = AgentResult(
            success=False,
            error="429 Rate limit",
            duration_ms=5000,
            status_hint="rate_limit",
        )

        await orch._record_run(
            project_id=1,
            user_id=10,
            event_type="merge_request",
            target_iid=42,
            agent_name="code-reviewer",
            model_used="code",
            result=result,
        )

        async with session_factory() as session:
            rows = (await session.execute(select(AgentRun))).scalars().all()
            assert len(rows) == 1
            assert rows[0].status == "rate_limit"
