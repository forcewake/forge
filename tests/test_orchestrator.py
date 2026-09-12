from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from forge.agents.models import AgentResult, InlineComment, ReviewResult
from forge.agents.registry import AgentDefinition, AgentRegistry, TriggerSpec
from forge.config import ForgeConfig, Settings
from forge.gitlab.events import MergeRequestEvent, MRObjectAttributes, ProjectInfo, UserInfo
from forge.models.agent_run import AgentRun
from forge.models.base import Base
from forge.models.review_state import ReviewState
from forge.orchestrator.orchestrator import Orchestrator, _format_summary_note


@pytest.fixture()
def test_settings() -> Settings:
    return Settings(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test"),
        GITLAB_WEBHOOK_SECRET=SecretStr("secret"),
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
    )


@pytest.fixture()
def forge_config() -> ForgeConfig:
    return ForgeConfig(path="nonexistent.yml")


@pytest.fixture()
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture()
def registry() -> AgentRegistry:
    reg = AgentRegistry("/nonexistent")
    # Manually inject an agent
    defn = AgentDefinition(
        name="code-reviewer",
        triggers=[TriggerSpec(event="merge_request", actions=["open", "update"])],
        model_alias="code",
        system_prompt="You are a reviewer for {project_path}.",
        settings={"skip_draft": True, "cooldown": 120},
        actions={"inline_comments": True, "summary_note": True, "labels": True},
    )
    reg._agents["code-reviewer"] = defn
    return reg


@pytest.fixture()
def mr_event() -> MergeRequestEvent:
    return MergeRequestEvent(
        object_kind="merge_request",
        project=ProjectInfo(
            id=1,
            name="test-project",
            path_with_namespace="group/test-project",
            web_url="https://gitlab.test/group/test-project",
        ),
        user=UserInfo(id=10, name="Test User", username="testuser"),
        object_attributes=MRObjectAttributes(
            id=100,
            iid=42,
            title="Fix the thing",
            action="open",
        ),
    )


class TestFormatSummaryNote:
    def test_formats_with_counts(self):
        result = AgentResult(
            success=True,
            review=ReviewResult(
                summary="Looks mostly good",
                severity="warning",
                comments=[
                    InlineComment(file="a.py", line=1, body="Fix this", severity="warning"),
                    InlineComment(file="b.py", line=2, body="Critical bug", severity="critical"),
                    InlineComment(file="c.py", line=3, body="Consider this", severity="suggestion"),
                ],
            ),
            discussions_created=["d1", "d2", "d3"],
        )
        note = _format_summary_note(result, "code-reviewer", "1.0")
        assert "Forge Code Review" in note
        assert "Looks mostly good" in note
        assert "3 inline comment(s)" in note
        assert "code-reviewer v1.0" in note

    def test_empty_review(self):
        result = AgentResult(success=True, review=None)
        note = _format_summary_note(result, "code-reviewer", "1.0")
        assert note == ""


class TestOrchestratorCooldown:
    async def test_cooldown_blocks_recent_run(
        self, test_settings, forge_config, session_factory, registry
    ):
        # Insert a recent successful run
        async with session_factory() as session:
            run = AgentRun(
                project_id=1,
                event_type="merge_request",
                target_iid=42,
                agent_name="code-reviewer",
                status="success",
                created_at=datetime.now(timezone.utc) - timedelta(seconds=30),
            )
            session.add(run)
            await session.commit()

        orchestrator = Orchestrator(test_settings, forge_config, session_factory, registry)
        is_cooling = await orchestrator._check_cooldown("code-reviewer", 1, 42, 120)
        assert is_cooling is True

    async def test_cooldown_allows_old_run(
        self, test_settings, forge_config, session_factory, registry
    ):
        async with session_factory() as session:
            run = AgentRun(
                project_id=1,
                event_type="merge_request",
                target_iid=42,
                agent_name="code-reviewer",
                status="success",
                created_at=datetime.now(timezone.utc) - timedelta(seconds=300),
            )
            session.add(run)
            await session.commit()

        orchestrator = Orchestrator(test_settings, forge_config, session_factory, registry)
        is_cooling = await orchestrator._check_cooldown("code-reviewer", 1, 42, 120)
        assert is_cooling is False

    async def test_cooldown_ignores_failed_runs(
        self, test_settings, forge_config, session_factory, registry
    ):
        async with session_factory() as session:
            run = AgentRun(
                project_id=1,
                event_type="merge_request",
                target_iid=42,
                agent_name="code-reviewer",
                status="error",
                created_at=datetime.now(timezone.utc) - timedelta(seconds=30),
            )
            session.add(run)
            await session.commit()

        orchestrator = Orchestrator(test_settings, forge_config, session_factory, registry)
        is_cooling = await orchestrator._check_cooldown("code-reviewer", 1, 42, 120)
        assert is_cooling is False


class TestOrchestratorRateLimits:
    async def test_rate_limit_not_exceeded(
        self, test_settings, forge_config, session_factory, registry
    ):
        orchestrator = Orchestrator(test_settings, forge_config, session_factory, registry)
        exceeded = await orchestrator._check_rate_limits(1)
        assert exceeded is False

    async def test_project_rate_limit_exceeded(
        self, test_settings, forge_config, session_factory, registry
    ):
        # Insert 31 runs in the last hour (limit is 30)
        async with session_factory() as session:
            for i in range(31):
                run = AgentRun(
                    project_id=1,
                    event_type="merge_request",
                    target_iid=i,
                    agent_name="code-reviewer",
                    status="success",
                    created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                )
                session.add(run)
            await session.commit()

        orchestrator = Orchestrator(test_settings, forge_config, session_factory, registry)
        exceeded = await orchestrator._check_rate_limits(1)
        assert exceeded is True


