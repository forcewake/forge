import pytest

from forge.config import ForgeConfig
from forge.context.engine import ContextEngine
from forge.gitlab.client import GitLabClient
from forge.gitlab.events import (
    MergeRequestEvent,
    MRObjectAttributes,
    NoteEvent,
    NoteObjectAttributes,
    NoteMRInfo,
    PipelineEvent,
    PipelineObjectAttributes,
    ProjectInfo,
    UserInfo,
)


@pytest.fixture
def gitlab_url():
    return "https://gitlab.example.com"


@pytest.fixture
def forge_config(tmp_path):
    """ForgeConfig with defaults (no forge.yml file)."""
    return ForgeConfig(path=tmp_path / "nonexistent.yml")


@pytest.fixture
def client(gitlab_url):
    return GitLabClient(base_url=gitlab_url, token="test-token")


@pytest.fixture
def engine(client, forge_config):
    return ContextEngine(client, forge_config)


def _make_project():
    return ProjectInfo(
        id=42,
        name="test-project",
        path_with_namespace="group/test-project",
        web_url="https://gitlab.example.com/group/test-project",
    )


def _make_user():
    return UserInfo(id=1, name="Test User", username="testuser")


def _make_mr_event(action="open"):
    return MergeRequestEvent(
        object_kind="merge_request",
        user=_make_user(),
        project=_make_project(),
        object_attributes=MRObjectAttributes(
            id=100,
            iid=1,
            title="Add feature X",
            description="This MR adds feature X",
            state="opened",
            action=action,
            source_branch="feature-x",
            target_branch="main",
        ),
    )


MR_JSON = {
    "id": 100,
    "iid": 1,
    "title": "Add feature X",
    "description": "This MR adds feature X",
    "state": "opened",
    "source_branch": "feature-x",
    "target_branch": "main",
    "web_url": "https://gitlab.example.com/group/test-project/-/merge_requests/1",
}

RAW_DIFF = (
    "diff --git a/foo.py b/foo.py\n"
    "index 1234567..abcdefg 100644\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -1,3 +1,4 @@\n"
    " import os\n"
    "+import sys\n"
    " \n"
    " def main():\n"
)


@pytest.mark.asyncio
async def test_build_mr_context(httpx_mock, engine):
    httpx_mock.add_response(
        url="https://gitlab.example.com/api/v4/projects/42/merge_requests/1",
        json=MR_JSON,
    )
    httpx_mock.add_response(
        url="https://gitlab.example.com/api/v4/projects/42/merge_requests/1/diffs",
        text=RAW_DIFF,
    )
    httpx_mock.add_response(
        url="https://gitlab.example.com/api/v4/projects/42/merge_requests/1/discussions?per_page=100",
        json=[],
    )

    event = _make_mr_event()
    ctx = await engine.build_mr_context(event)

    assert ctx.event_type == "merge_request"
    assert ctx.project_id == 42
    assert ctx.project_path == "group/test-project"
    assert ctx.mr is not None
    assert ctx.mr.title == "Add feature X"
    assert ctx.mr_source_branch == "feature-x"
    assert ctx.mr_target_branch == "main"
    assert "import sys" in ctx.raw_diff
    assert len(ctx.parsed_diff) == 1
    assert ctx.total_tokens_used > 0


@pytest.mark.asyncio
async def test_build_mr_context_redacts_secrets(httpx_mock, engine):
    diff_with_secret = (
        "diff --git a/config.py b/config.py\n"
        "--- a/config.py\n"
        "+++ b/config.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-API_KEY = old\n"
        "+API_KEY = glpat-xxxxxxxxxxxxxxxxxxxx\n"
    )
    httpx_mock.add_response(
        url="https://gitlab.example.com/api/v4/projects/42/merge_requests/1",
        json=MR_JSON,
    )
    httpx_mock.add_response(
        url="https://gitlab.example.com/api/v4/projects/42/merge_requests/1/diffs",
        text=diff_with_secret,
    )
    httpx_mock.add_response(
        url="https://gitlab.example.com/api/v4/projects/42/merge_requests/1/discussions?per_page=100",
        json=[],
    )

    event = _make_mr_event()
    ctx = await engine.build_mr_context(event)
    assert "glpat-" not in ctx.raw_diff
    assert "REDACTED" in ctx.raw_diff


