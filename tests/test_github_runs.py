"""GitHub run service tests (E3a): the plan + human gate path on FlowRun rows.

Drives :class:`forge.runs.github_service.GitHubRunService` over
:class:`tests.fixtures.fake_github.FakeGitHub` and the webhook payload
fixtures — the GitLab gate semantics (plan comment, pending decision,
cancel-as-revoke) exercised on the GitHub surface, no network, no model.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import ForgeConfig, Settings
from forge.durable import (
    FlowRun,
    FlowStatus,
    GateApproval,
    Outbox,
    RunSpec,
    StepRun,
    as_aware_utc,
)
from forge.factory.reviewer import ReviewVerdict
from forge.integrations.github_flow import GitHubAgents, GitHubPublishFlow
from forge.models.base import Base
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
) -> GitHubRunService:
    return GitHubRunService(
        db,
        settings or make_settings(),
        ForgeConfig(),
        stack=stack or make_stack(fake),
        repo_full_name=REPO,
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

        # The frozen RunSpec exists and its digest is what the decision binds.
        async with db() as session:
            spec = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .one()
            )
        assert spec.digest == run.spec_digest
        assert spec.document["subject"]["provider"] == "github"
        assert spec.document["source_base_oid"] == BASE_HEAD

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
        evidence_notes = [body for body in comments(fake) if "ready for human review" in body]
        assert len(evidence_notes) == 1
        assert pr["html_url"] in evidence_notes[0]
        assert pr["head"]["sha"] in evidence_notes[0]
        assert "Actions checks" in evidence_notes[0]

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
