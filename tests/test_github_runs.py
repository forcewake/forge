"""GitHub run service tests (E3a): the plan + human gate path on FlowRun rows.

Drives :class:`forge.runs.github_service.GitHubRunService` over
:class:`tests.fixtures.fake_github.FakeGitHub` and the webhook payload
fixtures — the GitLab gate semantics (plan comment, pending decision,
cancel-as-revoke) exercised on the GitHub surface, no network, no model.
"""

import hashlib
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import ForgeConfig, Settings
from forge.adaptive.pause_fence import (
    clear_pause_fence,
    pause_fence_decision,
    raise_pause_fence,
)
from forge.durable import (
    FlowRun,
    FlowStatus,
    GateApproval,
    Outbox,
    RunBudget,
    RunSpec,
    StepRun,
    as_aware_utc,
)
from forge.factory.implementer import IMPLEMENTER_TIER
from forge.factory.llm import LLMError
from forge.factory.reviewer import ReviewVerdict
from forge.harness_entry import PlanBindingError, fetch_issue_context
from forge.harnesses.brief_envelope import build_brief_envelope
from forge.integrations.github import GitHubAPIError
from forge.integrations.github_flow import (
    GitHubAgents,
    GitHubPublishFlow,
    github_factory_branch,
)
from forge.models.base import Base
from forge.adaptive.discovery_stage import DiscoveryRunContext
from forge.orchestrator.project_config import (
    ConfigReadResult,
    clear_cache,
    read_project_config,
)
from forge.repository import Change, ChangeSet, Operation
from forge.runs.admission import approvers_for, check_admission
from forge.runs.github_service import GitHubRunService, execute_github_run_command
from forge.runs.service import task_digest_of
from forge.runs.stubs import StubImplementer, StubPlanner
from tests.fixtures.fake_github import FakeGitHub

FIXTURES = Path(__file__).parent / "fixtures" / "github_payloads"
REPO = "acme/acme-widget"
OWNER, REPO_NAME = REPO.split("/", 1)
PROJECT_ID = 70010  # the webhook's numeric repository id
ISSUE = 42
ISSUE_TITLE = "Add password reset"
ISSUE_DESC = "Users cannot reset their password."
BASE_HEAD = "1" * 40


def make_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        FORGE_APPROVERS="alice",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        # Hermetic against the dev .env (dogfood lanes on): these tests
        # exercise the BUILTIN publish path — no harness workflow. The
        # verification grace is zeroed so the not_configured path is
        # reachable without sleeping.
        FORGE_GITHUB_HARNESS_WORKFLOW="",
        FORGE_VERIFICATION_GRACE_SECONDS=0,
    )
    values.update(overrides)
    return Settings(**values)


class StubPRReviewer:
    """Deterministic PR verdict — the GitHub-shaped review surface."""

    def __init__(self, verdict: str = "ok", summary: str = "clean implementation") -> None:
        self.verdict = verdict
        self.summary = summary
        self.calls: list[dict] = []

    async def review(
        self,
        *,
        owner: str,
        repo: str,
        pr_number: int,
        issue_title: str,
        plan_summary: str,
        base_sha: str,
        candidate_sha: str,
        flow_run_id: str | None = None,
    ) -> ReviewVerdict:
        self.calls.append(
            {
                "owner": owner,
                "repo": repo,
                "pr_number": pr_number,
                "candidate_sha": candidate_sha,
                "base_sha": base_sha,
            }
        )
        return ReviewVerdict(verdict=self.verdict, summary=self.summary, findings=())


class BoomPlanner:
    """Must never be constructed a prompt (admission denies first)."""

    async def plan(self, *args, **kwargs):
        raise AssertionError("planner ran for an admission-denied /implement")


def make_stack(
    fake: FakeGitHub,
    *,
    planner=None,
    implementer=None,
    reviewer=None,
) -> GitHubAgents:
    planner = planner or StubPlanner()
    implementer = implementer or StubImplementer()
    reviewer = reviewer or StubPRReviewer()
    flow = GitHubPublishFlow(fake, proposer=implementer, base_branch="main")
    return GitHubAgents(
        client=fake,
        reader=fake,
        planner=planner,
        implementer=implementer,
        reviewer=reviewer,
        flow=flow,
    )


def make_service(
    db,
    fake: FakeGitHub,
    *,
    settings: Settings | None = None,
    stack: GitHubAgents | None = None,
    repo: str = REPO,
) -> GitHubRunService:
    return GitHubRunService(
        db,
        settings or make_settings(),
        ForgeConfig(),
        stack=stack or make_stack(fake),
        repo_full_name=repo,
    )


@pytest.fixture()
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture()
def fake() -> FakeGitHub:
    github = FakeGitHub()
    github.seed_repo(REPO, {"src/app.py": "print('hi')\n"})
    github.heads[REPO]["main"] = BASE_HEAD
    github.seed_issue(REPO, ISSUE, ISSUE_TITLE, ISSUE_DESC)
    return github


async def get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        return await session.get(FlowRun, run_id)


async def start(service: GitHubRunService, author: str = "alice") -> str:
    return await service.start_run(
        project_id=PROJECT_ID,
        issue_number=ISSUE,
        issue_title=ISSUE_TITLE,
        issue_description=ISSUE_DESC,
        author_username=author,
    )


async def go(
    service: GitHubRunService,
    run_id: str,
    author: str = "alice",
    note: str | None = None,
    now: datetime | None = None,
) -> None:
    await service.handle_go(
        project_id=PROJECT_ID,
        issue_number=ISSUE,
        note_text=note or f"/go {run_id}",
        author_username=author,
        now=now,
    )


def comments(fake: FakeGitHub) -> list[str]:
    return [call[1][3] for call in fake.calls_of("create_issue_comment")]


def clear_comments(fake: FakeGitHub) -> None:
    """Drop the already-recorded comment calls (keep the write-call log)."""
    fake.calls[:] = [call for call in fake.calls if call[0] != "create_issue_comment"]


# ----------------------------------------------------------------------
# /implement: plan comment + waiting_approval (no PR yet!)
# ----------------------------------------------------------------------


class TestImplement:
    async def test_implement_parks_the_run_at_the_gate(self, db, fake):
        service = make_service(db, fake)

        run_id = await start(service)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        # The FlowRun row carries the GitHub subject identity (E3a schema).
        assert run.provider == "github"
        assert run.github_repo_full_name == REPO
        assert run.github_issue_number == ISSUE
        assert run.issue_iid == ISSUE
        assert run.project_id == PROJECT_ID
        assert run.base_sha == BASE_HEAD  # the frozen base, pinned at plan time
        assert run.plan_digest

        # The frozen EXECUTABLE RunSpec (v3, A02) exists; its digest is what
        # the decision binds.
        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .one()
            )
        assert spec.digest == run.spec_digest
        assert spec.schema_version == 3
        assert spec.document["subject"]["provider"] == "github"
        assert spec.document["subject"]["project_id"] == PROJECT_ID
        assert spec.document["subject"]["issue_iid"] == ISSUE
        assert spec.document["source_base_oid"] == BASE_HEAD
        # R04/A02: the executable content rides in the document.
        assert spec.document["task"] == {
            "title": ISSUE_TITLE,
            "description": ISSUE_DESC,
            "digest": task_digest_of(ISSUE_TITLE, ISSUE_DESC),
        }
        assert spec.document["plan"]["digest"] == run.plan_digest
        assert spec.document["plan"]["summary"]
        assert spec.document["model_route"] == {"tier": IMPLEMENTER_TIER}
        assert spec.document["verification"]["required_jobs"] == []
        assert spec.document["budgets"] == {
            "commit_cycles": 3,
            "harness_timeout": make_settings().FORGE_HARNESS_TIMEOUT_SECONDS,
        }
        backend = spec.document["backend_config"]
        assert backend["backend"] == "builtin"
        assert backend["harness"] == "claude-code"
        assert backend["model"] == make_settings().FORGE_HARNESS_MODEL
        assert "harness_workflow" not in backend  # builtin — no frozen lane

        # The pending decision exists, carrying the plan/base/spec/task digests.
        async with db() as session:
            gate = (
                (
                    await session.execute(
                        select(GateApproval).where(GateApproval.flow_run_id == run_id)
                    )
                )
                .scalars()
                .one()
            )
        assert gate.consumed_at is None
        assert gate.plan_digest == run.plan_digest
        assert gate.base_sha == BASE_HEAD
        assert gate.spec_digest == run.spec_digest
        assert gate.task_digest
        assert as_aware_utc(gate.expires_at) > datetime.now(timezone.utc)

        # The plan was posted as ONE issue comment with the digest and the
        # full-id /go instruction.
        (body,) = comments(fake)
        assert "Forge plan" in body
        assert run.plan_digest in body
        assert f"/go {run_id}" in body

        # NO PR yet — publishing happens only after /go.
        assert fake.calls_of("create_branch") == []
        assert fake.calls_of("create_commit_on_branch") == []
        assert fake.calls_of("create_draft_pr") == []

    async def test_plan_comment_carries_the_implementation_block(self, db, fake):
        """ADR-0023 §4: the gate sees the execution shape — the five-line
        Implementation block sits between the plan body and the /go footer."""
        service = make_service(db, fake)
        run_id = await start(service)

        (body,) = comments(fake)
        assert "## Implementation" in body
        assert f"- Harness: **claude-code** · model {make_settings().FORGE_HARNESS_MODEL}" in body
        assert "- Fallbacks: none" in body
        assert "- Budget class: standard" in body
        assert "- Commit cycles: 3" in body
        assert "- Selection reason: default" in body
        assert body.index("## Implementation") < body.index("Plan digest")
        assert body.index("## Implementation") < body.index(f"/go {run_id}")

        run = await get_run(db, run_id)
        assert (run.evidence or {})["harness_selection"]["harness"] == "claude-code"

    async def test_non_approver_implement_is_denied_before_any_model_call(self, db, fake):
        service = make_service(db, fake, stack=make_stack(fake, planner=BoomPlanner()))

        run_id = await start(service, author="mallory")

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "admission_denied" in (run.status_reason or "")
        (body,) = comments(fake)
        assert "admission denied" in body and "@mallory" in body

    async def test_planning_failure_parks_the_run_blocked(self, db, fake):
        from forge.factory.llm import LLMError

        class FailingPlanner:
            async def plan(self, *args, **kwargs):
                raise LLMError("proxy down")

        service = make_service(db, fake, stack=make_stack(fake, planner=FailingPlanner()))

        with pytest.raises(LLMError):
            await start(service)

        run = await get_run(db, (await _only_run_id(db)))
        assert run.status == FlowStatus.BLOCKED.value  # fatal: parked, never silent
        assert "planning_failed" in (run.status_reason or "")


async def _only_run_id(db) -> str:
    async with db() as session:
        return (await session.execute(select(FlowRun.id))).scalars().one()


# ----------------------------------------------------------------------
# Connection-scoped approvers (v0.6): FORGE_GITHUB_APPROVERS
# ----------------------------------------------------------------------


class TestApproverScoping:
    """The GitHub connection resolves its own approver list — the shared
    FORGE_APPROVERS (GitLab usernames) must never authorize a GitHub run."""

    def test_github_approvers_resolve_independently(self):
        settings = make_settings(FORGE_APPROVERS="demo,alice", FORGE_GITHUB_APPROVERS="alice")

        assert approvers_for("github", settings) == frozenset({"alice"})
        assert approvers_for("gitlab", settings) == frozenset({"demo", "alice"})

    def test_empty_github_list_falls_back_to_the_shared_list(self):
        settings = make_settings(FORGE_APPROVERS="alice")

        assert approvers_for("github", settings) == frozenset({"alice"})
        assert approvers_for("gitlab", settings) == frozenset({"alice"})

    async def test_gitlab_only_login_cannot_start_a_github_run(self, db, fake):
        # The live-found leak: 'demo' exists only in the GitLab list.
        settings = make_settings(FORGE_APPROVERS="demo", FORGE_GITHUB_APPROVERS="alice")
        service = make_service(
            db, fake, settings=settings, stack=make_stack(fake, planner=BoomPlanner())
        )

        run_id = await start(service, author="demo")

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "admission_denied" in (run.status_reason or "")

    async def test_go_from_a_gitlab_only_login_is_ignored(self, db, fake):
        settings = make_settings(FORGE_APPROVERS="demo", FORGE_GITHUB_APPROVERS="alice")
        service = make_service(db, fake, settings=settings)
        run_id = await start(service, author="alice")
        clear_comments(fake)

        await go(service, run_id, author="demo")

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert fake.calls_of("create_commit_on_branch") == []
        assert fake.calls_of("create_draft_pr") == []

    async def test_bot_in_the_github_list_denies_github_runs(self, db, fake):
        settings = make_settings(
            FORGE_APPROVERS="alice",
            FORGE_GITHUB_APPROVERS="forge-bot,alice",
            FORGE_BOT_USERNAME="forge-bot",
        )
        service = make_service(
            db, fake, settings=settings, stack=make_stack(fake, planner=BoomPlanner())
        )

        run_id = await start(service, author="alice")

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "must not appear in FORGE_GITHUB_APPROVERS" in (run.status_reason or "")

    def test_bot_in_the_shared_list_still_denies_github_via_fallback(self):
        settings = make_settings(FORGE_APPROVERS="forge-bot", FORGE_BOT_USERNAME="forge-bot")

        decision = check_admission(settings, ForgeConfig(), PROJECT_ID, "alice", provider="github")

        assert decision.allowed is False
        assert "must not appear in FORGE_APPROVERS" in decision.reason


# ----------------------------------------------------------------------
# /go: the gate and the publish leg
# ----------------------------------------------------------------------


