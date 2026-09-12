from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from forge.agents.models import AgentResult, ReviewResult
from forge.agents.registry import AgentDefinition, AgentRegistry, TriggerSpec
from forge.config import ForgeConfig, Settings
from forge.gitlab.events import MergeRequestEvent, MRObjectAttributes, ProjectInfo, UserInfo
from forge.gitlab.schemas import Discussion, Note, NotePosition
from forge.models.base import Base
from forge.models.review_state import ReviewState
from forge.orchestrator.orchestrator import Orchestrator


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


def _make_orchestrator(test_settings, forge_config, session_factory, registry):
    return Orchestrator(test_settings, forge_config, session_factory, registry)


class TestGetReviewState:
    async def test_returns_none_when_no_state(
        self, test_settings, forge_config, session_factory, registry
    ):
        orch = _make_orchestrator(test_settings, forge_config, session_factory, registry)
        state = await orch._get_review_state(1, 42)
        assert state is None

    async def test_returns_existing_state(
        self, test_settings, forge_config, session_factory, registry
    ):
        async with session_factory() as session:
            session.add(
                ReviewState(
                    project_id=1,
                    mr_iid=42,
                    last_reviewed_sha="abc123",
                    discussion_ids=["d1", "d2"],
                )
            )
            await session.commit()

        orch = _make_orchestrator(test_settings, forge_config, session_factory, registry)
        state = await orch._get_review_state(1, 42)
        assert state is not None
        assert state.last_reviewed_sha == "abc123"
        assert state.discussion_ids == ["d1", "d2"]


class TestResolveAddressedThreads:
    async def test_resolves_thread_on_changed_file(
        self, test_settings, forge_config, session_factory, registry
    ):
        review_state = ReviewState(
            project_id=1,
            mr_iid=42,
            last_reviewed_sha="old_sha",
            discussion_ids=["disc-1"],
        )

        mock_gitlab = AsyncMock()
        mock_gitlab.list_discussions.return_value = [
            Discussion(
                id="disc-1",
                notes=[
                    Note(
                        id=1,
                        body="Issue here",
                        resolved=False,
                        resolvable=True,
                        position=NotePosition(
                            new_path="src/app.py",
                            old_path="src/app.py",
                            new_line=10,
                        ),
                    )
                ],
            )
        ]

        orch = _make_orchestrator(test_settings, forge_config, session_factory, registry)
        resolved = await orch._resolve_addressed_threads(
            mock_gitlab,
            1,
            42,
            review_state,
            inter_diff_paths={"src/app.py"},
        )

        assert resolved == ["disc-1"]
        mock_gitlab.reply_to_discussion.assert_called_once()
        mock_gitlab.resolve_discussion.assert_called_once_with(1, 42, "disc-1", resolved=True)

    async def test_skips_thread_on_unchanged_file(
        self, test_settings, forge_config, session_factory, registry
    ):
        review_state = ReviewState(
            project_id=1,
            mr_iid=42,
            last_reviewed_sha="old_sha",
            discussion_ids=["disc-1"],
        )

        mock_gitlab = AsyncMock()
        mock_gitlab.list_discussions.return_value = [
            Discussion(
                id="disc-1",
                notes=[
                    Note(
                        id=1,
                        body="Issue here",
                        resolved=False,
                        resolvable=True,
                        position=NotePosition(
                            new_path="src/other.py",
                            new_line=5,
                        ),
                    )
                ],
            )
        ]

        orch = _make_orchestrator(test_settings, forge_config, session_factory, registry)
        resolved = await orch._resolve_addressed_threads(
            mock_gitlab,
            1,
            42,
            review_state,
            inter_diff_paths={"src/app.py"},  # different file
        )

        assert resolved == []
        mock_gitlab.reply_to_discussion.assert_not_called()
        mock_gitlab.resolve_discussion.assert_not_called()

    async def test_skips_already_resolved_thread(
        self, test_settings, forge_config, session_factory, registry
    ):
        review_state = ReviewState(
            project_id=1,
            mr_iid=42,
            last_reviewed_sha="old_sha",
            discussion_ids=["disc-1"],
        )

        mock_gitlab = AsyncMock()
        mock_gitlab.list_discussions.return_value = [
            Discussion(
                id="disc-1",
                notes=[
                    Note(
                        id=1,
                        body="Issue here",
                        resolved=True,
                        resolvable=True,
                        position=NotePosition(new_path="src/app.py", new_line=10),
                    )
                ],
            )
        ]

        orch = _make_orchestrator(test_settings, forge_config, session_factory, registry)
        resolved = await orch._resolve_addressed_threads(
            mock_gitlab,
            1,
            42,
            review_state,
            inter_diff_paths={"src/app.py"},
        )

        assert resolved == []

    async def test_handles_empty_discussion_ids(
        self, test_settings, forge_config, session_factory, registry
    ):
        review_state = ReviewState(
            project_id=1,
            mr_iid=42,
            last_reviewed_sha="old_sha",
            discussion_ids=[],
        )

        mock_gitlab = AsyncMock()
        orch = _make_orchestrator(test_settings, forge_config, session_factory, registry)
        resolved = await orch._resolve_addressed_threads(
            mock_gitlab,
            1,
            42,
            review_state,
            inter_diff_paths={"src/app.py"},
        )

        assert resolved == []
        mock_gitlab.list_discussions.assert_not_called()


