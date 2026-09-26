"""Tests for RunService: gate handling, stub propose→validate→commit→MR flow."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive.models import PlanRevision, PlanStep
from forge.config import Settings
from forge.durable import (
    ActionLog,
    FlowRun,
    FlowStatus,
    GateApproval,
    MRReservation,
    Outbox,
    RunSpec,
)
from forge.durable.controller import Controller
from forge.factory.llm import LLMError
from forge.gitlab.client import GitLabAPIError
from forge.models.base import Base
from forge.repository import WriteOutcome, WriteResult
from forge.runs import RunService
from forge.runs.service import task_digest_of
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

    def __init__(
        self, gitlab, session_factory, project_id: int, *, settle_seconds: int | None = None
    ) -> None:
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
        operation_key: str | None = None,
        *,
        provider: str = "gitlab",
        repo: str = "",
        idempotency_scope: str | None = None,
        commit_cycle: int = 1,
        content_digest: str | None = None,
    ) -> WriteResult:
        self.calls.append(
            {
                "flow_run_id": flow_run_id,
                "branch": cs.branch,
                "message": cs.commit_message,
                "changes": cs.changes,
                "start_ref": start_ref,
                "expected_head": expected_head,
                "operation_key": operation_key,
                "provider": provider,
                "repo": repo,
                "commit_cycle": commit_cycle,
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

    async def test_plan_comment_carries_the_implementation_block(self, db, fake_gitlab):
        """ADR-0023 §4: /go authorizes the execution shape — the five-line
        Implementation block sits between the plan body and the /go footer."""
        service = make_service(
            db, fake_gitlab, settings=make_settings(FORGE_IMPLEMENTER_BACKEND="ci_harness")
        )

        run_id = await start_issue_run(service)

        body = fake_gitlab.notes[0]["body"]
        assert "## Implementation" in body
        assert f"- Harness: **claude-code** · model {make_settings().FORGE_HARNESS_MODEL}" in body
        assert "- Fallbacks: none\n" in body
        assert "- Budget class: standard" in body
        assert "- Commit cycles: 3" in body
        assert "- Selection reason: default" in body
        # Footer order: the block precedes the digest line and the /go footer.
        assert body.index("## Implementation") < body.index("Plan digest")
        assert body.index("## Implementation") < body.index(f"/go {run_id}")
        assert body.index(f"/go {run_id}") < body.index("This is an automated message")

    async def test_plan_comment_lists_the_frozen_fallback_chain(self, db, fake_gitlab, tmp_path):
        config_path = tmp_path / "forge.yml"
        config_path.write_text(
            "forge:\n"
            "  implement:\n"
            "    harnesses:\n"
            "      - claude-code\n"
            "      - grok-build\n"
            "      - opencode\n"
        )
        from forge.config import ForgeConfig

        service = make_service(
            db,
            fake_gitlab,
            settings=make_settings(FORGE_IMPLEMENTER_BACKEND="ci_harness"),
            config=ForgeConfig(str(config_path)),
        )

        await start_issue_run(service)

        body = fake_gitlab.notes[0]["body"]
        assert "- Harness: **claude-code**" in body
        assert "- Fallbacks: grok-build, opencode" in body

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


class TestGoIdFormsAndRefusalNotes:
    """LIVE 2026-09-21: /go must accept the 8-char prefix the plan heading
    shows, and every ignore path must ANSWER the operator instead of
    silently succeeding — rate-limited to one reply per note id."""

    async def test_go_by_short_prefix_advances(self, service, fake_gitlab, db):
        run_id = await start_issue_run(service)

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id[:8]}", "alice", ISSUE_IID, author_user_id=11
        )

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        async with db() as session:
            (gate,) = (await session.execute(select(GateApproval))).scalars().all()
        assert gate.consumed_at is not None  # the short form consumed the real gate
        assert gate.approver_user_id == 11

    async def test_go_by_longer_prefix_advances(self, service, db):
        run_id = await start_issue_run(service)

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id[:12]}", "alice", ISSUE_IID, author_user_id=11
        )

        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_CI.value

    async def test_go_by_full_id_still_advances(self, service, db):
        run_id = await start_issue_run(service)

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_CI.value

    async def test_go_unknown_prefix_replies_with_valid_forms(self, service, fake_gitlab, db):
        run_id = await start_issue_run(service)
        fake_gitlab.notes = []  # drop the plan note — only the reply remains

        await service.handle_command_note(
            PROJECT_ID, "@forge /go e31fbae7", "alice", ISSUE_IID, delivery_id="note-1"
        )

        (note,) = note_bodies(fake_gitlab)
        assert "e31fbae7" in note
        assert "32-hex" in note  # names the valid id forms
        # Nothing was consumed or advanced.
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_APPROVAL.value
        async with db() as session:
            (gate,) = (await session.execute(select(GateApproval))).scalars().all()
        assert gate.consumed_at is None

    async def test_go_unknown_full_id_replies(self, service, fake_gitlab, db):
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {uuid4().hex}", "alice", ISSUE_IID, delivery_id="note-2"
        )

        # No plan note was posted (no run) — the refusal is the only note.
        (note,) = note_bodies(fake_gitlab)
        assert "32-hex" in note

    async def test_go_ambiguous_prefix_replies_with_candidates(self, service, fake_gitlab, db):
        run_id = await start_issue_run(service)
        fake_gitlab.notes = []
        # A dead twin sharing the 8-char prefix: the prefix is ambiguous.
        twin = run_id[:8] + "deadbeef" * 3
        async with db() as session:
            session.add(
                FlowRun(
                    id=twin,
                    project_id=PROJECT_ID,
                    issue_iid=ISSUE_IID,
                    provider="gitlab",
                    status=FlowStatus.FAILED.value,
                )
            )
            await session.commit()

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id[:8]}", "alice", ISSUE_IID, delivery_id="note-3"
        )

        (note,) = note_bodies(fake_gitlab)
        assert "matches 2 runs" in note
        assert run_id in note and twin in note  # both FULL ids listed
        # Ambiguity advances nothing.
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_APPROVAL.value

    async def test_non_approver_go_gets_a_note_and_stays_gated(self, service, fake_gitlab, db):
        run_id = await start_issue_run(service)
        fake_gitlab.notes = []

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id[:8]}", "mallory", ISSUE_IID, delivery_id="note-4"
        )

        (note,) = note_bodies(fake_gitlab)
        assert "mallory" in note and "approver" in note.lower()
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        async with db() as session:
            (gate,) = (await session.execute(select(GateApproval))).scalars().all()
        assert gate.consumed_at is None

    async def test_go_on_wrong_issue_gets_a_note(self, service, fake_gitlab, db):
        run_id = await start_issue_run(service)
        fake_gitlab.notes = []

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", 99, delivery_id="note-5"
        )

        (note,) = note_bodies(fake_gitlab)
        assert "different issue" in note
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_APPROVAL.value

    async def test_duplicate_go_is_a_no_op_and_replies_once_per_note_id(
        self, service, fake_gitlab, db
    ):
        run_id = await start_issue_run(service)
        note = f"@forge /go {run_id}"

        await service.handle_command_note(
            PROJECT_ID, note, "alice", ISSUE_IID, author_user_id=11, delivery_id="note-6"
        )
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_CI.value

        # The same note redelivered twice: a no-op for the run, and exactly
        # ONE duplicate reply — one reply per note id.
        await service.handle_command_note(
            PROJECT_ID, note, "alice", ISSUE_IID, author_user_id=11, delivery_id="note-6"
        )
        await service.handle_command_note(
            PROJECT_ID, note, "alice", ISSUE_IID, author_user_id=11, delivery_id="note-6"
        )

        duplicates = [body for body in note_bodies(fake_gitlab) if "duplicate" in body]
        assert len(duplicates) == 1
        assert run_id[:8] in duplicates[0]
        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_CI.value
        assert len(fake_gitlab.merge_requests) == 1  # no second advance
        assert len(FakeWriter.instances) == 1

        # A DIFFERENT note id earns its own single reply — the limit is per note.
        await service.handle_command_note(
            PROJECT_ID, note, "alice", ISSUE_IID, author_user_id=11, delivery_id="note-7"
        )
        duplicates = [body for body in note_bodies(fake_gitlab) if "duplicate" in body]
        assert len(duplicates) == 2


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

    async def test_unknown_commit_outcome_parks_run_blocked_without_retry(self, db, fake_gitlab):
        FakeWriter.reset()
        service = make_service(db, fake_gitlab)
        run_id = await start_issue_run(service)

        class UnknownWriter(FakeWriter):
            async def apply(
                self, flow_run_id, cs, start_ref="main", expected_head=None, **kwargs
            ) -> WriteResult:
                await super().apply(flow_run_id, cs, start_ref, expected_head, **kwargs)
                return WriteResult(WriteOutcome.UNKNOWN, None)

        service._writer_class = UnknownWriter
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )

        run = await get_run(db, run_id)
        assert (
            run.status == FlowStatus.BLOCKED.value
        )  # ADR-0005: unknown outcome never blind-retries
        assert run.status_reason == "commit_unknown_outcome"
        assert fake_gitlab.merge_requests == {}  # no MR after unresolved commit

    async def test_commit_api_error_parks_run_blocked(self, db, fake_gitlab):
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
        assert run.status == FlowStatus.BLOCKED.value  # 400 config error: fatal, no auto-retry
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


# ----------------------------------------------------------------------
# #29 lifecycle commands on the GitLab surface: issue-edit replan +
# trigger-label-off cancel — the mirror of the GitHub handlers
# ----------------------------------------------------------------------


async def go(service: RunService, run_id: str) -> None:
    """Approve at the gate; the builtin lane lands the run in waiting_ci."""
    await service.handle_command_note(
        PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
    )


def note_bodies(fake_gitlab: FakeGitLab) -> list[str]:
    return [note["body"] for note in fake_gitlab.notes]


class TestIssueEdited:
    async def test_edit_while_waiting_approval_replans(self, service, fake_gitlab, db):
        stale_id = await start_issue_run(service)
        fake_gitlab.notes = []
        new_body = "Users cannot reset their password. The reset mail bounces with SMTP 550."

        new_id = await service.handle_issue_edited(
            project_id=PROJECT_ID,
            issue_iid=ISSUE_IID,
            issue_title=ISSUE_TITLE,
            issue_body=new_body,
            author_username="alice",
        )

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
        bodies = note_bodies(fake_gitlab)
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

    async def test_redelivered_edit_is_a_no_op(self, service, fake_gitlab, db):
        """A redelivered edit (fresh run's snapshot already IS that text)
        must not spawn a third run or re-post anything."""
        await start_issue_run(service)
        new_body = "The issue body, edited once."
        first = await service.handle_issue_edited(
            project_id=PROJECT_ID,
            issue_iid=ISSUE_IID,
            issue_title=ISSUE_TITLE,
            issue_body=new_body,
            author_username="alice",
        )
        notes_after_first = len(fake_gitlab.notes)

        second = await service.handle_issue_edited(
            project_id=PROJECT_ID,
            issue_iid=ISSUE_IID,
            issue_title=ISSUE_TITLE,
            issue_body=new_body,
            author_username="alice",
        )

        assert second == first
        assert len(fake_gitlab.notes) == notes_after_first
        async with db() as session:
            runs = (await session.execute(select(FlowRun))).scalars().all()
        assert len(runs) == 2  # the stale run + the replan, nothing more

    async def test_gate_consumed_but_still_waiting_posts_note_only(self, service, fake_gitlab, db):
        """The "gate already consumed" guard: between consume_approval and
        the PROPOSING commit the run still reads waiting_approval — an edit
        in exactly that window must never cancel an approved run."""
        run_id = await start_issue_run(service)
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
        fake_gitlab.notes = []

        result = await service.handle_issue_edited(
            project_id=PROJECT_ID,
            issue_iid=ISSUE_IID,
            issue_title=ISSUE_TITLE,
            issue_body="an edited body",
            author_username="alice",
        )

        run = await get_run(db, run_id)
        assert result == run_id
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert run.cancel_requested is False
        (note,) = note_bodies(fake_gitlab)
        assert "not** in the approved plan" in note

    async def test_mid_flight_edit_notes_once_and_does_not_yank(self, service, fake_gitlab, db):
        run_id = await start_issue_run(service)
        await go(service, run_id)  # approved → published → waiting_ci
        fake_gitlab.notes = []

        result = await service.handle_issue_edited(
            project_id=PROJECT_ID,
            issue_iid=ISSUE_IID,
            issue_title=ISSUE_TITLE,
            issue_body="edited while the run executes",
            author_username="alice",
        )

        run = await get_run(db, run_id)
        assert result == run_id
        assert run.status == FlowStatus.WAITING_CI.value
        assert run.cancel_requested is False
        (note,) = note_bodies(fake_gitlab)
        assert "in flight" in note

    async def test_non_admitted_edit_is_ignored(self, service, fake_gitlab, db):
        run_id = await start_issue_run(service)
        fake_gitlab.notes = []

        result = await service.handle_issue_edited(
            project_id=PROJECT_ID,
            issue_iid=ISSUE_IID,
            issue_title=ISSUE_TITLE,
            issue_body="vandalism",
            author_username="mallory",
        )

        assert result is None
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert fake_gitlab.notes == []

    async def test_edit_without_an_issue_is_ignored(self, service, fake_gitlab, db):
        result = await service.handle_issue_edited(
            project_id=PROJECT_ID,
            issue_iid=None,
            issue_title=ISSUE_TITLE,
            issue_body="anything",
            author_username="alice",
        )
        assert result is None

    async def test_edited_command_dispatch_replans(self, service, fake_gitlab, db):
        """The gateway-normalized command drives the service end to end."""
        stale_id = await start_issue_run(service)
        metadata = {
            "command": "issue_edited",
            "provider": "gitlab",
            "project_id": PROJECT_ID,
            "issue_iid": ISSUE_IID,
            "issue_title": ISSUE_TITLE,
            "issue_body": "dispatched edit body",
            "author_username": "alice",
            "note_text": "",
            "note_id": "edit:12:abc:2026-03-22T09:15:00Z",
        }

        await service.run_command(metadata)

        stale = await get_run(db, stale_id)
        assert stale.status == FlowStatus.CANCELLED.value
        async with db() as session:
            runs = (await session.execute(select(FlowRun))).scalars().all()
        assert len(runs) == 2
        assert any(run.status == FlowStatus.WAITING_APPROVAL.value for run in runs)


class TestLabelOff:
    async def test_label_removal_cancels_the_gate_waiting_run(self, service, fake_gitlab, db):
        run_id = await start_issue_run(service)
        fake_gitlab.notes = []

        cancelled = await service.handle_label_removed(
            project_id=PROJECT_ID, issue_iid=ISSUE_IID, author_username="alice"
        )

        assert cancelled == 1
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.CANCELLED.value
        assert run.cancel_requested is True
        (note,) = note_bodies(fake_gitlab)
        assert "cancelled" in note and "label" in note

    async def test_label_removal_leaves_a_past_gate_run_alone(self, service, fake_gitlab, db):
        run_id = await start_issue_run(service)
        await go(service, run_id)  # the approval consumed the plan

        cancelled = await service.handle_label_removed(
            project_id=PROJECT_ID, issue_iid=ISSUE_IID, author_username="alice"
        )

        assert cancelled == 0
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value

    async def test_non_approver_label_removal_is_ignored(self, service, fake_gitlab, db):
        run_id = await start_issue_run(service)
        fake_gitlab.notes = []

        cancelled = await service.handle_label_removed(
            project_id=PROJECT_ID, issue_iid=ISSUE_IID, author_username="mallory"
        )

        assert cancelled == 0
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_APPROVAL.value
        assert fake_gitlab.notes == []

    async def test_unlabeled_command_dispatch_cancels(self, service, fake_gitlab, db):
        """The gateway-normalized command drives the service end to end."""
        run_id = await start_issue_run(service)
        metadata = {
            "command": "unlabeled",
            "provider": "gitlab",
            "project_id": PROJECT_ID,
            "issue_iid": ISSUE_IID,
            "author_username": "alice",
            "note_text": "",
            "note_id": "unlabel:12:2026-03-22T10:00:00Z",
        }

        await service.run_command(metadata)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.CANCELLED.value
        assert run.cancel_requested is True


class TestCiDeadlineBeforeIO:
    """R17 (deadline-before-I/O) on the GitLab verification pass: the durable
    ci_timeout and the cancel grant are LOCAL checks — a run past its budget
    parks blocked without a single GitLab read, so a permanently erroring API
    can never hold a run past FORGE_CI_WAIT_SECONDS."""

    async def _waiting_ci_run(self, service, db) -> str:
        run_id = await start_issue_run(service)
        await go(service, run_id)
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        return run_id

    async def _age_deadline(self, db, run_id: str, *, seconds: int) -> None:
        """Rewind the WAITING_CI outbox timestamp — the durable timer."""
        async with db() as session:
            rows = (
                (await session.execute(select(Outbox).where(Outbox.flow_run_id == run_id)))
                .scalars()
                .all()
            )
            for row in rows:
                if (row.payload or {}).get("to") == FlowStatus.WAITING_CI.value:
                    row.created_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
            await session.commit()

    async def test_expired_deadline_blocks_without_any_provider_call(
        self, service, fake_gitlab, db
    ):
        run_id = await self._waiting_ci_run(service, db)
        # Age past the EFFECTIVE budget (the environment may override the
        # config default) — provably behind, whatever the deployment sets.
        await self._age_deadline(db, run_id, seconds=make_settings().FORGE_CI_WAIT_SECONDS + 100)
        provider_calls = len(fake_gitlab.calls_of("get_branch_head")) + len(
            fake_gitlab.calls_of("list_pipelines")
        )

        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert run.status_reason == "ci_timeout"
        # The provider was NEVER called: no drift read, no pipeline listing.
        assert (
            len(fake_gitlab.calls_of("get_branch_head"))
            + len(fake_gitlab.calls_of("list_pipelines"))
            == provider_calls
        )

    async def test_cancel_requested_ignores_the_late_pass(self, service, fake_gitlab, db):
        run_id = await self._waiting_ci_run(service, db)
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            run.cancel_requested = True
            await session.commit()
        provider_calls = len(fake_gitlab.calls_of("get_branch_head")) + len(
            fake_gitlab.calls_of("list_pipelines")
        )

        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value  # terminal stays /cancel's
        assert (
            len(fake_gitlab.calls_of("get_branch_head"))
            + len(fake_gitlab.calls_of("list_pipelines"))
            == provider_calls
        )

    async def test_inside_the_deadline_the_provider_is_still_polled(self, service, fake_gitlab, db):
        """Control: within the budget the reconciler reads the provider (and
        a missing pipeline keeps the run waiting, not blocked)."""
        run_id = await self._waiting_ci_run(service, db)
        provider_calls = len(fake_gitlab.calls_of("get_branch_head"))

        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        assert len(fake_gitlab.calls_of("get_branch_head")) > provider_calls


# ----------------------------------------------------------------------
# B03: the MR reservation — logical intent vs immutable attempt history
# ----------------------------------------------------------------------


class TestMRReservations:
    """Migration 019 split the one-MR intent (mr_reservations, committed
    BEFORE provider I/O, FOR UPDATE-serialized) from the immutable action
    journal. The lost-response window — provider created the MR, the
    response died, the action row recorded unknown_outcome — used to
    deadlock adoption on InvalidActionTransition (review e53ffd2 B03)."""

    async def test_lost_response_adoption_never_rewrites_the_terminal_row(self, db, service):
        run_id = await start_issue_run(service)
        # The lost-response state: an unknown_outcome attempt row + the MR
        # already exists on the provider.
        async with db() as session:
            controller = Controller(session)
            action_id = await controller.record_action(
                run_id, "create_merge_request", correlation_id="factory/1/abc"
            )
            await controller.complete_action(action_id, "unknown_outcome")
            await session.commit()
        service._gitlab.seed_merge_request(
            PROJECT_ID, "factory/1/abc", "Draft: work", target="main", iid=444
        )

        mr_iid = await service._create_draft_mr(PROJECT_ID, run_id, "factory/1/abc", "sha-1")

        assert mr_iid == 444
        async with db() as session:
            rows = (
                (
                    await session.execute(
                        select(ActionLog)
                        .where(
                            ActionLog.flow_run_id == run_id,
                            ActionLog.action_kind == "create_merge_request",
                        )
                        .order_by(ActionLog.id)
                    )
                )
                .scalars()
                .all()
            )
            statuses = [row.status for row in rows]
            # the unknown history is IMMUTABLE — adoption journaled its own row
            assert statuses[0] == "unknown_outcome"
            assert statuses[-1] == "succeeded"
            assert (rows[-1].remote_result or {}).get("adopted") is True
            reservation = (
                (
                    await session.execute(
                        select(MRReservation).where(MRReservation.flow_run_id == run_id)
                    )
                )
                .scalars()
                .one()
            )
            assert reservation.status == "confirmed"
            assert reservation.mr_iid == 444

    async def test_a_failed_list_read_never_falls_through_to_create(self, db, service):
        """B03: 503/403 on the MR list proves nothing — fail closed."""
        run_id = await start_issue_run(service)

        async def exploding_list(*args, **kwargs):
            raise GitLabAPIError(503, "upstream unavailable")

        service._gitlab.list_merge_requests = exploding_list  # type: ignore[method-assign]

        with pytest.raises(GitLabAPIError):
            await service._create_draft_mr(PROJECT_ID, run_id, "factory/1/xyz", "sha-1")

        assert service._gitlab.calls_of("create_merge_request") == []
        async with db() as session:
            reservation = (
                (
                    await session.execute(
                        select(MRReservation).where(MRReservation.flow_run_id == run_id)
                    )
                )
                .scalars()
                .one()
            )
            assert reservation.status == "open"  # durable intent, unconfirmed

    async def test_the_reservation_is_durable_before_any_provider_io(self, db, service):
        """B03 criterion: the intent row is committed BEFORE any provider
        I/O — a separate session reads it back before create is called."""
        run_id = await start_issue_run(service)

        await service._reserve_mr(run_id, "factory/1/dur")  # its own commit
        assert service._gitlab.calls_of("create_merge_request") == []

        async with db() as other:  # a SECOND session sees the committed intent
            row = (
                (
                    await other.execute(
                        select(MRReservation).where(MRReservation.flow_run_id == run_id)
                    )
                )
                .scalars()
                .one()
            )
            assert row.status == "open"

        mr_iid = await service._create_draft_mr(PROJECT_ID, run_id, "factory/1/dur", "sha-1")
        assert mr_iid > 0

    async def test_a_confirmed_reservation_short_circuits_without_a_second_mr(self, db, service):
        run_id = await start_issue_run(service)
        first = await service._create_draft_mr(PROJECT_ID, run_id, "factory/1/one", "sha-1")
        second = await service._create_draft_mr(PROJECT_ID, run_id, "factory/1/one", "sha-1")

        assert first == second
        assert len(service._gitlab.calls_of("create_merge_request")) == 1


class TestBoundedMrIoC08:
    async def test_a_hung_provider_list_fails_closed_within_the_bound(self, db, service):
        """C08: a provider read that never answers must not pin the
        reservation lock forever — the bounded window fails closed (the
        reservation stays open, zero creates)."""
        run_id = await start_issue_run(service)

        async def hanging_list(*args, **kwargs):
            await asyncio.sleep(30)

        service._gitlab.list_merge_requests = hanging_list  # type: ignore[method-assign]
        service._settings = make_settings(FORGE_MR_IO_TIMEOUT_SECONDS=0.05)

        with pytest.raises((TimeoutError, GitLabAPIError)):
            await service._create_draft_mr(PROJECT_ID, run_id, "factory/1/hang", "sha-1")

        assert service._gitlab.calls_of("create_merge_request") == []

    async def test_a_hung_create_records_unknown_outcome(self, db, service):
        run_id = await start_issue_run(service)

        async def hanging_create(*args, **kwargs):
            await asyncio.sleep(30)

        service._gitlab.create_merge_request = hanging_create  # type: ignore[method-assign]
        service._settings = make_settings(FORGE_MR_IO_TIMEOUT_SECONDS=0.05)

        with pytest.raises(TimeoutError):
            await service._create_draft_mr(PROJECT_ID, run_id, "factory/1/hang2", "sha-1")

        async with db() as session:
            rows = (
                (
                    await session.execute(
                        select(ActionLog).where(
                            ActionLog.flow_run_id == run_id,
                            ActionLog.action_kind == "create_merge_request",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert rows[-1].status == "unknown_outcome"


# ----------------------------------------------------------------------
# Q39-02 (#321) — the GitLab dispatch briefs from the ACTIVE revision's
# TEXT. The production entry traces (tests/production_entry/
# test_gitlab_revision_rebind.py) prove the whole live-counterexample
# shape; these unit tests pin the dispatch-boundary rules over fakes.
# ----------------------------------------------------------------------


def _rebind_revision(revision: int, parent: int | None, summary: str) -> PlanRevision:
    step = PlanStep(
        step_id="S1",
        objective="Implement the entrypoint change.",
        write_repository_id="acme/forge",
        impact=["internal"],
        acceptance_refs=["AC-1"],
    )
    return PlanRevision(
        plan_id="plan-rebind",
        work_id="wp-rebind",
        revision=revision,
        parent_revision=parent,
        work_contract_digest="4" * 64,
        snapshot_set_digest="6" * 64,
        summary=summary,
        steps=[step],
    )


async def _activate_revision(db, run_id: str, first: PlanRevision, second: PlanRevision) -> None:
    """Revision 1 active, revision 2 staged + activated — the app's own
    durable transaction (the #313 staging precedent), never a hand-built
    pointer."""
    from forge.adaptive.revisions import (
        ActivePlanState,
        RevisionDecision,
        activate_pending_revision,
        plan_digest as rev_plan_digest,
        proposed_revision_identity,
        stage_pending_revision,
    )

    current = ActivePlanState(
        work_id=first.work_id,
        plan_id=first.plan_id,
        active_revision=first.revision,
        work_contract_digest=first.work_contract_digest,
        authorization_epoch=1,
        publication_epoch=1,
    )
    async with db() as session:
        run = await session.get(FlowRun, run_id)
        evidence = dict(run.evidence or {})
        evidence["active_plan"] = {
            "schema": "forge.revision.active-plan/1",
            "work_id": first.work_id,
            "plan_id": first.plan_id,
            "active_revision": first.revision,
            "plan_digest": rev_plan_digest(first),
            "revised_from_digest": "",
            "work_contract_digest": first.work_contract_digest,
            "authorization_epoch": 1,
            "publication_epoch": 1,
            "activated_by_decision": "",
        }
        run.evidence = evidence
        await session.commit()
    decision = RevisionDecision(
        decision_id=f"rd-rebind-{second.revision}",
        work_id=second.work_id,
        parent_revision=first.revision,
        proposed_revision_id=proposed_revision_identity(second),
        proposed_digest=rev_plan_digest(second),
        work_contract_digest=second.work_contract_digest,
        authorization_epoch=1,
    )
    await stage_pending_revision(db, run_id, decision, second, current, old=first)
    outcome = await activate_pending_revision(db, run_id, decision.decision_id, decided_by="alice")
    assert outcome.status == "activated", outcome.reason


def _harness_service(db, fake_gitlab) -> RunService:
    from forge.repository import ChangesetWriter

    return make_service(
        db,
        fake_gitlab,
        settings=make_settings(FORGE_IMPLEMENTER_BACKEND="ci_harness:claude-code"),
        writer_class=ChangesetWriter,
    )


async def _dispatches_of(fake_gitlab: FakeGitLab) -> list[dict]:
    return [
        {entry["key"]: entry["value"] for entry in call["variables"]}
        for call in fake_gitlab.pipeline_variables
    ]


class TestGitlabRevisionRebind:
    async def test_no_revision_dispatch_keeps_the_spec_brief_byte_identical(self, db, fake_gitlab):
        service = _harness_service(db, fake_gitlab)
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )
        (variables,) = await _dispatches_of(fake_gitlab)
        async with db() as session:
            row = (
                (await session.execute(select(RunSpec).where(RunSpec.run_id == run_id)))
                .scalars()
                .first()
            )
        spec_summary = row.document["plan"]["summary"]
        # Today's behavior, unchanged and labeled: the spec's plan text is
        # the brief, and NO rebind variable rides the envelope.
        assert variables["FORGE_PLAN"] == spec_summary
        assert "FORGE_PLAN_DIGEST" not in variables
        assert "FORGE_SPEC_DIGEST" not in variables
        assert "FORGE_BRIEF_ENVELOPE_DIGEST" not in variables
        run = await get_run(db, run_id)
        assert run.evidence["approved_input"]["source"] == "spec"

    async def test_an_activated_revision_rebinds_the_dispatch_brief(self, db, fake_gitlab):
        from forge.adaptive.revisions import (
            APPROVED_INPUT_KEY,
            REVISION_EXECUTOR_DIGEST_KEY,
            executor_input_digest,
            plan_digest as rev_plan_digest,
        )
        from forge.harnesses.brief_envelope import verify_brief_envelope

        service = _harness_service(db, fake_gitlab)
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )
        first = _rebind_revision(1, None, "Approach X: the entrypoint is check.")
        second = _rebind_revision(2, 1, "Approach Y: the entrypoint is validate_email.")
        await _activate_revision(db, run_id, first, second)

        # A repair re-dispatch: the same production leg every retry,
        # revival and repair funnel through.
        await service._advance_harness(
            PROJECT_ID, run_id, repair_context="verify job failed", repair_reason="code failure"
        )
        spec_dispatch, rebind_dispatch = await _dispatches_of(fake_gitlab)
        # AC-01: the brief carries revision 2's TEXT; the superseded spec
        # brief survives only in the FIRST dispatch (history) and
        # revision-1's text is nowhere in the new attempt's bytes.
        assert "validate_email" in rebind_dispatch["FORGE_PLAN"]
        assert "Approach X" not in rebind_dispatch["FORGE_PLAN"]
        assert "Implementation plan" in spec_dispatch["FORGE_PLAN"]
        assert "validate_email" not in spec_dispatch["FORGE_PLAN"]
        assert rebind_dispatch["FORGE_PLAN_DIGEST"] == rev_plan_digest(second)
        assert rebind_dispatch["FORGE_SPEC_DIGEST"] == (await get_run(db, run_id)).spec_digest

        # The brief envelope verifies over the dispatched bytes (the
        # runner-side re-verification shape).
        verify_brief_envelope(
            rebind_dispatch["FORGE_BRIEF_ENVELOPE_DIGEST"],
            run_id=run_id,
            task_title=ISSUE_TITLE,
            task_description=ISSUE_DESC,
            plan_text=rebind_dispatch["FORGE_PLAN"],
            spec_digest=rebind_dispatch["FORGE_SPEC_DIGEST"],
        )

        # THREE-WAY digest equality: the evidence document == a
        # recomputation from what the provider actually received.
        run = await get_run(db, run_id)
        approved = run.evidence[APPROVED_INPUT_KEY]
        executor = run.evidence[REVISION_EXECUTOR_DIGEST_KEY]
        assert approved["source"] == "revision"
        assert approved["plan_digest"] == rev_plan_digest(second)
        # The dispatched brief IS the approved plan text (the repair
        # context follows it, bounded, as before).
        assert rebind_dispatch["FORGE_PLAN"].startswith(approved["plan_text"])
        assert "## Repair context" in rebind_dispatch["FORGE_PLAN"]
        identity = {
            "run_id": rebind_dispatch["FORGE_RUN_ID"],
            "plan_digest": rebind_dispatch["FORGE_PLAN_DIGEST"],
            "envelope_digest": rebind_dispatch["FORGE_BRIEF_ENVELOPE_DIGEST"],
            "spec_digest": rebind_dispatch["FORGE_SPEC_DIGEST"],
            "lane_resume_mode": rebind_dispatch["FORGE_LANE_RESUME_MODE"],
        }
        assert executor["executor_input_digest"] == executor_input_digest(identity)
        assert executor["activated_by_decision"] == "rd-rebind-2"

    async def test_a_tampered_active_revision_blocks_the_dispatch(self, db, fake_gitlab):
        from forge.adaptive.revisions import REVISION_CONTENT_KEY

        service = _harness_service(db, fake_gitlab)
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )
        first = _rebind_revision(1, None, "Approach X.")
        second = _rebind_revision(2, 1, "Approach Y: validate_email.")
        await _activate_revision(db, run_id, first, second)
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            evidence = dict(run.evidence or {})
            active = dict(evidence["active_plan"])
            content = dict(active[REVISION_CONTENT_KEY])
            content["summary"] = "TAMPERED"
            active[REVISION_CONTENT_KEY] = content
            evidence["active_plan"] = active
            run.evidence = evidence
            await session.commit()

        await service._advance_harness(PROJECT_ID, run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "rebind_refused" in (run.status_reason or "")
        assert "content_digest_mismatch" in (run.status_reason or "")
        assert len(fake_gitlab.pipelines) == 1  # ZERO new provider dispatches
        bodies = [note["body"] for note in fake_gitlab.notes]
        assert any("rebind_refused" in body for body in bodies)

    async def test_a_required_resume_the_activation_routed_away_is_refused(self, db, fake_gitlab):
        service = _harness_service(db, fake_gitlab)
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )
        # A material revision that CHANGED the write-carrying step: the
        # activation's reuse decision routes the held checkpoint to an
        # explicit fresh attempt, and the persisted continuation decision
        # pins that checkpoint for a required resume.
        from forge.adaptive.revisions import CHECKPOINT_REUSE_DECISION_KEY

        first = _rebind_revision(1, None, "Approach X.")
        changed_step = PlanStep(
            step_id="S1",
            objective="REWRITTEN under the restrictive revision.",
            write_repository_id="acme/forge",
            impact=["internal"],
            acceptance_refs=["AC-1"],
        )
        second = PlanRevision(
            plan_id="plan-rebind",
            work_id="wp-rebind",
            revision=2,
            parent_revision=1,
            work_contract_digest="4" * 64,
            snapshot_set_digest="6" * 64,
            summary="Restrictive revision: the WIP no longer applies.",
            steps=[changed_step],
        )
        await _activate_revision(db, run_id, first, second)
        async with db() as session:
            run = await session.get(FlowRun, run_id)
            evidence = dict(run.evidence or {})
            evidence[CHECKPOINT_REUSE_DECISION_KEY] = {
                "schema": "forge.checkpoint.reuse-decision/1",
                "activated_revision": 2,
                "plan_digest": evidence["active_plan"]["plan_digest"],
                "route": "fresh_attempt",
                "route_reason": (
                    "write-carrying step(s) the checkpoint's WIP anchors on changed "
                    "or were invalidated by revision 2: ['S1'] — the restored bytes "
                    "cannot stand under the new plan"
                ),
                "artifacts": [
                    {"artifact_id": "ckpt-9", "kind": "checkpoint", "decision": "invalidate"}
                ],
            }
            evidence["continuation"] = {"checkpoint_digest": "ckpt-9", "decision_id": "cd-1"}
            run.evidence = evidence
            await session.commit()

        await service._advance_harness(PROJECT_ID, run_id, resume_mode="required")

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "checkpoint_reuse_refused" in (run.status_reason or "")
        assert "S1" in (run.status_reason or "")  # the rejected reuse, explained
        assert len(fake_gitlab.pipelines) == 1  # refused BEFORE any provider I/O
        bodies = [note["body"] for note in fake_gitlab.notes]
        assert any("checkpoint_reuse_refused" in body for body in bodies)


# ----------------------------------------------------------------------
# Q39-06 (#325): the reviewer-leg budget decision consults the closing
# reserve — the promised closing review is protected
# ----------------------------------------------------------------------


class BudgetRefusedReviewer(StubReviewer):
    """Refuses the first N review calls exactly like the budget guard's
    ``LLMError("budget_exhausted")`` (the reviewer never ran: the call is
    refused BEFORE the provider is contacted, so the stub records
    nothing)."""

    def __init__(self, refusals: int = 1) -> None:
        super().__init__()
        self._refusals = refusals
        self.refused = 0

    async def review(self, **kwargs):
        if self.refused < self._refusals:
            self.refused += 1
            raise LLMError("budget_exhausted")
        return await super().review(**kwargs)


def _verified_service(db, fake_gitlab, reviewer) -> RunService:
    """A service whose runs freeze a REAL verification contract (the
    required ``tests`` job) — R40-04: the review-only shortcut only ever
    fires on a verification bound to the tested candidate."""
    return make_service(
        db, fake_gitlab, reviewer=reviewer, settings=make_settings(FORGE_REQUIRED_JOBS="tests")
    )


async def drive_to_review_refusal(db, service, fake_gitlab) -> str:
    """start → /go → green pipeline (required job passed) on the candidate
    → the review leg, where the reviewer's call is refused by the budget
    guard."""
    run_id = await start_issue_run(service)
    await service.handle_command_note(
        PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
    )
    sha = "fake-sha-1"
    branch = factory_branch(ISSUE_IID, run_id)
    pipeline_id = (await fake_gitlab.create_pipeline(PROJECT_ID, branch))["id"]
    fake_gitlab.set_pipeline_status(pipeline_id, "success", sha)
    fake_gitlab.set_pipeline_jobs(pipeline_id, [{"id": 556, "name": "tests", "status": "success"}])
    fake_gitlab.seed_commit(branch, sha, "forge commit")  # head == candidate
    await service.evaluate_waiting_ci()
    return run_id


class TestReviewerBudgetDecisionConsultsTheClosingReserve:
    """The live-trace shape: candidate landed, independent CI green, the
    budget guard refuses the REVIEWER's call. The decision now consults
    the closing reserve — the run either completes its closing review
    within the reserve (via the explicit review-only continuation) or
    ends in the precise non-ready state with the reserve visible. Never
    a hidden retry."""

    async def test_refusal_with_an_intact_reserve_keeps_the_run_reviewing(
        self, db, fake_gitlab, monkeypatch
    ):
        monkeypatch.setenv("FORGE_CLOSING_RESERVE_USD", "0.60")
        monkeypatch.setenv("FORGE_SPEND_CAP_USD", "2.00")
        FakeWriter.reset()
        reviewer = BudgetRefusedReviewer(refusals=1)
        service = _verified_service(db, fake_gitlab, reviewer)
        run_id = await drive_to_review_refusal(db, service, fake_gitlab)

        run = await get_run(db, run_id)
        # the precise NON-READY state — not a terminal budget block
        assert run.status == FlowStatus.REVIEWING.value
        block = (run.evidence or {})["review_budget_block"]
        assert block["budget_decision"] == "budget_exhausted"
        assert block["stage"] == "reviewer"
        assert block["released"] is False
        budget = block["budget"]
        assert budget["closing_reserve_usd"] == pytest.approx(0.60)
        assert budget["closing_review_fits"] is True  # the reserve is intact
        assert budget["coder_ceiling_usd"] == pytest.approx(1.40)
        # the five distinguishable report fields ride the evidence
        for field in (
            "exact_usd",
            "known_subtotal_usd",
            "lower_bound_usd",
            "reserved_liability_usd",
            "unknown_intervals",
        ):
            assert field in budget

    async def test_a_scanner_re_drive_never_re_attempts_the_review(
        self, db, fake_gitlab, monkeypatch
    ):
        """The standing decision guards the leg: the reconciler's review
        resume stands down while the block is unreleased — the paid
        review call is never retried behind the operator's back."""
        monkeypatch.setenv("FORGE_CLOSING_RESERVE_USD", "0.60")
        monkeypatch.setenv("FORGE_SPEND_CAP_USD", "2.00")
        FakeWriter.reset()
        reviewer = BudgetRefusedReviewer(refusals=1)
        service = _verified_service(db, fake_gitlab, reviewer)
        run_id = await drive_to_review_refusal(db, service, fake_gitlab)
        assert reviewer.calls == []  # the refused call never recorded

        await service.evaluate_waiting_ci()  # the scanner re-drive
        await service.evaluate_waiting_ci()

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.REVIEWING.value  # still held
        assert reviewer.calls == []  # NO hidden retry of the review

    async def test_refusal_without_a_closing_policy_blocks_precisely(
        self, db, fake_gitlab, monkeypatch
    ):
        monkeypatch.delenv("FORGE_CLOSING_RESERVE_USD", raising=False)
        monkeypatch.delenv("FORGE_CLOSING_RESERVE_FRACTION", raising=False)
        monkeypatch.delenv("FORGE_SPEND_CAP_USD", raising=False)
        FakeWriter.reset()
        reviewer = BudgetRefusedReviewer(refusals=1)
        service = _verified_service(db, fake_gitlab, reviewer)
        run_id = await drive_to_review_refusal(db, service, fake_gitlab)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert (run.status_reason or "").startswith("budget_exhausted: reviewer refused")
        assert "no closing reserve policy" in (run.status_reason or "")
        block = (run.evidence or {})["review_budget_block"]
        assert block["budget"]["closing_reserve_usd"] is None  # honest unknown

    async def test_review_only_continuation_completes_the_review(
        self, db, fake_gitlab, monkeypatch
    ):
        """AC-04: after the explicit budget decision + an explicit,
        auditable amendment (R40-04: keyed by the native command identity),
        the continuation repeats ONLY the review of the SAME candidate —
        zero coder dispatches, zero new commits."""
        monkeypatch.setenv("FORGE_CLOSING_RESERVE_USD", "0.60")
        monkeypatch.setenv("FORGE_SPEND_CAP_USD", "2.00")
        FakeWriter.reset()
        reviewer = BudgetRefusedReviewer(refusals=1)
        service = _verified_service(db, fake_gitlab, reviewer)
        run_id = await drive_to_review_refusal(db, service, fake_gitlab)
        writer_calls_before = len(FakeWriter.instances[0].calls)
        shas_before = list((await get_run(db, run_id)).candidate_shas or [])

        outcome = await service.continue_review_only(
            run_id,
            operator="human:alice",
            command_id="run:continue_review:42:9101",
            axis="usd",
            amount=0.50,
            reason="close the review within its reserve",
        )
        assert outcome["allowed"] is True
        assert outcome["coder_dispatches"] == 0  # ZERO coder dispatches
        assert outcome["commits"] == 0  # ZERO commits
        assert outcome["amendment"]["applied"] is True  # the usd amendment
        assert outcome["delivery.review_only_calls"] == 1  # the observable

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        assert list(run.candidate_shas or []) == shas_before  # SAME candidate
        assert len(FakeWriter.instances[0].calls) == writer_calls_before  # no commit
        assert len(reviewer.calls) == 1  # the review ran exactly once more
        block = (run.evidence or {})["review_budget_block"]
        assert block["released"]["operator"] == "human:alice"
        assert block["released"]["command_id"] == "run:continue_review:42:9101"
        assert block["top_up_total_usd"] == pytest.approx(0.50)
        assert len(block["amendments"]) == 1
        assert block["amendments"][0]["axis"] == "usd"
        assert block["amendments"][0]["reason"] == "close the review within its reserve"

    async def test_a_moved_head_invalidates_the_review_shortcut(self, db, fake_gitlab, monkeypatch):
        """AC-05: a human push past the reviewed candidate invalidates
        the review-only shortcut with the TYPED staleness — the required
        verification reruns (the run parks, never a stale ready)."""
        monkeypatch.setenv("FORGE_CLOSING_RESERVE_USD", "0.60")
        monkeypatch.setenv("FORGE_SPEND_CAP_USD", "2.00")
        FakeWriter.reset()
        reviewer = BudgetRefusedReviewer(refusals=2)
        service = _verified_service(db, fake_gitlab, reviewer)
        run_id = await drive_to_review_refusal(db, service, fake_gitlab)
        # the human push lands while the run is held in reviewing
        branch = factory_branch(ISSUE_IID, run_id)
        fake_gitlab.seed_commit(branch, "9" * 40, "human push")

        outcome = await service.continue_review_only(run_id, operator="human:alice")
        assert outcome["allowed"] is False
        assert outcome["reason"] == "review_shortcut_stale"

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert (run.status_reason or "").startswith("review_shortcut_stale")
        assert reviewer.calls == []  # the stale shortcut never reviewed

    async def test_the_service_top_up_is_replay_idempotent(self, db, fake_gitlab, monkeypatch):
        """A still-refusing guard (the amendment did not reach the call
        axis yet): the SAME operator command retried adds its amount
        exactly once across refusal cycles — the native command identity,
        never the content, decides (R40-04)."""
        monkeypatch.setenv("FORGE_CLOSING_RESERVE_USD", "0.60")
        monkeypatch.setenv("FORGE_SPEND_CAP_USD", "2.00")
        FakeWriter.reset()
        reviewer = BudgetRefusedReviewer(refusals=99)
        service = _verified_service(db, fake_gitlab, reviewer)
        run_id = await drive_to_review_refusal(db, service, fake_gitlab)

        first = await service.continue_review_only(
            run_id,
            operator="human:alice",
            command_id="run:continue_review:42:9102",
            axis="usd",
            amount=0.50,
            reason="explicit closing allowance",
        )
        assert first["allowed"] is True  # the shortcut itself is sound
        assert first["amendment"]["applied"] is True
        replay = await service.continue_review_only(
            run_id,
            operator="human:alice",
            command_id="run:continue_review:42:9102",  # the SAME command
            axis="usd",
            amount=0.50,
            reason="explicit closing allowance",
        )
        assert replay["allowed"] is True
        assert replay["amendment"]["applied"] is False
        assert replay["amendment"]["replayed"] is True  # applied ONCE

        run = await get_run(db, run_id)
        # the reviewer kept refusing, so the run is held again — but the
        # retried command added its amount EXACTLY ONCE
        assert run.status == FlowStatus.REVIEWING.value
        block = (run.evidence or {})["review_budget_block"]
        assert block["top_up_total_usd"] == pytest.approx(0.50)
        assert len(block["amendments"]) == 1

    async def test_two_identical_amount_reason_commands_are_two_decisions(
        self, db, fake_gitlab, monkeypatch
    ):
        """R40-04 acceptance 4: two distinct top-up commands with identical
        amount/reason are TWO decisions — both apply; only a redelivery of
        ONE command (the same id) applies once."""
        monkeypatch.setenv("FORGE_CLOSING_RESERVE_USD", "0.60")
        monkeypatch.setenv("FORGE_SPEND_CAP_USD", "2.00")
        FakeWriter.reset()
        reviewer = BudgetRefusedReviewer(refusals=99)
        service = _verified_service(db, fake_gitlab, reviewer)
        run_id = await drive_to_review_refusal(db, service, fake_gitlab)

        first = await service.continue_review_only(
            run_id,
            operator="human:alice",
            command_id="run:continue_review:42:9201",
            axis="usd",
            amount=0.50,
            reason="identical wording",
        )
        second = await service.continue_review_only(
            run_id,
            operator="human:alice",
            command_id="run:continue_review:42:9202",  # a DIFFERENT note
            axis="usd",
            amount=0.50,
            reason="identical wording",  # identical amount + reason
        )
        assert first["amendment"]["applied"] is True
        assert second["amendment"]["applied"] is True  # a second decision
        assert second["amendment"]["replayed"] is False

        block = ((await get_run(db, run_id)).evidence or {})["review_budget_block"]
        assert block["top_up_total_usd"] == pytest.approx(1.00)  # both applied
        assert len(block["amendments"]) == 2

    async def test_an_amendment_without_a_command_identity_is_refused(
        self, db, fake_gitlab, monkeypatch
    ):
        """The amendment rides the ORIGINATING NATIVE COMMAND identity —
        a bare amount/reason cannot tell two decisions apart and is
        refused before anything moves."""
        monkeypatch.setenv("FORGE_CLOSING_RESERVE_USD", "0.60")
        monkeypatch.setenv("FORGE_SPEND_CAP_USD", "2.00")
        FakeWriter.reset()
        reviewer = BudgetRefusedReviewer(refusals=99)
        service = _verified_service(db, fake_gitlab, reviewer)
        run_id = await drive_to_review_refusal(db, service, fake_gitlab)

        outcome = await service.continue_review_only(
            run_id,
            operator="human:alice",
            top_up_usd=0.50,  # the legacy spelling, no command identity
            top_up_reason="anonymous",
        )
        assert outcome["allowed"] is False
        assert outcome["reason"] == "amendment_requires_command_identity"
        # nothing moved and the review never ran
        run = await get_run(db, run_id)
        assert run.status == FlowStatus.REVIEWING.value
        assert reviewer.calls == []

    async def test_an_expired_authority_invalidates_the_shortcut_before_paid_review(
        self, db, fake_gitlab, monkeypatch
    ):
        """R40-04 acceptance 6: the recorded decision's authority window
        passed — the shortcut refuses BEFORE any amendment or paid
        review; the run re-enters its normal gate, never a stale replay."""
        from datetime import datetime, timedelta, timezone

        monkeypatch.setenv("FORGE_CLOSING_RESERVE_USD", "0.60")
        monkeypatch.setenv("FORGE_SPEND_CAP_USD", "2.00")
        FakeWriter.reset()
        reviewer = BudgetRefusedReviewer(refusals=99)
        service = _verified_service(db, fake_gitlab, reviewer)
        run_id = await drive_to_review_refusal(db, service, fake_gitlab)
        run = await get_run(db, run_id)
        stale_block = dict((run.evidence or {})["review_budget_block"])
        stale_block["authority_expires_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        await service._merge_run_evidence(run_id, {"review_budget_block": stale_block})

        outcome = await service.continue_review_only(
            run_id,
            operator="human:alice",
            command_id="run:continue_review:42:9301",
            axis="usd",
            amount=0.50,
            reason="late",
        )
        assert outcome["allowed"] is False
        assert outcome["reason"] == "review_shortcut_authority_expired"

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.REVIEWING.value  # parked, not blocked
        assert reviewer.calls == []  # the paid review never ran
        assert (
            "amendments"
            not in (  # no amendment was even attempted
                (run.evidence or {})["review_budget_block"]
            )
            or not (run.evidence or {})["review_budget_block"]["amendments"]
        )

    async def test_missing_verification_invalidates_the_shortcut_before_paid_review(
        self, db, fake_gitlab, monkeypatch
    ):
        """R40-04 acceptance 6: no verification bound to the tested
        candidate — the shortcut refuses; a paid review must not ride an
        unproven candidate."""
        monkeypatch.setenv("FORGE_CLOSING_RESERVE_USD", "0.60")
        monkeypatch.setenv("FORGE_SPEND_CAP_USD", "2.00")
        FakeWriter.reset()
        reviewer = BudgetRefusedReviewer(refusals=99)
        service = _verified_service(db, fake_gitlab, reviewer)
        run_id = await drive_to_review_refusal(db, service, fake_gitlab)
        run = await get_run(db, run_id)
        evidence = dict(run.evidence or {})
        evidence.pop("verification", None)  # the fragment is GONE
        await service._merge_run_evidence(run_id, {"verification": None})
        from sqlalchemy import update

        from forge.durable import FlowRun as _FlowRun

        async with db() as session:
            await session.execute(
                update(_FlowRun).where(_FlowRun.id == run_id).values(evidence=evidence)
            )
            await session.commit()

        outcome = await service.continue_review_only(
            run_id,
            operator="human:alice",
            command_id="run:continue_review:42:9302",
            axis="usd",
            amount=0.50,
            reason="unproven",
        )
        assert outcome["allowed"] is False
        assert outcome["reason"] == "review_shortcut_unverified"
        assert reviewer.calls == []  # nothing paid happened

    async def test_a_restart_between_amendment_commit_and_review_dispatch(
        self, db, fake_gitlab, monkeypatch
    ):
        """R40-04 acceptance 7: the amendment commits durably BEFORE the
        re-drive; a crash right after the commit loses neither the top-up
        nor runs the review twice — the recovery scanner completes the
        review exactly once on the restarted service."""
        monkeypatch.setenv("FORGE_CLOSING_RESERVE_USD", "0.60")
        monkeypatch.setenv("FORGE_SPEND_CAP_USD", "2.00")
        FakeWriter.reset()
        reviewer = BudgetRefusedReviewer(refusals=1)
        service = _verified_service(db, fake_gitlab, reviewer)
        run_id = await drive_to_review_refusal(db, service, fake_gitlab)

        # Crash the re-drive leg AFTER the amendment's commit: the
        # continuation has applied + released, then the process dies.
        real_review_and_ready = service._review_and_ready

        async def crash_after_amendment(*args, **kwargs):
            raise RuntimeError("simulated crash between amendment and review")

        monkeypatch.setattr(service, "_review_and_ready", crash_after_amendment)
        with pytest.raises(RuntimeError, match="simulated crash"):
            await service.continue_review_only(
                run_id,
                operator="human:alice",
                command_id="run:continue_review:42:9303",
                axis="usd",
                amount=0.50,
                reason="pre-crash decision",
            )
        # The crash happened AFTER the amendment + release committed:
        run = await get_run(db, run_id)
        block = (run.evidence or {})["review_budget_block"]
        assert block["amendments"][0]["command_id"] == "run:continue_review:42:9303"
        assert block["top_up_total_usd"] == pytest.approx(0.50)

        # The restarted service (a fresh scanner pass) resumes the review.
        monkeypatch.setattr(service, "_review_and_ready", real_review_and_ready)
        await service._resume_review(run_id)

        run = await get_run(db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        assert len(reviewer.calls) == 1  # the review ran EXACTLY once
        block = (run.evidence or {})["review_budget_block"]
        assert len(block["amendments"]) == 1  # the top-up survived
        assert block["top_up_total_usd"] == pytest.approx(0.50)

        # A redelivery of the same command after the restart replays.
        replay = await service.continue_review_only(
            run_id,
            operator="human:alice",
            command_id="run:continue_review:42:9303",
            axis="usd",
            amount=0.50,
            reason="pre-crash decision",
        )
        assert replay["reason"] == "no_review_budget_block"  # run is ready —
        # the shortcut closed; the amendment was never re-applied.