class TestGo:
    async def test_go_by_non_approver_changes_nothing(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)
        clear_comments(fake)

        await go(service, run_id, author="mallory")

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        async with db() as session:
            gate = (
                (
                    await session.execute(
                        select(GateApproval).where(GateApproval.flow_run_id == run_id)
                    )
                )
                .scalars()
                .one()
            )
        assert gate.consumed_at is None
        assert comments(fake) == []  # nothing published, nothing said
        assert fake.calls_of("create_commit_on_branch") == []

    async def test_go_publishes_and_reaches_ready_for_human(self, db, fake):
        reviewer = StubPRReviewer()
        service = make_service(db, fake, stack=make_stack(fake, reviewer=reviewer))
        run_id = await start(service)
        clear_comments(fake)

        await go(service, run_id)

        # R02: publication parks at waiting_ci — PR checks are an
        # independent gate. This repo has NO CI configured (the fake returns
        # no runs), so the verification pass continues as honestly
        # unverified.
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        assert (run.evidence or {}).get("verification", {}).get("status") == "not_configured"

        # The decision was consumed exactly once.
        async with db() as session:
            gate = (
                (
                    await session.execute(
                        select(GateApproval).where(GateApproval.flow_run_id == run_id)
                    )
                )
                .scalars()
                .one()
            )
        assert gate.consumed_at is not None

        # CAS commit on the run-owned branch cut from the FROZEN base, and a
        # Draft PR (find-by-head-first).
        (commit_call,) = fake.calls_of("create_commit_on_branch")
        branch = f"forge/{ISSUE}/{run_id[:8]}"
        assert commit_call[1][2] == branch
        (pr,) = fake.prs_for(REPO, branch)
        assert pr["draft"] is True
        assert run.mr_iid == pr["number"]
        assert run.candidate_shas == [pr["head"]["sha"]]
        evidence = run.evidence or {}
        assert evidence["published_candidate"]["base"] == BASE_HEAD

        # The evidence comment carries the PR link, the candidate sha and the
        # verification note (no required checks enforced yet — E3b wires that).
        evidence_notes = [
            body for body in comments(fake) if "candidate published" in body
        ]  # B13: not ready yet — verification pending
        assert len(evidence_notes) == 1
        assert pr["html_url"] in evidence_notes[0]
        assert pr["head"]["sha"] in evidence_notes[0]
        assert "verification pending" in evidence_notes[0]  # B13: no premature ready claim

        # R02: the ready reason says unverified — no CI exists on the repo.
        assert "unverified" in (run.status_reason or "")

        # The readonly review ran over the PR diff and is bound to the sha.
        (review_call,) = reviewer.calls
        assert review_call["pr_number"] == pr["number"]
        assert review_call["candidate_sha"] == pr["head"]["sha"]
        assert evidence["review"]["sha"] == pr["head"]["sha"]
        assert evidence["review"]["verdict"] == "ok"

        # The run walked waiting_ci → evaluating_ci → reviewing → ready.
        async with db() as session:
            targets = [
                row.payload["to"]
                for row in (
                    await session.execute(
                        select(Outbox).where(Outbox.flow_run_id == run_id).order_by(Outbox.id)
                    )
                )
                .scalars()
                .all()
            ]
        assert targets[-4:] == [
            FlowStatus.WAITING_CI.value,
            FlowStatus.EVALUATING_CI.value,
            FlowStatus.REVIEWING.value,
            FlowStatus.READY_FOR_HUMAN.value,
        ]

    async def test_go_is_idempotent_on_redelivery(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)
        await go(service, run_id)
        commits_after_first = len(fake.calls_of("create_commit_on_branch"))

        await go(service, run_id)  # re-delivered /go

        assert len(fake.calls_of("create_commit_on_branch")) == commits_after_first
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value  # parked for verification

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value

    async def test_go_after_ttl_blocks_decision_expired(self, db, fake):
        settings = make_settings(FORGE_DECISION_TTL_SECONDS=3600)
        service = make_service(db, fake, settings=settings)
        run_id = await start(service)
        clear_comments(fake)

        late = datetime.now(timezone.utc) + timedelta(seconds=3600 + 60)
        await go(service, run_id, now=late)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "decision_expired" in (run.status_reason or "")
        (body,) = comments(fake)
        assert "blocked" in body
        # Nothing published; the decision stays unconsumed.
        assert fake.calls_of("create_commit_on_branch") == []
        async with db() as session:
            gate = (
                (
                    await session.execute(
                        select(GateApproval).where(GateApproval.flow_run_id == run_id)
                    )
                )
                .scalars()
                .one()
            )
        assert gate.consumed_at is None

    async def test_branch_drift_blocks_the_run_without_retry(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)
        clear_comments(fake)

        # Simulate a concurrent writer: the branch head moves after the cut,
        # before the commit mutation — the CAS must refuse.
        original_commit = fake.create_commit_on_branch

        async def intercept_commit(*args, **kwargs):
            fake.heads[REPO][f"forge/{ISSUE}/{run_id[:8]}"] = "f" * 40
            return await original_commit(*args, **kwargs)

        fake.create_commit_on_branch = intercept_commit  # type: ignore[method-assign]

        await go(service, run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "branch_drift" in (run.status_reason or "")
        assert len(fake.calls_of("create_commit_on_branch")) == 1  # never retried
        (body,) = comments(fake)
        assert "could not publish" in body


# ----------------------------------------------------------------------
# One active run per (repo, issue)
# ----------------------------------------------------------------------


class TestOneActiveRun:
    async def test_second_implement_while_active_is_refused(self, db, fake):
        service = make_service(db, fake)
        first_id = await start(service)
        clear_comments(fake)

        second_id = await start(service)

        assert second_id == first_id  # the existing run is adopted, not forked
        async with db() as session:
            runs = (await session.execute(select(FlowRun))).scalars().all()
        assert len(runs) == 1
        (refusal,) = comments(fake)
        assert "already active" in refusal
        assert first_id in refusal
        assert f"/go {first_id}" in refusal

    async def test_race_loser_adopts_the_existing_run(self, db, fake, monkeypatch):
        service = make_service(db, fake)
        first_id = await start(service)
        clear_comments(fake)

        # Simulate the race: the pre-check misses (returns None) but the
        # insert hits uq_active_run_per_issue — the handler must adopt.
        original_find = service._find_active_run
        first_call = True

        async def racing_find(project_id, issue_number):
            nonlocal first_call
            if first_call:
                first_call = False
                return None
            return await original_find(project_id, issue_number)

        monkeypatch.setattr(service, "_find_active_run", racing_find)

        adopted_id = await start(service)

        assert adopted_id == first_id
        async with db() as session:
            runs = (await session.execute(select(FlowRun))).scalars().all()
        assert len(runs) == 1
        (refusal,) = comments(fake)
        assert "already active" in refusal

    async def test_partial_index_rejects_a_second_active_run(self, db):
        """The DB invariant of last resort: two active (repo, issue) rows
        cannot coexist; a terminal run does not block a fresh one."""
        async with db() as session:
            session.add(
                FlowRun(
                    id="a" * 32,
                    project_id=PROJECT_ID,
                    issue_iid=ISSUE,
                    provider="github",
                    github_repo_full_name=REPO,
                    github_issue_number=ISSUE,
                    status=FlowStatus.WAITING_APPROVAL.value,
                )
            )
            await session.commit()
            session.add(
                FlowRun(
                    id="b" * 32,
                    project_id=PROJECT_ID,
                    issue_iid=ISSUE,
                    provider="github",
                    github_repo_full_name=REPO,
                    github_issue_number=ISSUE,
                    status=FlowStatus.WAITING_APPROVAL.value,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()

        # A terminal run does NOT hold the slot (partial index predicate).
        async with db() as session:
            session.add(
                FlowRun(
                    id="c" * 32,
                    project_id=PROJECT_ID,
                    issue_iid=ISSUE,
                    provider="github",
                    github_repo_full_name=REPO,
                    github_issue_number=ISSUE,
                    status=FlowStatus.READY_FOR_HUMAN.value,
                )
            )
            await session.commit()  # must not raise


# ----------------------------------------------------------------------
# /cancel: cancel-as-revoke (F13)
# ----------------------------------------------------------------------


class TestCancel:
    async def test_cancel_revokes_the_grant_and_cancels_scheduled_steps(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)
        clear_comments(fake)
        async with db() as session:
            session.add(
                StepRun(
                    flow_run_id=run_id,
                    step_name="go",
                    status="scheduled",
                    source_event_id="e" * 64,
                )
            )
            await session.commit()

        await service.handle_cancel(
            project_id=PROJECT_ID,
            issue_number=ISSUE,
            note_text=f"@forge /cancel {run_id}",
            author_username="alice",
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.CANCELLED.value
        assert run.cancel_requested is True  # the publication grant is revoked
        async with db() as session:
            step = (
                (await session.execute(select(StepRun).where(StepRun.flow_run_id == run_id)))
                .scalars()
                .one()
            )
        assert step.status == "cancelled"
        (body,) = comments(fake)
        assert "cancelled" in body

        # A late /go is ignored: the run is terminal, nothing publishes.
        await go(service, run_id)
        assert fake.calls_of("create_commit_on_branch") == []
        assert fake.calls_of("create_draft_pr") == []

    async def test_cancel_from_non_approver_is_ignored(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)

        await service.handle_cancel(
            project_id=PROJECT_ID,
            issue_number=ISSUE,
            note_text=f"@forge /cancel {run_id}",
            author_username="mallory",
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert run.cancel_requested is False

    async def test_cancel_without_run_id_targets_the_active_run(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)

        await service.handle_cancel(
            project_id=PROJECT_ID,
            issue_number=ISSUE,
            note_text="@forge /cancel",
            author_username="alice",
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.CANCELLED.value

    async def test_mid_leg_cancel_stands_the_publish_down(self, db, fake):
        """F13: a cancel that lands while the publish leg is in flight revokes
        the grant — the leg stands down instead of racing the cancel."""
        service = make_service(db, fake)
        run_id = await start(service)
        clear_comments(fake)
        # Simulate the cancel having committed its revoke flag between the
        # gate consumption and the publish (the run is not terminal yet).
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.cancel_requested = True
            await session.commit()

        await go(service, run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.PROPOSING.value  # the leg stood down
        assert run.candidate_shas == []  # nothing published
        assert fake.calls_of("create_branch") == []
        assert fake.calls_of("create_commit_on_branch") == []
        assert fake.calls_of("create_draft_pr") == []


# ----------------------------------------------------------------------
# Dispatch: webhook payload fixtures → durable step → service
# ----------------------------------------------------------------------


class TestDispatch:
    def payload_metadata(self, name: str) -> dict:
        from forge.gateway.github_webhook import normalize_issue_comment

        payload = json.loads((FIXTURES / name).read_bytes())
        metadata = normalize_issue_comment(payload)
        assert metadata is not None and metadata["command"] == "start_run"
        return metadata

    async def test_execute_github_run_command_start_run(self, db, fake):
        metadata = self.payload_metadata("issue_comment_created.json")
        settings = make_settings()

        await execute_github_run_command(
            settings,
            ForgeConfig(),
            db,
            metadata,
            stack_factory=lambda owner, name: make_stack(fake),
        )

        async with db() as session:
            run = (await session.execute(select(FlowRun))).scalars().one()
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert run.provider == "github"
        assert run.github_repo_full_name == REPO
        assert metadata["project_id"] == PROJECT_ID
        assert run.issue_iid == metadata["issue_number"]

    async def test_durable_step_executes_the_github_command(self, db, fake, monkeypatch):
        """The scheduled command step (gateway ingress) drives the GitHub
        service through the SAME claim/execute protocol as the worker."""
        from forge.gateway.github_webhook import github_source_event_id, normalize_issue_comment
        from forge.worker.steps import (
            claim_command_step,
            execute_claimed_step,
            schedule_command_step,
        )

        import forge.worker.steps as steps_module

        payload = json.loads((FIXTURES / "issue_comment_created.json").read_bytes())
        metadata = normalize_issue_comment(payload)
        source_event_id = github_source_event_id(
            metadata["connection_id"], "issue_comment", "created", f"comment:{metadata['note_id']}"
        )

        async with db() as session:
            async with session.begin():
                await schedule_command_step(session, metadata, source_event_id=source_event_id)

        # The worker calls forge.runs.execute_run_command; route it to the
        # GitHub service with the fake stack injected.
        from forge.config import ForgeConfig

        async def routed(settings, forge_config, session_factory, cmd_metadata):
            await execute_github_run_command(
                settings,
                forge_config,
                session_factory,
                cmd_metadata,
                stack_factory=lambda owner, name: make_stack(fake),
            )

        monkeypatch.setattr(steps_module, "execute_run_command", routed)

        settings = make_settings()
        claimed = await claim_command_step(db, "worker-test", source_event_id)
        assert claimed is not None
        await execute_claimed_step(db, settings, ForgeConfig(), claimed)

        async with db() as session:
            run = (await session.execute(select(FlowRun))).scalars().one()
            step = (await session.execute(select(StepRun))).scalars().one()
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert step.status == "succeeded"
        assert fake.calls_of("create_issue_comment")  # the plan comment went out


# ----------------------------------------------------------------------
# issues.edited: replan a stale gate-waiting run, never yank a live one
# ----------------------------------------------------------------------

WORKFLOW = "forge-harness.github.yml"


async def edit_issue(
    service: GitHubRunService,
    *,
    body: str,
    title: str = ISSUE_TITLE,
    author: str = "alice",
) -> str | None:
    return await service.handle_issue_edited(
        project_id=PROJECT_ID,
        issue_number=ISSUE,
        issue_title=title,
        issue_body=body,
        author_username=author,
    )


class TestIssueEdited:
    async def test_edit_while_waiting_approval_replans(self, db, fake):
        service = make_service(db, fake)
        stale_id = await start(service)
        clear_comments(fake)
        new_body = "Users cannot reset their password. The reset mail bounces with SMTP 550."

        new_id = await edit_issue(service, body=new_body)

        assert new_id is not None and new_id != stale_id
        stale = await get_run(db, stale_id)
        fresh = await get_run(db, new_id)
        # The stale run is cancelled DURABLY: grant revoked, not just parked.
        assert stale.status == FlowStatus.CANCELLED.value
        assert stale.cancel_requested is True
        assert fresh.status == FlowStatus.WAITING_APPROVAL.value

        # The fresh run's frozen snapshot IS the new text.
        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == new_id)))
                .scalars()
                .one()
            )
        assert spec.document["task_digest"] == task_digest_of(ISSUE_TITLE, new_body)

        # The plan comment went out again, plus the regeneration note.
        bodies = comments(fake)
        assert len([b for b in bodies if "Forge plan" in b]) == 1
        (note,) = [b for b in bodies if "stale" in b]
        assert stale_id[:8] in note and new_id[:8] in note

        # The stale run's gate was never consumed by the replan.
        async with db() as session:
            gates = (
                (
                    await session.execute(
                        select(GateApproval).where(GateApproval.flow_run_id == stale_id)
                    )
                )
                .scalars()
                .all()
            )
        assert gates and all(gate.consumed_at is None for gate in gates)

    async def test_redelivered_edit_is_a_no_op(self, db, fake):
        """A redelivered edit (fresh run's snapshot already IS that text)
        must not spawn a third run or re-post anything."""
        service = make_service(db, fake)
        await start(service)
        new_body = "The issue body, edited once."
        first = await edit_issue(service, body=new_body)
        notes_after_first = len(fake.calls_of("create_issue_comment"))

        second = await edit_issue(service, body=new_body)

        assert second == first
        assert len(fake.calls_of("create_issue_comment")) == notes_after_first
        async with db() as session:
            runs = (await session.execute(select(FlowRun))).scalars().all()
        assert len(runs) == 2  # the stale run + the replan, nothing more

    async def test_gate_consumed_but_still_waiting_posts_note_only(self, db, fake):
        """The "gate already consumed" guard: between consume_approval and
        the PROPOSING commit the run still reads waiting_approval — an edit
        in exactly that window must never cancel an approved run."""
        service = make_service(db, fake)
        run_id = await start(service)
        async with db() as session:
            gate = (
                (
                    await session.execute(
                        select(GateApproval).where(GateApproval.flow_run_id == run_id)
                    )
                )
                .scalars()
                .one()
            )
            gate.consumed_at = datetime.now(timezone.utc)
            await session.commit()
        clear_comments(fake)

        result = await edit_issue(service, body="an edited body")

        run = await get_run(db, run_id)
        assert result == run_id
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert run.cancel_requested is False
        (note,) = comments(fake)
        assert "not** in the approved plan" in note

    async def test_mid_flight_edit_notes_once_and_does_not_yank(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)
        await go(service, run_id)  # approved → published → waiting_ci
        clear_comments(fake)

        result = await edit_issue(service, body="edited while the run executes")

        run = await get_run(db, run_id)
        assert result == run_id
        assert run.status == FlowStatus.WAITING_CI.value
        assert run.cancel_requested is False
        (note,) = comments(fake)
        assert "in flight" in note

    async def test_non_admitted_edit_is_ignored(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)
        clear_comments(fake)

        result = await edit_issue(service, body="vandalism", author="mallory")

        assert result is None
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert comments(fake) == []

    async def test_edited_command_dispatch_replans(self, db, fake):
        """The gateway-normalized command drives the service end to end."""
        service = make_service(db, fake)
        stale_id = await start(service)
        metadata = {
            "command": "issue_edited",
            "provider": "github",
            "repo_full_name": REPO,
            "project_id": PROJECT_ID,
            "issue_number": ISSUE,
            "issue_title": ISSUE_TITLE,
            "issue_body": "dispatched edit body",
            "author_username": "alice",
            "note_text": "",
            "note_id": "edit:1010:abc",
        }

        await execute_github_run_command(
            make_settings(),
            ForgeConfig(),
            db,
            metadata,
            stack_factory=lambda owner, name: make_stack(fake),
        )

        stale = await get_run(db, stale_id)
        assert stale.status == FlowStatus.CANCELLED.value
        async with db() as session:
            runs = (await session.execute(select(FlowRun))).scalars().all()
        assert len(runs) == 2
        assert any(run.status == FlowStatus.WAITING_APPROVAL.value for run in runs)


# ----------------------------------------------------------------------
# issues.unlabeled (trigger label): label-off = cancel at the gate
# ----------------------------------------------------------------------


class TestLabelOff:
    async def test_label_removal_cancels_the_gate_waiting_run(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)
        clear_comments(fake)

        cancelled = await service.handle_label_removed(
            project_id=PROJECT_ID, issue_number=ISSUE, author_username="alice"
        )

        assert cancelled == 1
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.CANCELLED.value
        assert run.cancel_requested is True
        (note,) = comments(fake)
        assert "cancelled" in note and "label" in note

    async def test_label_removal_leaves_a_past_gate_run_alone(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)
        await go(service, run_id)  # the approval consumed the plan

        cancelled = await service.handle_label_removed(
            project_id=PROJECT_ID, issue_number=ISSUE, author_username="alice"
        )

        assert cancelled == 0
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value

    async def test_non_approver_label_removal_is_ignored(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)

        cancelled = await service.handle_label_removed(
            project_id=PROJECT_ID, issue_number=ISSUE, author_username="mallory"
        )

        assert cancelled == 0
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value


# ----------------------------------------------------------------------
# Superseded-PR janitor: a successor run closes dead runs' Draft PRs
# ----------------------------------------------------------------------


class TestSupersededDraftPR:
    async def test_successor_dispatch_closes_the_dead_run_s_draft_pr(self, db, fake):
        service = make_service(db, fake)
        stale_id = await start(service)
        await go(service, stale_id)  # Draft PR published, parked at waiting_ci
        # The operator's old chore: /cancel the dead run, close its PR by hand.
        await service.handle_cancel(
            project_id=PROJECT_ID,
            issue_number=ISSUE,
            note_text=f"/cancel {stale_id}",
            author_username="alice",
        )
        branch = f"forge/{ISSUE}/{stale_id[:8]}"
        assert fake.prs_for(REPO, branch)[0]["state"] == "open"

        # A successor claims the issue on the harness lane → waiting_harness.
        harness = make_service(
            db, fake, settings=make_settings(FORGE_GITHUB_HARNESS_WORKFLOW=WORKFLOW)
        )
        successor_id = await start(harness)
        clear_comments(fake)
        await go(harness, successor_id)

        assert fake.prs_for(REPO, branch)[0]["state"] == "closed"
        assert fake.calls_of("close_pull_request")
        (note,) = [body for body in comments(fake) if "Superseded" in body]
        assert stale_id[:8] in note and successor_id[:8] in note

    async def test_ready_for_human_pr_is_never_janitored(self, db, fake):
        """ready_for_human is terminal too, but its PR is the LIVE deliverable."""
        service = make_service(db, fake)
        ready_id = await start(service)
        await go(service, ready_id)
        await service.evaluate_waiting_ci_one(ready_id)
        assert (await get_run(db, ready_id)).status == FlowStatus.READY_FOR_HUMAN.value
        # A sibling run that died after publishing its Draft PR.
        dead_id = await start(service)
        await go(service, dead_id)
        await service.handle_cancel(
            project_id=PROJECT_ID,
            issue_number=ISSUE,
            note_text=f"/cancel {dead_id}",
            author_username="alice",
        )

        closed = await service.close_superseded_draft_prs(
            project_id=PROJECT_ID, issue_number=ISSUE, successor_run_id="f" * 32
        )

        assert closed == 1
        assert fake.prs_for(REPO, f"forge/{ISSUE}/{ready_id[:8]}")[0]["state"] == "open"
        assert fake.prs_for(REPO, f"forge/{ISSUE}/{dead_id[:8]}")[0]["state"] == "closed"

    async def test_janitor_is_idempotent(self, db, fake):
        service = make_service(db, fake)
        dead_id = await start(service)
        await go(service, dead_id)
        await service.handle_cancel(
            project_id=PROJECT_ID,
            issue_number=ISSUE,
            note_text=f"/cancel {dead_id}",
            author_username="alice",
        )
        successor = "a" * 32

        first = await service.close_superseded_draft_prs(
            project_id=PROJECT_ID, issue_number=ISSUE, successor_run_id=successor
        )
        second = await service.close_superseded_draft_prs(
            project_id=PROJECT_ID, issue_number=ISSUE, successor_run_id=successor
        )

        assert (first, second) == (1, 0)
        assert len(fake.calls_of("close_pull_request")) == 1

    async def test_terminal_run_without_a_pr_is_a_no_op(self, db, fake):
        service = make_service(db, fake)
        await start(service)  # never approved — no Draft PR exists

        closed = await service.close_superseded_draft_prs(
            project_id=PROJECT_ID, issue_number=ISSUE, successor_run_id="b" * 32
        )

        assert closed == 0
        assert fake.calls_of("close_pull_request") == []


# ----------------------------------------------------------------------
# A02: the executable spec v3 on the GitHub path
# ----------------------------------------------------------------------


class CountingStackPlanner:
    """Plan-call recorder for the A13 gate: counts, records, answers stub."""

    def __init__(self) -> None:
        self.calls = 0
        self.path_scopes: list[list[str] | None] = []

    async def plan(self, issue_title, issue_description, *, flow_run_id=None, path_scope=None):
        self.calls += 1
        self.path_scopes.append(path_scope)
        return "## Implementation plan\n\n- create the thing\n"


class TestCurrentPlanSelectionB11:
    """B11: the task-aware selection is compiled against the CURRENT plan —
    a planner object reused across runs must never leak a PREVIOUS run's
    last_plan into this run's frozen selection."""

    class ProposingPlanner(CountingStackPlanner):
        def __init__(self, proposals: list[dict | None]) -> None:
            super().__init__()
            self._proposals = proposals

        async def plan(self, issue_title, issue_description, *, flow_run_id=None, path_scope=None):
            plan = await super().plan(
                issue_title, issue_description, flow_run_id=flow_run_id, path_scope=path_scope
            )
            proposal = self._proposals.pop(0) if self._proposals else None
            # A real planner always re-points last_plan at the plan it just
            # made (the proposal keys ride along when present).
            self.last_plan = {"text": plan, **(proposal or {})}
            return plan

    async def test_previous_last_plan_never_leaks_into_the_next_selection(self, db, fake):
        planner = self.ProposingPlanner(
            [
                {"harness": "opencode", "budget_class": "trivial"},  # run 1's proposal
                None,  # run 2: NO proposal
            ]
        )
        fake.seed_repo(REPO, {"src/app.py": "x\n"})
        service = make_service(db, fake, stack=make_stack(fake, planner=planner))

        issue2 = ISSUE + 1
        fake.seed_issue(REPO, issue2, ISSUE_TITLE, ISSUE_DESC)
        run1 = await start(service)
        await go(service, run1)
        run2 = await service.start_run(
            project_id=PROJECT_ID,
            issue_number=issue2,
            issue_title=ISSUE_TITLE,
            issue_description=ISSUE_DESC,
            author_username="alice",
        )  # same REUSED planner; last_plan still run 1's
        assert run2 != run1
        await go(service, run2)

        for run_id, expect_lite in ((run1, True), (run2, False)):
            async with db() as session:
                spec = (
                    (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                    .scalars()
                    .one()
                )
            budget_class = spec.document["backend_config"]["budget_class"]
            if expect_lite:
                assert budget_class == "trivial", run_id  # run 1: its OWN proposal
            else:
                assert budget_class == "standard", run_id  # run 2: NOT run 1's leak


class TestConfigGateA13:
    """A13: an unreadable/invalid `.forge.yml` parks the run — scope never
    widens, nothing is paid while the reconciler retries the read."""

    RESTRICTED = "implement:\n  paths:\n    - 'services/**'\n"

    @pytest.fixture(autouse=True)
    def _clear_config_cache(self):
        clear_cache()
        yield
        clear_cache()

    def arm_403(self, fake: FakeGitHub, *, restore: bool = False):
        """403 every `.forge.yml` read until disarmed — routed through the
        fake's typed ``read_blob`` like a real GitHub 403. Returns the
        original bound method for disarming."""
        original = fake.get_file

        async def flaky(project_id, file_path, ref="HEAD"):
            if file_path == ".forge.yml":
                raise GitHubAPIError(403, "read forbidden")
            return await original(project_id, file_path, ref)

        if restore:
            fake.get_file = original
            return original
        fake.get_file = flaky
        return original

    async def test_403_parks_config_unreadable_with_zero_paid_calls(self, db, fake):
        fake.seed_repo(REPO, {".forge.yml": self.RESTRICTED})
        self.arm_403(fake)
        planner = CountingStackPlanner()
        service = make_service(db, fake, stack=make_stack(fake, planner=planner))

        run_id = await start(service)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("config_unreadable:")
        assert planner.calls == 0
        assert fake.calls_of("create_draft_pr") == []
        assert fake.calls_of("create_commit_on_branch") == []
        assert run.evidence["config_block"]["issue_title"] == ISSUE_TITLE
        assert any("config_unreadable" in body for body in comments(fake))

    async def test_malformed_yaml_parks_config_invalid(self, db, fake):
        fake.seed_repo(REPO, {".forge.yml": "not: a: valid: [[["})
        planner = CountingStackPlanner()
        service = make_service(db, fake, stack=make_stack(fake, planner=planner))

        run_id = await start(service)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("config_invalid:")
        assert planner.calls == 0

    async def test_confirmed_404_freezes_absence_provenance(self, db, fake):
        planner = CountingStackPlanner()
        service = make_service(db, fake, stack=make_stack(fake, planner=planner))

        run_id = await start(service)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert planner.calls == 1
        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .one()
            )
        assert "allowed_paths" not in spec.document
        assert spec.document["project_config"]["status"] == "confirmed_absent"

    async def test_valid_config_freezes_scope_and_provenance(self, db, fake):
        fake.seed_repo(REPO, {".forge.yml": self.RESTRICTED})
        planner = CountingStackPlanner()
        service = make_service(db, fake, stack=make_stack(fake, planner=planner))

        run_id = await start(service)

        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .one()
            )
        assert spec.document["allowed_paths"] == ["services/**"]
        provenance = spec.document["project_config"]
        assert provenance["status"] == "valid"
        assert provenance["sha256"] == hashlib.sha256(self.RESTRICTED.encode()).hexdigest()
        assert planner.path_scopes == [["services/**"]]

    async def test_recovery_re_enters_planning_after_the_read_recovers(self, db, fake):
        fake.seed_repo(REPO, {".forge.yml": self.RESTRICTED})
        original = self.arm_403(fake)
        planner = CountingStackPlanner()
        service = make_service(db, fake, stack=make_stack(fake, planner=planner))
        run_id = await start(service)
        assert (await get_run(db, run_id)).status == FlowStatus.BLOCKED.value
        assert planner.calls == 0

        fake.get_file = original  # the config is readable again
        await service.evaluate_config_recovery()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert planner.calls == 1
        assert planner.path_scopes == [["services/**"]]

    async def test_a_repo_bound_recovery_never_touches_another_repository(self, db, fake):
        """B08: the service's adapters are bound to ONE repo. A
        config-blocked run of repo B must not be recovered (read, re-planned,
        commented) through repo A's service — two SEPARATE fakes, one per
        repository, prove the routing."""
        fake.seed_repo(REPO, {".forge.yml": self.RESTRICTED})
        other_repo = "acme/other-repo"
        fake_b = FakeGitHub()
        fake_b.seed_repo(other_repo, {".forge.yml": self.RESTRICTED})
        fake_b.seed_issue(other_repo, ISSUE, ISSUE_TITLE, ISSUE_DESC)
        fake_b.heads[other_repo]["main"] = BASE_HEAD

        planner_a = CountingStackPlanner()
        planner_b = CountingStackPlanner()
        service_a = make_service(db, fake, stack=make_stack(fake, planner=planner_a))

        # Drive repo B's run into config-block THROUGH ITS OWN service.
        original_b = self.arm_403(fake_b)
        service_b = make_service(
            db,
            fake_b,
            stack=make_stack(fake_b, planner=planner_b),
            repo=other_repo,
        )
        run_b = await start(service_b)
        assert (await get_run(db, run_b)).status == FlowStatus.BLOCKED.value

        # The 403 recovers on B's client only.
        fake_b.get_file = original_b

        # A's recovery pass must not touch B's run: separate adapters.
        await service_a.evaluate_config_recovery()
        run = await get_run(db, run_b)
        assert run.status == FlowStatus.BLOCKED.value  # untouched
        assert planner_a.calls == 0
        assert planner_b.calls == 0

        # B's own recovery pass resumes it.
        await service_b.evaluate_config_recovery()
        run = await get_run(db, run_b)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert planner_b.calls == 1

    async def test_still_failing_read_leaves_the_run_parked(self, db, fake):
        fake.seed_repo(REPO, {".forge.yml": self.RESTRICTED})
        planner = CountingStackPlanner()
        service = make_service(db, fake, stack=make_stack(fake, planner=planner))
        self.arm_403(fake)
        run_id = await start(service)
        assert (await get_run(db, run_id)).status == FlowStatus.BLOCKED.value

        await service.evaluate_config_recovery()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("config_unreadable:")
        assert planner.calls == 0


class TestExecutableSpecA02:
    """The GitHub lane freezes and consumes the SAME executable spec v3 as
    the GitLab path: post-/go settings changes never alter execution, the
    budget ceilings come from the R13 profiles, an un-onboarded driver is
    never selected, and a legacy v2 spec parks re-approval-required."""

    async def test_settings_drift_after_freeze_never_moves_the_spec(self, db, fake):
        """The document (and its digest) frozen at plan acceptance is the
        approved input — mutating the live settings afterwards moves
        nothing."""
        settings = make_settings(
            FORGE_REQUIRED_JOBS="pytest",
            FORGE_MAX_COMMIT_CYCLES=2,
            FORGE_HARNESS_PREFERENCE="grok-build,claude-code",
        )
        service = make_service(db, fake, settings=settings)
        run_id = await start(service)
        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .one()
            )
            session.expunge(spec)
        frozen_digest = spec.digest
        frozen_document = dict(spec.document)

        settings.FORGE_REQUIRED_JOBS = ""
        settings.FORGE_MAX_COMMIT_CYCLES = 9
        settings.FORGE_HARNESS_PREFERENCE = "claude-code"
        settings.FORGE_HARNESS_MODEL = "post-gate-model"

        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .one()
            )
        assert spec.document == frozen_document
        assert spec.digest == frozen_digest
        assert spec.document["verification"]["required_jobs"] == ["pytest"]
        assert spec.document["budgets"]["commit_cycles"] == 2
        assert spec.document["backend_config"]["harness"] == "grok-build"

    async def test_unavailable_driver_is_never_selected(self, db, fake):
        """R31 manifest: a driver the project did not onboard is dropped
        from the frozen chain — not selected by the preference."""
        settings = make_settings(
            FORGE_HARNESS_PREFERENCE="grok-build,claude-code",
            FORGE_AVAILABLE_DRIVERS='["claude-code"]',
        )
        service = make_service(db, fake, settings=settings)

        run_id = await start(service)

        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .one()
            )
        backend = spec.document["backend_config"]
        assert backend["harness"] == "claude-code"
        assert backend["harness_fallbacks"] == []  # grok-build is not onboarded

    async def test_budget_profile_freezes_numeric_ceilings(self, db, fake):
        """R13: the class's numeric profile resolves AT FREEZE TIME and
        rides in the spec's budgets block with its honest enforcement
        level (full — the builtin lane intercepts every call)."""
        settings = make_settings(
            FORGE_BUDGET_PROFILES='{"standard": {"max_calls": 40, "max_tokens": 500000,'
            ' "wallclock_s": 3600}}',
        )
        service = make_service(db, fake, settings=settings)

        run_id = await start(service)

        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .one()
            )
            run = await session.get(FlowRun, run_id)
            budgets = (
                (await session.execute(select(RunBudget).where(RunBudget.run_id == run_id)))
                .scalars()
                .one()
            )
        assert spec.document["budgets"] == {
            "commit_cycles": 3,
            "harness_timeout": make_settings().FORGE_HARNESS_TIMEOUT_SECONDS,
            "max_calls": 40,
            "max_tokens": 500000,
            "wallclock_s": 3600,
            "enforcement": "full",
        }
        # The budget row was opened BEFORE the first paid call and bound to
        # the frozen spec digest.
        assert budgets.max_calls == 40
        assert budgets.spec_digest == run.spec_digest
        assert (run.evidence or {})["budget"]["enforcement"] == "full"

    async def test_legacy_v2_spec_parks_reapproval_required(self, db, fake):
        """A02 legacy policy: a run whose stored spec is v2 (created
        pre-upgrade) is never silently executed as v3 — the next dispatch
        leg parks blocked(spec_legacy: re-approval required)."""
        service = make_service(db, fake)
        run_id = await start(service)
        await go(service, run_id)  # the gate consumed the v3 spec digest
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_CI.value
        # Rewrite the row into exactly what the pre-upgrade lane stored: a
        # v2-shaped, digest-consistent document.
        v2_document = {
            "subject": {
                "provider": "github",
                "repo_full_name": REPO,
                "project_id": PROJECT_ID,
                "issue_iid": ISSUE,
            },
            "source_base_oid": BASE_HEAD,
            "plan_digest": (await get_run(db, run_id)).plan_digest,
            "task_digest": task_digest_of(ISSUE_TITLE, ISSUE_DESC),
            "policy_digest": service._policy_digest(),
            "backend_config": {
                "backend": "builtin",
                "model": make_settings().FORGE_HARNESS_MODEL,
                "target_branch": "main",
                "harness": "claude-code",
                "harness_fallbacks": [],
                "budget_class": "standard",
                "selection_reason": "default",
            },
            "budgets": {"commit_cycles": 3, "harness_timeout": 1800},
        }
        v2_digest = hashlib.sha256(
            json.dumps(v2_document, sort_keys=True).encode("utf-8")
        ).hexdigest()
        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .one()
            )
            spec.schema_version = 2
            spec.document = v2_document
            spec.digest = v2_digest
            run = await session.get(FlowRun, run_id)
            run.spec_digest = v2_digest
            await session.commit()
        clear_comments(fake)

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("spec_legacy")
        assert "re-approval required" in (run.status_reason or "")


