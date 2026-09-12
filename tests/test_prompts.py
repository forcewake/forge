from forge.context.engine import AgentContext
from forge.gitlab.schemas import MergeRequest, Pipeline, Job
from forge.llm.prompts import (
    format_chat_prompt,
    format_pipeline_prompt,
    format_review_prompt,
    format_security_prompt,
)


def _make_mr_context(**overrides) -> AgentContext:
    defaults = dict(
        event_type="merge_request",
        project_id=42,
        project_path="group/project",
        mr=MergeRequest(
            id=100,
            iid=1,
            title="Add feature X",
            description="Implements feature X",
            state="opened",
            source_branch="feature-x",
            target_branch="main",
        ),
        mr_description="Implements feature X",
        mr_source_branch="feature-x",
        mr_target_branch="main",
        raw_diff="--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new",
    )
    defaults.update(overrides)
    return AgentContext(**defaults)


class TestFormatReviewPrompt:
    def test_returns_system_and_user_messages(self):
        ctx = _make_mr_context()
        messages = format_review_prompt(ctx)
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    def test_system_contains_reviewer_instructions(self):
        ctx = _make_mr_context()
        messages = format_review_prompt(ctx)
        assert "code reviewer" in messages[0]["content"].lower()

    def test_user_contains_diff(self):
        ctx = _make_mr_context()
        messages = format_review_prompt(ctx)
        assert "```diff" in messages[1]["content"]
        assert "+new" in messages[1]["content"]

    def test_user_contains_mr_title(self):
        ctx = _make_mr_context()
        messages = format_review_prompt(ctx)
        assert "Add feature X" in messages[1]["content"]

    def test_custom_rules_included(self):
        ctx = _make_mr_context()
        messages = format_review_prompt(ctx, rules=["No print statements"])
        assert "No print statements" in messages[0]["content"]

    def test_description_included(self):
        ctx = _make_mr_context()
        messages = format_review_prompt(ctx)
        assert "Implements feature X" in messages[1]["content"]


class TestFormatChatPrompt:
    def test_returns_system_and_user(self):
        ctx = _make_mr_context(trigger_note="@forge what does this do?")
        messages = format_chat_prompt(ctx)
        assert messages[0]["role"] == "system"
        assert messages[-1]["role"] == "user"

    def test_trigger_note_in_user_message(self):
        ctx = _make_mr_context(trigger_note="@forge explain the change")
        messages = format_chat_prompt(ctx)
        assert "@forge explain the change" in messages[-1]["content"]

    def test_thread_history_included(self):
        ctx = _make_mr_context(trigger_note="follow-up question")
        history = [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
        ]
        messages = format_chat_prompt(ctx, thread_history=history)
        assert len(messages) == 4  # system + 2 history + user
        assert messages[1]["content"] == "first question"

    def test_explicit_user_message_overrides_trigger(self):
        ctx = _make_mr_context(trigger_note="original note")
        messages = format_chat_prompt(ctx, user_message="custom question")
        assert "custom question" in messages[-1]["content"]


class TestFormatPipelinePrompt:
    def test_returns_system_and_user(self):
        ctx = AgentContext(
            event_type="pipeline",
            project_id=42,
            project_path="group/project",
            pipeline=Pipeline(id=10, status="failed", ref="main", sha="abc123"),
            failed_jobs=[
                Job(
                    id=101,
                    name="test",
                    stage="test",
                    status="failed",
                    failure_reason="script_failure",
                ),
            ],
            job_logs={101: "FAILED: test_foo.py\nAssertionError"},
        )
        messages = format_pipeline_prompt(ctx)
        assert len(messages) == 2
        assert messages[0]["role"] == "system"

    def test_failed_job_logs_in_user_message(self):
        ctx = AgentContext(
            event_type="pipeline",
            project_id=42,
            pipeline=Pipeline(id=10, status="failed"),
            failed_jobs=[
                Job(id=101, name="unit-tests", stage="test", status="failed"),
            ],
            job_logs={101: "AssertionError: expected 1"},
        )
        messages = format_pipeline_prompt(ctx)
        assert "unit-tests" in messages[1]["content"]
        assert "AssertionError" in messages[1]["content"]


class TestFormatSecurityPrompt:
    def test_returns_system_and_user(self):
        ctx = _make_mr_context()
        messages = format_security_prompt(ctx)
        assert len(messages) == 2

    def test_system_mentions_owasp(self):
        ctx = _make_mr_context()
        messages = format_security_prompt(ctx)
        assert "OWASP" in messages[0]["content"]

    def test_diff_in_user_message(self):
        ctx = _make_mr_context()
        messages = format_security_prompt(ctx)
        assert "```diff" in messages[1]["content"]