class TestIncrementalReviewFlow:
    async def test_first_review_runs_full(
        self, test_settings, forge_config, session_factory, registry, mr_event
    ):
        """No ReviewState → full review runs, state saved."""
        agent_result = AgentResult(
            success=True,
            review=ReviewResult(summary="OK", severity="info"),
            duration_ms=500,
            discussions_created=["d1"],
        )

        with (
            patch("forge.orchestrator.orchestrator.GitLabClient") as MockClient,
            patch("forge.orchestrator.orchestrator.load_project_config") as mock_load_config,
            patch("forge.orchestrator.orchestrator.get_agent_class") as mock_get_agent_class,
            patch("forge.orchestrator.orchestrator.get_model"),
            patch("forge.orchestrator.orchestrator.ContextEngine") as MockCtxEngine,
        ):
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
            mock_context.mr = MagicMock(iid=42, sha="new_sha_1")
            mock_context.project_id = 1
            mock_context.metadata = {}
            MockCtxEngine.return_value.build_mr_context = AsyncMock(return_value=mock_context)

            MockAgent = MagicMock()
            mock_agent_instance = AsyncMock()
            mock_agent_instance.run.return_value = agent_result
            MockAgent.return_value = mock_agent_instance
            mock_get_agent_class.return_value = MockAgent

            orch = _make_orchestrator(test_settings, forge_config, session_factory, registry)
            await orch.handle_event(mr_event)

            # Agent ran
            mock_agent_instance.run.assert_called_once()

            # Review state saved
            async with session_factory() as session:
                state = (await session.execute(select(ReviewState))).scalar_one_or_none()
                assert state is not None
                assert state.last_reviewed_sha == "new_sha_1"
                assert "d1" in state.discussion_ids

    async def test_same_sha_skipped(
        self, test_settings, forge_config, session_factory, registry, mr_event
    ):
        """Same SHA already reviewed → agent does not run."""
        # Pre-seed review state
        async with session_factory() as session:
            session.add(
                ReviewState(
                    project_id=1,
                    mr_iid=42,
                    last_reviewed_sha="same_sha",
                    discussion_ids=["d1"],
                )
            )
            await session.commit()

        with (
            patch("forge.orchestrator.orchestrator.GitLabClient") as MockClient,
            patch("forge.orchestrator.orchestrator.load_project_config") as mock_load_config,
            patch("forge.orchestrator.orchestrator.get_agent_class") as mock_get_agent_class,
            patch("forge.orchestrator.orchestrator.get_model"),
            patch("forge.orchestrator.orchestrator.ContextEngine") as MockCtxEngine,
        ):
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
            mock_context.mr = MagicMock(iid=42, sha="same_sha")
            mock_context.project_id = 1
            mock_context.metadata = {}
            MockCtxEngine.return_value.build_mr_context = AsyncMock(return_value=mock_context)

            MockAgent = MagicMock()
            mock_agent_instance = AsyncMock()
            MockAgent.return_value = mock_agent_instance
            mock_get_agent_class.return_value = MockAgent

            orch = _make_orchestrator(test_settings, forge_config, session_factory, registry)
            await orch.handle_event(mr_event)

            # Agent should NOT have run
            mock_agent_instance.run.assert_not_called()

    async def test_incremental_review_builds_inter_diff(
        self, test_settings, forge_config, session_factory, registry, mr_event
    ):
        """Different SHA → incremental context built, agent gets inter-diff."""
        async with session_factory() as session:
            session.add(
                ReviewState(
                    project_id=1,
                    mr_iid=42,
                    last_reviewed_sha="old_sha",
                    discussion_ids=["d1"],
                )
            )
            await session.commit()

        agent_result = AgentResult(
            success=True,
            review=ReviewResult(summary="New changes OK", severity="info"),
            duration_ms=300,
            discussions_created=["d2"],
        )

        with (
            patch("forge.orchestrator.orchestrator.GitLabClient") as MockClient,
            patch("forge.orchestrator.orchestrator.load_project_config") as mock_load_config,
            patch("forge.orchestrator.orchestrator.get_agent_class") as mock_get_agent_class,
            patch("forge.orchestrator.orchestrator.get_model"),
            patch("forge.orchestrator.orchestrator.ContextEngine") as MockCtxEngine,
        ):
            mock_gitlab = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_gitlab)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_load_config.return_value = MagicMock(
                disabled_agents=[],
                enabled_agents=None,
                review_rules=[],
                skip_paths=[],
            )

            # Full context from build_mr_context
            mock_context = MagicMock()
            mock_context.mr = MagicMock(iid=42, sha="new_sha_2")
            mock_context.project_id = 1
            mock_context.metadata = {}
            MockCtxEngine.return_value.build_mr_context = AsyncMock(return_value=mock_context)

            # Incremental context from build_incremental_mr_context
            mock_incremental_ctx = MagicMock()
            mock_incremental_ctx.mr = mock_context.mr
            mock_incremental_ctx.project_id = 1
            mock_incremental_ctx.metadata = {"incremental": True}
            MockCtxEngine.return_value.build_incremental_mr_context = AsyncMock(
                return_value=(mock_incremental_ctx, {"src/changed.py"})
            )

            # Mock discussion listing for thread resolution
            mock_gitlab.list_discussions.return_value = [
                Discussion(
                    id="d1",
                    notes=[
                        Note(
                            id=1,
                            body="Old issue",
                            resolved=False,
                            position=NotePosition(new_path="src/changed.py", new_line=5),
                        )
                    ],
                )
            ]

            MockAgent = MagicMock()
            mock_agent_instance = AsyncMock()
            mock_agent_instance.run.return_value = agent_result
            MockAgent.return_value = mock_agent_instance
            mock_get_agent_class.return_value = MockAgent

            orch = _make_orchestrator(test_settings, forge_config, session_factory, registry)
            await orch.handle_event(mr_event)

            # Agent ran with incremental context
            mock_agent_instance.run.assert_called_once()

            # Incremental context was built
            MockCtxEngine.return_value.build_incremental_mr_context.assert_called_once_with(
                mock_context, "old_sha", "new_sha_2"
            )

            # Thread d1 was resolved (file was in inter-diff)
            mock_gitlab.reply_to_discussion.assert_called_once()
            mock_gitlab.resolve_discussion.assert_called_once()

    async def test_incremental_fallback_on_error(
        self, test_settings, forge_config, session_factory, registry, mr_event
    ):
        """If incremental context build fails, falls back to full review."""
        async with session_factory() as session:
            session.add(
                ReviewState(
                    project_id=1,
                    mr_iid=42,
                    last_reviewed_sha="old_sha",
                    discussion_ids=[],
                )
            )
            await session.commit()

        agent_result = AgentResult(
            success=True,
            review=ReviewResult(summary="Full review", severity="info"),
            duration_ms=400,
        )

        with (
            patch("forge.orchestrator.orchestrator.GitLabClient") as MockClient,
            patch("forge.orchestrator.orchestrator.load_project_config") as mock_load_config,
            patch("forge.orchestrator.orchestrator.get_agent_class") as mock_get_agent_class,
            patch("forge.orchestrator.orchestrator.get_model"),
            patch("forge.orchestrator.orchestrator.ContextEngine") as MockCtxEngine,
        ):
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
            mock_context.mr = MagicMock(iid=42, sha="new_sha_3")
            mock_context.project_id = 1
            mock_context.metadata = {}
            MockCtxEngine.return_value.build_mr_context = AsyncMock(return_value=mock_context)

            # Incremental build fails
            MockCtxEngine.return_value.build_incremental_mr_context = AsyncMock(
                side_effect=Exception("API error")
            )

            MockAgent = MagicMock()
            mock_agent_instance = AsyncMock()
            mock_agent_instance.run.return_value = agent_result
            MockAgent.return_value = mock_agent_instance
            mock_get_agent_class.return_value = MockAgent

            orch = _make_orchestrator(test_settings, forge_config, session_factory, registry)
            await orch.handle_event(mr_event)

            # Agent still ran (fallback to full review)
            mock_agent_instance.run.assert_called_once()
