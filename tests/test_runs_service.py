"""Tests for RunService: gate handling, stub propose→validate→commit→MR flow."""

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.config import Settings
from forge.durable import FlowRun, FlowStatus, GateApproval, Outbox
from forge.gitlab.client import GitLabAPIError
from forge.models.base import Base
from forge.repository import WriteOutcome, WriteResult
from forge.runs import RunService
from forge.runs.stubs import (
    StubImplementer,
    StubPlanner,
    StubReviewer,
    factory_branch,
    plan_digest_of,
)
from tests.fixtures.fake_gitlab import FakeGitLab

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


class FakeWriter:
    """Stands in for ChangesetWriter; records apply() calls per instance."""

    instances: list["FakeWriter"] = []

    # Behavior knobs are class attributes so subclasses can override them.
    outcome: WriteOutcome = WriteOutcome.COMMITTED
    raise_error: Exception | None = None

    def __init__(self, gitlab, session_factory, project_id: int) -> None:
        self.project_id = project_id
        self.calls: list[dict] = []
        FakeWriter.instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.instances = []

    async def apply(
        self,
        flow_run_id: str,
        cs,
        start_ref: str = "main",
        expected_head: str | None = None,
    ) -> WriteResult:
        self.calls.append(
            {
                "flow_run_id": flow_run_id,
                "branch": cs.branch,
                "message": cs.commit_message,
                "changes": cs.changes,
                "start_ref": start_ref,
                "expected_head": expected_head,
            }
        )
        if self.outcome is WriteOutcome.UNKNOWN:
            return WriteResult(WriteOutcome.UNKNOWN, None)
        if self.raise_error is not None:
            raise self.raise_error
        return WriteResult(WriteOutcome.COMMITTED, "fake-sha-1")


def make_service(db, fake_gitlab, **overrides) -> RunService:
    """RunService with deterministic stub agents (no LLM anywhere)."""
    values = dict(
        session_factory=db,
        gitlab=fake_gitlab,
        settings=make_settings(),
        writer_class=FakeWriter,
        planner=StubPlanner(),
        implementer=StubImplementer(),
        reviewer=StubReviewer(),
    )
    values.update(overrides)
    return RunService(**values)


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


async def outbox_targets(db, run_id: str) -> list[str]:
    async with db() as session:
        rows = (
            (
                await session.execute(
                    select(Outbox).where(Outbox.flow_run_id == run_id).order_by(Outbox.id)
                )
            )
            .scalars()
            .all()
        )
        return [row.payload["to"] for row in rows]


async def start_issue_run(service: RunService) -> str:
    # ADR-0018 §3: only approvers may start runs — the /implement author is
    # an approver here; non-approver denial is covered by its own tests.
    return await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")


class TestStartRun:
    async def test_posts_plan_comment_and_parks_at_gate(self, service, fake_gitlab, db):
        run_id = await start_issue_run(service)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert run.project_id == PROJECT_ID
        assert run.issue_iid == ISSUE_IID
        assert run.plan_digest  # plan digest recorded for the gate
        assert run.base_sha == "base-sha-1"  # pinned snapshot head (ADR-0006)

        # The plan comment carries the gate instruction and approver info.
        assert len(fake_gitlab.notes) == 1
        body = fake_gitlab.notes[0]["body"]
        assert f"@forge /go {run_id}" in body
        assert run.plan_digest in body
        assert "@alice" in body  # approvers named in the instruction

    async def test_plan_digest_is_sha256_of_plan(self, service, db):
        run_id = await start_issue_run(service)
        run = await get_run(db, run_id)
        expected = plan_digest_of(await StubPlanner().plan(ISSUE_TITLE, ISSUE_DESC))
        assert run.plan_digest == expected

    async def test_plan_evidence_stored(self, service, db):
        """The plan summary is folded into the run's evidence (ADR-0008)."""
        run_id = await start_issue_run(service)
        run = await get_run(db, run_id)
        plan_evidence = (run.evidence or {}).get("plan") or {}
        assert plan_evidence["digest"] == run.plan_digest
        assert "Implementation plan" in plan_evidence["summary"]

    async def test_transitions_journaled_in_outbox(self, service, db):
        run_id = await start_issue_run(service)
        targets = await outbox_targets(db, run_id)
        assert targets == ["preflight", "planning", "waiting_approval"]