class TestOrchestratorRecordRun:
    async def test_records_successful_run(
        self, test_settings, forge_config, session_factory, registry
    ):
        orchestrator = Orchestrator(test_settings, forge_config, session_factory, registry)
        result = AgentResult(
            success=True,
            duration_ms=1500,
            input_tokens=100,
            output_tokens=50,
        )
        await orchestrator._record_run(
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
            assert rows[0].project_id == 1
            assert rows[0].user_id == 10
            assert rows[0].agent_name == "code-reviewer"
            assert rows[0].status == "success"
            assert rows[0].duration_ms == 1500

    async def test_records_failed_run(self, test_settings, forge_config, session_factory, registry):
        orchestrator = Orchestrator(test_settings, forge_config, session_factory, registry)
        result = AgentResult(
            success=False,
            error="LLM timeout",
            duration_ms=30000,
        )
        await orchestrator._record_run(
            project_id=1,
            user_id=None,
            event_type="merge_request",
            target_iid=42,
            agent_name="code-reviewer",
            model_used="code",
            result=result,
        )

        async with session_factory() as session:
            rows = (await session.execute(select(AgentRun))).scalars().all()
            assert len(rows) == 1
            assert rows[0].status == "error"
            assert rows[0].error_message == "LLM timeout"


class TestOrchestratorReviewState:
    async def test_creates_review_state(
        self, test_settings, forge_config, session_factory, registry
    ):
        orchestrator = Orchestrator(test_settings, forge_config, session_factory, registry)
        await orchestrator._update_review_state(
            project_id=1,
            mr_iid=42,
            sha="abc123",
            discussion_ids=["d1", "d2"],
        )

        async with session_factory() as session:
            rows = (await session.execute(select(ReviewState))).scalars().all()
            assert len(rows) == 1
            assert rows[0].last_reviewed_sha == "abc123"
            assert rows[0].discussion_ids == ["d1", "d2"]

    async def test_updates_existing_review_state(
        self, test_settings, forge_config, session_factory, registry
    ):
        orchestrator = Orchestrator(test_settings, forge_config, session_factory, registry)

        # First review
        await orchestrator._update_review_state(1, 42, "sha1", ["d1"])
        # Second review
        await orchestrator._update_review_state(1, 42, "sha2", ["d2"])

        async with session_factory() as session:
            rows = (await session.execute(select(ReviewState))).scalars().all()
            assert len(rows) == 1
            assert rows[0].last_reviewed_sha == "sha2"
            assert rows[0].discussion_ids == ["d1", "d2"]


class TestOrchestratorHandleEvent:
    async def test_skips_event_without_project(
        self, test_settings, forge_config, session_factory, registry
    ):
        orchestrator = Orchestrator(test_settings, forge_config, session_factory, registry)
        event = MergeRequestEvent(
            object_kind="merge_request",
            object_attributes=MRObjectAttributes(id=1, iid=1, title="Test"),
        )
        # Should not raise
        await orchestrator.handle_event(event)

    async def test_full_dispatch(
        self, test_settings, forge_config, session_factory, registry, mr_event
    ):
        """Test the full orchestrator dispatch with mocked agent execution."""
        review_result = ReviewResult(
            summary="Code looks good",
            severity="info",
            comments=[],
        )
        agent_result = AgentResult(
            success=True,
            review=review_result,
            duration_ms=500,
        )

        with (
            patch("forge.orchestrator.orchestrator.GitLabClient") as MockClient,
            patch("forge.orchestrator.orchestrator.load_project_config") as mock_load_config,
            patch("forge.orchestrator.orchestrator.get_agent_class") as mock_get_agent_class,
            patch("forge.orchestrator.orchestrator.get_model") as mock_get_model,
            patch("forge.orchestrator.orchestrator.ContextEngine") as MockCtxEngine,
        ):
            # Setup mocks
            mock_gitlab = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_gitlab)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_load_config.return_value = MagicMock(
                disabled_agents=[],
                enabled_agents=None,
                review_rules=[],
                skip_paths=[],
            )

            mock_context = MagicMock()
            mock_context.mr = MagicMock(iid=42, sha="abc123")
            mock_context.project_id = 1
            MockCtxEngine.return_value.build_mr_context = AsyncMock(return_value=mock_context)

            MockAgent = MagicMock()
            mock_agent_instance = AsyncMock()
            mock_agent_instance.run.return_value = agent_result
            MockAgent.return_value = mock_agent_instance
            mock_get_agent_class.return_value = MockAgent

            mock_get_model.return_value = MagicMock()

            orchestrator = Orchestrator(test_settings, forge_config, session_factory, registry)
            await orchestrator.handle_event(mr_event)

            # Verify agent was created and run
            MockAgent.assert_called_once()
            mock_agent_instance.run.assert_called_once()

            # Verify summary note was posted
            mock_gitlab.create_mr_note.assert_called_once()

            # Verify run was recorded
            async with session_factory() as session:
                rows = (await session.execute(select(AgentRun))).scalars().all()
                assert len(rows) == 1
                assert rows[0].status == "success"