# ----------------------------------------------------------------------
# A01: the positive-proof verification contract (GitHub gate)
# ----------------------------------------------------------------------


def workflow_run(
    sha: str,
    name: str,
    conclusion: str | None,
    *,
    status: str = "completed",
    path: str | None = None,
    run_id: int | None = None,
    attempt: int | None = None,
) -> dict:
    """One Actions workflow-run payload shaped like the runs-list answer."""
    run: dict = {"head_sha": sha, "name": name, "status": status, "conclusion": conclusion}
    if path is not None:
        run["path"] = path
    if run_id is not None:
        run["id"] = run_id
    if attempt is not None:
        run["run_attempt"] = attempt
    return run


class PRHeadMover(StubPRReviewer):
    """A reviewer during whose (in-flight) run a human push lands — the
    PR's head sha moves past the reviewed candidate mid-review."""

    def __init__(self, fake: FakeGitHub, new_head: str) -> None:
        super().__init__()
        self._fake = fake
        self._new_head = new_head

    async def review(self, **kwargs):
        for pr in self._fake.pull_requests.get(REPO, []):
            pr["head"]["sha"] = self._new_head
        return await super().review(**kwargs)


async def drive_to_waiting_ci(db, service: GitHubRunService, fake: FakeGitHub) -> tuple[str, str]:
    """start_run → /go on the builtin lane → the run parked in waiting_ci.

    Returns (run_id, candidate_sha)."""
    run_id = await start(service)
    clear_comments(fake)
    await go(service, run_id)
    run = await get_run(db, run_id)
    assert run.status == FlowStatus.WAITING_CI.value
    return run_id, run.candidate_shas[-1]