class TestGate:
    async def test_go_by_non_approver_is_ignored(self, service, fake_gitlab, db):
        run_id = await start_issue_run(service)

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "mallory", ISSUE_IID, author_user_id=66
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        async with db() as session:
            gates = (await session.execute(select(GateApproval))).scalars().all()
        # F15: the decision was created at plan publication; a non-approver's
        # /go leaves it pending and unconsumed.
        assert len(gates) == 1
        assert gates[0].consumed_at is None

    async def test_go_without_run_id_is_ignored(self, service, db):
        # No run id in the command: parse returns nothing — must not raise.
        await service.handle_command_note(PROJECT_ID, "@forge /go", "alice", ISSUE_IID)
        async with db() as session:
            runs = (await session.execute(select(FlowRun))).scalars().all()
        assert runs == []

    async def test_go_for_unknown_run_is_ignored(self, service, db):
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {uuid4().hex}", "alice", ISSUE_IID
        )  # must not raise

    async def test_go_on_wrong_issue_is_ignored(self, service, db):
        run_id = await start_issue_run(service)
        await service.handle_command_note(PROJECT_ID, f"@forge /go {run_id}", "alice", 99)
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value

    async def test_go_by_approver_advances_to_waiting_ci(self, service, fake_gitlab, db):
        run_id = await start_issue_run(service)

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        assert run.mr_iid is not None
        assert run.candidate_shas == ["fake-sha-1"]

        # The journaled commit used the stub branch, message and the
        # FROZEN base as start ref (review F03) — never the live target.
        assert len(FakeWriter.instances) == 1
        (call,) = FakeWriter.instances[0].calls
        assert call["flow_run_id"] == run_id
        assert call["branch"] == factory_branch(ISSUE_IID, run_id)
        assert call["message"] == f"forge: implement {ISSUE_IID} (run {run_id[:8]})"
        assert call["start_ref"] == run.base_sha

        # The Draft MR follows GitLab draft convention and carries evidence.
        (mr,) = fake_gitlab.merge_requests.values()
        assert mr["title"] == f"Draft: {ISSUE_TITLE}"
        assert run.plan_digest in mr["description"]
        assert "fake-sha-1" in mr["description"]
        assert mr["source_branch"] == factory_branch(ISSUE_IID, run_id)
        assert mr["target_branch"] == "main"

    async def test_gate_recorded_and_consumed_once(self, service, db):
        run_id = await start_issue_run(service)
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )
        async with db() as session:
            (gate,) = (await session.execute(select(GateApproval))).scalars().all()
        assert gate.approver_user_id == 11
        assert gate.consumed_at is not None
        assert gate.plan_digest == (await get_run(db, run_id)).plan_digest

    async def test_redelivered_go_note_does_not_duplicate_work(self, service, fake_gitlab, db):
        run_id = await start_issue_run(service)
        note = f"@forge /go {run_id}"

        await service.handle_command_note(PROJECT_ID, note, "alice", ISSUE_IID, author_user_id=11)
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_CI.value

        # The same note arrives again (queue redelivery): idempotent no-op.
        await service.handle_command_note(PROJECT_ID, note, "alice", ISSUE_IID, author_user_id=11)

        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_CI.value
        assert len(fake_gitlab.merge_requests) == 1
        assert len(FakeWriter.instances) == 1


