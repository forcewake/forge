from unittest.mock import AsyncMock, MagicMock, patch


from forge.agents.chat import ChatAgent
from forge.agents.models import ReviewResult
from forge.agents.registry import AgentDefinition
from forge.context.engine import AgentContext
from forge.gitlab.schemas import MergeRequest


def _make_definition(**overrides) -> AgentDefinition:
    defaults = dict(
        name="chat",
        type="chat",
        system_prompt="You are Forge.",
        actions={},
    )
    defaults.update(overrides)
    return AgentDefinition(**defaults)


def _make_context(**overrides) -> AgentContext:
    defaults = dict(
        event_type="note",
        project_id=1,
        project_path="org/repo",
    )
    defaults.update(overrides)
    return AgentContext(**defaults)


class TestChatAgentSchema:
    def test_output_schema_returns_none(self):
        agent = ChatAgent(
            definition=_make_definition(),
            model=MagicMock(),
            context=_make_context(),
            project_config=MagicMock(review_rules=[]),
            gitlab=MagicMock(),
        )
        assert agent._output_schema() is None

    def test_output_schema_is_not_review_result(self):
        agent = ChatAgent(
            definition=_make_definition(),
            model=MagicMock(),
            context=_make_context(),
            project_config=MagicMock(review_rules=[]),
            gitlab=MagicMock(),
        )
        assert agent._output_schema() is not ReviewResult


class TestBuildUserMessage:
    def test_includes_mention_text(self):
        agent = ChatAgent(
            definition=_make_definition(),
            model=MagicMock(),
            context=_make_context(),
            project_config=MagicMock(review_rules=[]),
            gitlab=MagicMock(),
            mention_text="explain this function",
        )
        msg = agent._build_user_message()
        assert "explain this function" in msg

    def test_falls_back_to_trigger_note(self):
        agent = ChatAgent(
            definition=_make_definition(),
            model=MagicMock(),
            context=_make_context(trigger_note="@forge what is this?"),
            project_config=MagicMock(review_rules=[]),
            gitlab=MagicMock(),
        )
        msg = agent._build_user_message()
        assert "@forge what is this?" in msg

    def test_includes_thread_history(self):
        history = [
            {"role": "user", "content": "what does X do?"},
            {"role": "assistant", "content": "X does Y."},
        ]
        agent = ChatAgent(
            definition=_make_definition(),
            model=MagicMock(),
            context=_make_context(),
            project_config=MagicMock(review_rules=[]),
            gitlab=MagicMock(),
            thread_history=history,
            mention_text="follow up question",
        )
        msg = agent._build_user_message()
        assert "what does X do?" in msg
        assert "X does Y." in msg
        assert "follow up question" in msg

    def test_includes_diff_when_present(self):
        agent = ChatAgent(
            definition=_make_definition(),
            model=MagicMock(),
            context=_make_context(raw_diff="--- a/file.py\n+++ b/file.py\n+new line"),
            project_config=MagicMock(review_rules=[]),
            gitlab=MagicMock(),
            mention_text="explain this change",
        )
        msg = agent._build_user_message()
        assert "```diff" in msg
        assert "+new line" in msg


class TestRenderSystemPrompt:
    def test_includes_project_path(self):
        agent = ChatAgent(
            definition=_make_definition(),
            model=MagicMock(),
            context=_make_context(project_path="org/my-repo"),
            project_config=MagicMock(review_rules=[]),
            gitlab=MagicMock(),
        )
        prompt = agent._render_system_prompt()
        assert "org/my-repo" in prompt

    def test_includes_mr_context_when_present(self):
        mr = MergeRequest(
            id=1,
            iid=42,
            title="Fix login bug",
            state="opened",
            source_branch="fix/login",
            target_branch="main",
        )
        agent = ChatAgent(
            definition=_make_definition(),
            model=MagicMock(),
            context=_make_context(
                mr=mr,
                mr_source_branch="fix/login",
                mr_target_branch="main",
                mr_description="Fixes the login timeout issue",
            ),
            project_config=MagicMock(review_rules=[]),
            gitlab=MagicMock(),
        )
        prompt = agent._render_system_prompt()
        assert "Fix login bug" in prompt
        assert "fix/login" in prompt
        assert "Fixes the login timeout issue" in prompt

    def test_includes_issue_context_when_present(self):
        agent = ChatAgent(
            definition=_make_definition(),
            model=MagicMock(),
            context=_make_context(
                issue_title="Login timeout",
                issue_description="Users report timeout after 30s",
            ),
            project_config=MagicMock(review_rules=[]),
            gitlab=MagicMock(),
        )
        prompt = agent._render_system_prompt()
        assert "Login timeout" in prompt
        assert "Users report timeout" in prompt


class TestChatAgentRun:
    async def test_run_returns_text_response(self):
        agent = ChatAgent(
            definition=_make_definition(),
            model=MagicMock(),
            context=_make_context(),
            project_config=MagicMock(review_rules=[]),
            gitlab=MagicMock(),
            mention_text="explain this",
        )
        response = MagicMock()
        response.content = "This function calculates the sum of two numbers."
        response.metrics = None

        with patch("forge.agents.base.Agent") as MockAgent:
            instance = MockAgent.return_value
            instance.arun = AsyncMock(return_value=response)
            result = await agent.run()

        assert result.success is True
        assert result.text_response == "This function calculates the sum of two numbers."
        assert result.review is None