class TestPositiveVerification:
    """A01: verified=True is POSITIVE PROOF — every REQUIRED check from the
    FROZEN spec list must be present with an explicitly successful
    conclusion for the exact candidate sha. Green optional checks never
    substitute; skipped/neutral are unknown (the FORGE_VERIFICATION_WAIVE_
    CONCLUSIONS policy waiver is the only bridge); cancelled/timed_out are
    infrastructure and never repair; a human push during the LLM review
    supersedes the candidate — never READY."""

    async def test_required_absent_with_a_green_docs_workflow_never_verifies(self, db, fake):
        """A01 AC1: the required `tests` check never ran; a green
        `documentation` workflow proves nothing — the run keeps waiting
        with an honest unknown verdict (it never verifies, never repairs,
        never reviews)."""
        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        fake.seed_workflow_runs([workflow_run(candidate, "documentation", "success")])
        reviewer = StubPRReviewer()
        service._stack = make_stack(fake, reviewer=reviewer)

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value  # unknown — keep waiting
        verification = (run.evidence or {})["verification"]
        assert verification["status"] == "unknown"
        assert verification["tested_oid"] == candidate
        assert "tests" in verification["summary"]
        assert verification["surface"][0]["name"] == "documentation"
        assert verification["surface"][0]["conclusion"] == "success"
        assert verification["surface"][0]["workflow"]  # B02: identity rides the surface
        assert reviewer.calls == []  # the gate never let it reach review

    async def test_repeated_unknown_observations_never_extend_the_deadline(self, db, fake):
        """B01: every observation merges evidence and bumps updated_at —
        the deadline must anchor to the verification EPOCH, not updated_at
        (the review's probe: 480 polls / 7200s never timed out)."""
        settings = make_settings(
            FORGE_REQUIRED_JOBS="tests", FORGE_VERIFICATION_TIMEOUT_SECONDS=600
        )
        service = make_service(db, fake, settings=settings)
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        # The required check exists but never concludes successfully.
        fake.seed_workflow_runs([workflow_run(candidate, "tests", "skipped")])

        # Many observation passes — each merges fresh evidence (and slides
        # updated_at). Simulate by evaluating with a fixed now, twice.
        for _ in range(3):
            await service.evaluate_waiting_ci_one(run_id, now=datetime.now(timezone.utc))
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        epoch = (run.evidence or {})["verification_epoch"]

        # Well past the epoch deadline — must block regardless of how many
        # observations happened since (epoch.started_at never moved).
        far = datetime.now(timezone.utc) + timedelta(seconds=601)
        await service.evaluate_waiting_ci_one(run_id, now=far)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "verification_timeout" in (run.status_reason or "")
        # and the epoch survived the observations untouched
        assert (run.evidence or {})["verification_epoch"] == epoch

    async def test_an_old_rerun_success_never_masks_a_newer_failure(self, db, fake):
        """B02: run_attempt counts attempts WITHIN one run — it must never
        order ACROSS runs. Old run #10 re-executed to attempt 3 (success)
        vs newer run #11 attempt 1 (failure): the newer run is the
        authoritative occurrence; verified must not happen."""
        settings = make_settings(FORGE_REQUIRED_JOBS="tests")
        service = make_service(db, fake, settings=settings)
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        fake.seed_workflow_runs(
            [
                workflow_run(
                    candidate,
                    "tests",
                    "success",
                    path=".github/workflows/tests.yml",
                    run_id=10,
                    attempt=3,
                ),
                workflow_run(
                    candidate,
                    "tests",
                    "failure",
                    path=".github/workflows/tests.yml",
                    run_id=11,
                    attempt=1,
                ),
            ]
        )

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        # the newer FAILURE is code-failure evidence — the repair loop (or
        # its exhaustion) fires on `tests`; NEVER verified-ready.
        assert run.status != FlowStatus.READY_FOR_HUMAN.value
        assert "tests" in (run.status_reason or "") or run.status == FlowStatus.PROPOSING.value

    @pytest.mark.parametrize("order", ["new-first", "old-first"])
    async def test_the_api_response_order_never_decides_the_outcome(self, db, fake, order):
        """B02: reversed API order must not change which run is
        authoritative — same seeding, both orders, same verdict."""
        settings = make_settings(FORGE_REQUIRED_JOBS="tests")
        if True:
            fake2 = FakeGitHub()
            fake2.seed_repo(REPO, {"src/app.py": "print('hi')\n"})
            fake2.heads[REPO]["main"] = BASE_HEAD
            fake2.seed_issue(REPO, ISSUE, ISSUE_TITLE, ISSUE_DESC)
            service = make_service(db, fake2, settings=settings)
            run_id, candidate = await drive_to_waiting_ci(db, service, fake2)
            runs = [
                workflow_run(
                    candidate,
                    "tests",
                    "success",
                    path=".github/workflows/tests.yml",
                    run_id=10,
                    attempt=2,
                ),
                workflow_run(
                    candidate,
                    "tests",
                    "success",
                    path=".github/workflows/tests.yml",
                    run_id=11,
                    attempt=1,
                ),
            ]
            if order == "old-first":
                runs.reverse()
            fake2.seed_workflow_runs(runs)

            await service.evaluate_waiting_ci_one(run_id)

            run = await get_run(db, run_id)
            assert run.status == FlowStatus.READY_FOR_HUMAN.value, order
            verification = (run.evidence or {})["verification"]
            assert verification["surface"][0]["run_id"] == 11, order

    async def test_two_workflows_sharing_a_name_do_not_overwrite_each_other(self, db, fake):
        """B02: display names collide — observations merge by workflow
        IDENTITY. A same-named decoy workflow's conclusion must not replace
        the required workflow's (the newer RUN wins the display key, and
        both identities ride the surface)."""
        settings = make_settings(FORGE_REQUIRED_JOBS="tests")
        service = make_service(db, fake, settings=settings)
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        fake.seed_workflow_runs(
            [
                workflow_run(
                    candidate,
                    "tests",
                    "failure",
                    path=".github/workflows/tests.yml",
                    run_id=30,
                    attempt=1,
                ),
                # same DISPLAY name, different workflow (decoy), older run
                workflow_run(
                    candidate,
                    "tests",
                    "success",
                    path=".github/workflows/decoy.yml",
                    run_id=29,
                    attempt=1,
                ),
            ]
        )

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        # the required workflow's failure drives repair — never READY
        assert run.status != FlowStatus.READY_FOR_HUMAN.value

    async def test_a_newer_same_named_success_never_masks_an_older_failure(self, db, fake):
        """C01, the INVERTED collision: the reviewer's counterexample — the
        successful same-named workflow is the NEWER run, so the old
        name-collapse picked its success over the failed required workflow.
        Identity disagrees → ambiguous_check_identity → never verified."""
        settings = make_settings(FORGE_REQUIRED_JOBS="tests")
        service = make_service(db, fake, settings=settings)
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        fake.seed_workflow_runs(
            [
                workflow_run(
                    candidate,
                    "tests",
                    "failure",
                    path=".github/workflows/tests.yml",
                    run_id=30,
                    attempt=1,
                ),
                # same DISPLAY name, DIFFERENT workflow, NEWER run — success
                workflow_run(
                    candidate,
                    "tests",
                    "success",
                    path=".github/workflows/decoy.yml",
                    run_id=99,
                    attempt=1,
                ),
            ]
        )

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status != FlowStatus.READY_FOR_HUMAN.value  # never verified
        verification = (run.evidence or {}).get("verification") or {}
        assert "ambiguous_check_identity" in str(verification.get("summary", ""))

    async def test_agreeing_same_named_workflows_are_not_ambiguous(self, db, fake):
        """C01 boundary: identities that AGREE on the conclusion are not an
        ambiguity — any occurrence proves the same fact."""
        settings = make_settings(FORGE_REQUIRED_JOBS="tests")
        service = make_service(db, fake, settings=settings)
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        fake.seed_workflow_runs(
            [
                workflow_run(
                    candidate,
                    "tests",
                    "success",
                    path=".github/workflows/tests.yml",
                    run_id=30,
                    attempt=1,
                ),
                workflow_run(
                    candidate,
                    "tests",
                    "success",
                    path=".github/workflows/mirror.yml",
                    run_id=99,
                    attempt=1,
                ),
            ]
        )

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value

    async def test_a_stale_pending_optional_run_never_holds_required_checks(self, db, fake):
        """C01: pending is decided over the authoritative occurrences and
        the PROOF set — an optional workflow stuck pending cannot hold
        completed required checks hostage."""
        settings = make_settings(FORGE_REQUIRED_JOBS="tests")
        service = make_service(db, fake, settings=settings)
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        fake.seed_workflow_runs(
            [
                workflow_run(
                    candidate,
                    "tests",
                    "success",
                    path=".github/workflows/tests.yml",
                    run_id=10,
                    attempt=1,
                ),
                workflow_run(
                    candidate,
                    "docs",
                    None,
                    path=".github/workflows/docs.yml",
                    run_id=11,
                    attempt=1,
                    status="in_progress",
                ),
            ]
        )

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value  # required proven

    async def test_a_pending_required_run_still_waits(self, db, fake):
        """C01 inverse: a pending run whose name IS required still holds
        the verdict (completeness over the proof set)."""
        settings = make_settings(FORGE_REQUIRED_JOBS="tests")
        service = make_service(db, fake, settings=settings)
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        fake.seed_workflow_runs(
            [
                workflow_run(
                    candidate,
                    "tests",
                    None,
                    path=".github/workflows/tests.yml",
                    run_id=10,
                    attempt=1,
                    status="queued",
                ),
            ]
        )

        await service.evaluate_waiting_ci_one(run_id)
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_CI.value

    async def test_a_class_change_freezes_one_set_of_numbers_everywhere(self, db, fake):
        """C02: planner moves standard→trivial post-plan; the selection pin,
        the frozen RunSpec ceilings and the OPENED RunBudget must all carry
        the SAME numbers (the opened ones) — the spec builder must not
        re-resolve the trivial profile."""
        from forge.durable.budgets import budget_for_run

        limits = {"max_calls": 100, "max_tokens": 500000, "wallclock_s": 3600}
        settings = make_settings(
            FORGE_BUDGET_PROFILES='{"standard": {"max_calls": 100, "max_tokens": 500000, "wallclock_s": 3600}, "trivial": {"max_calls": 5, "max_tokens": 1000, "wallclock_s": 60}}'
        )
        planner = TestCurrentPlanSelectionB11.ProposingPlanner(
            [{"harness": "claude-code", "budget_class": "trivial"}]
        )
        fake.seed_repo(REPO, {"src/app.py": "x\n"})
        service = make_service(db, fake, settings=settings, stack=make_stack(fake, planner=planner))
        run_id = await start(service)

        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .one()
            )
            budget = await budget_for_run(session, run_id)
        doc = spec.document
        assert doc["budgets"]["max_calls"] == limits["max_calls"]  # the OPENED row
        assert doc["budgets"]["max_tokens"] == limits["max_tokens"]
        assert doc["budgets"]["wallclock_s"] == limits["wallclock_s"]
        assert budget is not None and budget.max_calls == limits["max_calls"]
        # the selection evidence records the pin honestly
        ev = (await get_run(db, run_id)).evidence["harness_selection"]
        assert "budget pinned" in str(ev.get("selection_reason") or ev.get("reason") or "")

    async def test_unproven_required_still_blocks_on_the_verification_deadline(self, db, fake):
        """Unknown is a WAITING verdict — the R17 deadline is the bound: a
        required check that never registers parks verification_timeout."""
        settings = make_settings(
            FORGE_REQUIRED_JOBS="tests", FORGE_VERIFICATION_TIMEOUT_SECONDS=600
        )
        service = make_service(db, fake, settings=settings)
        run_id, _candidate = await drive_to_waiting_ci(db, service, fake)
        fake.seed_workflow_runs([workflow_run("d" * 40, "documentation", "success")])
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            # B01: the deadline reads the verification EPOCH, not updated_at
            # (evidence merges slide updated_at — the bug this pins).
            started = (datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat()
            run.evidence = dict(run.evidence or {}) | {
                "verification_epoch": {"candidate_sha": _candidate, "started_at": started}
            }
            await session.commit()

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("verification_timeout")

    async def test_skipped_required_check_is_unknown_without_a_waiver(self, db, fake):
        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        fake.seed_workflow_runs([workflow_run(candidate, "tests", "skipped")])

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        verification = (run.evidence or {})["verification"]
        assert verification["status"] == "unknown"
        assert verification["surface"][0]["name"] == "tests"
        assert verification["surface"][0]["conclusion"] == "skipped"
        assert verification["surface"][0]["workflow"]

    async def test_neutral_required_check_is_unknown(self, db, fake):
        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        fake.seed_workflow_runs([workflow_run(candidate, "tests", "neutral")])

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        assert (run.evidence or {})["verification"]["status"] == "unknown"

    async def test_waiver_config_flips_a_skipped_required_to_verified(self, db, fake):
        """FORGE_VERIFICATION_WAIVE_CONCLUSIONS is the explicit deployment
        policy that lets a skipped required check count as satisfied."""
        settings = make_settings(
            FORGE_REQUIRED_JOBS="tests",
            FORGE_VERIFICATION_WAIVE_CONCLUSIONS="skipped,neutral",
        )
        service = make_service(db, fake, settings=settings)
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        fake.seed_workflow_runs([workflow_run(candidate, "tests", "skipped")])

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        verification = (run.evidence or {})["verification"]
        assert verification["status"] == "passed"
        assert verification["tested_oid"] == candidate  # the provider-verified sha
        assert run.status_reason == "checks passed; merge is a human decision"

    @pytest.mark.parametrize("conclusion", ["cancelled", "timed_out"])
    async def test_cancelled_or_timed_out_is_infrastructure_never_repair(
        self, db, fake, conclusion
    ):
        """A01 AC3: a cancelled/timed_out workflow run is evidence the
        EXECUTION died — the run parks as infrastructure and the repair
        budget is never spent on it."""
        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        fake.seed_workflow_runs([workflow_run(candidate, "tests", conclusion)])
        reviewer = StubPRReviewer()
        service._stack = make_stack(fake, reviewer=reviewer)

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("verification_infrastructure")
        assert "tests" in (run.status_reason or "")  # the offending check is named
        verification = (run.evidence or {})["verification"]
        assert verification["status"] == "unknown"
        assert reviewer.calls == []  # no review, no repair, no ready

    async def test_human_push_during_review_supersedes_the_candidate(self, db, fake):
        """A01 AC4: a push that lands while the LLM review is in flight
        invalidates the candidate-specific result — superseded evidence,
        never READY (the F19 parity of the GitLab leg)."""
        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        fake.seed_workflow_runs([workflow_run(candidate, "tests", "success")])
        moved_head = "9" * 40
        service._stack = make_stack(fake, reviewer=PRHeadMover(fake, moved_head))

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason.startswith("candidate_drift_after_review")
        verification = (run.evidence or {})["verification"]
        assert verification["status"] == "unknown"
        assert verification["tested_oid"] == moved_head
        assert "superseded" in verification["summary"]
        assert len(service._stack.reviewer.calls) == 1  # the review DID run

    async def test_cancel_during_review_stands_the_ready_down(self, db, fake):
        """The cancellation generation is re-read with the fresh head: a
        cancel that lands during the review revokes the READY."""
        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        fake.seed_workflow_runs([workflow_run(candidate, "tests", "success")])

        original_review = service._stack.reviewer.review

        async def cancelling_review(**kwargs):
            # the cancel lands while the review is in flight
            async with db() as session:
                run = await session.get(FlowRun, run_id)
                run.cancel_requested = True
                await session.commit()
            return await original_review(**kwargs)

        service._stack.reviewer.review = cancelling_review  # type: ignore[method-assign]

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.REVIEWING.value  # stood down — never READY

    async def test_a_waiver_flipped_after_approval_never_loosens_the_run(self, db, fake):
        """B06: approving without waivers freezes the proof rules — a
        later global FORGE_VERIFICATION_WAIVE_CONCLUSIONS flip must not
        let this run's skipped check pass."""
        settings = make_settings(FORGE_REQUIRED_JOBS="tests")
        service = make_service(db, fake, settings=settings)
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        fake.seed_workflow_runs([workflow_run(candidate, "tests", "skipped")])
        await service.evaluate_waiting_ci_one(run_id)
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_CI.value

        # the operator flips the global waiver AFTER approval
        service._settings = make_settings(
            FORGE_REQUIRED_JOBS="tests", FORGE_VERIFICATION_WAIVE_CONCLUSIONS="skipped"
        )
        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value  # still unproven
        assert (run.evidence or {})["verification"]["status"] == "unknown"

    async def test_required_list_comes_from_the_frozen_spec_not_live_settings(self, db, fake):
        """A02 integration: the proof set is the spec's frozen
        `required_jobs` — mutating FORGE_REQUIRED_JOBS after the freeze
        cannot weaken the gate."""
        settings = make_settings(FORGE_REQUIRED_JOBS="tests")
        service = make_service(db, fake, settings=settings)
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .one()
            )
        assert spec.document["verification"]["required_jobs"] == ["tests"]
        # the post-gate settings drift that A01 must be immune to:
        settings.FORGE_REQUIRED_JOBS = ""
        fake.seed_workflow_runs([workflow_run(candidate, "documentation", "success")])

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value  # the FROZEN list enforced
        assert (run.evidence or {})["verification"]["status"] == "unknown"

    async def test_verified_surface_records_the_native_run_identity(self, db, fake):
        """A01: the surface carries the FULL check identity — the native
        run id and the attempt that produced the conclusion."""
        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        fake.seed_workflow_runs(
            [
                workflow_run(
                    candidate,
                    "tests",
                    "success",
                    path=".github/workflows/tests.yml",
                    run_id=777,
                    attempt=2,
                )
            ]
        )

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        verification = (run.evidence or {})["verification"]
        assert verification["surface"] == [
            {
                "name": "tests",
                "conclusion": "success",
                "workflow": "path:.github/workflows/tests.yml",
                "run_id": 777,
                "attempt": 2,
            }
        ]

    async def test_harness_lane_is_excluded_by_path_not_display_name(self, db, fake):
        """A01 identity rule: the harness lane is excluded by the workflow
        PATH encoding the spec-frozen FILENAME — a harness run whose
        display NAME differs from the filename is still excluded, and its
        green conclusion never substitutes for a required check."""
        settings = make_settings(
            FORGE_REQUIRED_JOBS="tests",
            FORGE_GITHUB_HARNESS_WORKFLOW=WORKFLOW,
        )
        service = make_service(db, fake, settings=settings)
        run_id = await start(service)
        clear_comments(fake)
        await go(service, run_id)  # the frozen lane dispatch → waiting_harness
        branch = f"forge/{ISSUE}/{run_id[:8]}"
        candidate = "a" * 40
        fake.seed_commit(REPO, branch, candidate, "lane candidate")
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.WAITING_CI.value
            run.candidate_shas = [candidate]
            await session.commit()
        # The harness ran for the candidate sha and "succeeded" — but it is
        # EXECUTION, not verification. Its display name is NOT the filename.
        fake.seed_workflow_runs(
            [
                workflow_run(
                    candidate,
                    "Forge Harness",
                    "success",
                    path=f".github/workflows/{WORKFLOW}",
                ),
                workflow_run(candidate, "tests", "success", path=".github/workflows/tests.yml"),
            ]
        )

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        verification = (run.evidence or {})["verification"]
        assert verification["status"] == "passed"
        # Only the independent check is on the surface — the harness is gone.
        assert verification["surface"][0]["name"] == "tests"
        assert verification["surface"][0]["conclusion"] == "success"
        assert verification["surface"][0]["workflow"] == "path:.github/workflows/tests.yml"

    async def test_a_display_name_decoy_is_never_treated_as_the_harness(self, db, fake):
        """The OLD name-based exclusion dropped any run whose display name
        equaled the harness FILENAME — a decoy workflow named exactly that
        masked the whole verification surface. Exclusion is by PATH: the
        decoy is an (insufficient, optional) observation, so the missing
        required check keeps the run waiting — never not_configured-ready."""
        settings = make_settings(
            FORGE_REQUIRED_JOBS="tests",
            FORGE_GITHUB_HARNESS_WORKFLOW=WORKFLOW,
        )
        service = make_service(db, fake, settings=settings)
        run_id = await start(service)
        clear_comments(fake)
        await go(service, run_id)
        branch = f"forge/{ISSUE}/{run_id[:8]}"
        candidate = "b" * 40
        fake.seed_commit(REPO, branch, candidate, "lane candidate")
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.WAITING_CI.value
            run.candidate_shas = [candidate]
            await session.commit()
        # A decoy whose DISPLAY NAME equals the frozen filename but whose
        # path is an unrelated workflow, and it is green.
        fake.seed_workflow_runs(
            [
                workflow_run(
                    candidate,
                    WORKFLOW,
                    "success",
                    path=".github/workflows/docs.yml",
                ),
            ]
        )

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        # Old behavior: the decoy was excluded "as the harness" → empty
        # surface → not_configured → READY. New behavior: the decoy is an
        # observation; `tests` is unproven → waiting with unknown.
        assert run.status == FlowStatus.WAITING_CI.value
        verification = (run.evidence or {})["verification"]
        assert verification["status"] == "unknown"
        assert verification["surface"][0]["name"] == WORKFLOW
        assert verification["surface"][0]["conclusion"] == "success"
        assert verification["surface"][0]["workflow"] == "path:.github/workflows/docs.yml"

    async def test_code_failure_on_a_required_check_still_enters_repair(self, db, fake):
        """The proof contract keeps ADR-0008: a conclusion that blames the
        change on a required check is the ONLY repair trigger."""
        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        fake.seed_workflow_runs(
            [
                workflow_run(candidate, "tests", "failure"),
                workflow_run(candidate, "documentation", "success"),
            ]
        )

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        # The frozen builtin lane has no repair dispatch — it blocks with
        # the honest quality_contract reason naming the failed check.
        assert run.status == FlowStatus.BLOCKED.value
        assert "quality_contract" in (run.status_reason or "")
        assert "tests" in (run.status_reason or "")


def test_gateway_commands_are_reachable_through_the_dispatch_guard():
    """Regression (LIVE-found): /retry was accepted by the gateway, routed
    below, and then silently dropped by the dispatch GUARD set that did not
    list it — dead code behind a rejection, invisible to handler tests.
    Every command the gateway can emit must appear in the service's
    dispatch guard."""
    import inspect
    import re

    from forge.gateway import github_webhook
    from forge.runs import github_service

    # The mapping VALUES are the emitted command names; security_triage is
    # consumed upstream in execute_run_command (runs/service.py) and never
    # reaches the provider dispatch.
    gateway_cmds = set(
        re.findall(r'"/[a-z_]+":\s*"([a-z_]+)"', inspect.getsource(github_webhook))
    ) - {"security_triage"}
    dispatch_src = inspect.getsource(github_service)
    guard = re.search(r"command not in \{([^}]+)\}", dispatch_src)
    assert guard, "dispatch guard not found"
    for cmd in gateway_cmds:
        assert f'"{cmd}"' in guard.group(1), (
            f"gateway command {cmd} is rejected by the dispatch guard"
        )


def test_azure_gateway_commands_are_reachable_through_the_dispatch_guard():
    import inspect
    import re

    from forge.gateway import azure_webhook
    from forge.runs import azure_service

    gateway_cmds = set(
        re.findall(r'"/[a-z_]+":\s*"([a-z_]+)"', inspect.getsource(azure_webhook))
    ) - {"security_triage"}
    dispatch_src = inspect.getsource(azure_service)
    guard = re.search(r"command not in \{([^}]+)\}", dispatch_src)
    assert guard, "dispatch guard not found"
    for cmd in gateway_cmds:
        assert f'"{cmd}"' in guard.group(1), (
            f"gateway command /{cmd} is rejected by the Azure dispatch guard"
        )


def test_provider_command_sets_stay_in_parity():
    """R27 guard: every feature command must be dispatched by ALL THREE
    provider services unless explicitly whitelisted. LIVE-found twice:
    /retry shipped with a missing Azure guard entry, and the #29 lifecycle
    commands (issue_edited/unlabeled) shipped GitHub-only despite the
    issue's mirror promise. A deviation here must carry a reason in the
    whitelist below — otherwise this test fails until parity is restored."""
    import inspect
    import re

    from forge.runs import azure_service, github_service, service

    def dispatched_commands(module_src: str) -> set[str]:
        return set(re.findall(r'command == "([a-z_]+)"', module_src))

    gitlab_cmds = dispatched_commands(inspect.getsource(service))
    github_cmds = dispatched_commands(inspect.getsource(github_service))
    azure_cmds = dispatched_commands(inspect.getsource(azure_service))

    # reactive/observation commands are single-provider by design (GitHub
    # PR review fanout, GitLab pipeline debug) — not part of the run
    # lifecycle parity contract.
    lifecycle_only = {
        "start_run",
        "go",
        "cancel",
        "retry",
        "issue_edited",
        "unlabeled",
        # R29 operator commands: /status, /why-blocked (read-only) and
        # /reconcile (the explicit R11 recovery driver) are lifecycle
        # commands too — every provider service must dispatch them.
        "status",
        "why_blocked",
        "reconcile",
    }
    gitlab_cmds &= lifecycle_only
    github_cmds &= lifecycle_only
    azure_cmds &= lifecycle_only

    # #29 propagation landed: every lifecycle command is dispatched by all
    # three provider services — the whitelist stays empty, and a deviation
    # must carry a reason HERE (a new known_gaps entry with an issue pointer)
    # rather than shipping silent.
    known_gaps: set[tuple[str, str]] = set()
    all_sets = {"gitlab": gitlab_cmds, "github": github_cmds, "azure": azure_cmds}
    union = set().union(*all_sets.values())
    for provider, cmds in all_sets.items():
        missing = union - cmds - {cmd for p, cmd in known_gaps if p == provider}
        assert not missing, f"{provider} is missing lifecycle commands: {sorted(missing)}"


# ----------------------------------------------------------------------
# A14 (docs/reviews/2026-09-18-d16f523): COMPOSED invariant-failure
# scenarios on REAL service legs. Each driver below composes two landed
# fixes (Axx + Ayy) over the production entry points — never a helper —
# and asserts the ACTUAL effect (zero writes / adopted / blocked with
# reason). Failing-before for every scenario is documented by the review
# probes (P01-P07 in reproduce_review_findings.py); the drivers are the
# passing-after regression proof the review's acceptance asks for.
# ----------------------------------------------------------------------

HARNESS_WORKFLOW = "forge-harness.github.yml"
HARNESS_MODEL = "glm-5.3-flash[1m]"

#: MCP mount identities for the A06 composed scenario: one token scoped to
#: THIS repo's namespace (acme/*) and one scoped to a foreign namespace —
#: the same run, the same mount, opposite object-level verdicts.
MIRROR_ALLOWED_TOKEN = "forge-mirror-allowed"
MIRROR_FOREIGN_TOKEN = "forge-mirror-foreign"