class TestAdvanceFailures:
    async def test_changeset_violations_block_the_run(self, db, fake_gitlab, monkeypatch):
        import forge.runs.service as service_module

        monkeypatch.setattr(
            service_module,
            "validate_changeset",
            lambda cs, git_base=None, allowed_paths=None: ["forbidden path"],
        )
        FakeWriter.reset()
        service = make_service(db, fake_gitlab)
        run_id = await start_issue_run(service)
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "changeset_invalid" in run.status_reason
        assert "forbidden path" in run.status_reason
        assert fake_gitlab.merge_requests == {}
        assert FakeWriter.instances == []  # never reached the commit step

    async def test_unknown_commit_outcome_fails_run_without_retry(self, db, fake_gitlab):
        FakeWriter.reset()
        service = make_service(db, fake_gitlab)
        run_id = await start_issue_run(service)

        class UnknownWriter(FakeWriter):
            async def apply(
                self, flow_run_id, cs, start_ref="main", expected_head=None
            ) -> WriteResult:
                await super().apply(flow_run_id, cs, start_ref, expected_head)
                return WriteResult(WriteOutcome.UNKNOWN, None)

        service._writer_class = UnknownWriter
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.FAILED.value
        assert run.status_reason == "commit_unknown_outcome"
        assert fake_gitlab.merge_requests == {}  # no MR after unresolved commit

    async def test_commit_api_error_fails_run(self, db, fake_gitlab):
        FakeWriter.reset()
        service = make_service(db, fake_gitlab)
        run_id = await start_issue_run(service)

        class ErrorWriter(FakeWriter):
            raise_error = GitLabAPIError(400, "bad actions")

        service._writer_class = ErrorWriter
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.FAILED.value
        assert run.status_reason.startswith("commit_failed")


class TestStubContract:
    async def test_stub_changeset_shape(self, db, fake_gitlab):
        """The stub proposes exactly one CREATE on the run-owned branch."""
        service = make_service(db, fake_gitlab)
        run_id = await start_issue_run(service)

        async with db() as session:
            run = await session.get(FlowRun, run_id)
            cs = await StubImplementer().propose(run, ISSUE_TITLE)

        short = run_id[:8]
        assert cs.branch == f"factory/{ISSUE_IID}/{short}"
        assert cs.commit_message == f"forge: implement {ISSUE_IID} (run {short})"
        assert len(cs.changes) == 1
        change = cs.changes[0]
        assert change.operation.value == "create"
        assert change.path == f"forge-demo/run-{short}.md"
        assert run_id in change.content
        assert ISSUE_TITLE in change.content


class TestControllerGraphConsistency:
    async def test_full_loop_records_one_outbox_row_per_hop(self, service, fake_gitlab, db):
        """Every status change went through Controller.transition (ADR-0004)."""
        run_id = await start_issue_run(service)
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )
        assert (await get_run(db, run_id)).candidate_shas == ["fake-sha-1"]

        # Drive the reconciler success leg on the exact candidate sha.
        sha = "fake-sha-1"
        branch = factory_branch(ISSUE_IID, run_id)
        pipeline_id = (await fake_gitlab.create_pipeline(PROJECT_ID, branch))["id"]
        fake_gitlab.set_pipeline_status(pipeline_id, "success", sha)
        fake_gitlab.seed_commit(branch, sha, "forge commit")  # head == candidate
        await service.evaluate_waiting_ci()

        targets = await outbox_targets(db, run_id)
        assert targets == [
            "preflight",
            "planning",
            "waiting_approval",
            "proposing",
            "validating",
            "committing",
            "ensuring_draft_mr",
            "waiting_ci",
            "evaluating_ci",
            "reviewing",
            "ready_for_human",
        ]
        assert (await get_run(db, run_id)).status == FlowStatus.READY_FOR_HUMAN.value


async def test_head_checks_use_branch_endpoint_not_commit_history(service, fake_gitlab, db):
    """F28: reading the target-branch head must NOT paginate commit history.

    _read_base_sha pins the base at planning time; after the F28 switch it
    is a single branch-object read (get_branch_head), never list_commits.
    """
    base = await service._read_base_sha(PROJECT_ID)

    assert base == "base-sha-1"
    assert fake_gitlab.calls_of("list_commits") == []
    assert fake_gitlab.calls_of("get_branch_head")
