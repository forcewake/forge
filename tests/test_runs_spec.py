"""Stage B2 (ADR-0018): RunSpec, pending decision with deadline, admission.

- F14: the immutable RunSpec is frozen at plan acceptance — canonical JSON
  document + digest, mirrored into ``run.spec_digest`` — before the plan note
  is posted. The extended policy digest binds the effective execution policy.
- F15: the pending decision is created at plan publication carrying the
  plan/task/spec digests and an absolute deadline; ``/go`` consumes THAT row;
  expiry or spec/policy drift invalidates it; issue-text drift after approval
  is flagged in the evidence comment.
- F16: admission refuses non-approvers (and a bot-in-approvers config)
  BEFORE the planner — a denial never burns a model call.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import Settings
from forge.durable import Controller, FlowRun, FlowStatus, GateApproval, RunSpec
from forge.models.base import Base
from forge.runs import RunService
from forge.runs.service import task_digest_of
from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer, factory_branch
from tests.fixtures.fake_gitlab import FakeGitLab
from tests.test_runs_service import FakeWriter

PROJECT_ID = 42
ISSUE_IID = 7
ISSUE_TITLE = "Add a widget"
ISSUE_DESC = "Widgets make the app better."


def make_settings(**overrides) -> Settings:
    values = dict(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN="glpat-test",  # noqa: S105 — fake value for tests
        GITLAB_WEBHOOK_SECRET="whsec",  # noqa: S105
        FORGE_IMPLEMENTER_BACKEND="builtin",
        FORGE_APPROVERS="alice",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
    )
    values.update(overrides)
    return Settings(**values)


class RecordingPlanner(StubPlanner):
    """StubPlanner that counts calls — admission denials must never call it."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls = 0

    async def plan(self, *args, **kwargs) -> str:
        self.calls += 1
        return await super().plan(*args, **kwargs)


