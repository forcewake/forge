"""Tests for slash command routing and @mention handling in the orchestrator."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from forge.agents.models import AgentResult
from forge.agents.registry import AgentDefinition, AgentRegistry, TriggerSpec
from forge.config import ForgeConfig, Settings
from forge.gitlab.events import (
    NoteEvent,
    NoteObjectAttributes,
    NoteMRInfo,
    ProjectInfo,
    UserInfo,
)
from forge.models.base import Base
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
    """Registry with code-reviewer and chat agents."""
    reg = AgentRegistry("/nonexistent")

    code_reviewer = AgentDefinition(
        name="code-reviewer",
        type="code-reviewer",
        triggers=[TriggerSpec(event="merge_request", actions=["open", "update"])],
        model_alias="code",
        system_prompt="You are a reviewer.",
        settings={"skip_draft": True, "cooldown": 120},
        actions={"inline_comments": True, "summary_note": True},
    )
    reg._agents["code-reviewer"] = code_reviewer

    chat = AgentDefinition(
        name="chat",
        type="chat",
        triggers=[TriggerSpec(event="note", mention=True)],
        model_alias="default",
        system_prompt="You are Forge.",
        settings={"cooldown": 10},
        actions={},
    )
    reg._agents["chat"] = chat

    return reg


def _make_note_event(note_text: str, **overrides) -> NoteEvent:
    defaults = dict(
        object_kind="note",
        project=ProjectInfo(
            id=1,
            name="test-project",
            path_with_namespace="group/test-project",
            web_url="https://gitlab.test/group/test-project",
        ),
        user=UserInfo(id=10, name="Dev User", username="devuser"),
        object_attributes=NoteObjectAttributes(
            id=500,
            note=note_text,
            noteable_type="MergeRequest",
            discussion_id="disc-abc123",
        ),
        merge_request=NoteMRInfo(id=100, iid=42, title="Fix bug"),
    )
    defaults.update(overrides)
    return NoteEvent(**defaults)


class TestSlashCommandRouting:
    """Test that slash commands route to the correct agents."""

    async def test_help_returns_static_response(
        self, test_settings, forge_config, session_factory, registry
    ):
        orch = Orchestrator(test_settings, forge_config, session_factory, registry)

        event = _make_note_event("@forge /help")

        with patch("forge.orchestrator.orchestrator.GitLabClient") as MockClient:
            mock_gitlab = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_gitlab)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_gitlab.get_file = AsyncMock(side_effect=Exception("not found"))

            await orch.handle_event(event)

            # Should have posted the help text as a reply
            mock_gitlab.reply_to_discussion.assert_called_once()
            call_args = mock_gitlab.reply_to_discussion.call_args
            assert "Forge Help" in call_args[0][3]
            assert "/review" in call_args[0][3]

    async def test_review_routes_to_code_reviewer(
        self, test_settings, forge_config, session_factory, registry
    ):
        orch = Orchestrator(test_settings, forge_config, session_factory, registry)

        event = _make_note_event("@forge /review please check security")

        with (
            patch("forge.orchestrator.orchestrator.GitLabClient") as MockClient,
            patch.object(orch, "_run_agent", new_callable=AsyncMock) as mock_run,
        ):
            mock_gitlab = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_gitlab)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_gitlab.get_file = AsyncMock(side_effect=Exception("not found"))

            await orch.handle_event(event)

            # Should have called _run_agent with the code-reviewer definition
            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args[1]
            assert call_kwargs["definition"].name == "code-reviewer"

    async def test_unknown_command_posts_error(
        self, test_settings, forge_config, session_factory, registry
    ):
        # /unknown is not in KNOWN_COMMANDS, so extract_mention won't
        # detect it as a slash command — it'll be treated as plain mention text.
        # Let's test with a registered but unmapped command scenario.
        # Actually, /unknown won't be detected by extract_mention as a slash command
        # because it's not in KNOWN_COMMANDS. So the user message will be
        # "/unknown do something" and it goes to the chat agent.
        # This is the expected behavior per the plan.
        pass  # See test_unknown_slash_treated_as_mention below

    async def test_unknown_slash_treated_as_mention(
        self, test_settings, forge_config, session_factory, registry
    ):
        """Unknown slash commands are treated as regular mention text."""
        orch = Orchestrator(test_settings, forge_config, session_factory, registry)

        event = _make_note_event("@forge /potato do something")

        with (
            patch("forge.orchestrator.orchestrator.GitLabClient") as MockClient,
            patch("forge.orchestrator.orchestrator.ChatAgent") as MockChatAgent,
            patch("forge.orchestrator.orchestrator.ContextEngine") as MockCtxEngine,
            patch("forge.orchestrator.orchestrator.get_model") as mock_get_model,
        ):
            mock_gitlab = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_gitlab)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_gitlab.get_file = AsyncMock(side_effect=Exception("not found"))

            # Mock context engine
            mock_ctx = MagicMock()
            mock_ctx_instance = AsyncMock()
            mock_ctx_instance.build_note_context = AsyncMock(return_value=mock_ctx)
            MockCtxEngine.return_value = mock_ctx_instance

            # Mock the chat agent
            mock_agent_instance = AsyncMock()
            mock_agent_instance.run = AsyncMock(
                return_value=AgentResult(success=True, text_response="Here's my answer")
            )
            MockChatAgent.return_value = mock_agent_instance
            mock_get_model.return_value = MagicMock()

            await orch.handle_event(event)

            # Should have instantiated ChatAgent (not routed to code-reviewer)
            MockChatAgent.assert_called_once()
            # The mention_text should contain "/potato do something"
            call_kwargs = MockChatAgent.call_args[1]
            assert "/potato do something" in call_kwargs["mention_text"]

    async def test_no_mention_falls_through(
        self, test_settings, forge_config, session_factory, registry
    ):
        """NoteEvent without @forge mention should fall through to generic matcher."""
        orch = Orchestrator(test_settings, forge_config, session_factory, registry)

        event = _make_note_event("Just a regular comment, no mention")

        with (
            patch("forge.orchestrator.orchestrator.GitLabClient") as MockClient,
            patch("forge.orchestrator.orchestrator.match_agents", return_value=[]) as mock_match,
        ):
            mock_gitlab = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_gitlab)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_gitlab.get_file = AsyncMock(side_effect=Exception("not found"))

            await orch.handle_event(event)

            # Should have fallen through to match_agents
            mock_match.assert_called_once()


class TestChatMentionFlow:
    """Test the chat agent flow for plain @mention (no slash command)."""

    async def test_mention_triggers_chat_agent(
        self, test_settings, forge_config, session_factory, registry
    ):
        orch = Orchestrator(test_settings, forge_config, session_factory, registry)

        event = _make_note_event("@forge explain this function")

        with (
            patch("forge.orchestrator.orchestrator.GitLabClient") as MockClient,
            patch("forge.orchestrator.orchestrator.ChatAgent") as MockChatAgent,
            patch("forge.orchestrator.orchestrator.ContextEngine") as MockCtxEngine,
            patch("forge.orchestrator.orchestrator.get_model") as mock_get_model,
        ):
            mock_gitlab = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_gitlab)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_gitlab.get_file = AsyncMock(side_effect=Exception("not found"))

            # Mock context engine
            mock_ctx = MagicMock()
            mock_ctx_instance = AsyncMock()
            mock_ctx_instance.build_note_context = AsyncMock(return_value=mock_ctx)
            MockCtxEngine.return_value = mock_ctx_instance

            # Mock chat agent
            mock_agent_instance = AsyncMock()
            mock_agent_instance.run = AsyncMock(
                return_value=AgentResult(
                    success=True,
                    text_response="This function does XYZ.",
                )
            )
            MockChatAgent.return_value = mock_agent_instance
            mock_get_model.return_value = MagicMock()

            await orch.handle_event(event)

            # Chat agent was instantiated with correct mention_text
            MockChatAgent.assert_called_once()
            call_kwargs = MockChatAgent.call_args[1]
            assert call_kwargs["mention_text"] == "explain this function"

            # Reply was posted
            mock_gitlab.reply_to_discussion.assert_called_once()
            posted_body = mock_gitlab.reply_to_discussion.call_args[0][3]
            assert "This function does XYZ." in posted_body

    async def test_chat_saves_conversation(
        self, test_settings, forge_config, session_factory, registry
    ):
        orch = Orchestrator(test_settings, forge_config, session_factory, registry)

        event = _make_note_event("@forge what is this?")

        with (
            patch("forge.orchestrator.orchestrator.GitLabClient") as MockClient,
            patch("forge.orchestrator.orchestrator.ChatAgent") as MockChatAgent,
            patch("forge.orchestrator.orchestrator.ContextEngine") as MockCtxEngine,
            patch("forge.orchestrator.orchestrator.get_model") as mock_get_model,
        ):
            mock_gitlab = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_gitlab)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_gitlab.get_file = AsyncMock(side_effect=Exception("not found"))

            mock_ctx = MagicMock()
            mock_ctx_instance = AsyncMock()
            mock_ctx_instance.build_note_context = AsyncMock(return_value=mock_ctx)
            MockCtxEngine.return_value = mock_ctx_instance

            mock_agent_instance = AsyncMock()
            mock_agent_instance.run = AsyncMock(
                return_value=AgentResult(
                    success=True,
                    text_response="It's a widget.",
                )
            )
            MockChatAgent.return_value = mock_agent_instance
            mock_get_model.return_value = MagicMock()

            await orch.handle_event(event)

            # Verify conversation was saved
            from forge.stores.conversation import ConversationStore

            store = ConversationStore(session_factory)
            history = await store.get_history(1, "MergeRequest", 42, "disc-abc123")
            assert len(history) == 2
            assert history[0]["role"] == "user"
            assert history[0]["content"] == "what is this?"
            assert history[1]["role"] == "assistant"
            assert history[1]["content"] == "It's a widget."
