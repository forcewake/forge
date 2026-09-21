"""NXT-01: the typed /go resolver's subject scoping and dispatch ingress.

The resolver behind ``RunService.handle_command_note`` returns a
discriminated outcome (``_ResolvedGo`` / ``_RefusedGo`` — see
:mod:`forge.runs.service`); these tests pin the SEMANTICS that typed shape
must preserve, on top of the id-form/refusal cases already covered by
``tests/test_runs_service.py``:

- provider/project/issue predicates are identical for the full-id and the
  prefix forms, so two projects SHARING issue numbers never resolve each
  other's runs (NXT-01 negative test);
- the signed gateway ingress (``run_command``'s ``command="go"`` dispatch)
  reaches the same resolver path as direct note handling;
- a redelivered note (same delivery id) consumes the gate exactly once and
  never re-advances.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import pytest

from forge.config import Settings
from forge.durable import FlowRun, FlowStatus, GateApproval
from forge.models.base import Base
from forge.runs import RunService
from forge.runs.stubs import StubImplementer, StubPlanner, StubReviewer
from tests.fixtures.fake_gitlab import FakeGitLab

from tests.test_runs_service import FakeWriter, PROJECT_ID, ISSUE_IID, ISSUE_DESC, ISSUE_TITLE


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


def make_service(db, fake_gitlab) -> RunService:
    """RunService with deterministic stub agents (no LLM anywhere)."""
    return RunService(
        session_factory=db,
        gitlab=fake_gitlab,
        settings=make_settings(),
        writer_class=FakeWriter,
        planner=StubPlanner(),
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


async def gates_of(db, run_id: str) -> list[GateApproval]:
    async with db() as session:
        return [
            gate
            for gate in (
                (
                    await session.execute(
                        select(GateApproval).where(GateApproval.flow_run_id == run_id)
                    )
                )
                .scalars()
                .all()
            )
        ]


def note_bodies(fake_gitlab: FakeGitLab) -> list[str]:
    return [note["body"] for note in fake_gitlab.notes]


# A SECOND project on the same GitLab connection with the SAME issue iid —
# the NXT-01 negative-test shape: identical issue numbers must not blur the
# subject scope of either identifier form.
OTHER_PROJECT_ID = 43


class TestResolverScopesToThePostingProject:
    async def test_full_id_of_the_other_project_is_unknown_here(self, service, fake_gitlab, db):
        run_a = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        run_b = await service.start_run(
            OTHER_PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice"
        )
        fake_gitlab.notes = []  # drop the plan notes — only the reply remains

        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_b}", "alice", ISSUE_IID, delivery_id="scope-1"
        )

        (note,) = note_bodies(fake_gitlab)
        assert "matched no run" in note  # unknown, NOT silently adopted
        # Both runs untouched: B keeps its gate, A was never advanced.
        assert (await get_run(db, run_b)).status == FlowStatus.WAITING_APPROVAL.value
        assert (await get_run(db, run_a)).status == FlowStatus.WAITING_APPROVAL.value
        assert (await gates_of(db, run_b))[0].consumed_at is None

    async def test_prefix_of_the_other_project_is_unknown_here(self, service, fake_gitlab, db):
        await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        run_b = await service.start_run(
            OTHER_PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice"
        )
        fake_gitlab.notes = []

        # The prefix path resolves by query predicates — a same-iid run of
        # another project must not match even a LONG unambiguous prefix.
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_b[:16]}", "alice", ISSUE_IID, delivery_id="scope-2"
        )

        (note,) = note_bodies(fake_gitlab)
        assert "matched no run" in note
        assert (await get_run(db, run_b)).status == FlowStatus.WAITING_APPROVAL.value
        assert (await gates_of(db, run_b))[0].consumed_at is None

    async def test_each_project_resolves_its_own_runs_by_both_forms(self, service, db):
        run_a = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        await service.start_run(OTHER_PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")

        # The full id of A, posted on A's issue, advances A only.
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_a}", "alice", ISSUE_IID, author_user_id=11
        )
        assert (await get_run(db, run_a)).status == FlowStatus.WAITING_CI.value

        # A fresh sibling run of A, approved by its 8-char prefix.
        sibling = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {sibling[:8]}", "alice", ISSUE_IID, author_user_id=12
        )
        assert (await get_run(db, sibling)).status == FlowStatus.WAITING_CI.value
        for run_id in (run_a, sibling):
            (gate,) = await gates_of(db, run_id)
            assert gate.consumed_at is not None


class TestSignedIngressDrivesTheResolver:
    async def test_run_command_go_dispatch_reaches_the_resolver(self, service, db, fake_gitlab):
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        fake_gitlab.notes = []

        # The gateway router produces exactly this metadata shape for a
        # signed @forge note; run_command is the dispatch seam.
        await service.run_command(
            {
                "command": "go",
                "project_id": PROJECT_ID,
                "issue_iid": ISSUE_IID,
                "note_text": f"@forge /go {run_id[:8]}",
                "author_username": "alice",
                "author_user_id": 11,
                "note_id": 9001,
            }
        )

        assert (await get_run(db, run_id)).status == FlowStatus.WAITING_CI.value
        (gate,) = await gates_of(db, run_id)
        assert gate.consumed_at is not None
        assert gate.approver_user_id == 11

    async def test_redelivered_command_consumes_the_gate_once(self, service, db, fake_gitlab):
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        metadata = {
            "command": "go",
            "project_id": PROJECT_ID,
            "issue_iid": ISSUE_IID,
            "note_text": f"@forge /go {run_id}",
            "author_username": "alice",
            "author_user_id": 11,
            "note_id": 9002,
        }
        fake_gitlab.notes = []

        await service.run_command(metadata)  # first delivery
        first_status = (await get_run(db, run_id)).status
        (gate,) = await gates_of(db, run_id)
        consumed_at, approver = gate.consumed_at, gate.approver_user_id

        await service.run_command(metadata)  # redelivery — same note id
        await service.run_command(metadata)

        # No second gate consumption, no second advance, ONE duplicate reply.
        assert first_status == FlowStatus.WAITING_CI.value
        assert (await get_run(db, run_id)).status == first_status
        (gate_again,) = await gates_of(db, run_id)
        assert gate_again.consumed_at == consumed_at
        assert gate_again.approver_user_id == approver
        duplicates = [body for body in note_bodies(fake_gitlab) if "duplicate" in body]
        assert len(duplicates) == 1
        assert len(fake_gitlab.merge_requests) == 1
        assert len(FakeWriter.instances) == 1