def make_service(db, fake_gitlab, *, settings=None, planner=None) -> RunService:
    return RunService(
        session_factory=db,
        gitlab=fake_gitlab,
        settings=settings or make_settings(),
        writer_class=FakeWriter,
        planner=planner or StubPlanner(),
        implementer=StubImplementer(),
        reviewer=StubReviewer(),
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
def fake_gitlab() -> FakeGitLab:
    fake = FakeGitLab()
    fake.seed_issue(ISSUE_IID, ISSUE_TITLE, ISSUE_DESC)
    fake.seed_commit("main", "base-sha-1", "initial")
    return fake


@pytest.fixture()
def service(db, fake_gitlab):
    FakeWriter.reset()
    return make_service(db, fake_gitlab)


async def get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        return await session.get(FlowRun, run_id)


async def get_gate(db, run_id: str) -> GateApproval | None:
    async with db() as session:
        gate = (
            (
                await session.execute(
                    select(GateApproval)
                    .where(GateApproval.flow_run_id == run_id)
                    .order_by(GateApproval.id.desc())
                )
            )
            .scalars()
            .first()
        )
        if gate is not None:
            session.expunge(gate)
        return gate


async def get_spec(db, run_id: str) -> RunSpec | None:
    async with db() as session:
        spec = (
            (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
            .scalars()
            .first()
        )
        if spec is not None:
            session.expunge(spec)
        return spec


async def drive_to_waiting_ci(service, fake_gitlab: FakeGitLab, db) -> str:
    """start_run → /go → committed candidate waiting for CI."""
    run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
    await service.handle_command_note(
        PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
    )
    branch = factory_branch(ISSUE_IID, run_id)
    sha = (await get_run(db, run_id)).candidate_shas[-1]
    fake_gitlab.seed_commit(branch, sha, "forge commit")
    pipeline_id = (await fake_gitlab.create_pipeline(PROJECT_ID, branch))["id"]
    fake_gitlab.set_pipeline_status(pipeline_id, "success", sha)
    return run_id


def sha256_of_document(document: dict) -> str:
    """An independent canonical-JSON sha256 (pins the digest contract)."""
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode("utf-8")).hexdigest()


class TestRunSpec:
    async def test_spec_frozen_at_plan_acceptance(self, service, fake_gitlab, db):
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        run = await get_run(db, run_id)
        spec = await get_spec(db, run_id)
        assert spec is not None
        assert spec.run_id == run_id
        assert spec.schema_version == 1
        assert run.spec_digest == spec.digest

        document = spec.document
        assert document["subject"] == {"project_id": PROJECT_ID, "issue_iid": ISSUE_IID}
        assert document["source_base_oid"] == "base-sha-1"  # run.base_sha
        assert document["plan_digest"] == run.plan_digest
        assert document["task_digest"] == task_digest_of(ISSUE_TITLE, ISSUE_DESC)
        assert document["policy_digest"] == service._policy_digest()
        assert document["backend_config"] == {
            "backend": "builtin",
            "model": make_settings().FORGE_HARNESS_MODEL,
            "target_branch": "main",
        }
        assert document["budgets"] == {"commit_cycles": 3, "harness_timeout": 1800}

        # The digest is the sha256 over the canonical (sorted-key) JSON.
        assert spec.digest == sha256_of_document(document)

    async def test_policy_digest_binds_effective_policy(self, service):
        settings = make_settings(FORGE_REQUIRED_JOBS="pytest", FORGE_TARGET_BRANCH="release")
        scoped = make_service(None, None, settings=settings)  # type: ignore[arg-type]
        document = {
            "approvers": ["alice"],
            "target_branch": "release",
            "required_jobs": ["pytest"],
            "implementer_backend": "builtin",
            "harness_model": settings.FORGE_HARNESS_MODEL,
        }
        assert scoped._policy_digest() == sha256_of_document(document)
        # The default policy digest differs once any policy input moves.
        assert scoped._policy_digest() != service._policy_digest()

    async def test_setting_drift_after_start_changes_the_policy_digest(self, service, db):
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        gate = await get_gate(db, run_id)
        spec = await get_spec(db, run_id)
        before = service._policy_digest()

        service._settings.FORGE_REQUIRED_JOBS = "pytest"

        assert service._policy_digest() != before
        assert gate.policy_digest == before  # the decision froze the old policy
        assert spec.document["policy_digest"] == before


class TestPendingDecision:
    async def test_decision_created_at_plan_publication(self, service, fake_gitlab, db):
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        run = await get_run(db, run_id)
        gate = await get_gate(db, run_id)

        assert gate is not None
        assert gate.generation == 0
        assert gate.consumed_at is None
        assert gate.approver_user_id == 0  # no approver yet — recorded at /go
        assert gate.plan_digest == run.plan_digest
        assert gate.base_sha == run.base_sha
        assert gate.policy_digest == service._policy_digest()
        assert gate.spec_digest == run.spec_digest
        assert gate.task_digest == task_digest_of(ISSUE_TITLE, ISSUE_DESC)
        # Absolute deadline: FORGE_DECISION_TTL_SECONDS after publication.
        ttl = make_settings().FORGE_DECISION_TTL_SECONDS
        span = (gate.expires_at - gate.created_at).total_seconds()
        assert abs(span - ttl) < 5

    async def test_go_consumes_the_pending_decision(self, service, fake_gitlab, db):
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        gate = await get_gate(db, run_id)
        assert gate.consumed_at is not None
        assert gate.approver_user_id == 11
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_CI.value

    async def test_go_without_pending_decision_is_ignored(self, db, fake_gitlab):
        """A waiting_approval run whose decision row is missing: /go is a no-op."""
        service = make_service(db, fake_gitlab)
        run_id = uuid4().hex
        async with db() as session:
            controller = Controller(session)
            session.add(FlowRun(id=run_id, project_id=PROJECT_ID, issue_iid=ISSUE_IID))
            await controller.transition(run_id, FlowStatus.PREFLIGHT)
            await controller.transition(run_id, FlowStatus.PLANNING)
            await controller.transition(run_id, FlowStatus.WAITING_APPROVAL)
            await session.commit()

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_APPROVAL.value

    async def test_expired_decision_is_invalid(self, service, db):
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
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
            gate.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.commit()

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_APPROVAL.value
        gate = await get_gate(db, run_id)
        assert gate.consumed_at is None

    async def test_spec_digest_drift_invalidates_go(self, service, db):
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.spec_digest = "f" * 64  # the spec the gate froze moved
            await session.commit()

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_APPROVAL.value
        assert (await get_gate(db, run_id)).consumed_at is None

    async def test_issue_text_drift_is_flagged_in_evidence(self, service, fake_gitlab, db):
        run_id = await drive_to_waiting_ci(service, fake_gitlab, db)

        # The issue body changed AFTER the approval — the run still executes
        # the approved task snapshot and says so in the evidence comment.
        fake_gitlab.seed_issue(ISSUE_IID, "A different task", "The body moved on.")
        await service.evaluate_waiting_ci()

        assert fake_gitlab.notes_containing(
            "⚠️ issue text changed since approval; the run executed the approved task snapshot"
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value

    async def test_no_issue_drift_no_warning(self, service, fake_gitlab, db):
        await drive_to_waiting_ci(service, fake_gitlab, db)
        await service.evaluate_waiting_ci()

        assert fake_gitlab.notes, "evidence comment posted"
        assert not fake_gitlab.notes_containing("issue text changed")


class TestAdmission:
    async def test_non_approver_implement_is_denied_before_the_planner(self, db, fake_gitlab):
        planner = RecordingPlanner()
        service = make_service(db, fake_gitlab, planner=planner)

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "mallory")

        # F16: the refusal is comment-only — no planner (LLM) call happened.
        assert planner.calls == 0
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason == "admission_denied: actor @mallory not in approvers"
        assert await get_spec(db, run_id) is None  # no spec frozen either
        assert fake_gitlab.notes_containing("admission denied")
        assert fake_gitlab.notes_containing("@mallory")
        assert not fake_gitlab.notes_containing("Forge plan")

    async def test_bot_username_in_approvers_is_a_config_contradiction(self, db, fake_gitlab):
        settings = make_settings(FORGE_APPROVERS="alice,forge-bot", FORGE_BOT_USERNAME="forge-bot")
        planner = RecordingPlanner()
        service = make_service(db, fake_gitlab, settings=settings, planner=planner)

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        assert planner.calls == 0
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "must not appear in FORGE_APPROVERS" in run.status_reason

    async def test_approver_implement_is_admitted(self, db, fake_gitlab):
        planner = RecordingPlanner()
        service = make_service(db, fake_gitlab, planner=planner)

        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        assert planner.calls == 1
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_APPROVAL.value
