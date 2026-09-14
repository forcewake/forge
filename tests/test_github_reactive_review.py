"""GitHub reactive review engine tests (v0.7, research port F1).

Three layers, mirroring the slice's established test styles:

- Engine unit tests drive
  :func:`forge.reactive.github_review.execute_reactive_review` over
  :class:`tests.fixtures.fake_github.FakeGitHub` and
  :class:`tests.fixtures.fake_llm.FakeLLM` — incremental delta, full inline
  set, verdict mapping, recursion guard, sticky progress, cap folding.
- Ingress tests push signed webhook deliveries through the FastAPI app
  (the same harness as ``test_github_webhook.py``) and assert the durable
  inbox/step contract for ``pull_request`` ``opened``/``synchronize``.
- A dispatch test proves the ``review_pr`` step payload routes from the
  shared run-command executor into the reactive lane.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from forge.config import ForgeConfig, Settings
from forge.database import reset_engine
from forge.durable import EventInbox, StepRun
from forge.integrations.github import GITHUB_BODY_MAX_CHARS
from forge.main import create_app
from forge.reactive.github_review import (
    REVIEW_MARKER_TEMPLATE,
    REVIEW_MAX_COMMENTS,
    GitHubReactiveReviewer,
    execute_reactive_review,
)
from tests.fixtures.fake_github import FakeGitHub
from tests.fixtures.fake_llm import FakeLLM
from tests.test_github_webhook import GITHUB_WEBHOOK_SECRET, github_settings

REPO = "acme/acme-widget"
OWNER, REPO_NAME = REPO.split("/", 1)
PROJECT_ID = 70010
PR = 7
BOT = "forge-app[bot]"
OLD = "a" * 40
NEW = "b" * 40

FIXTURES = Path(__file__).parent / "fixtures" / "github_payloads"


def make_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec-test",  # noqa: S105
        FORGE_BOT_TOKEN="glpat-bot-test",  # noqa: S105
        FORGE_BOT_USERNAME=BOT,
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
    )
    values.update(overrides)
    return Settings(**values)


def verdict_text(
    *,
    verdict: str = "ok",
    summary: str = "clean implementation",
    findings: list[dict] | None = None,
) -> str:
    return json.dumps({"verdict": verdict, "summary": summary, "findings": findings or []})


def make_llm(**kwargs) -> FakeLLM:
    return FakeLLM(script=[verdict_text(**kwargs)])


def make_stack(fake: FakeGitHub, llm: FakeLLM, settings: Settings):
    """The reactive stack the engine dispatches with (client, reviewer)."""
    return fake, GitHubReactiveReviewer(llm, fake, settings=settings)


def synchronize_metadata(**overrides) -> dict:
    metadata = {
        "command": "review_pr",
        "provider": "github",
        "repo_full_name": REPO,
        "project_id": PROJECT_ID,
        "pr_number": PR,
        "issue_number": PR,
        "action": "synchronize",
        "head_sha": NEW,
        "after_sha": NEW,
        "before_sha": OLD,
        "head_branch": "feature/rate-limiter",
        "sender_type": "User",
        "pr_author_type": "User",
        "author_username": "alice",
    }
    metadata.update(overrides)
    return metadata


async def run_engine(fake, llm, metadata, settings=None):
    settings = settings or make_settings()
    stack = make_stack(fake, llm, settings)
    return await execute_reactive_review(
        settings,
        ForgeConfig(),
        None,  # the fake LLM journals nothing; no session needed
        metadata,
        stack_factory=lambda owner, repo: stack,
    )


def comments_of(fake: FakeGitHub, pr_number: int = PR) -> list[dict]:
    return fake.issue_comments.get(REPO, {}).get(pr_number, [])


class TestIncrementalDelta:
    async def test_synchronize_reviews_only_the_new_commits(self):
        fake = FakeGitHub()
        llm = make_llm(verdict="ok", summary="delta clean")
        fake.seed_review(PR, commit_id=OLD)
        # Full PR surface: the old file plus the new one — the engine must
        # read the compare delta instead, so only the new file reaches the LLM.
        fake.seed_pr_files(
            PR,
            [
                {
                    "filename": "src/old.py",
                    "status": "modified",
                    "patch": "@@ -1,2 +1,3 @@\n old line\n+old addition",
                },
                {
                    "filename": "src/new.py",
                    "status": "added",
                    "patch": "@@ -0,0 +1,2 @@\n+fresh code",
                },
            ],
        )
        fake.seed_compare(
            OLD,
            NEW,
            [
                {
                    "filename": "src/new.py",
                    "status": "added",
                    "patch": "@@ -0,0 +1,2 @@\n+fresh code",
                }
            ],
        )

        outcome = await run_engine(fake, llm, synchronize_metadata())

        assert outcome == {
            "status": "reviewed",
            "event": "COMMENT",
            "pr_number": PR,
            "head_sha": NEW,
            "inline_comments": 0,
            "review_id": outcome["review_id"],
        }
        # The free delta: compare was read, the full PR files were not.
        assert fake.calls_of("get_compare") == [("get_compare", (OWNER, REPO_NAME, OLD, NEW))]
        assert fake.calls_of("get_pr_files") == []
        prompt = llm.calls[0]["user"]
        assert "src/new.py" in prompt
        assert "src/old.py" not in prompt
        # The review marks itself incremental and anchors the delta range.
        review = fake.reviews[PR][-1]
        assert review["state"] == "COMMENT"
        assert review["commit_id"] == NEW
        assert "Incremental Review" in review["body"]
        assert f"`{OLD[:8]}..{NEW[:8]}`" in review["body"]

    async def test_first_review_covers_the_full_pr(self):
        fake = FakeGitHub()
        llm = make_llm()
        fake.seed_pr_files(
            PR, [{"filename": "src/new.py", "status": "added", "patch": "+fresh code"}]
        )

        outcome = await run_engine(
            fake, llm, synchronize_metadata(action="opened", before_sha="", head_sha=NEW)
        )

        assert outcome["status"] == "reviewed"
        assert fake.calls_of("get_pr_files") == [("get_pr_files", (OWNER, REPO_NAME, PR))]
        assert fake.calls_of("get_compare") == []
        assert fake.reviews[PR][-1]["commit_id"] == NEW

    async def test_empty_delta_skips_the_llm_call(self):
        fake = FakeGitHub()
        llm = make_llm()
        fake.seed_review(PR, commit_id=OLD)
        fake.seed_compare(OLD, NEW, [])

        outcome = await run_engine(fake, llm, synchronize_metadata())

        assert outcome == {"status": "skipped", "reason": "empty_delta"}
        assert llm.calls == []
        assert fake.reviews[PR] == [fake.reviews[PR][0]]  # no second review

    async def test_head_already_reviewed_is_skipped(self):
        fake = FakeGitHub()
        llm = make_llm()
        fake.seed_review(PR, commit_id=NEW)

        outcome = await run_engine(fake, llm, synchronize_metadata(before_sha=NEW))

        assert outcome == {"status": "skipped", "reason": "already_reviewed"}
        assert llm.calls == []
        assert "already reviewed" in comments_of(fake)[0]["body"]

    async def test_another_reviewer_does_not_anchor_the_delta(self):
        fake = FakeGitHub()
        llm = make_llm()
        fake.seed_review(PR, commit_id=OLD, login="someone-else")
        fake.seed_pr_files(PR, [{"filename": "src/new.py", "patch": "+fresh"}])

        outcome = await run_engine(fake, llm, synchronize_metadata())

        assert outcome["status"] == "reviewed"
        # A human's (or another bot's) review is not forge's anchor: full PR.
        assert fake.calls_of("get_pr_files") != []
        assert fake.calls_of("get_compare") == []


class TestFullReviewAndVerdictMapping:
    async def test_critical_finding_requests_changes(self):
        fake = FakeGitHub()
        llm = make_llm(
            verdict="concerns",
            summary="secret committed",
            findings=[
                {
                    "severity": "critical",
                    "file": "src/new.py",
                    "line": 2,
                    "note": "hardcoded credential",
                }
            ],
        )
        fake.seed_pr_files(
            PR, [{"filename": "src/new.py", "status": "added", "patch": "+fresh code"}]
        )

        outcome = await run_engine(
            fake, llm, synchronize_metadata(action="opened", before_sha="", head_sha=NEW)
        )

        assert outcome["event"] == "REQUEST_CHANGES"
        review = fake.reviews[PR][-1]
        assert review["state"] == "REQUEST_CHANGES"
        (comment,) = review["comments"]
        assert comment["path"] == "src/new.py"
        assert comment["line"] == 2
        assert comment["side"] == "RIGHT"
        assert "critical" in comment["body"]
        assert "hardcoded credential" in comment["body"]

    async def test_non_critical_findings_comment(self):
        fake = FakeGitHub()
        llm = make_llm(
            verdict="concerns",
            summary="style issues",
            findings=[
                {"severity": "warning", "file": "src/new.py", "line": 1, "note": "naming"},
                {"severity": "suggestion", "file": "src/new.py", "line": 2, "note": "docstring"},
            ],
        )
        fake.seed_pr_files(
            PR,
            [
                {
                    "filename": "src/new.py",
                    "status": "added",
                    "patch": "+fresh code\n+more code",
                }
            ],
        )

        outcome = await run_engine(
            fake, llm, synchronize_metadata(action="opened", before_sha="", head_sha=NEW)
        )

        assert outcome["event"] == "COMMENT"
        assert len(fake.reviews[PR][-1]["comments"]) == 2

    async def test_findings_without_a_diff_anchor_fold_into_the_summary(self):
        fake = FakeGitHub()
        llm = make_llm(
            findings=[
                {"severity": "warning", "file": "docs/notes.md", "note": "untouched file"},
                {"severity": "warning", "file": "src/new.py", "note": "no line given"},
            ]
        )
        fake.seed_pr_files(
            PR, [{"filename": "src/new.py", "status": "added", "patch": "+fresh code"}]
        )

        outcome = await run_engine(
            fake, llm, synchronize_metadata(action="opened", before_sha="", head_sha=NEW)
        )

        assert outcome["inline_comments"] == 0
        body = fake.reviews[PR][-1]["body"]
        assert "2 finding(s) folded into this summary" in body
        assert "docs/notes.md" in body
        assert "| \u26a0\ufe0f Warnings | 2 |" in body


class TestCommentCapAndBodyLimit:
    async def test_more_than_50_findings_fold_into_the_summary(self):
        fake = FakeGitHub()
        findings = [
            {
                "severity": "warning",
                "file": "src/new.py",
                "line": index + 1,
                "note": f"issue {index}",
            }
            for index in range(REVIEW_MAX_COMMENTS + 5)
        ]
        llm = make_llm(findings=findings)
        fake.seed_pr_files(
            PR,
            [
                {
                    "filename": "src/new.py",
                    "status": "added",
                    "patch": "\n".join(f"+line {n}" for n in range(80)),
                }
            ],
        )

        outcome = await run_engine(
            fake, llm, synchronize_metadata(action="opened", before_sha="", head_sha=NEW)
        )

        assert outcome["inline_comments"] == REVIEW_MAX_COMMENTS
        review = fake.reviews[PR][-1]
        assert len(review["comments"]) == REVIEW_MAX_COMMENTS
        assert "5 finding(s) folded into this summary" in review["body"]
        assert "issue 54" in review["body"]  # the overflow finding is in the body
        assert "issue 54" not in json.dumps(review["comments"])

    async def test_review_body_never_exceeds_githubs_limit(self):
        fake = FakeGitHub()
        llm = make_llm(summary="x" * (GITHUB_BODY_MAX_CHARS + 5000))
        fake.seed_pr_files(PR, [])

        outcome = await run_engine(
            fake, llm, synchronize_metadata(action="opened", before_sha="", head_sha=NEW)
        )

        body = fake.reviews[PR][-1]["body"]
        assert outcome["status"] == "reviewed"
        assert len(body) <= GITHUB_BODY_MAX_CHARS
        assert "[truncated" in body


class TestRecursionGuard:
    async def test_forge_branch_is_skipped(self):
        fake = FakeGitHub()
        llm = make_llm()

        outcome = await run_engine(fake, llm, synchronize_metadata(head_branch="forge/42/deadbeef"))

        assert outcome == {"status": "skipped", "reason": "forge_branch"}
        assert llm.calls == []
        assert fake.reviews == {}
        assert comments_of(fake) == []

    async def test_bot_sender_is_skipped(self):
        fake = FakeGitHub()
        llm = make_llm()

        outcome = await run_engine(fake, llm, synchronize_metadata(sender_type="Bot"))

        assert outcome == {"status": "skipped", "reason": "bot_sender"}
        assert llm.calls == []
        assert comments_of(fake) == []

    async def test_bot_authored_pr_is_skipped(self):
        fake = FakeGitHub()
        llm = make_llm()

        outcome = await run_engine(fake, llm, synchronize_metadata(pr_author_type="Bot"))

        assert outcome == {"status": "skipped", "reason": "bot_authored_pr"}
        assert llm.calls == []


class TestStickyProgressComment:
    async def test_first_run_posts_the_marker_comment_and_finalizes_it(self):
        fake = FakeGitHub()
        llm = make_llm()
        fake.seed_pr_files(PR, [{"filename": "src/new.py", "patch": "+fresh"}])

        await run_engine(
            fake, llm, synchronize_metadata(action="opened", before_sha="", head_sha=NEW)
        )

        (comment,) = comments_of(fake)
        assert comment["body"].startswith(REVIEW_MARKER_TEMPLATE.format(pr_number=PR))
        assert "Review posted" in comment["body"]

    async def test_redelivery_updates_the_comment_in_place(self):
        fake = FakeGitHub()
        # First run: full review at OLD (opened).
        first_llm = make_llm()
        fake.seed_pr_files(PR, [{"filename": "src/v1.py", "patch": "+v1"}])
        await run_engine(
            fake, first_llm, synchronize_metadata(action="opened", before_sha="", head_sha=OLD)
        )
        assert len(comments_of(fake)) == 1

        # Re-delivered-style second run: new commits (OLD..NEW).
        second_llm = make_llm()
        fake.seed_review(PR, commit_id=OLD)
        fake.seed_compare(OLD, NEW, [{"filename": "src/v2.py", "patch": "+v2"}])
        await run_engine(fake, second_llm, synchronize_metadata())

        assert len(comments_of(fake)) == 1  # no duplicate comment
        # The in-place updates targeted the one sticky comment; it was
        # created exactly once (first run: POST, both runs: final PATCH).
        updates = fake.calls_of("update_issue_comment")
        assert updates and all(update[1][2] == comments_of(fake)[0]["id"] for update in updates)
        assert len(fake.calls_of("create_issue_comment")) == 1

    async def test_progress_marker_owned_by_someone_else_is_not_reused(self):
        fake = FakeGitHub()
        fake.issue_comments.setdefault(REPO, {})[PR] = [
            {
                "id": 555,
                "body": f"{REVIEW_MARKER_TEMPLATE.format(pr_number=PR)} quoted marker",
                "user": {"login": "human-reviewer", "type": "User"},
            }
        ]
        llm = make_llm()
        fake.seed_pr_files(PR, [{"filename": "src/new.py", "patch": "+fresh"}])

        await run_engine(
            fake, llm, synchronize_metadata(action="opened", before_sha="", head_sha=NEW)
        )

        assert len(comments_of(fake)) == 2  # forge posted its own comment
        # The human's comment body is untouched; forge never PATCHed it.
        assert comments_of(fake)[0]["id"] == 555
        assert "quoted marker" in comments_of(fake)[0]["body"]
        assert all(update[1][2] != 555 for update in fake.calls_of("update_issue_comment"))


class TestLLMFailure:
    async def test_llm_error_raises_after_noting_the_progress_comment(self):
        from forge.factory.llm import LLMError

        fake = FakeGitHub()
        llm = FakeLLM(script=[LLMError("proxy down")])
        fake.seed_pr_files(PR, [{"filename": "src/new.py", "patch": "+fresh"}])

        with pytest.raises(LLMError):
            await run_engine(
                fake, llm, synchronize_metadata(action="opened", before_sha="", head_sha=NEW)
            )

        assert "review failed" in comments_of(fake)[0]["body"]
        assert fake.reviews == {}  # nothing posted


# ----------------------------------------------------------------------
# Ingress: pull_request deliveries land on the durable step lane
# ----------------------------------------------------------------------


class TestPullRequestIngress:
    @pytest.fixture()
    async def app(self, tmp_path):
        reset_engine()
        application = create_app(settings=github_settings(tmp_path))
        async with application.router.lifespan_context(application):
            application.state.task_queue = AsyncMock()
            application.state.task_queue.is_duplicate = AsyncMock(return_value=False)
            yield application
        reset_engine()

    @pytest.fixture()
    async def client(self, app) -> AsyncClient:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    def payload(self, name: str, **mutations) -> bytes:
        data = json.loads((FIXTURES / name).read_bytes())
        for dotted_path, value in mutations.items():
            parts = dotted_path.split(".")
            target = data
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = value
        return json.dumps(data).encode()

    async def post(self, client, body: bytes, event: str = "pull_request"):
        import hashlib
        import hmac

        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256="
            + hmac.new(GITHUB_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest(),
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": "d" * 32,
        }
        return await client.post("/webhook/github", content=body, headers=headers)

    async def rows(self, app):
        async with app.state.session_factory() as session:
            inbox = (await session.execute(select(EventInbox))).scalars().all()
            steps = (await session.execute(select(StepRun))).scalars().all()
        return inbox, steps

    async def test_synchronize_schedules_a_review_step(self, app, client):
        body = self.payload("pull_request_synchronize.json")
        response = await self.post(client, body)

        assert response.status_code == 202
        assert response.json()["run_command"] is True
        inbox, steps = await self.rows(app)
        assert len(inbox) == 1 and len(steps) == 1
        payload = inbox[0].payload
        assert payload["provider"] == "github"
        assert payload["command"] == "review_pr"
        assert payload["repo_full_name"] == REPO
        assert payload["pr_number"] == 7
        assert payload["head_sha"] == "b" * 40
        assert payload["before_sha"] == "a" * 40
        assert payload["head_branch"] == "feature/rate-limiter"
        assert payload["sender_type"] == "User"
        assert steps[0].step_name == "review_pr"
        assert steps[0].status == "scheduled"
        assert steps[0].source_event_id == inbox[0].source_event_id
        (task,) = app.state.task_queue.submit.await_args[0]
        assert task.task_type == "run_command"
        assert task.metadata["command"] == "review_pr"

    async def test_redelivered_synchronize_is_deduplicated(self, app, client):
        body = self.payload("pull_request_synchronize.json")
        first = await self.post(client, body)
        second = await self.post(client, body)

        assert first.json()["run_command"] is True
        assert second.json() == {
            "status": "accepted",
            "event": "pull_request",
            "deduplicated": True,
        }
        inbox, steps = await self.rows(app)
        assert len(inbox) == 1 and len(steps) == 1

    async def test_opened_draft_pr_schedules_a_review_step(self, app, client):
        body = self.payload("pull_request_opened.json")
        response = await self.post(client, body)

        assert response.json()["run_command"] is True
        inbox, steps = await self.rows(app)
        assert steps[0].step_name == "review_pr"
        assert inbox[0].payload["action"] == "opened"
        assert inbox[0].payload["before_sha"] == ""  # zero SHA blanked

    async def test_bot_sender_delivery_is_skipped_entirely(self, app, client):
        body = self.payload("pull_request_synchronize.json", **{"sender.type": "Bot"})
        response = await self.post(client, body)

        assert response.json() == {"status": "skipped", "reason": "bot-loop"}
        inbox, steps = await self.rows(app)
        assert inbox == [] and steps == []

    async def test_non_draft_pr_gets_reviewed(self, app, client):
        """Non-draft PRs are the common case — the reviewer must react to
        them exactly like to drafts (the draft marker is not a skip)."""
        body = self.payload("pull_request_synchronize.json", **{"pull_request.draft": False})
        response = await self.post(client, body)

        assert response.json()["run_command"] is True
        inbox, steps = await self.rows(app)
        assert any(s.step_name == "review_pr" for s in steps)

    async def test_forge_branch_is_recorded_inbox_only(self, app, client):
        body = self.payload(
            "pull_request_synchronize.json", **{"pull_request.head.ref": "forge/7/cafe1234"}
        )
        response = await self.post(client, body)

        assert response.json()["recorded"] is True
        inbox, steps = await self.rows(app)
        assert len(inbox) == 1 and steps == []

    async def test_unrelated_pull_request_action_is_inbox_only(self, app, client):
        body = self.payload("pull_request_synchronize.json", action="closed")
        response = await self.post(client, body)

        assert response.json()["recorded"] is True
        inbox, steps = await self.rows(app)
        assert len(inbox) == 1
        assert inbox[0].event_type == "github:pull_request"
        assert steps == []


class TestStepDispatch:
    async def test_review_pr_command_routes_to_the_reactive_lane(self, monkeypatch):
        captured: dict = {}

        async def spy(settings, forge_config, session_factory, metadata, **kwargs):
            captured.update(metadata)
            return {"status": "reviewed"}

        monkeypatch.setattr("forge.reactive.github_review.execute_reactive_review", spy)
        from forge.runs.service import execute_run_command

        await execute_run_command(
            make_settings(),
            ForgeConfig(),
            None,
            metadata=synchronize_metadata(),
        )
        assert captured["command"] == "review_pr"
        assert captured["repo_full_name"] == REPO
        assert captured["pr_number"] == PR