def _engine(db_path: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    return engine


async def _go_resumes(service: GitHubRunService, run_id: str) -> None:
    """The re-claimed ``/go`` command step: the real recovery driver.

    ``handle_go`` finds the consumed gate and the run parked mid-publish
    (``_RESUMABLE_PUBLISH_STATUSES``) and resumes the publish leg.
    """
    await service.handle_go(
        project_id=PROJECT_ID,
        issue_number=ISSUE,
        note_text=f"/go {run_id}",
        author_username="alice",
    )


async def composed_a06_run_mirror_scoped_token() -> None:
    """A06 composed: scoped token → run mirror over a REAL service leg.

    The run is produced by the production ``start_run`` → ``/go`` leg (its
    durable subject is the real ``github_repo_full_name`` the service
    stamps), then read through the REAL streamable-HTTP MCP mount with a
    scoped, repo-allowlisted token. A token allowlisted for ``acme/*``
    reads the run, its frozen plan and its evidence; a token scoped to a
    foreign namespace gets the byte-exact NOT FOUND a missing run produces
    on every tool and cannot even discover the run via ``run_list``. The
    mirror never writes: zero provider calls before/after the denied reads.
    """
    from forge.database import reset_engine
    from forge.main import create_app

    with tempfile.TemporaryDirectory(prefix="forge-a06-") as tmp:
        db_path = Path(tmp) / "a06.db"
        # The MCP app boots FIRST: its lifespan bootstraps the fresh sqlite
        # schema (alembic-stamped); the service leg then runs on the same
        # file through its own engine.
        reset_engine()
        try:
            application = create_app(
                settings=Settings(
                    GITLAB_URL="https://gitlab.test",
                    GITLAB_TOKEN=SecretStr("glpat-test"),
                    GITLAB_WEBHOOK_SECRET=SecretStr("whsec"),
                    DATABASE_URL=f"sqlite+aiosqlite:///{db_path}",
                    FORGE_MCP_ENABLED=True,
                    FORGE_MCP_KEY=SecretStr("forge-a06-master"),
                    FORGE_MCP_SCOPED_TOKENS=SecretStr(
                        json.dumps(
                            {
                                MIRROR_ALLOWED_TOKEN: ["forge:read"],
                                MIRROR_FOREIGN_TOKEN: ["forge:read"],
                            }
                        )
                    ),
                    FORGE_MCP_TOKEN_REPOS=SecretStr(
                        json.dumps(
                            {
                                MIRROR_ALLOWED_TOKEN: ["acme/*"],
                                MIRROR_FOREIGN_TOKEN: ["other/*"],
                            }
                        )
                    ),
                    FORGE_MCP_ALLOWED_HOSTS="testserver",
                )
            )
            async with application.router.lifespan_context(application):
                application.state.task_queue = AsyncMock()
                session_factory = async_sessionmaker(_engine(db_path), expire_on_commit=False)
                fake = FakeGitHub()
                fake.seed_repo(REPO, {"src/app.py": "print('hi')\n"})
                fake.heads[REPO]["main"] = BASE_HEAD
                fake.seed_issue(REPO, ISSUE, ISSUE_TITLE, ISSUE_DESC)
                service = make_service(db=session_factory, fake=fake)

                run_id = await start(service)
                await go(service, run_id)
                run = await get_run(session_factory, run_id)
                assert run.status == FlowStatus.WAITING_CI.value
                assert run.github_repo_full_name == REPO
                provider_calls_before = len(fake.calls)

                transport = ASGITransport(app=application)
                async with AsyncClient(transport=transport, base_url="http://testserver") as ac:

                    async def call(name: str, arguments: dict[str, Any], token: str):
                        return await ac.post(
                            "/mcp/mcp",
                            json={
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "tools/call",
                                "params": {"name": name, "arguments": arguments},
                            },
                            headers={
                                "Authorization": f"Bearer {token}",
                                "Accept": "application/json, text/event-stream",
                            },
                        )

                    async def result_text(response) -> str:
                        ctype = response.headers.get("content-type", "")
                        if ctype.startswith("text/event-stream"):
                            for line in response.text.splitlines():
                                if line.startswith("data:"):
                                    payload = json.loads(line[5:].strip())
                                    return str(payload["result"]["content"][0]["text"])
                            raise AssertionError(f"no SSE data line: {response.text[:200]}")
                        payload = response.json()
                        return str(payload["result"]["content"][0]["text"])

                    # The foreign-scoped token: every read tool answers with
                    # the MISSING-run text — forbidden is indistinguishable
                    # from absent — and run_list does not even discover it.
                    not_found = f"NOT FOUND: no run '{run_id}'"
                    for tool, args in (
                        ("run_get", {"run_id": run_id}),
                        ("plan_get", {"run_id": run_id}),
                        ("run_evidence_get", {"run_id": run_id}),
                    ):
                        text = await result_text(await call(tool, args, MIRROR_FOREIGN_TOKEN))
                        assert text == not_found, tool
                    listed = await result_text(await call("run_list", {}, MIRROR_FOREIGN_TOKEN))
                    assert run_id not in listed
                    assert REPO not in listed

                    # The acme-scoped token: the same run mirror is fully
                    # readable — status, subject, frozen spec document.
                    mirrored = await result_text(
                        await call("run_get", {"run_id": run_id}, MIRROR_ALLOWED_TOKEN)
                    )
                    assert f"acme/acme-widget#{ISSUE}" in mirrored
                    assert FlowStatus.WAITING_CI.value in mirrored
                    plan = await result_text(
                        await call("plan_get", {"run_id": run_id}, MIRROR_ALLOWED_TOKEN)
                    )
                    assert run.spec_digest in plan

                    # Zero provider calls: the denied reads never probed the
                    # fake (the object-level check ran on durable state).
                    assert len(fake.calls) == provider_calls_before
        finally:
            reset_engine()


async def composed_a01_required_absent_green_optional() -> None:
    """A01 composed: full ``evaluate_waiting_ci_one`` — the required check
    is ABSENT while an OPTIONAL workflow is green.

    Failing-before (probe P03): the old verdict treated any completed
    non-failing run — skipped, neutral, unknown-conclusion, a green docs
    workflow — as verified. The positive-proof contract must keep the run
    WAITING (an honestly unknown verdict; a late required check may still
    register), and when the required check finally succeeds the same
    production entry must drive the run to a VERIFIED ready. Zero writes
    while unproven: the wait publishes nothing.
    """
    db, fake = await _composed_db()
    service = make_service(
        db,
        fake,
        settings=make_settings(FORGE_REQUIRED_JOBS="tests"),
    )
    run_id = await start(service)
    await go(service, run_id)
    run = await get_run(db, run_id)
    assert run.status == FlowStatus.WAITING_CI.value
    candidate = (run.candidate_shas or [])[-1]

    # A green OPTIONAL workflow; the required ``tests`` workflow never ran
    # for this sha.
    fake.seed_workflow_runs(
        [
            {
                "head_sha": candidate,
                "name": "docs",
                "status": "completed",
                "conclusion": "success",
            }
        ]
    )
    writes_before = _mutation_calls(fake)

    await service.evaluate_waiting_ci_one(run_id)

    run = await get_run(db, run_id)
    assert run.status == FlowStatus.WAITING_CI.value, (
        "a green optional workflow must never substitute the required check"
    )
    verification = (run.evidence or {}).get("verification") or {}
    assert verification.get("status") == "unknown"
    assert verification.get("summary") == "required checks not run: tests"
    surface = verification.get("surface") or []
    assert [entry.get("name") for entry in surface] == ["docs"]
    assert _mutation_calls(fake) == writes_before, "an unproven wait publishes nothing"

    # The required check registers late — the same entry point now proves
    # the run and drives it to a VERIFIED ready.
    fake.seed_workflow_runs(
        [
            {
                "head_sha": candidate,
                "name": "tests",
                "status": "completed",
                "conclusion": "success",
            }
        ]
    )
    await service.evaluate_waiting_ci_one(run_id)

    run = await get_run(db, run_id)
    assert run.status == FlowStatus.READY_FOR_HUMAN.value
    verification = (run.evidence or {}).get("verification") or {}
    assert verification.get("status") == "passed"
    assert verification.get("tested_oid") == candidate
    assert [entry.get("name") for entry in verification.get("surface") or []] == [
        "docs",
        "tests",
    ]
    assert "checks passed" in (run.status_reason or "")


#: The provider mutations an honest wait must never add to.
_MUTATION_CALL_NAMES = (
    "create_commit_on_branch",
    "create_draft_pr",
    "create_issue_comment",
    "update_issue_comment",
    "dispatch_workflow",
)


def _mutation_calls(fake: FakeGitHub) -> list[tuple]:
    return [call for call in fake.calls if call[0] in _MUTATION_CALL_NAMES]


async def _composed_db() -> tuple[async_sessionmaker, FakeGitHub]:
    """A pristine in-memory database + seeded fake for one driver."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _composed_db_engines.append(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    fake = FakeGitHub()
    fake.seed_repo(REPO, {"src/app.py": "print('hi')\n"})
    fake.heads[REPO]["main"] = BASE_HEAD
    fake.seed_issue(REPO, ISSUE, ISSUE_TITLE, ISSUE_DESC)
    return async_sessionmaker(engine, expire_on_commit=False), fake


#: Engines opened by :func:`_composed_db`; disposed by the autouse fixture
#: below (drivers own no fixture, so the suite reaps their engines).
_composed_db_engines: list[AsyncEngine] = []


@pytest.fixture(autouse=True)
async def _dispose_composed_engines():
    yield
    while _composed_db_engines:
        await _composed_db_engines.pop().dispose()


def _arm_mid_publish_crash(fake: FakeGitHub) -> None:
    """The A12 crash point: the publication write was ACCEPTED (the
    provider is applying it slowly) and the WORKER DIES at the next
    provider call — before the outcome is journaled. The hard failure
    propagates (a process-death stand-in: the leg gets no outcome at all),
    unlike a GitHubAPIError which the flow's PR leg deliberately absorbs."""
    original = fake.create_draft_pr
    attempts = {"n": 0}

    async def worker_died(*args: Any, **kwargs: Any):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("worker died mid-publish (the A12 crash point)")
        return await original(*args, **kwargs)

    fake.create_draft_pr = worker_died  # type: ignore[method-assign]


async def composed_a12_pending_remote_application() -> None:
    """A12 composed: the delayed-apply stub against the REAL recovery paths.

    The provider ACCEPTS the publication write but its application is
    delayed past the crash (the negative-probe view: old head, zero marker
    hits). Two production recoveries then race the pending effect:

    1. the resumed ``/go`` leg redrives its probe-first publish — the
       branch-wide CAS makes the redispatch duplicate-safe, and the slow
       first write is REFUSED when it finally applies (exactly one
       logical candidate on the branch, the run's candidate);
    2. the recovery scanner's negative probe must NOT dispatch: the intent
       parks in the effect-certainty window (``probing``) with the write
       count still at one, and the late-landing commit is ADOPTED at the
       window-end re-probe (the run advances on the FOUND sha).

    Failing-before: the R11 shortcut read "zero hits + unchanged head" as
    safe-to-redispatch — variant 2 would POST a duplicate and BOTH would
    land.
    """
    # --- variant 1: the resumed leg — the CAS refuses the late first write
    db, fake = await _composed_db()
    service = make_service(db, fake)
    fake.delayed_apply = True
    _arm_mid_publish_crash(fake)
    run_id = await start(service)

    with pytest.raises(RuntimeError, match="worker died mid-publish"):
        await go(service, run_id)
    run = await get_run(db, run_id)
    assert run.status == FlowStatus.PROPOSING.value, "the crash left the run mid-publish"
    assert list(run.candidate_shas or []) == []
    branch = f"forge/{ISSUE}/{run_id[:8]}"
    assert fake.heads[REPO][branch] == BASE_HEAD, "accepted, NOT applied (the negative view)"
    assert len(fake.pending_delayed_commits[branch]) == 1
    commit_posts = len(fake.calls_of("create_commit_on_branch"))
    assert commit_posts == 1

    # The re-claimed command step resumes the leg. The provider's slow
    # window is over for NEW writes (the first is still in flight); the
    # redispatch's CAS pins the unchanged head.
    fake.delayed_apply = False
    await _go_resumes(service, run_id)

    run = await get_run(db, run_id)
    assert run.status == FlowStatus.WAITING_CI.value
    candidate = (run.candidate_shas or [])[-1]
    assert fake.heads[REPO][branch] == candidate
    # The slow first write finally applies — its CAS (expected BASE, actual
    # the redrive's commit) REFUSES it. One logical candidate survives.
    fake.flush_delayed_apply(branch)
    assert fake.delayed_refused == 1
    assert len(fake.commits[REPO][branch]) == 1
    assert fake.commits[REPO][branch][0]["sha"] == candidate
    assert len(fake.calls_of("create_commit_on_branch")) == commit_posts + 1
    assert len(fake.pull_requests[REPO]) == 1

    # --- variant 2: the recovery scanner — settle, then ADOPT
    db2, fake2 = await _composed_db()
    service2 = make_service(db2, fake2)
    fake2.delayed_apply = True
    _arm_mid_publish_crash(fake2)
    run_id2 = await start(service2)

    with pytest.raises(RuntimeError, match="worker died mid-publish"):
        await go(service2, run_id2)
    branch2 = f"forge/{ISSUE}/{run_id2[:8]}"
    assert fake2.heads[REPO][branch2] == BASE_HEAD

    # The scanner's FIRST probe is negative (the accepted write is still in
    # flight): the intent must park in the certainty window — never a
    # dispatch off one negative read.
    now = datetime.now(timezone.utc)
    await service2.resolve_publication_intents(now=now)
    async with db2() as session:
        from forge.durable.models import PublicationIntent

        intent = (
            (
                await session.execute(
                    select(PublicationIntent).where(PublicationIntent.run_id == run_id2)
                )
            )
            .scalars()
            .one()
        )
    assert intent.status == "probing", intent.status
    assert len(fake2.calls_of("create_commit_on_branch")) == 1, (
        "a negative probe must not redispatch while an effect may be in flight"
    )
    run2 = await get_run(db2, run_id2)
    assert run2.status == FlowStatus.PROPOSING.value

    # The provider's slow application completes — the marker+parent commit
    # lands — and the window-end re-probe ADOPTS it: the run advances on
    # the FOUND sha with zero further writes.
    fake2.flush_delayed_apply(branch2)
    landed = fake2.heads[REPO][branch2]
    assert landed != BASE_HEAD
    await service2.resolve_publication_intents(now=now + timedelta(seconds=61))

    async with db2() as session:
        intent = (
            (
                await session.execute(
                    select(PublicationIntent).where(PublicationIntent.run_id == run_id2)
                )
            )
            .scalars()
            .one()
        )
    assert intent.status == "adopted"
    assert intent.provider_object_id == landed
    run2 = await get_run(db2, run_id2)
    assert run2.status == FlowStatus.WAITING_CI.value
    assert list(run2.candidate_shas or []) == [landed]
    assert len(fake2.calls_of("create_commit_on_branch")) == 1, "adoption never re-POSTs"
    assert len(fake2.pull_requests[REPO]) == 1


async def composed_a03_comment_same_id_edited_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """A03 composed: full dispatch → lane render with the SAME comment id
    but an edited approved byte.

    The service leg freezes the BriefEnvelope at plan acceptance and the
    ``/go`` dispatch carries ``plan_note_id`` + ``envelope_digest`` +
    ``spec_digest``; the lane then fetches EXACTLY that comment. An edit
    after approval — same id, same author, same header, same run id, ONE
    approved byte changed — must fail the lane's envelope re-verification
    closed ("re-approval required"), and the intact comment must render
    the frozen bytes. The live issue is never fetched on the enforced
    path. Failing-before: the R05 interim guard validated only
    author/header/run-id — all unchanged by the edit — and waved it through.
    """
    from urllib.request import Request

    db, fake = await _composed_db()
    fake.reviewer_login = "forcewake-forge[bot]"  # a shipped forge App login
    service = make_service(
        db,
        fake,
        settings=make_settings(
            FORGE_GITHUB_HARNESS_WORKFLOW=HARNESS_WORKFLOW,
            FORGE_HARNESS_MODEL=HARNESS_MODEL,
        ),
    )
    run_id = await start(service)
    plan_comment = next(
        comment
        for comment in fake.issue_comments[REPO][ISSUE]
        if comment["body"].startswith("## Forge plan")
    )
    envelope = (await get_run(db, run_id)).evidence["brief_envelope"]

    await go(service, run_id)

    # The REAL dispatch carried the binding inputs (id, envelope digest,
    # frozen spec digest) — the lane render below consumes exactly them.
    (dispatch,) = fake.dispatch_inputs
    inputs = dispatch["inputs"]
    assert inputs["run_id"] == run_id
    assert inputs["plan_note_id"] == str(plan_comment["id"])
    assert inputs["envelope_digest"] == envelope["envelope_digest"]
    assert inputs["spec_digest"] == (await get_run(db, run_id)).spec_digest

    requests: list[str] = []

    def fake_urlopen(request: Request, timeout: int = 30):  # noqa: ARG001
        url = request.full_url
        requests.append(url)
        prefix = f"https://api.github.com/repos/{REPO}/issues/comments/"
        if url.startswith(prefix):
            note_id = int(url.rsplit("/", 1)[-1])
            for comments in fake.issue_comments[REPO].values():
                for comment in comments:
                    if comment["id"] == note_id:
                        return _FakeUrllibResponse(json.dumps(comment))
        raise AssertionError(f"unexpected lane fetch: {url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    dispatched = dict(
        plan_note_id=int(inputs["plan_note_id"]),
        run_id=run_id,
        envelope_digest=inputs["envelope_digest"],
        spec_digest=inputs["spec_digest"],
    )

    # Positive control: the intact comment renders the FROZEN bytes.
    task_text, plan_text = fetch_issue_context(REPO, ISSUE, "ghs_runner", **dispatched)
    assert task_text == f"{ISSUE_TITLE}\n{ISSUE_DESC}"
    assert plan_text == envelope["plan_text"]
    assert not any("/issues/" in url and "/comments/" not in url for url in requests), (
        "the enforced path never reads the live issue"
    )

    # The A03 core: SAME id, SAME author/header/run-id — ONE approved byte
    # edited after /go.
    plan_comment["body"] = plan_comment["body"].replace(
        ISSUE_TITLE, f"{ISSUE_TITLE} (edited after approval)"
    )

    with pytest.raises(PlanBindingError, match="re-approval required"):
        fetch_issue_context(REPO, ISSUE, "ghs_runner", **dispatched)

    # The envelope itself is untouched: the frozen bytes (and the digest
    # the dispatch carried) still verify — the refusal is about the COMMENT
    # drifting from the approval, never about re-freezing in place.
    assert (
        build_brief_envelope(
            run_id=run_id,
            task_title=envelope["task_title"],
            task_description=envelope["task_description"],
            plan_text=envelope["plan_text"],
            spec_digest=envelope["spec_digest"],
        )["envelope_digest"]
        == envelope["envelope_digest"]
    )


class _FakeUrllibResponse:
    """Minimal stdlib-mock response for the lane's urllib reads."""

    def __init__(self, payload: str) -> None:
        self._payload = payload.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


class TestA14ComposedServiceLegs:
    """The review's combined invariant-failure scenarios, each over the
    production entry points. The conformance kit drives the same drivers
    and records their outcomes into the attestation ledger (A14:
    registration integrity)."""

    async def test_a06_scoped_token_over_the_real_run_mirror(self):
        await composed_a06_run_mirror_scoped_token()

    async def test_a01_required_absent_with_green_optional_stays_waiting(self):
        await composed_a01_required_absent_green_optional()

    async def test_a12_pending_remote_application_never_duplicates(self):
        await composed_a12_pending_remote_application()

    async def test_a03_edited_plan_comment_fails_the_lane_render_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        await composed_a03_comment_same_id_edited_body(monkeypatch)


# ----------------------------------------------------------------------
# C11: mutation tests — re-introducing the reviewed defect must go RED.
# These run the PRODUCTION path; each simulates exactly one reverted line.
# ----------------------------------------------------------------------


class TestMutationGuardsC11:
    async def test_mutation_name_collapse_restores_the_false_verified(self, db, fake, monkeypatch):
        """Revert C01 (collapse by name without the ambiguity map) → the
        inverted collision verifies again → the guard must FAIL the run's
        readiness assertion (proving the regression test bites)."""
        import forge.runs.github_service as gs

        settings = make_settings(FORGE_REQUIRED_JOBS="tests")
        service = make_service(db, fake, settings=settings)
        run_id, candidate = await drive_to_waiting_ci(db, service, fake)
        fake.seed_workflow_runs(
            [
                workflow_run(
                    candidate,
                    "tests",
                    "failure",
                    path=".github/workflows/tests.yml",
                    run_id=30,
                    attempt=1,
                ),
                workflow_run(
                    candidate,
                    "tests",
                    "success",
                    path=".github/workflows/decoy.yml",
                    run_id=99,
                    attempt=1,
                ),
            ]
        )

        # MUTATION: drop the ambiguity map before the decision
        _real_observe = gs.observe_verification

        def observe_no_ambiguous(**kwargs):
            kwargs.pop("ambiguous_checks", None)
            return _real_observe(**kwargs)

        monkeypatch.setattr(gs, "observe_verification", observe_no_ambiguous)

        await service.evaluate_waiting_ci_one(run_id)

        run = await get_run(db, run_id)
        # with the mutation the name-collapse verifies → the mutation is
        # VISIBLE (this asserts the mutated behavior differs from shipped)
        mutated_verified = run.status == FlowStatus.READY_FOR_HUMAN.value
        monkeypatch.undo()
        assert mutated_verified, (
            "mutation no longer changes behavior — the C01 regression lost its bite; "
            "update it alongside the identity contract"
        )

    async def test_mutation_updated_at_deadline_restores_the_slide(self, db, fake, monkeypatch):
        """D10: BASELINE first, then the SAME trace with the mutation —
        the epoch is fixed by a first poll at T0, observations follow, and
        the deadline poll at T0+601s must block the baseline while the
        sliding-epoch mutant keeps waiting. Without the baseline arm the
        test proves nothing (a first poll at deadline-time would fix the
        epoch either way)."""
        import forge.runs.github_service as gs

        async def _drive(mutated: bool) -> str:
            clear_comments(fake)
            fake2 = FakeGitHub()
            fake2.seed_repo(REPO, {"src/app.py": "print('hi')\n"})
            fake2.heads[REPO]["main"] = BASE_HEAD
            fake2.seed_issue(REPO, ISSUE, ISSUE_TITLE, ISSUE_DESC)
            settings = make_settings(
                FORGE_REQUIRED_JOBS="tests", FORGE_VERIFICATION_TIMEOUT_SECONDS=600
            )
            service = make_service(db, fake2, settings=settings)
            run_id, candidate = await drive_to_waiting_ci(db, service, fake2)
            fake2.seed_workflow_runs([workflow_run(candidate, "tests", "skipped")])

            t0 = datetime.now(timezone.utc)
            # first poll fixes the epoch at T0
            await service.evaluate_waiting_ci_one(run_id, now=t0)
            # repeated observations (each merges evidence)
            await service.evaluate_waiting_ci_one(run_id, now=t0 + timedelta(seconds=300))
            await service.evaluate_waiting_ci_one(run_id, now=t0 + timedelta(seconds=450))

            if mutated:

                def sliding_epoch(evidence, sha, now):
                    # MUTATION: the epoch restarts every observation
                    return {"candidate_sha": sha, "started_at": now.isoformat()}, True

                monkeypatch.setattr(gs, "verification_epoch", sliding_epoch)
            try:
                # deadline poll: T0 + 601s > the 600s window
                await service.evaluate_waiting_ci_one(run_id, now=t0 + timedelta(seconds=601))
            finally:
                monkeypatch.undo()
            return (await get_run(db, run_id)).status

        baseline = await _drive(mutated=False)
        assert baseline == FlowStatus.BLOCKED.value, "baseline must block at the deadline"

        mutant = await _drive(mutated=True)
        assert mutant == FlowStatus.WAITING_CI.value, (
            "the sliding-deadline mutation no longer slides — the B01 regression lost its bite"
        )


class TestFrozenScopeToPublisherD03:
    """D03: the frozen path scope reaches the commit boundary — an
    out-of-scope candidate publishes NOTHING (zero commits, zero PRs)."""

    @pytest.fixture(autouse=True)
    def _clear_config_cache(self):
        clear_cache()
        yield
        clear_cache()

    async def test_an_out_of_scope_builtin_candidate_is_refused(self, db, fake):
        fake.seed_repo(REPO, {".forge.yml": "implement:\n  paths:\n    - 'src/**'\n"})
        fake.heads[REPO]["main"] = BASE_HEAD

        class OutOfScopeStub(StubImplementer):
            async def propose(self, run, issue_title, **kwargs):
                from forge.repository.changeset import Change, ChangeSet, Operation

                return ChangeSet(
                    branch=f"forge/{run.issue_iid}/{run.id[:8]}",
                    commit_message="out-of-scope proposal",
                    changes=[
                        Change(
                            path="other/outside.py",
                            operation=Operation.CREATE,
                            content="print('out of scope')\n",
                        )
                    ],
                )

        service = make_service(db, fake, stack=make_stack(fake, implementer=OutOfScopeStub()))
        run_id = await start(service)
        await go(service, run_id)

        run = await get_run(db, run_id)
        assert run.status in (FlowStatus.BLOCKED.value, FlowStatus.FAILED.value)
        # the boundary refused BEFORE any native write
        assert fake.calls_of("create_commit_on_branch") == []
        assert fake.calls_of("create_draft_pr") == []


class TestCancelDuringProposeD04:
    """D04: a cancel landing DURING the paid propose forbids the native
    write — the branch CAS checks the expected head, not the run's right
    to publish. Zero commits/PRs after the cancel."""

    @pytest.fixture(autouse=True)
    def _clear_config_cache(self):
        clear_cache()
        yield
        clear_cache()

    async def test_cancel_during_propose_refuses_the_commit(self, db, fake):

        class CancelMidPropose(StubImplementer):
            def __init__(self, session_factory, target_run: str) -> None:
                self._session_factory = session_factory
                self._target = target_run

            async def propose(self, run, issue_title, **kwargs):
                # the operator cancels in a SEPARATE transaction while the
                # propose is in flight
                from forge.durable.controller import Controller
                from forge.durable import FlowRun, FlowStatus

                async with self._session_factory() as session:
                    controller = Controller(session)
                    await controller.transition(
                        self._target, FlowStatus.CANCELLED, reason="operator cancel"
                    )
                    row = await session.get(FlowRun, self._target)
                    row.cancel_requested = True
                    await session.commit()
                return await StubImplementer.propose(self, run, issue_title, **kwargs)

        class Factory:
            def __init__(self) -> None:
                self.inner: CancelMidPropose | None = None

            # constructor tolerance for make_stack's default wiring
            def __call__(self, *a, **k):
                return self

            async def propose(self, run, issue_title, **kwargs):
                return await self.inner.propose(run, issue_title, **kwargs)

        factory = Factory()
        service = make_service(db, fake, stack=make_stack(fake, implementer=factory))
        run_id = await start(service)
        factory.inner = CancelMidPropose(db, run_id)

        await go(service, run_id)

        # the grant was revoked mid-reads — the final-boundary guard
        # refuses the native write (zero commits/PRs); the run's lifecycle
        # transition lands via the normal cancel path.
        assert fake.calls_of("create_commit_on_branch") == []
        assert fake.calls_of("create_draft_pr") == []


class TestFinalBoundaryFenceFND02:
    """FND-02: a cancel landing during the publisher's OWN awaited reads
    (branch head / blob hydration / branch setup) is refused at the final
    native-effect boundary — after the reads, immediately before the
    commit-API call. The pre-propose guard alone fenced nothing here."""

    @pytest.fixture(autouse=True)
    def _clear_config_cache(self):
        clear_cache()
        yield
        clear_cache()

    async def test_cancel_during_blob_reads_refuses_at_the_boundary(self, db, fake):

        real_create_branch = fake.create_branch

        async def cancelling_create_branch(owner, repo, branch, sha):
            # branch setup is INSIDE the publisher (after propose, before
            # the commit call) — the cancel lands during its operations
            async with db() as session:
                row = await session.get(FlowRun, service._last_run)
                row.cancel_requested = True  # F13: revokes the grant
                await session.commit()
            return await real_create_branch(owner, repo, branch, sha)

        service = make_service(db, fake)
        run_id = await start(service)
        service._last_run = run_id
        fake.create_branch = cancelling_create_branch

        await go(service, run_id)

        # the grant was revoked mid-reads — the final-boundary guard
        # refuses the native write (zero commits/PRs).
        assert fake.calls_of("create_commit_on_branch") == []
        assert fake.calls_of("create_draft_pr") == []


# ----------------------------------------------------------------------
# R28-08: the durable pause fence — one persisted authority state
# ----------------------------------------------------------------------


class TestPauseFenceStore:
    """The fence row's own semantics (migration 022's ORM surface)."""

    async def test_raise_then_decision_is_fenced_with_the_durable_facts(self, db):
        decision = await raise_pause_fence(
            db, "run-1", publication_epoch_bumped=2, raised_by_command="cmd-a"
        )
        assert decision.fenced is True
        assert decision.publication_epoch_bumped == 2
        assert decision.fenced_at is not None
        assert "publication epoch 2" in decision.reason

        reread = await pause_fence_decision(db, "run-1")
        assert reread.fenced is True
        assert reread.publication_epoch_bumped == 2

    async def test_an_unknown_work_is_not_fenced(self, db):
        assert (await pause_fence_decision(db, "nope")).fenced is False

    async def test_resume_clears_under_a_new_epoch_and_keeps_the_row(self, db):
        await raise_pause_fence(db, "run-1", publication_epoch_bumped=4)
        cleared = await clear_pause_fence(db, "run-1", cleared_by_command="cmd-b")
        assert cleared is True

        after = await pause_fence_decision(db, "run-1")
        assert after.fenced is False
        assert after.resumed_publication_epoch == 5  # fenced epoch + 1: fresh
        # idempotent: a second resume clears nothing
        assert await clear_pause_fence(db, "run-1") is False

    async def test_re_arming_a_cleared_fence_is_monotonic_in_the_epoch(self, db):
        await raise_pause_fence(db, "run-1", publication_epoch_bumped=4)
        await clear_pause_fence(db, "run-1")
        # a replayed OLD pause must not roll the authority backwards
        await raise_pause_fence(db, "run-1", publication_epoch_bumped=3)
        decision = await pause_fence_decision(db, "run-1")
        assert decision.fenced is True
        assert decision.publication_epoch_bumped == 4


class TestPauseFenceIsDurableAcrossRestart:
    """The composed R28-08 invariant: pause → RESTART the API process →
    the delayed candidate callback arrives → the publisher REFUSES,
    because the fence is a committed row, not in-memory state."""

    async def test_delayed_publish_after_restart_is_refused_then_resume_reopens(self, db, fake):
        service = make_service(db, fake)
        run_id = await start(service)

        # The pause lands (what ControlCommandRouter does on /pause): the
        # command is recorded, the publication epoch bumped, the durable
        # fence raised — in THAT order.
        await raise_pause_fence(db, run_id, publication_epoch_bumped=1)

        # The API process "restarts": a fresh service instance over the
        # same durable state. Its memory holds no pause; only the row does.
        restarted = make_service(db, fake)
        assert (await pause_fence_decision(db, run_id)).fenced is True

        clear_comments(fake)
        await go(restarted, run_id)

        run = await get_run(db, run_id)
        assert run.status in (FlowStatus.FAILED.value, FlowStatus.BLOCKED.value)
        assert "publication_refused" in (run.status_reason or "")
        assert "pause fence active" in (run.status_reason or "")
        # ZERO native effects were authorized after the fence.
        assert fake.calls_of("create_commit_on_branch") == []
        assert fake.calls_of("create_draft_pr") == []

        # Resume clears the fence under a NEW epoch — publication reopens
        # for a fresh grant (the run-aware entry publishes again).
        await clear_pause_fence(db, run_id)
        assert (await pause_fence_decision(db, run_id)).fenced is False
        outcome = await restarted._publish_candidate_run_aware(
            run_id,
            issue_number=ISSUE,
            changeset=ChangeSet(
                branch=github_factory_branch(ISSUE, run_id),
                commit_message="resume probe",
                changes=[
                    Change(
                        path="forge-demo/resumed.py",
                        operation=Operation.CREATE,
                        content="VALUE = 1\n",
                    )
                ],
            ),
            expected_head=BASE_HEAD,
            operation_key="resume-probe",
        )
        assert outcome.ok is True
        assert len(fake.calls_of("create_commit_on_branch")) == 1


# ----------------------------------------------------------------------
# NEXT-01/NEXT-02: the execution-identity chain — one attempt credential
# per dispatch, conditional pause/resume authority transitions
# ----------------------------------------------------------------------

LANE_SECRET = "lane-secret-github"  # noqa: S105 — a fake shared secret for tests


class TestAttemptScopedDispatchCredential:
    """NEXT-01: the production dispatch mints the GENERATION-SCOPED lane
    token — the same credential both lane APIs verify — and a retry that
    opens a new attempt retires the previous dispatch's token."""

    def _harness_settings(self) -> Settings:
        return make_settings(
            FORGE_GITHUB_HARNESS_WORKFLOW=HARNESS_WORKFLOW,
            FORGE_HARNESS_MODEL=HARNESS_MODEL,
            FORGE_LANE_CONTROL_SECRET=LANE_SECRET,
        )

    async def test_the_dispatch_mints_a_generation_scoped_lane_token(self, db, fake):
        from forge.api_lane_control import lane_control_token

        service = make_service(db, fake, settings=self._harness_settings())
        run_id = await start(service)
        await go(service, run_id)

        (dispatch,) = fake.dispatch_inputs
        token = dispatch["inputs"]["lane_control_token"]
        run = await get_run(db, run_id)
        assert token
        # scoped to the run's CURRENT durable attempt generation — exactly
        # what both lane APIs verify
        assert token == lane_control_token(
            LANE_SECRET, run_id, generation=int(run.cancellation_generation)
        )
        # ...and NOT the legacy work-only derivation
        assert token != lane_control_token(LANE_SECRET, run_id)

    async def test_no_secret_configured_leaves_the_token_empty(self, db, fake):
        service = make_service(db, fake, settings=self._harness_settings())
        service._settings = make_settings(FORGE_GITHUB_HARNESS_WORKFLOW=HARNESS_WORKFLOW)
        run_id = await start(service)
        await go(service, run_id)

        (dispatch,) = fake.dispatch_inputs
        assert dispatch["inputs"]["lane_control_token"] == ""

    async def test_a_retry_opens_a_new_attempt_generation_and_retires_the_old_token(
        self, db, fake, monkeypatch
    ):
        from forge.api_lane_control import lane_control_token
        from forge.runs import revival

        # Q35-02: the retry dispatches only with a JUSTIFIED continuation —
        # a committed checkpoint makes the exact-WIP resume the authorized
        # one, so this shape still re-dispatches (required mode).
        from forge.adaptive import checkpoint_repository as _cr

        async def _r36_lookup(run_id, **_):
            return _cr.CheckpointLookupOutcome.exact("e" * 64, authority="test")

        monkeypatch.setattr(revival, "durable_checkpoint_outcome", _r36_lookup)

        service = make_service(db, fake, settings=self._harness_settings())
        run_id = await start(service)
        await go(service, run_id)
        (first,) = fake.dispatch_inputs
        old_token = first["inputs"]["lane_control_token"]

        # The attempt dies with a candidate on record (retryable shape).
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.FAILED.value
            run.status_reason = "harness_start_failed: boom"
            run.candidate_shas = ["c1"]
            await session.commit()
        fake.dispatch_inputs.clear()

        await service.handle_retry(
            project_id=PROJECT_ID,
            issue_number=ISSUE,
            note_text=f"/retry {run_id}",
            author_username="alice",
            delivery_id="retry-delivery-1",
        )

        run = await get_run(db, run_id)
        assert int(run.cancellation_generation) == 1  # the new attempt's generation
        (second,) = fake.dispatch_inputs
        new_token = second["inputs"]["lane_control_token"]
        assert new_token == lane_control_token(LANE_SECRET, run_id, generation=1)
        assert new_token != old_token  # the dead attempt's credential is retired


# ----------------------------------------------------------------------
# R32-04: the dispatch SELECTS the lane's WIP-continuity mode. The resume
# guard read FORGE_LANE_RESUME from lane env only tests used to set — the
# production dispatch now carries lane_resume_mode (fresh | required |
# restart) through the workflow_dispatch inputs, and the shipped template
# maps it onto the driver step's FORGE_LANE_RESUME env.
# ----------------------------------------------------------------------


class TestDispatchSelectsResumeMode:
    def _service(self, db, fake) -> GitHubRunService:
        return make_service(
            db,
            fake,
            settings=make_settings(
                FORGE_GITHUB_HARNESS_WORKFLOW=HARNESS_WORKFLOW,
                FORGE_HARNESS_MODEL=HARNESS_MODEL,
            ),
        )

    async def _drive_to_failed_with_candidate(self, db, fake, service) -> str:
        """The retryable shape: dispatched once, then terminal with a
        candidate on record (the /retry and revival precondition)."""
        run_id = await start(service)
        await go(service, run_id)
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.FAILED.value
            run.status_reason = "harness_start_failed: boom"
            run.candidate_shas = ["c1"]
            await session.commit()
        return run_id

    async def test_the_initial_dispatch_selects_fresh(self, db, fake):
        """The first /go dispatch restores nothing accidentally: a 404
        checkpoint read is NORMAL for it, never a required-restore halt."""
        service = self._service(db, fake)
        run_id = await start(service)

        await go(service, run_id)

        (dispatch,) = fake.dispatch_inputs
        assert dispatch["inputs"]["lane_resume_mode"] == "fresh"

    async def test_a_retry_dispatches_the_required_mode(self, db, fake, monkeypatch):
        """/retry continues the work in place (no re-planning) — and Q35-02
        keeps the required contract for exactly the shape that justifies it:
        a committed checkpoint IS the authorized continuation, so the held
        checkpoint's restore is a PRECONDITION of the retried lane, decided
        by the dispatch, not lane-side env setup."""
        from forge.runs import revival

        from forge.adaptive import checkpoint_repository as _cr

        async def _r36_lookup(run_id, **_):
            return _cr.CheckpointLookupOutcome.exact("e" * 64, authority="test")

        monkeypatch.setattr(revival, "durable_checkpoint_outcome", _r36_lookup)

        service = self._service(db, fake)
        run_id = await self._drive_to_failed_with_candidate(db, fake, service)
        fake.dispatch_inputs.clear()

        await service.handle_retry(
            project_id=PROJECT_ID,
            issue_number=ISSUE,
            note_text=f"/retry {run_id}",
            author_username="alice",
            delivery_id="retry-delivery-mode-1",
        )

        (dispatch,) = fake.dispatch_inputs
        assert dispatch["inputs"]["lane_resume_mode"] == "required"

    async def test_a_revival_redispatch_dispatches_the_required_mode(self, db, fake, monkeypatch):
        """The revival recovery scan's re-dispatch (a stalled attempt
        re-opened through the revival graph edge) carries the SAME
        WIP-continuity contract as /retry — required when the committed
        checkpoint is the authorized continuation (Q35-02)."""
        from forge.durable.controller import Controller
        from forge.runs import revival

        from forge.adaptive import checkpoint_repository as _cr

        async def _r36_lookup(run_id, **_):
            return _cr.CheckpointLookupOutcome.exact("e" * 64, authority="test")

        monkeypatch.setattr(revival, "durable_checkpoint_outcome", _r36_lookup)

        service = self._service(db, fake)
        run_id = await self._drive_to_failed_with_candidate(db, fake, service)
        async with db() as session:
            controller = Controller(session)
            await controller.revive_transition(
                run_id, reason="test revival", authorized_by="operator:test"
            )
            await session.commit()
        fake.dispatch_inputs.clear()

        await service._redispatch_revival(run_id)

        (dispatch,) = fake.dispatch_inputs
        assert dispatch["inputs"]["lane_resume_mode"] == "required"

    async def test_the_restart_mode_is_dispatchable_and_unknown_modes_are_refused(self, db, fake):
        """The operator's explicit discard-WIP decision rides the same
        input; a mode outside the closed set is a programming error raised
        BEFORE any dispatch I/O (no branch, no workflow call)."""
        service = self._service(db, fake)
        run_id = await start(service)
        await go(service, run_id)  # waiting_harness: a fallback-shape advance
        fake.dispatch_inputs.clear()

        await service._advance_harness(
            run_id, project_id=PROJECT_ID, issue_number=ISSUE, resume_mode="restart"
        )
        (dispatch,) = fake.dispatch_inputs
        assert dispatch["inputs"]["lane_resume_mode"] == "restart"

        fake.dispatch_inputs.clear()
        with pytest.raises(ValueError, match="resume_mode must be one of"):
            await service._advance_harness(
                run_id, project_id=PROJECT_ID, issue_number=ISSUE, resume_mode="urgent"
            )
        assert fake.dispatch_inputs == []  # refused before any dispatch


class TestConditionalFenceTransitions:
    """NEXT-02: the fence's clear is a compare-and-swap; a cleared fence
    still refuses candidates whose GRANT generation died with the resume."""

    async def test_a_clear_with_a_stale_expected_epoch_fails_and_keeps_the_fence(self, db):
        await raise_pause_fence(db, "run-1", publication_epoch_bumped=4)

        cleared = await clear_pause_fence(db, "run-1", expected_publication_epoch=3)

        assert cleared is False
        assert (await pause_fence_decision(db, "run-1")).fenced is True

    async def test_a_clear_with_the_expected_epoch_wins_once(self, db):
        await raise_pause_fence(db, "run-1", publication_epoch_bumped=4)

        first = await clear_pause_fence(db, "run-1", expected_publication_epoch=4)
        replay = await clear_pause_fence(db, "run-1", expected_publication_epoch=4)

        assert first is True
        assert replay is False  # already cleared — one winner, ever
        after = await pause_fence_decision(db, "run-1")
        assert after.fenced is False
        assert after.resumed_publication_epoch == 5  # bumped + 1, atomically

    async def test_a_re_armed_pause_is_not_cleared_under_the_old_expectation(self, db):
        """Resume raced a NEW pause: the re-armed fence's epoch differs from
        the expectation the resume read — the CAS refuses, the new pause
        stands."""
        await raise_pause_fence(db, "run-1", publication_epoch_bumped=4)
        await clear_pause_fence(db, "run-1", expected_publication_epoch=4)
        await raise_pause_fence(db, "run-1", publication_epoch_bumped=5)  # pause after resume

        stale_resume = await clear_pause_fence(db, "run-1", expected_publication_epoch=4)

        assert stale_resume is False
        assert (await pause_fence_decision(db, "run-1")).fenced is True

    async def test_a_delayed_candidate_from_the_old_generation_stays_refused(self, db):
        """The composed NEXT-02 chain, fence-leg only: pause at epoch 1 →
        resume (resumed generation 2) → the retry aligns the run's own
        generation to 2 → the gen-1 candidate is refused, the gen-2 one
        publishes."""
        async with db() as session:
            session.add(FlowRun(id="run-1", project_id=1, provider="github"))
            await session.commit()

        await raise_pause_fence(db, "run-1", publication_epoch_bumped=1)  # pause at gen 1
        await clear_pause_fence(db, "run-1")  # resume: generation 2

        async with db() as session:
            run = await session.get(FlowRun, "run-1")
            run.cancellation_generation = 2  # the resume's re-dispatch aligned it
            await session.commit()

        stale = await pause_fence_decision(db, "run-1", expected_generation=1)
        assert stale.fenced is True
        assert stale.stale_generation == 1
        assert "stale publication grant" in stale.reason

        current = await pause_fence_decision(db, "run-1", expected_generation=2)
        assert current.fenced is False

    async def test_an_unaligned_resume_keeps_the_live_attempts_grants(self, db):
        """A same-attempt steering resume (no new attempt dispatched, the
        run's generation never moved): the cleared fence reopens
        publication for the CONTINUING attempt — its claim is not renamed
        by the resume."""
        async with db() as session:
            session.add(
                FlowRun(id="run-1", project_id=1, provider="github", cancellation_generation=0)
            )
            await session.commit()

        await raise_pause_fence(db, "run-1", publication_epoch_bumped=1)
        await clear_pause_fence(db, "run-1")  # resumed epoch 2; run still at 0

        decision = await pause_fence_decision(db, "run-1", expected_generation=0)

        assert decision.fenced is False

    async def test_the_publisher_refuses_the_old_attempts_candidate_and_publishes_the_new(
        self, db, fake, monkeypatch
    ):
        """The composed NEXT-02 chain through the REAL publisher: pause →
        resume → retry (new attempt, generation aligned to the fence) → a
        delayed candidate from the OLD attempt's claim is refused at the
        pre-dispatch guard with the stale-grant reason and ZERO native
        writes; the new attempt's candidate publishes."""
        from forge.durable.claims import ExecutionClaim, bind_claim
        from forge.runs import revival

        # Q35-02: a committed checkpoint keeps the retry dispatchable
        # (exact-WIP continuation) so the fence chain below is exercised.
        from forge.adaptive import checkpoint_repository as _cr

        async def _r36_lookup(run_id, **_):
            return _cr.CheckpointLookupOutcome.exact("e" * 64, authority="test")

        monkeypatch.setattr(revival, "durable_checkpoint_outcome", _r36_lookup)

        service = make_service(
            db,
            fake,
            settings=make_settings(
                FORGE_GITHUB_HARNESS_WORKFLOW=HARNESS_WORKFLOW,
                FORGE_HARNESS_MODEL=HARNESS_MODEL,
            ),
        )
        run_id = await start(service)
        await go(service, run_id)

        # The pause/resume pair: fence raised at epoch 1, cleared under 2.
        await raise_pause_fence(db, run_id, publication_epoch_bumped=1)
        await clear_pause_fence(db, run_id)

        # The run dies with a candidate; /retry opens the new attempt —
        # its generation aligned with (never below) the resumed epoch.
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.FAILED.value
            run.status_reason = "harness_start_failed: boom"
            run.candidate_shas = ["c1"]
            await session.commit()
        await service.handle_retry(
            project_id=PROJECT_ID,
            issue_number=ISSUE,
            note_text=f"/retry {run_id}",
            author_username="alice",
            delivery_id="retry-delivery-2",
        )
        assert int((await get_run(db, run_id)).cancellation_generation) == 2

        def _changeset() -> ChangeSet:
            return ChangeSet(
                branch=github_factory_branch(ISSUE, run_id),
                commit_message="stale grant probe",
                changes=[
                    Change(
                        path="forge-demo/stale.py",
                        operation=Operation.CREATE,
                        content="VALUE = 1\n",
                    )
                ],
            )

        old_attempt_claim = ExecutionClaim(
            step_id=1, attempt=1, owner="old-lane", fence_token=1, cancellation_generation=1
        )
        with bind_claim(old_attempt_claim):
            refused = await service._publish_candidate_run_aware(
                run_id,
                issue_number=ISSUE,
                changeset=_changeset(),
                expected_head=BASE_HEAD,
                operation_key="stale-probe",
            )

        assert refused.ok is False
        assert "stale publication grant" in (refused.reason or "")
        assert fake.calls_of("create_commit_on_branch") == []  # ZERO native writes

        new_attempt_claim = ExecutionClaim(
            step_id=2, attempt=2, owner="new-lane", fence_token=2, cancellation_generation=2
        )
        with bind_claim(new_attempt_claim):
            published = await service._publish_candidate_run_aware(
                run_id,
                issue_number=ISSUE,
                changeset=_changeset(),
                expected_head=BASE_HEAD,
                operation_key="fresh-probe",
            )

        assert published.ok is True
        assert len(fake.calls_of("create_commit_on_branch")) == 1


# ----------------------------------------------------------------------
# R28-15: discovery and implementation on ONE immutable source set
# ----------------------------------------------------------------------


class TestDiscoverySpliceInsidePlanning:
    """The splice sits INSIDE the planning try: a discovery failure gets the
    deliberate planning-failed handling — a parked run and an operator note,
    never a silently stuck preflight run. Unanswered discovery questions are
    the R28-17 exception: a WAIT (``blocked(waiting_question: …)``), not a
    failure."""

    async def _assert_parked_visibly(self, db, fake, make_exc):
        from forge.adaptive.discovery_stage import DiscoveryStageError
        from forge.runs import github_service as service_module

        original = service_module.maybe_run_discovery

        async def patched(run_ctx, planner_input):
            raise make_exc()

        service_module.maybe_run_discovery = patched
        try:
            service = make_service(db, fake)
            with pytest.raises(DiscoveryStageError):
                await start(service)
        finally:
            service_module.maybe_run_discovery = original

        run = await get_run(db, await _only_run_id(db))
        assert run.status == FlowStatus.BLOCKED.value  # parked, never stuck preflight
        assert "planning_failed" in (run.status_reason or "")
        assert "discovery" in (run.status_reason or "")
        assert any("failed" in body for body in comments(fake))

    async def test_a_failed_discovery_stage_parks_the_run(self, db, fake):
        from forge.adaptive.discovery_stage import DiscoveryStageError

        await self._assert_parked_visibly(
            db, fake, lambda: DiscoveryStageError("discovery disc-1 refuses: probes died")
        )

    async def test_unanswered_questions_park_as_a_wait_not_a_failure(self, db, fake):
        """R28-17: QuestionsOutstanding is a WAIT, not a planning failure.

        The run parks ``blocked(waiting_question: …)`` with the question
        stash durable (the recovery pass re-enters planning from it), an
        operator-visible note names each question with its /answer form,
        and the exception does NOT propagate — a wait is not an error."""
        from forge.adaptive.discovery_stage import QuestionsOutstanding
        from forge.runs import github_service as service_module

        original = service_module.maybe_run_discovery

        async def patched(run_ctx, planner_input):
            raise QuestionsOutstanding(
                "disc-1",
                "run-x",
                [
                    {
                        "question_id": "q-1",
                        "text": "which repo owns the expiry key?",
                        "criticality": "critical",
                    }
                ],
            )

        service_module.maybe_run_discovery = patched
        try:
            service = make_service(db, fake)
            run_id = await start(service)  # does NOT raise — a wait, not a failure
        finally:
            service_module.maybe_run_discovery = original

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert (run.status_reason or "").startswith("waiting_question:")
        stash = (run.evidence or {}).get("question_block") or {}
        assert stash.get("discovery_id") == "disc-1"
        assert stash.get("question_ids") == ["q-1"]
        assert stash.get("issue_description") == ISSUE_DESC  # the replan input is stashed
        assert any("waiting for clarification" in body for body in comments(fake))
        assert any("`q-1`" in body and "/answer" in body for body in comments(fake))


class TestDiscoveryFreezesOneSourceSet:
    """R28-15's freeze: the base SHA is resolved FIRST and binds the
    config read, the discovery snapshot and the attempt base — a branch
    that moves mid-planning cannot mix two snapshots into one approved
    plan."""

    async def test_discovery_reads_the_resolved_sha_and_base_is_never_reread(
        self, db, fake, monkeypatch
    ):
        monkeypatch.setenv("FORGE_DISCOVERY_ENABLED", "1")
        tree_refs: list[str] = []

        real_get_tree = fake.get_tree

        async def moving_get_tree(project_id, path="", ref="HEAD", recursive=False):
            # main moves on EVERY listing — the movable name is poison; a
            # frozen SHA is not.
            fake.heads[REPO]["main"] = "9" * 40
            tree_refs.append(ref)
            return await real_get_tree(project_id, path, ref, recursive)

        fake.get_tree = moving_get_tree  # type: ignore[method-assign]

        service = make_service(db, fake)
        run_id = await start(service)

        run = await get_run(db, run_id)
        # The attempt base is the FIRST resolved SHA — the branch moved
        # during planning and the freeze never re-read the name.
        assert run.base_sha == BASE_HEAD
        # The discovery snapshot read the SAME immutable SHA, never the
        # movable branch name.
        assert tree_refs, "the enabled discovery stage never read a tree"
        assert set(tree_refs) == {BASE_HEAD}
        record = (run.evidence or {}).get("discovery") or {}
        assert (record.get("dispatch") or {}).get("source_oid") == BASE_HEAD

    async def test_disabled_discovery_stays_unresearched_with_no_io(self, db, fake):
        calls_before = list(fake.calls)
        service = make_service(db, fake)
        run_id = await start(service)
        run = await get_run(db, run_id)
        assert "discovery" not in (run.evidence or {})
        # Only the head read + config/profile reads happened — no tree
        # listing (the disabled stage never pays the snapshot read).
        assert [name for name, _ in fake.calls[len(calls_before) :] if name == "get_tree"] == []


# ----------------------------------------------------------------------
# NEXT-11/R32-05: the execution lease at the GitHub dispatch boundary —
# the same choke point Azure crosses. Four approvals under a limit of
# three produce at most THREE native job-start requests; the fourth
# parks blocked(execution_capacity) with the snapshot in its evidence
# and an issue comment carrying the next action; a terminal release
# frees its slot for the next dispatch; a re-driven dispatch adopts the
# reservation instead of consuming another slot.
# ----------------------------------------------------------------------


class TestExecutionLeaseAtDispatch:
    @staticmethod
    def _harness_service(db, fake) -> GitHubRunService:
        # The Actions lane: each successful dispatch is one native
        # workflow_dispatch (the job-start request counted below).
        return make_service(
            db, fake, settings=make_settings(FORGE_GITHUB_HARNESS_WORKFLOW=WORKFLOW)
        )

    @staticmethod
    async def _start_on(service: GitHubRunService, fake: FakeGitHub, issue: int) -> str:
        fake.seed_issue(REPO, issue, f"task {issue}", f"body {issue}")
        return await service.start_run(
            project_id=PROJECT_ID,
            issue_number=issue,
            issue_title=f"task {issue}",
            issue_description=f"body {issue}",
            author_username="alice",
        )

    @staticmethod
    async def _go_on(service: GitHubRunService, issue: int, run_id: str) -> None:
        await service.handle_go(
            project_id=PROJECT_ID,
            issue_number=issue,
            note_text=f"/go {run_id}",
            author_username="alice",
        )

    async def test_four_approvals_under_limit_three_start_three_jobs(self, db, fake, monkeypatch):
        """The R32-05 acceptance on GitHub: capacity policy does not depend
        on the source-control provider — the fourth approved run parks with
        a typed capacity reason while exactly three native dispatches went
        out, and zero dispatch-API work happened for the parked run."""
        monkeypatch.setenv("FORGE_ADMISSION_MAX_ACTIVE_PER_PROJECT", "3")
        service = self._harness_service(db, fake)
        issues = [ISSUE + 10, ISSUE + 20, ISSUE + 30, ISSUE + 40, ISSUE + 50]
        runs = {issue: await self._start_on(service, fake, issue) for issue in issues}

        for issue in issues[:3]:
            await self._go_on(service, issue, runs[issue])
        for issue in issues[:3]:
            assert (await get_run(db, runs[issue])).status == FlowStatus.WAITING_HARNESS.value
        assert len(fake.calls_of("dispatch_workflow")) == 3

        dispatches_before_park = len(fake.calls_of("dispatch_workflow"))
        create_branch_before = len(fake.calls_of("create_branch"))
        await self._go_on(service, issues[3], runs[issues[3]])
        parked = await get_run(db, runs[issues[3]])
        assert parked.status == FlowStatus.BLOCKED.value
        assert "execution_capacity" in (parked.status_reason or "")
        lease_evidence = dict(parked.evidence or {})["execution_lease"]
        assert lease_evidence["acquired"] is False
        assert lease_evidence["capacity"]["held"] == 3
        assert lease_evidence["capacity"]["limit"] == 3
        # ZERO native start work for the parked run — no dispatch, no branch.
        assert len(fake.calls_of("dispatch_workflow")) == dispatches_before_park
        assert len(fake.calls_of("create_branch")) == create_branch_before
        parked_comments = [
            call[1][3]
            for call in fake.calls
            if call[0] == "create_issue_comment" and call[1][2] == issues[3]
        ]
        assert any("execution slot" in body for body in parked_comments)

        # A terminal outcome releases the slot THROUGH OBSERVED NATIVE
        # TERMINATION (Q35-04): cancel the first run — its lease parks
        # draining because the dispatched Actions run is still live — then
        # the reconciler's occupancy pass observes the run finished and
        # frees the slot. The NEXT dispatch (a fifth, never-approved run —
        # the parked one consumed its gate when it parked, its recovery
        # spelling is /retry) acquires what it freed.
        await service.handle_cancel(
            project_id=PROJECT_ID,
            issue_number=issues[0],
            note_text="/cancel",
            author_username="alice",
        )
        assert (await get_run(db, runs[issues[0]])).status == FlowStatus.CANCELLED.value
        # The native job ends: the reconciler's probe now observes terminal.
        for actions_run in fake.actions_runs:
            actions_run.update(status="completed", conclusion="cancelled")
        await service.evaluate_waiting_harness()
        await self._go_on(service, issues[4], runs[issues[4]])
        assert (await get_run(db, runs[issues[4]])).status == FlowStatus.WAITING_HARNESS.value
        assert len(fake.calls_of("dispatch_workflow")) == dispatches_before_park + 1

    async def test_the_run_evidence_records_the_acquired_lease(self, db, fake):
        service = self._harness_service(db, fake)
        run_id = await start(service)
        await go(service, run_id)

        evidence = dict((await get_run(db, run_id)).evidence or {})
        assert evidence["execution_lease"]["check"] == "execution_lease"
        assert evidence["execution_lease"]["acquired"] is True
        assert evidence["execution_lease"]["slot"] == 1
        assert evidence["execution_lease"]["lease_id"]

    async def test_a_redriven_dispatch_adopts_the_reservation_not_a_new_slot(
        self, db, fake, monkeypatch
    ):
        """Duplicate/resumed dispatches (the recovery scan's re-drive of a
        crashed worker's leg) adopt the EXISTING reservation — one run,
        one slot, however many drivers re-cross the boundary."""
        from forge.adaptive.admission import lease_snapshot
        from forge.adaptive.admission import AdmissionPolicy

        monkeypatch.setenv("FORGE_ADMISSION_MAX_ACTIVE_PER_PROJECT", "1")
        service = self._harness_service(db, fake)
        run_id = await start(service)
        await go(service, run_id)
        first = dict((await get_run(db, run_id)).evidence or {})["execution_lease"]

        # The recovery re-drive: the same run crosses the boundary again.
        await service._advance_harness(run_id, project_id=PROJECT_ID, issue_number=ISSUE)
        second = dict((await get_run(db, run_id)).evidence or {})["execution_lease"]

        assert second["lease_id"] == first["lease_id"]  # adopted, not doubled
        assert second["slot"] == first["slot"]
        snapshot = await lease_snapshot(
            AdmissionPolicy(max_active_per_project=1), PROJECT_ID, db, provider="github"
        )
        assert snapshot["held"] == 1  # one slot, one run — R32-06's invariant

    async def test_the_parked_capacity_run_consumed_no_execution_attempt(
        self, db, fake, monkeypatch
    ):
        """NEXT-12: the parked-at-dispatch run never held a lease — the
        refusal is visible as capacity, not as an executed attempt."""
        from forge.adaptive.admission import AdmissionPolicy, lease_snapshot

        monkeypatch.setenv("FORGE_ADMISSION_MAX_ACTIVE_PER_PROJECT", "1")
        service = self._harness_service(db, fake)
        first_issue, second_issue = ISSUE + 50, ISSUE + 60
        first = await self._start_on(service, fake, first_issue)
        second = await self._start_on(service, fake, second_issue)
        await self._go_on(service, first_issue, first)
        await self._go_on(service, second_issue, second)

        assert (await get_run(db, second)).status == FlowStatus.BLOCKED.value
        snapshot = await lease_snapshot(
            AdmissionPolicy(max_active_per_project=1), PROJECT_ID, db, provider="github"
        )
        assert snapshot["held"] == 1
        assert snapshot["completed"] == 0  # the parked run never executed


# ----------------------------------------------------------------------
# R32-10: the system-context connection — neighbor-aware discovery on
# the normal /implement path (NEXT-21 wired into the planning leg)
# ----------------------------------------------------------------------


class _CountingReader:
    """A duck-typed NEIGHBOR reader that records what discovery read."""

    def __init__(self, files: dict[str, str]) -> None:
        from types import SimpleNamespace

        self._entry = SimpleNamespace
        self._files = files
        self.text_reads: list[str] = []

    async def get_tree(
        self, project_id: int, path: str = "", ref: str = "HEAD", recursive: bool = False
    ) -> list:
        return [self._entry(path=p, type="blob") for p in sorted(self._files)]

    async def read_text(self, file_path: str, ref: str = "HEAD") -> str:
        self.text_reads.append(file_path)
        return self._files[file_path]


class TestSystemContextDiscovery:
    """R32-10: a ``neighbors:`` section in the project's ``.forge.yml``
    turns the planning leg's discovery into the read-many/write-one
    system context — the own repo plus every AUTHORIZED neighbor, each
    through its own reader — while a project without neighbors keeps the
    single-repo path exactly as it was."""

    @pytest.fixture(autouse=True)
    def _clear_config_cache(self):
        # The typed config read caches by authority identity; the fake's
        # legacy key contains its repr, and CPython reuses freed
        # addresses — the same guard the A13 config tests apply.
        clear_cache()
        yield
        clear_cache()

    @staticmethod
    def _seed_with_neighbors(fake: FakeGitHub, forge_yml: str) -> None:
        fake.seed_repo(REPO, {"src/app.py": "print('hi')\n", ".forge.yml": forge_yml})

    async def test_two_authorized_neighbors_discover_across_three_repositories(
        self, db, fake, monkeypatch
    ):
        from forge.adaptive.research_planner import FORGE_DISCOVERY_MODE_ENV

        monkeypatch.setenv(FORGE_DISCOVERY_MODE_ENV, "lexical")
        self._seed_with_neighbors(
            fake,
            "forge:\n"
            "  neighbors:\n"
            "    - provider: github\n"
            "      repository_id: acme/partner\n"
            "      ref: 1111111111111111111111111111111111111111\n"
            "    - provider: github\n"
            "      repository_id: acme/second\n"
            "      allowed_globs: ['pkg/**']\n",
        )
        partner = _CountingReader({"src/planner.py": "class LLMPlanner:\n    pass\n"})
        second = _CountingReader({"pkg/api.md": "# the shared contract\n"})
        readers = {"github:acme/partner": partner, "github:acme/second": second}
        service = GitHubRunService(
            db,
            make_settings(),
            ForgeConfig(),
            stack=make_stack(fake),
            repo_full_name=REPO,
            neighbor_reader_factory=lambda neighbor: readers[neighbor.key],
        )

        run_id = await start(service)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value  # planned normally
        record = dict((run.evidence or {}).get("discovery") or {})
        assert record.get("status") == "complete"
        # ONE discovery over the whole authorized set: the own repo plus
        # both neighbors, each under its composite namespace.
        assert set(record["dispatch"]["repositories"]) == {
            "own",
            "github:acme/partner",
            "github:acme/second",
        }
        # all THREE repositories were actually read (the own repo through
        # the stack reader, each neighbor through its own).
        assert fake.calls_of("get_tree"), "the own repo was read"
        assert partner.text_reads, "neighbor 1 was read"
        assert second.text_reads == ["pkg/api.md"]  # its globs bounded the read

    async def test_without_neighbors_the_single_repo_path_stays_byte_identical(self, db, fake):
        service = make_service(db, fake)  # no factory, no neighbors section
        profile = service._system_context_profile(
            ConfigReadResult.confirmed_absent(ref="main"), frozen_ref="main", path_scope=[]
        )
        assert profile.neighbors == ()

        ctx = service._discovery_context(
            run_id="a" * 32,
            project_id=1,
            frozen_ref="main",
            profile=profile,
            neighbor_readers={},
        )
        expected = DiscoveryRunContext.from_reader(
            run_id="a" * 32,
            project_id=1,
            session_factory=db,
            reader=fake,
            ref="main",
            repository_id=REPO,
            allowed_globs=None,
            research=None,
        )
        # the legacy single-repo shapes, byte for byte (the lazy loader is
        # a closure — everything DURABLE about the two contexts compares
        # equal: identity, ref, scope and the one own-repo spec).
        assert (ctx.run_id, ctx.project_id, ctx.repository_id, ctx.source_oid) == (
            expected.run_id,
            expected.project_id,
            expected.repository_id,
            expected.source_oid,
        )
        assert ctx.allowed_globs == expected.allowed_globs is None
        assert ctx.repo_specs == expected.repo_specs

    async def test_the_own_path_scope_and_neighbor_pins_ride_their_entries(self, db, fake):
        self._seed_with_neighbors(
            fake,
            "forge:\n"
            "  implement:\n"
            "    paths: ['src/**']\n"
            "  neighbors:\n"
            "    - provider: github\n"
            "      repository_id: acme/partner\n"
            "      ref: 1111111111111111111111111111111111111111\n"
            "    - provider: github\n"
            "      repository_id: acme/second\n"
            "      allowed_globs: ['pkg/**']\n",
        )
        config_read = await read_project_config(fake, PROJECT_ID, ref=BASE_HEAD)
        assert config_read.status == "valid"
        service = make_service(db, fake)
        profile = service._system_context_profile(
            config_read,
            frozen_ref=BASE_HEAD,
            path_scope=list(config_read.config.implement_paths),
        )
        readers = {
            "github:acme/partner": _CountingReader({"src/planner.py": "x\n"}),
            "github:acme/second": _CountingReader({"pkg/api.md": "y\n"}),
        }
        ctx = service._discovery_context(
            run_id="b" * 32,
            project_id=PROJECT_ID,
            frozen_ref=BASE_HEAD,
            profile=profile,
            neighbor_readers=readers,
        )
        assert [(s.repo_key, s.repository_id, s.ref, s.allowed_globs) for s in ctx.repo_specs] == [
            ("own", REPO, BASE_HEAD, ["src/**"]),  # the monorepo scope survives
            ("github:acme/partner", "acme/partner", "1" * 40, None),
            ("github:acme/second", "acme/second", "HEAD", ["pkg/**"]),
        ]

    async def test_a_misdeclared_neighbor_parks_the_run_before_any_paid_call(self, db, fake):
        self._seed_with_neighbors(
            fake,
            "forge:\n  neighbors:\n    - provider: gitea\n      repository_id: partner/neighbor\n",
        )
        service = make_service(db, fake, stack=make_stack(fake, planner=BoomPlanner()))

        run_id = await start(service)  # BoomPlanner proves no model call happened

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "config_invalid" in (run.status_reason or "")
        assert "neighbors" in (run.status_reason or "")

    async def test_a_foreign_provider_neighbor_needs_its_own_connection(self, db, fake):
        self._seed_with_neighbors(
            fake,
            "forge:\n  neighbors:\n    - provider: gitlab\n      repository_id: partner/neighbor\n",
        )
        service = make_service(db, fake)

        run_id = await start(service)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "config_invalid" in (run.status_reason or "")
        assert "github connection" in (run.status_reason or "")


# ----------------------------------------------------------------------
# R32-11: the dispatch boundary consumes the ACTIVE plan revision
# (NEXT-20 wired into the GitHub /go → harness dispatch leg)
# ----------------------------------------------------------------------

R32_CONTRACT_DIGEST = "1" * 64
R32_SNAPSHOT_DIGEST = "2" * 64


def _gh_revision(revision: int, parent: int | None):
    from forge.adaptive.models import PlanRevision

    return PlanRevision.model_validate(
        {
            "plan_id": "plan-gh-1",
            "work_id": "wp-gh-1",
            "revision": revision,
            "parent_revision": parent,
            "work_contract_digest": R32_CONTRACT_DIGEST,
            "snapshot_set_digest": R32_SNAPSHOT_DIGEST,
            "summary": "Two steps inside the approved scope.",
            "steps": [
                {
                    "step_id": "S1",
                    "objective": "Inspect existing behavior.",
                    "acceptance_refs": ["AC-1"],
                    "impact": ["internal"],
                },
                {
                    "step_id": "S2",
                    "objective": "Implement the authorized change.",
                    "depends_on": ["S1"],
                    "acceptance_refs": ["AC-1"],
                    "impact": ["internal"],
                },
            ],
        }
    )


class TestRevisionDispatchBoundary:
    """R32-11: /approve-revision activates the revised plan in one durable
    transaction (the run row and the human gate move with it), and the
    NEXT /go dispatches under the ACTIVE plan's digest — read from the
    durable pointer, never the superseded comment. A /go still claiming
    the OLD digest refuses before any dispatch I/O."""

    def _service(self, db, fake) -> GitHubRunService:
        return make_service(
            db,
            fake,
            settings=make_settings(
                FORGE_GITHUB_HARNESS_WORKFLOW=HARNESS_WORKFLOW,
                FORGE_HARNESS_MODEL=HARNESS_MODEL,
            ),
        )

    async def _activate_revision(self, db, run_id: str):
        """Seed revision 1 as the durable active plan, then stage + approve
        revision 2 through the REAL activation transaction."""
        from forge.adaptive.revisions import (
            ACTIVE_PLAN_KEY,
            RevisionDecision,
            activate_pending_revision,
            plan_digest,
            proposed_revision_identity,
            stage_pending_revision,
        )

        first = _gh_revision(1, None)
        second = _gh_revision(2, 1)
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            merged = dict(run.evidence or {})
            merged[ACTIVE_PLAN_KEY] = {
                "schema": "forge.revision.active-plan/1",
                "work_id": "wp-gh-1",
                "plan_id": "plan-gh-1",
                "active_revision": 1,
                "plan_digest": plan_digest(first),
                "revised_from_digest": "",
                "work_contract_digest": R32_CONTRACT_DIGEST,
                "authorization_epoch": 3,
                "publication_epoch": 1,
                "activated_by_decision": "",
            }
            run.evidence = merged
            await session.commit()

        decision = RevisionDecision(
            decision_id="rd-gh-1",
            work_id="wp-gh-1",
            parent_revision=1,
            proposed_revision_id=proposed_revision_identity(second),
            proposed_digest=plan_digest(second),
            work_contract_digest=R32_CONTRACT_DIGEST,
            authorization_epoch=3,
        )
        from forge.adaptive.revisions import ActivePlanState

        await stage_pending_revision(
            db,
            run_id,
            decision,
            second,
            ActivePlanState(
                work_id="wp-gh-1",
                plan_id="plan-gh-1",
                active_revision=1,
                work_contract_digest=R32_CONTRACT_DIGEST,
                authorization_epoch=3,
                publication_epoch=1,
            ),
        )
        outcome = await activate_pending_revision(
            db, run_id, decision.decision_id, decided_by="alice"
        )
        assert outcome.status == "activated"
        return plan_digest(first), plan_digest(second)

    async def test_approve_revision_then_go_dispatches_the_new_plan_digest(self, db, fake):
        service = self._service(db, fake)
        run_id = await start(service)
        old_digest, new_digest = await self._activate_revision(db, run_id)

        await go(service, run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value  # dispatched
        # the dispatch envelope carries the ACTIVE (new) plan digest —
        # not the original comment's — and the evidence freezes the same
        # binding beside the handle the reconciler restarts from.
        (dispatch,) = fake.dispatch_inputs
        assert dispatch["inputs"]["plan_digest"] == new_digest
        assert dispatch["inputs"]["plan_digest"] != old_digest
        binding = dict((run.evidence or {}).get("plan_binding") or {})
        assert binding["plan_digest"] == new_digest
        assert binding["revised_from_digest"] == old_digest
        assert binding["active_revision"] == 2
        # the run row follows the switch (the gate and every later leg
        # name the ACTIVE plan), and the consumed gate is the REBOUND
        # generation bound to the revised digest.
        assert run.plan_digest == new_digest
        async with db() as session:
            gates = (
                (
                    await session.execute(
                        select(GateApproval)
                        .where(GateApproval.flow_run_id == run_id)
                        .order_by(GateApproval.id)
                    )
                )
                .scalars()
                .all()
            )
        assert [g.generation for g in gates] == [0, 1]
        assert gates[0].plan_digest != new_digest and gates[0].consumed_at is None
        assert gates[1].plan_digest == new_digest and gates[1].consumed_at is not None

    async def test_a_stale_go_with_the_old_digest_is_refused_before_dispatch(self, db, fake):
        service = self._service(db, fake)
        run_id = await start(service)
        old_digest, new_digest = await self._activate_revision(db, run_id)
        clear_comments(fake)
        # The operator approved the OLD plan: the gate they consumed and
        # the run row still carry the superseded digest (a pre-revision
        # approval round — the world the dispatch fence exists for).
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.plan_digest = old_digest
            gate = (
                (
                    await session.execute(
                        select(GateApproval)
                        .where(GateApproval.flow_run_id == run_id)
                        .order_by(GateApproval.id.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .one()
            )
            gate.plan_digest = old_digest
            await session.commit()

        await go(service, run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert (run.status_reason or "").startswith("stale_plan_digest")
        assert not fake.calls_of("dispatch_workflow")  # nothing was dispatched
        [note] = [body for body in comments(fake) if "stale_plan_digest" in body]
        assert old_digest[:12] in note  # names both sides of the drift…
        assert new_digest[:12] in note
        assert "/go" in note and "/implement" in note  # …and the way out

    async def test_a_run_without_an_active_revision_dispatches_unchanged(self, db, fake):
        """The legacy /go (no revision ever activated) keeps the dispatch
        inputs byte-identical — no plan_digest input appears at all."""
        service = self._service(db, fake)
        run_id = await start(service)

        await go(service, run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value
        (dispatch,) = fake.dispatch_inputs
        assert "plan_digest" not in dispatch["inputs"]
        assert "plan_binding" not in (run.evidence or {})

    async def test_a_replanned_run_supersedes_the_revision_lineage(self, db, fake):
        """A fresh frozen plan drops any leftover ``active_plan`` pointer
        from a replaced plan — the new plan's digest is the dispatch
        identity, and a stale pointer would false-refuse the next /go."""
        from forge.adaptive.revisions import ACTIVE_PLAN_KEY

        service = self._service(db, fake)
        run_id = await start(service)
        old_digest, _new_digest = await self._activate_revision(db, run_id)
        assert ACTIVE_PLAN_KEY in (await get_run(db, run_id)).evidence

        # The replan (the config-block recovery pass re-enters this same
        # leg through the fenced plan-restart edge — the run is back at
        # preflight when planning restarts).
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.status = FlowStatus.PREFLIGHT.value
            await session.commit()
        await service._plan_and_publish(
            run_id,
            project_id=PROJECT_ID,
            issue_number=ISSUE,
            issue_title=ISSUE_TITLE,
            issue_description=ISSUE_DESC,
            author_username="alice",
        )

        run = await get_run(db, run_id)
        assert ACTIVE_PLAN_KEY not in (run.evidence or {})
        assert run.plan_digest != old_digest  # the fresh plan's own digest
        await go(service, run_id)
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_HARNESS.value  # dispatched clean
        (dispatch,) = fake.dispatch_inputs
        assert "plan_digest" not in dispatch["inputs"]


# ----------------------------------------------------------------------
# Q39-06 (#325): the reviewer-leg budget decision consults the closing
# reserve (A02 parity with the GitLab leg)
# ----------------------------------------------------------------------


class BudgetRefusedPRReviewer(StubPRReviewer):
    """Refuses the first N review calls exactly like the budget guard's
    ``LLMError("budget_exhausted")`` — the reviewer never ran (the call
    is refused before the provider is contacted)."""

    def __init__(self, refusals: int = 1) -> None:
        super().__init__()
        self._refusals = refusals
        self.refused = 0

    async def review(self, **kwargs):
        if self.refused < self._refusals:
            self.refused += 1
            raise LLMError("budget_exhausted")
        return await super().review(**kwargs)


async def drive_to_review_refusal(
    db, service: GitHubRunService, fake: FakeGitHub, reviewer: BudgetRefusedPRReviewer
) -> tuple[str, str]:
    """start → /go → the required check green → the review leg, where
    the reviewer's call is refused by the budget guard."""
    run_id, candidate = await drive_to_waiting_ci(db, service, fake)
    fake.seed_workflow_runs([workflow_run(candidate, "tests", "success")])
    service._stack = make_stack(fake, reviewer=reviewer)
    await service.evaluate_waiting_ci_one(run_id)
    return run_id, candidate


class TestReviewerBudgetDecisionConsultsTheClosingReserve:
    """The live-trace shape on the GitHub lane: candidate landed, the
    required check green, the budget guard refuses the REVIEWER's call.
    The decision consults the closing reserve — a budget refusal is
    never misclassified as ``review_failed``, the run ends in the
    precise non-ready state with the reserve visible, and the explicit
    review-only continuation completes it. Never a hidden retry."""

    async def test_a_budget_refusal_is_not_a_review_failure_and_holds_the_reserve(
        self, db, fake, monkeypatch
    ):
        monkeypatch.setenv("FORGE_CLOSING_RESERVE_USD", "0.60")
        monkeypatch.setenv("FORGE_SPEND_CAP_USD", "2.00")
        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        reviewer = BudgetRefusedPRReviewer(refusals=1)
        run_id, candidate = await drive_to_review_refusal(db, service, fake, reviewer)

        run = await get_run(db, run_id)
        # the precise NON-READY state — never blocked(review_failed:...)
        assert run.status == FlowStatus.REVIEWING.value
        assert not (run.status_reason or "").startswith("review_failed")
        block = (run.evidence or {})["review_budget_block"]
        assert block["budget_decision"] == "budget_exhausted"
        assert block["stage"] == "reviewer"
        assert block["released"] is False
        budget = block["budget"]
        assert budget["closing_reserve_usd"] == pytest.approx(0.60)
        assert budget["closing_review_fits"] is True
        for field in (
            "exact_usd",
            "known_subtotal_usd",
            "lower_bound_usd",
            "reserved_liability_usd",
            "unknown_intervals",
        ):
            assert field in budget

    async def test_resume_verification_never_re_attempts_the_review(self, db, fake, monkeypatch):
        monkeypatch.setenv("FORGE_CLOSING_RESERVE_USD", "0.60")
        monkeypatch.setenv("FORGE_SPEND_CAP_USD", "2.00")
        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        reviewer = BudgetRefusedPRReviewer(refusals=1)
        run_id, candidate = await drive_to_review_refusal(db, service, fake, reviewer)
        assert reviewer.refused == 1 and reviewer.calls == []

        await service.resume_verification(run_id)  # the scanner re-drive
        await service.resume_verification(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.REVIEWING.value  # still held
        assert reviewer.refused == 1  # NO hidden retry of the review call

    async def test_review_only_continuation_completes_the_github_review(
        self, db, fake, monkeypatch
    ):
        monkeypatch.setenv("FORGE_CLOSING_RESERVE_USD", "0.60")
        monkeypatch.setenv("FORGE_SPEND_CAP_USD", "2.00")
        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        reviewer = BudgetRefusedPRReviewer(refusals=1)
        run_id, candidate = await drive_to_review_refusal(db, service, fake, reviewer)
        shas_before = list((await get_run(db, run_id)).candidate_shas or [])

        outcome = await service.continue_review_only(
            run_id,
            operator="human:alice",
            top_up_usd=0.50,
            top_up_reason="close the review within its reserve",
        )
        assert outcome["allowed"] is True
        assert outcome["coder_dispatches"] == 0  # ZERO coder dispatches
        assert outcome["commits"] == 0  # ZERO commits

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        assert list(run.candidate_shas or []) == shas_before  # SAME candidate
        assert len(reviewer.calls) == 1  # the review ran exactly once more
        block = (run.evidence or {})["review_budget_block"]
        assert block["released"]["operator"] == "human:alice"
        assert block["top_up_total_usd"] == pytest.approx(0.50)
        assert block["top_ups"][0]["reason"] == "close the review within its reserve"

    async def test_a_moved_pr_head_invalidates_the_review_shortcut(self, db, fake, monkeypatch):
        monkeypatch.setenv("FORGE_CLOSING_RESERVE_USD", "0.60")
        monkeypatch.setenv("FORGE_SPEND_CAP_USD", "2.00")
        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        reviewer = BudgetRefusedPRReviewer(refusals=2)
        run_id, candidate = await drive_to_review_refusal(db, service, fake, reviewer)
        # the human push lands while the run is held in reviewing
        for pr in fake.pull_requests.get(REPO, []):
            pr["head"]["sha"] = "9" * 40

        outcome = await service.continue_review_only(run_id, operator="human:alice")
        assert outcome["allowed"] is False
        assert outcome["reason"] == "review_shortcut_stale"

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert (run.status_reason or "").startswith("review_shortcut_stale")
        assert reviewer.calls == []  # the stale shortcut never reviewed

    async def test_refusal_without_a_closing_policy_blocks_precisely(self, db, fake, monkeypatch):
        monkeypatch.delenv("FORGE_CLOSING_RESERVE_USD", raising=False)
        monkeypatch.delenv("FORGE_CLOSING_RESERVE_FRACTION", raising=False)
        monkeypatch.delenv("FORGE_SPEND_CAP_USD", raising=False)
        service = make_service(db, fake, settings=make_settings(FORGE_REQUIRED_JOBS="tests"))
        reviewer = BudgetRefusedPRReviewer(refusals=1)
        run_id, candidate = await drive_to_review_refusal(db, service, fake, reviewer)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert (run.status_reason or "").startswith("budget_exhausted: reviewer refused")
        assert "no closing reserve policy" in (run.status_reason or "")
        block = (run.evidence or {})["review_budget_block"]
        assert block["budget"]["closing_reserve_usd"] is None