@pytest.mark.asyncio
async def test_build_mr_context_respects_budget(httpx_mock, engine):
    # Create a very large diff
    large_diff = "diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n@@ -1,1 +1,5000 @@\n"
    large_diff += "\n".join(f"+line number {i} with some content padding" for i in range(5000))

    httpx_mock.add_response(
        url="https://gitlab.example.com/api/v4/projects/42/merge_requests/1",
        json=MR_JSON,
    )
    httpx_mock.add_response(
        url="https://gitlab.example.com/api/v4/projects/42/merge_requests/1/diffs",
        text=large_diff,
    )
    httpx_mock.add_response(
        url="https://gitlab.example.com/api/v4/projects/42/merge_requests/1/discussions?per_page=100",
        json=[],
    )

    event = _make_mr_event()
    ctx = await engine.build_mr_context(event)
    # The diff should have been truncated — total tokens should not exceed budget
    assert ctx.total_tokens_used <= 24_000


@pytest.mark.asyncio
async def test_build_note_context_mr(httpx_mock, engine):
    httpx_mock.add_response(
        url="https://gitlab.example.com/api/v4/projects/42/merge_requests/1",
        json=MR_JSON,
    )
    httpx_mock.add_response(
        url="https://gitlab.example.com/api/v4/projects/42/merge_requests/1/diffs",
        text=RAW_DIFF,
    )
    httpx_mock.add_response(
        url="https://gitlab.example.com/api/v4/projects/42/merge_requests/1/discussions?per_page=100",
        json=[],
    )

    event = NoteEvent(
        object_kind="note",
        user=_make_user(),
        project=_make_project(),
        object_attributes=NoteObjectAttributes(
            id=200,
            note="@forge please review this",
            noteable_type="MergeRequest",
            discussion_id="disc-abc",
        ),
        merge_request=NoteMRInfo(iid=1, title="Add feature X"),
    )

    ctx = await engine.build_note_context(event)
    assert ctx.event_type == "note"
    assert ctx.trigger_note == "@forge please review this"
    assert ctx.discussion_id == "disc-abc"
    assert ctx.mr is not None


@pytest.mark.asyncio
async def test_build_note_context_issue(engine):
    """Note on an issue — no diff or MR context."""
    from forge.gitlab.events import NoteIssueInfo

    event = NoteEvent(
        object_kind="note",
        user=_make_user(),
        project=_make_project(),
        object_attributes=NoteObjectAttributes(
            id=201,
            note="@forge what is this about?",
            noteable_type="Issue",
        ),
        issue=NoteIssueInfo(iid=5, title="Bug report"),
    )

    ctx = await engine.build_note_context(event)
    assert ctx.event_type == "note"
    assert ctx.issue_title == "Bug report"
    assert ctx.mr is None
    assert ctx.raw_diff == ""


@pytest.mark.asyncio
async def test_build_pipeline_context(httpx_mock, engine):
    httpx_mock.add_response(
        url="https://gitlab.example.com/api/v4/projects/42/pipelines/10",
        json={"id": 10, "status": "failed", "ref": "main", "sha": "abc123"},
    )
    httpx_mock.add_response(
        url="https://gitlab.example.com/api/v4/projects/42/pipelines/10/jobs?per_page=100",
        json=[
            {
                "id": 101,
                "name": "test",
                "stage": "test",
                "status": "failed",
                "failure_reason": "script_failure",
            },
            {"id": 102, "name": "lint", "stage": "test", "status": "success"},
        ],
    )
    httpx_mock.add_response(
        url="https://gitlab.example.com/api/v4/projects/42/jobs/101/trace",
        text="ERROR: test_foo.py::test_bar FAILED\nAssertionError: expected 1, got 2",
    )
    httpx_mock.add_response(
        url="https://gitlab.example.com/api/v4/projects/42/repository/files/.gitlab-ci.yml?ref=main",
        json={
            "file_name": ".gitlab-ci.yml",
            "file_path": ".gitlab-ci.yml",
            "encoding": "base64",
            "content": "c3RhZ2VzOgogIC0gdGVzdA==",
            "ref": "main",
        },
    )

    event = PipelineEvent(
        object_kind="pipeline",
        user=_make_user(),
        project=_make_project(),
        object_attributes=PipelineObjectAttributes(
            id=10,
            status="failed",
            ref="main",
        ),
        builds=[],
    )

    ctx = await engine.build_pipeline_context(event)
    assert ctx.event_type == "pipeline"
    assert ctx.pipeline is not None
    assert ctx.pipeline.status == "failed"
    assert len(ctx.failed_jobs) == 1
    assert ctx.failed_jobs[0].name == "test"
    assert 101 in ctx.job_logs
    assert "AssertionError" in ctx.job_logs[101]
    assert "stages" in ctx.ci_config
