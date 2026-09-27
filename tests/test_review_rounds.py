"""R40-02 (#338) — the linked review round after ``ready_for_human``.

The verified gap (external review b521e1a, item R40-02): the correction
window closed when the run reached ``ready_for_human`` — feedback earned
``correction_window_closed`` while the natural journey asks for
corrections AFTER readiness is announced. The model under test:

    Delivery 1 — immutable, ready.
        ↓ human requests a change (/fix on the MR)
    Review round 2: current MR head + request + scope + budget + approval
        ↓
    New candidate, new checks, new review.

The spine (AT-02), over the REAL note ingress and the REAL lifecycle:

- an authorized /fix on a ready delivery admits a LINKED round — its own
  child work unit, its own budget, its own candidate — without editing
  the original terminal record or its evidence;
- the round starts from the EXACT approved current MR head, including a
  human-added nonconflicting file;
- a merged/closed MR refuses the feedback with an explanation and ZERO
  new commits;
- after round two completes a SECOND independent correction works, while
  replaying round one's note cannot create round three;
- an ORDINARY run that never staged a PlanRevision follows the route via
  the VERIFIED adapter deriving the initial approved input from the
  frozen spec + the accepted request;
- the required checks and the readonly review bind to the NEW candidate;
  the old green evidence stays historical;
- no bot action resolves the discussion or merges.

Negative/recovery: the head fence during approval and at publication; a
restart between round admission and the native start (at most one child
round + one effect intent); two authorized corrections raced on the same
head (one deterministic active policy); a cancelled child leaving
delivery 1 and the MR readable.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive.closing_budget import ClosingReservePolicy, closing_partition
from forge.adaptive.models import PlanRevision, PlanStep
from forge.adaptive.revisions import (
    ACTIVE_PLAN_KEY,
    IN_SCOPE_CORRECTION_CLASS,
    REQUEST_CONFLICTING,
    REQUEST_MR_CLOSED,
    REQUEST_RECORDED,
    REQUEST_ROUND_ADMITTED,
    REQUEST_ROUND_LIMIT,
    REQUEST_STALE_HEAD,
    REVISION_CONTENT_KEY,
    ReviewFeedbackRequest,
    classic_spec_revision,
    correction_decision_id,
    plan_digest,
    resolve_approved_input,
    review_feedback_requests_of,
    round_active_plan_seed,
)
from forge.durable import FlowRun, FlowStatus, MRReservation, Outbox, PublicationIntent, RunSpec
from forge.durable.budgets import budget_for_run, budget_limits_from_spec
from forge.durable.models import ReviewRound, RunBudget
from forge.models.base import Base
from forge.gateway.feedback import (
    DEFAULT_MAX_REVIEW_ROUNDS,
    FORGE_MAX_REVIEW_ROUNDS_ENV,
    max_review_rounds,
)
from forge.repository import WriteOutcome, WriteResult
from tests.test_review_feedback import (
    ISSUE_DESC,
    ISSUE_IID,
    ISSUE_TITLE,
    PROJECT_ID,
    RecordingImplementer,
    ReviewFakeGitLab,
    _feedback_command,
    make_review_service,
)
from tests.test_runs_service import FakeWriter, make_settings


# ----------------------------------------------------------------------
# The harness — a REAL run driven to ready_for_human, then the round lane
# ----------------------------------------------------------------------


class RoundWriter(FakeWriter):
    """FakeWriter whose commits MOVE the fake branch (the honest shape).

    The stock FakeWriter returns a static ``fake-sha-1`` and never touches
    the branch — fine for the delivery-1 fixtures that pre-seed the head,
    wrong for a round: the new candidate must become the branch head the
    post-review freshness check reads, and each round needs its own sha.
    """

    counter = 0

    def __init__(self, gitlab, session_factory, project_id: int, *, settle_seconds=None):
        super().__init__(gitlab, session_factory, project_id, settle_seconds=settle_seconds)
        self._gitlab_fake = gitlab

    async def apply(self, flow_run_id, cs, start_ref="main", expected_head=None, **kwargs):
        RoundWriter.counter += 1
        sha = f"round-sha-{RoundWriter.counter}"
        # A real writer commits ON the branch (parent = the pinned head).
        self._gitlab_fake.seed_commit(
            cs.branch, sha, cs.commit_message, parents=[expected_head] if expected_head else None
        )
        self.calls.append(
            {
                "flow_run_id": flow_run_id,
                "branch": cs.branch,
                "start_ref": start_ref,
                "expected_head": expected_head,
                "sha": sha,
            }
        )
        return WriteResult(WriteOutcome.COMMITTED, sha)


@pytest.fixture()
async def rounds_db():
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
def rounds_fake():
    fake = ReviewFakeGitLab()
    fake.seed_issue(ISSUE_IID, ISSUE_TITLE, ISSUE_DESC)
    fake.seed_commit("main", "base-sha-1", "initial")
    fake.seed_file(".forge.yml", "implement:\n  paths:\n    - forge-demo/**\n")
    return fake


@pytest.fixture(autouse=True)
def _round_writer_reset():
    FakeWriter.reset()
    RoundWriter.counter = 0
    yield
    FakeWriter.reset()


@pytest.fixture(autouse=True)
def _round_policy_default(monkeypatch):
    monkeypatch.delenv(FORGE_MAX_REVIEW_ROUNDS_ENV, raising=False)


def make_round_service(db, fake, **overrides):
    values = dict(writer_class=RoundWriter)
    values.update(overrides)
    return make_review_service(db, fake, **values)


# ----------------------------------------------------------------------
# R41-01 (#356) — finite-budget fixtures: the child before its budget
# ----------------------------------------------------------------------

#: The finite budget matrix: every enforceable axis ALONE, plus the fully
#: finite profile — each a SEPARATE configuration of the fixture factory.
#: The #356 defect hid because the suite's only fixture was unbudgeted
#: (a spec without ceilings opens no budget, so ``open_budget``'s
#: run-existence guard never ran); these profiles make every axis drive
#: the REAL budget seam on every admission.
FINITE_BUDGET_PROFILES: dict[str, dict[str, int]] = {
    "max_calls": {"max_calls": 20},
    "max_tokens": {"max_tokens": 50_000},
    "wallclock_s": {"wallclock_s": 3_600},
    "fully_finite": {"max_calls": 20, "max_tokens": 50_000, "wallclock_s": 3_600},
}


def make_finite_round_service(db, fake, profile: str, **overrides):
    """The round factory over a REAL finite budget profile (R41-01).

    The ceilings ride the production path — ``FORGE_BUDGET_PROFILES`` →
    the harness selection's resolved ceilings → the frozen RunSpec
    ``budgets`` block — so the parent's AND the child's budgets open from
    the same real seam (``open_budget_from_spec``), never a monkeypatch.
    """
    ceilings = dict(FINITE_BUDGET_PROFILES[profile])
    values = dict(settings=make_settings(FORGE_BUDGET_PROFILES=json.dumps({"standard": ceilings})))
    values.update(overrides)
    return make_round_service(db, fake, **values)


async def _budget_of(db, run_id: str) -> RunBudget | None:
    async with db() as session:
        return await budget_for_run(session, run_id)


async def _admission_events_of(db, run_id: str) -> list[Outbox]:
    async with db() as session:
        rows = (
            (
                await session.execute(
                    select(Outbox).where(
                        Outbox.flow_run_id == run_id,
                        Outbox.event_type == "review_round.admitted",
                    )
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


async def _child_spec_document(db, run_id: str) -> dict:
    async with db() as session:
        row = (
            (
                await session.execute(
                    select(RunSpec)
                    .where(RunSpec.run_id == run_id)
                    .order_by(RunSpec.id.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        assert row is not None
        return dict(row.document)


async def _assert_nothing_partial(db, run_id: str, *, parent_budgeted: bool) -> None:
    """The atomicity bar (R41-01): a kill anywhere before the admission
    commit leaves NO orphan child, budget, reservation, round or outbox
    row — delivery 1 stands alone exactly as it was."""
    async with db() as session:
        runs = (await session.execute(select(FlowRun))).scalars().all()
        assert [row.id for row in runs] == [run_id]  # delivery 1 alone — no child
        assert (await session.execute(select(ReviewRound))).scalars().all() == []
        reservations = (
            (
                await session.execute(
                    select(MRReservation).where(MRReservation.flow_run_id != run_id)
                )
            )
            .scalars()
            .all()
        )
        assert reservations == []
        events = (await session.execute(select(Outbox.event_type))).scalars().all()
        assert "review_round.admitted" not in events
        budgets = (await session.execute(select(RunBudget.run_id))).scalars().all()
    assert budgets == ([run_id] if parent_budgeted else [])


async def _get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        return await session.get(FlowRun, run_id)


async def _rounds_of(db, root_run_id: str) -> list[ReviewRound]:
    async with db() as session:
        rows = (
            (
                await session.execute(
                    select(ReviewRound)
                    .where(ReviewRound.root_run_id == root_run_id)
                    .order_by(ReviewRound.round_number.asc())
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


async def _branch_of(db, run: FlowRun) -> str:
    """The run's collaboration branch — read from the PERSISTED target
    (R41-04: the child's independent id no longer derives it). The
    recorded MR reservation is the fallback for pre-target fixtures;
    the id derivation only the last resort."""
    from forge.durable.models import CollaborationTarget, MRReservation
    from forge.runs.stubs import factory_branch

    async with db() as session:
        if run.target_id:
            target = await session.get(CollaborationTarget, run.target_id)
            if target is not None and target.source_branch:
                return str(target.source_branch)
        reservation = (
            (
                await session.execute(
                    select(MRReservation).where(MRReservation.flow_run_id == run.id).limit(1)
                )
            )
            .scalars()
            .first()
        )
        if reservation is not None:
            return str(reservation.branch)
    return factory_branch(run.issue_iid, run.id)


def _candidate_of(run: FlowRun) -> str:
    return str(list(run.candidate_shas or [])[-1]) if run.candidate_shas else ""


def _round_command(mr_iid: int, note_id: str, text: str, *, discussion: str = "d-fix") -> dict:
    return _feedback_command(mr_iid, text, note_id=note_id, discussion_id=discussion)


async def _ready_run(db, fake, service, *, seed_revision: bool = False) -> tuple[str, str, int]:
    """A run driven to ``ready_for_human`` over the real lifecycle.

    The default is the ORDINARY classic world — no staged PlanRevision,
    exactly what the verified adapter serves; ``seed_revision`` stages the
    #337-shaped active plan instead (the correction-chaining world).
    Returns ``(run_id, candidate_sha, mr_iid)``.
    """
    from forge.runs.stubs import factory_branch

    FakeWriter.reset()
    RoundWriter.counter = 0
    run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
    await service.handle_command_note(
        PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
    )
    run = await _get_run(db, run_id)
    assert run.status == FlowStatus.WAITING_CI.value
    candidate = _candidate_of(run)
    assert candidate  # the RoundWriter moved the branch head to it
    branch = factory_branch(ISSUE_IID, run_id)
    if seed_revision:
        active = PlanRevision(
            plan_id="plan-1",
            work_id=run_id,
            revision=1,
            parent_revision=None,
            work_contract_digest="3" * 64,
            snapshot_set_digest="5" * 64,
            summary="Land the validator.",
            steps=[PlanStep(step_id="s1", objective="Land the validator.")],
        )
        async with db() as session:
            row = await session.get(FlowRun, run_id)
            evidence = dict(row.evidence or {})
            evidence[ACTIVE_PLAN_KEY] = round_active_plan_seed(
                active, revised_from_digest="", decision_id="rd-one"
            )
            row.evidence = evidence
            await session.commit()
    pipeline_id = (await fake.create_pipeline(PROJECT_ID, branch))["id"]
    fake.set_pipeline_status(pipeline_id, "success", candidate)
    await service.evaluate_waiting_ci()
    run = await _get_run(db, run_id)
    assert run.status == FlowStatus.READY_FOR_HUMAN.value, run.status
    return run_id, candidate, run.mr_iid


async def _green_child(db, fake, service, round_row: ReviewRound) -> FlowRun:
    """Green the round child's pipeline; returns the fresh child row."""
    child = await _get_run(db, round_row.child_run_id)
    pipeline_id = (await fake.create_pipeline(PROJECT_ID, await _branch_of(db, child)))["id"]
    fake.set_pipeline_status(pipeline_id, "success", _candidate_of(child))
    await service.evaluate_waiting_ci()
    return await _get_run(db, round_row.child_run_id)


# ----------------------------------------------------------------------
# The pure layer — the verified classic-run adapter and the round policy
# ----------------------------------------------------------------------


class TestClassicAdapter:
    def _spec(self) -> dict[str, Any]:
        return {
            "task_title": "Add the validator",
            "task_description": "Validate emails on entry.",
            "plan_summary": "Create forge-demo/validator.py with the entry hook.",
            "plan_digest": "p" * 64,
            "policy_digest": "q" * 64,
            "allowed_paths": ["forge-demo/**"],
            "source_base_oid": "base-sha-1",
        }

    def _request(self) -> ReviewFeedbackRequest:
        return ReviewFeedbackRequest(
            note_id="9001",
            run_id="r" * 32,
            discussion_id="d-abc",
            mr_iid=17,
            actor="alice",
            head_sha="a" * 40,
            classification=IN_SCOPE_CORRECTION_CLASS,
            text="also handle `forge-demo/validator.py` empty input",
            referenced_paths=("forge-demo/validator.py",),
        )

    def test_the_adapter_derives_one_step_from_the_frozen_plan(self):
        revision = classic_spec_revision(
            work_id="c" * 32, spec=self._spec(), request=self._request()
        )
        assert len(revision.steps) == 1
        assert "validator.py" in revision.steps[0].objective
        assert revision.revision == 2  # the adapter's base 1 + the folded correction
        assert revision.parent_revision == 1
        # the correction rides the summary with its full provenance
        assert "empty input" in revision.summary
        assert "9001" in revision.summary

    def test_the_derivation_is_deterministic_and_spec_bound(self):
        first = classic_spec_revision(work_id="c" * 32, spec=self._spec(), request=self._request())
        again = classic_spec_revision(work_id="c" * 32, spec=self._spec(), request=self._request())
        assert plan_digest(first) == plan_digest(again)
        changed = dict(self._spec(), plan_summary="A DIFFERENT approved plan.")
        other = classic_spec_revision(work_id="c" * 32, spec=changed, request=self._request())
        assert plan_digest(other) != plan_digest(first)

    def test_the_seed_document_resolves_through_the_approved_input_join(self):
        revision = classic_spec_revision(
            work_id="c" * 32, spec=self._spec(), request=self._request()
        )
        seed = round_active_plan_seed(
            revision, revised_from_digest="p" * 64, decision_id="rd-review-1"
        )
        assert seed["activated_by_decision"] == "rd-review-1"
        assert seed["revised_from_digest"] == "p" * 64
        assert seed[REVISION_CONTENT_KEY]["summary"] == revision.summary
        assert plan_digest(revision) == seed["plan_digest"]


class TestRoundPolicy:
    def test_the_default_admits_one_round_after_delivery_one(self):
        assert max_review_rounds() == DEFAULT_MAX_REVIEW_ROUNDS == 1

    def test_zero_disables_the_route_and_typos_narrow(self, monkeypatch):
        monkeypatch.setenv(FORGE_MAX_REVIEW_ROUNDS_ENV, "0")
        assert max_review_rounds() == 0
        monkeypatch.setenv(FORGE_MAX_REVIEW_ROUNDS_ENV, "not-a-number")
        assert max_review_rounds() == DEFAULT_MAX_REVIEW_ROUNDS
        monkeypatch.setenv(FORGE_MAX_REVIEW_ROUNDS_ENV, "99")
        assert max_review_rounds() == 10  # clamped — a typo never widens


# ----------------------------------------------------------------------
# The service lane — AT-02, the spine
# ----------------------------------------------------------------------


class TestRoundAdmission:
    async def test_an_authorized_fix_opens_a_linked_round_off_the_ready_delivery(
        self, rounds_db, rounds_fake
    ):
        implementer = RecordingImplementer()
        service = make_round_service(rounds_db, rounds_fake, implementer=implementer)
        run_id, candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9100, body="/fix also cover `forge-demo/a.md` empty input"
        )

        await service.run_command(
            _round_command(mr_iid, "9100", "/fix also cover `forge-demo/a.md` empty input")
        )

        rounds = await _rounds_of(rounds_db, run_id)
        assert len(rounds) == 1
        round_row = rounds[0]
        assert round_row.round_number == 2
        assert round_row.base_head_sha == candidate
        assert round_row.decision_id == correction_decision_id(run_id, "9100")
        assert round_row.status == "dispatched"
        # the LINKED child: same issue, same MR, the approved head as base
        child = await _get_run(rounds_db, round_row.child_run_id)
        assert child.status == FlowStatus.WAITING_CI.value
        assert child.mr_iid == mr_iid
        assert child.base_sha == candidate
        assert child.id != run_id and child.id[:8] != run_id[:8]  # R41-04: INDEPENDENT ids
        # ...and the collaboration identity is the ONE persisted target:
        # both deliveries link it, and its recorded branch is where the
        # child's reservation sits (the pre-#359 prefix-reuse contract,
        # made explicit).
        from forge.durable.models import CollaborationTarget
        from forge.durable.models import MRReservation as _MR

        parent = await _get_run(rounds_db, run_id)
        assert child.target_id is not None and child.target_id == parent.target_id
        async with rounds_db() as session:
            row = await session.get(CollaborationTarget, child.target_id)
            assert row is not None and row.status == "active"
            assert row.root_run_id == run_id
            assert row.mr_iid == mr_iid
            reservation = (
                (await session.execute(select(_MR).where(_MR.flow_run_id == child.id).limit(1)))
                .scalars()
                .first()
            )
            assert reservation is not None and reservation.branch == row.source_branch
        assert child.spec_digest == parent.spec_digest  # the verified spec copy
        assert _candidate_of(child) and _candidate_of(child) != candidate
        # the request on the PARENT carries the linkage
        requests = review_feedback_requests_of(parent.evidence or {})
        assert requests["9100"].status == REQUEST_ROUND_ADMITTED
        # the correction rode the executor's dispatch input
        implementer_dispatch = implementer.dispatches[-1]
        assert "empty input" in (implementer_dispatch.get("repair_context") or "")
        assert implementer_dispatch.get("attempt_base") == candidate
        # the operator reply names the round
        assert any("Review round 2 opened" in n["body"] for n in rounds_fake.mr_notes)

    async def test_the_terminal_record_is_never_edited(self, rounds_db, rounds_fake):
        service = make_round_service(rounds_db, rounds_fake)
        run_id, _candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        parent_before = await _get_run(rounds_db, run_id)
        evidence_before = dict(parent_before.evidence or {})
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9101, body="/fix tweak `forge-demo/a.md`"
        )
        await service.run_command(_round_command(mr_iid, "9101", "/fix tweak `forge-demo/a.md`"))

        parent_after = await _get_run(rounds_db, run_id)
        assert parent_after.status == FlowStatus.READY_FOR_HUMAN.value
        assert list(parent_after.candidate_shas or []) == list(parent_before.candidate_shas or [])
        after = dict(parent_after.evidence or {})
        # the DELIVERY evidence is byte-identical: the verdict, the
        # verification and the pipeline of delivery 1 are history.
        for key in ("review", "verification", "pipeline", "plan"):
            assert after.get(key) == evidence_before.get(key)
        # supersession is a RELATIONSHIP (the round row), never a rewrite
        assert len(await _rounds_of(rounds_db, run_id)) == 1

    async def test_the_round_starts_from_the_exact_approved_head_with_human_files(
        self, rounds_db, rounds_fake
    ):
        implementer = RecordingImplementer()
        service = make_round_service(rounds_db, rounds_fake, implementer=implementer)
        run_id, _candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        parent = await _get_run(rounds_db, run_id)
        branch = await _branch_of(rounds_db, parent)

        # a HUMAN adds a nonconflicting file AFTER readiness, BEFORE /fix
        rounds_fake.seed_file("forge-demo/human-note.md", "# human addition\n")
        rounds_fake.seed_commit(branch, "human-head-1", "human: add a note")
        dispatches_before = len(implementer.dispatches)

        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9102, body="/fix cover `forge-demo/a.md` empty input"
        )
        await service.run_command(
            _round_command(mr_iid, "9102", "/fix cover `forge-demo/a.md` empty input")
        )

        rounds = await _rounds_of(rounds_db, run_id)
        assert rounds[0].base_head_sha == "human-head-1"
        child = await _get_run(rounds_db, rounds[0].child_run_id)
        assert child.base_sha == "human-head-1"
        # the executor read at the human head — the addition is in its
        # snapshot, never replayed against the old base
        assert len(implementer.dispatches) == dispatches_before + 1
        implementer_dispatch = implementer.dispatches[-1]
        assert implementer_dispatch.get("attempt_base") == "human-head-1"
        # the writer pinned the SAME head (no force-push contract)
        writer_calls = [c for w in FakeWriter.instances for c in w.calls]
        assert writer_calls[-1]["expected_head"] == "human-head-1"

    async def test_a_merged_mr_refuses_with_zero_commits(self, rounds_db, rounds_fake):
        implementer = RecordingImplementer()
        service = make_round_service(rounds_db, rounds_fake, implementer=implementer)
        run_id, _candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        dispatches_before = len(implementer.dispatches)
        writers_before = len(FakeWriter.instances)

        rounds_fake.merge_requests[mr_iid]["state"] = "merged"
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9103, body="/fix cover `forge-demo/a.md`"
        )
        await service.run_command(_round_command(mr_iid, "9103", "/fix cover `forge-demo/a.md`"))

        assert await _rounds_of(rounds_db, run_id) == []  # zero rounds, zero commits
        assert len(implementer.dispatches) == dispatches_before
        assert len(FakeWriter.instances) == writers_before
        parent = await _get_run(rounds_db, run_id)
        requests = review_feedback_requests_of(parent.evidence or {})
        assert requests["9103"].status == REQUEST_MR_CLOSED
        assert any("merge request is **merged**" in n["body"] for n in rounds_fake.mr_notes)

    async def test_the_second_independent_correction_works_and_replay_admits_nothing(
        self, rounds_db, rounds_fake, monkeypatch
    ):
        monkeypatch.setenv(FORGE_MAX_REVIEW_ROUNDS_ENV, "2")
        service = make_round_service(rounds_db, rounds_fake)
        run_id, _candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)

        # round 2 — the reviewer asks; the thread resolves when satisfied
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9200, body="/fix cover `forge-demo/a.md` empty input"
        )
        await service.run_command(
            _round_command(mr_iid, "9200", "/fix cover `forge-demo/a.md` empty input")
        )
        rounds = await _rounds_of(rounds_db, run_id)
        child = await _green_child(rounds_db, rounds_fake, service, rounds[0])
        # the required discussion still gates the NEW candidate's readiness
        assert child.status == FlowStatus.EVALUATING_CI.value
        rounds_fake.resolve(mr_iid, "d-fix")
        await service.evaluate_waiting_ci()
        child = await _get_run(rounds_db, rounds[0].child_run_id)
        assert child.status == FlowStatus.READY_FOR_HUMAN.value
        await service.evaluate_review_rounds()  # closes round 2
        assert (await _rounds_of(rounds_db, run_id))[0].status == "completed"

        # REPLAYING round two's note cannot create round three
        await service.run_command(
            _round_command(mr_iid, "9200", "/fix cover `forge-demo/a.md` empty input")
        )
        assert len(await _rounds_of(rounds_db, run_id)) == 1

        # a SECOND, INDEPENDENT correction opens round 3 off round 2's run
        rounds_fake.seed_discussion(
            mr_iid, "d-fix2", note_id=9201, body="/fix rename `forge-demo/a.md` heading"
        )
        await service.run_command(
            _round_command(
                mr_iid, "9201", "/fix rename `forge-demo/a.md` heading", discussion="d-fix2"
            )
        )
        rounds = await _rounds_of(rounds_db, run_id)
        assert [row.round_number for row in rounds] == [2, 3]
        assert rounds[1].parent_run_id == child.id  # linked to round 2's delivery
        child3 = await _get_run(rounds_db, rounds[1].child_run_id)
        assert child3.status == FlowStatus.WAITING_CI.value

    async def test_the_bounded_round_count_refuses_past_the_policy(
        self, rounds_db, rounds_fake, monkeypatch
    ):
        monkeypatch.setenv(FORGE_MAX_REVIEW_ROUNDS_ENV, "0")  # the route disabled
        service = make_round_service(rounds_db, rounds_fake)
        run_id, _candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9210, body="/fix cover `forge-demo/a.md`"
        )
        await service.run_command(_round_command(mr_iid, "9210", "/fix cover `forge-demo/a.md`"))
        assert await _rounds_of(rounds_db, run_id) == []
        parent = await _get_run(rounds_db, run_id)
        requests = review_feedback_requests_of(parent.evidence or {})
        assert requests["9210"].status == REQUEST_ROUND_LIMIT
        assert any("FORGE_MAX_REVIEW_ROUNDS=0" in n["body"] for n in rounds_fake.mr_notes)

    async def test_an_ordinary_run_without_a_staged_revision_uses_the_verified_adapter(
        self, rounds_db, rounds_fake
    ):
        service = make_round_service(rounds_db, rounds_fake)
        run_id, _candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        parent = await _get_run(rounds_db, run_id)
        assert ACTIVE_PLAN_KEY not in (parent.evidence or {})  # the classic world
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9220, body="/fix cover `forge-demo/a.md` empty input"
        )
        await service.run_command(
            _round_command(mr_iid, "9220", "/fix cover `forge-demo/a.md` empty input")
        )
        rounds = await _rounds_of(rounds_db, run_id)
        child = await _get_run(rounds_db, rounds[0].child_run_id)
        active = (child.evidence or {})[ACTIVE_PLAN_KEY]
        # derived from the frozen spec + the request — the adapter's seed
        assert active["seed"] == "forge.revision.round-seed/1"
        assert active["revised_from_digest"] == parent.plan_digest
        assert "empty input" in active[REVISION_CONTENT_KEY]["summary"]
        # the #321 join: the child's dispatch briefs from the seeded revision
        resolved = await resolve_approved_input(
            rounds_db,
            child.id,
            task_title="T",
            task_description="D",
            spec_plan_text="the frozen spec brief",
            spec_plan_digest="s" * 64,
        )
        assert resolved.source == "revision"
        assert "empty input" in resolved.brief()
        assert "the frozen spec brief" not in resolved.brief()

    async def test_a_run_with_a_staged_revision_chains_the_correction(self, rounds_db, rounds_fake):
        service = make_round_service(rounds_db, rounds_fake)
        run_id, _candidate, mr_iid = await _ready_run(
            rounds_db, rounds_fake, service, seed_revision=True
        )
        parent = await _get_run(rounds_db, run_id)
        parent_active = (parent.evidence or {})[ACTIVE_PLAN_KEY]
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9230, body="/fix cover `forge-demo/a.md` empty input"
        )
        await service.run_command(
            _round_command(mr_iid, "9230", "/fix cover `forge-demo/a.md` empty input")
        )
        rounds = await _rounds_of(rounds_db, run_id)
        child = await _get_run(rounds_db, rounds[0].child_run_id)
        active = (child.evidence or {})[ACTIVE_PLAN_KEY]
        # the round's revision follows the parent's active revision
        assert active["active_revision"] == 2
        assert active["revised_from_digest"] == parent_active["plan_digest"]
        assert "Land the validator." in active[REVISION_CONTENT_KEY]["summary"]

    async def test_checks_and_review_bind_to_the_new_candidate_old_evidence_historical(
        self, rounds_db, rounds_fake
    ):
        service = make_round_service(rounds_db, rounds_fake)
        run_id, parent_candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        parent = await _get_run(rounds_db, run_id)
        parent_review = dict((parent.evidence or {}).get("review") or {})
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9240, body="/fix cover `forge-demo/a.md` empty input"
        )
        await service.run_command(
            _round_command(mr_iid, "9240", "/fix cover `forge-demo/a.md` empty input")
        )
        rounds = await _rounds_of(rounds_db, run_id)
        child = await _get_run(rounds_db, rounds[0].child_run_id)
        child_candidate = _candidate_of(child)
        assert child_candidate != parent_candidate

        rounds_fake.resolve(mr_iid, "d-fix")
        child = await _green_child(rounds_db, rounds_fake, service, rounds[0])
        assert child.status == FlowStatus.READY_FOR_HUMAN.value
        child_review = (child.evidence or {}).get("review") or {}
        assert child_review["sha"] == child_candidate  # the NEW candidate's review
        # the old verdict stays, untouched, as delivery 1's history
        parent_after = await _get_run(rounds_db, run_id)
        assert (parent_after.evidence or {})["review"] == parent_review

    async def test_no_bot_merge_and_no_bot_discussion_resolution(self, rounds_db, rounds_fake):
        service = make_round_service(rounds_db, rounds_fake)
        run_id, _candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9250, body="/fix cover `forge-demo/a.md`"
        )
        await service.run_command(_round_command(mr_iid, "9250", "/fix cover `forge-demo/a.md`"))
        assert rounds_fake.resolve_calls == []
        assert rounds_fake.merge_calls == []
        assert not hasattr(rounds_fake, "accept_merge_request")


# ----------------------------------------------------------------------
# Negative / recovery
# ----------------------------------------------------------------------


class TestRoundFences:
    async def test_a_head_moved_during_approval_refuses_stale_and_preserves_the_human_commit(
        self, rounds_db, rounds_fake, monkeypatch
    ):
        implementer = RecordingImplementer()
        service = make_round_service(rounds_db, rounds_fake, implementer=implementer)
        run_id, candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        parent = await _get_run(rounds_db, run_id)
        branch = await _branch_of(rounds_db, parent)
        dispatches_before = len(implementer.dispatches)

        # The head the note binds vs the head at admission: the note reads
        # the branch head, then a HUMAN push lands before the admission's
        # own re-read — the authorization is stale, the commit preserved.
        reads = {"n": 0}
        original_head = rounds_fake.get_branch_head

        async def moved_on_second_read(project_id: int, branch_name: str) -> str:
            reads["n"] += 1
            if reads["n"] == 2:  # the admission's own re-read
                return "moved-by-human"
            return await original_head(project_id, branch_name)

        monkeypatch.setattr(rounds_fake, "get_branch_head", moved_on_second_read)
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9260, body="/fix cover `forge-demo/a.md`"
        )
        await service.run_command(_round_command(mr_iid, "9260", "/fix cover `forge-demo/a.md`"))

        assert await _rounds_of(rounds_db, run_id) == []
        assert len(implementer.dispatches) == dispatches_before  # nothing dispatched
        parent = await _get_run(rounds_db, run_id)
        requests = review_feedback_requests_of(parent.evidence or {})
        assert requests["9260"].status == REQUEST_STALE_HEAD
        assert any("stale_head" in n["body"] for n in rounds_fake.mr_notes)
        # the human commit is preserved on the branch — no force-push happened
        assert rounds_fake.branches[branch][0]["sha"] == candidate

    async def test_a_head_moved_just_before_publication_blocks_the_drift(
        self, rounds_db, rounds_fake
    ):
        service = make_round_service(rounds_db, rounds_fake)
        run_id, _candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9261, body="/fix cover `forge-demo/a.md`"
        )
        await service.run_command(_round_command(mr_iid, "9261", "/fix cover `forge-demo/a.md`"))
        rounds = await _rounds_of(rounds_db, run_id)
        child = await _get_run(rounds_db, rounds[0].child_run_id)
        candidate = _candidate_of(child)

        # A HUMAN push lands after the round's candidate: the pre-review
        # external-change fence (and, past it, the post-review freshness
        # fence) refuses readiness — the run blocks, the branch keeps the
        # human's commit (never force-fixed).
        rounds_fake.seed_commit(
            await _branch_of(rounds_db, child), "late-human-sha", "human: late edit"
        )
        rounds_fake.resolve(mr_iid, "d-fix")
        pipeline_id = (
            await rounds_fake.create_pipeline(PROJECT_ID, await _branch_of(rounds_db, child))
        )["id"]
        rounds_fake.set_pipeline_status(pipeline_id, "success", candidate)
        await service.evaluate_waiting_ci()
        child = await _get_run(rounds_db, rounds[0].child_run_id)
        assert child.status == FlowStatus.BLOCKED.value
        assert "external_change" in (child.status_reason or "") or "candidate_drift" in (
            child.status_reason or ""
        )
        # the human commit stands on the branch
        assert (
            rounds_fake.branches[await _branch_of(rounds_db, child)][0]["sha"] == "late-human-sha"
        )

    async def test_a_restart_between_admission_and_native_start_keeps_one_child(
        self, rounds_db, rounds_fake, monkeypatch
    ):
        implementer = RecordingImplementer()
        service = make_round_service(rounds_db, rounds_fake, implementer=implementer)
        run_id, _candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9270, body="/fix cover `forge-demo/a.md`"
        )

        # the worker dies right after the admission commit, before the
        # advance leg — the crash surfaces as the dispatch's failure.
        async def crash_dispatch(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("worker died before the native start")

        monkeypatch.setattr(service, "_dispatch_review_round", crash_dispatch)
        with pytest.raises(RuntimeError):
            await service.run_command(
                _round_command(mr_iid, "9270", "/fix cover `forge-demo/a.md`")
            )
        rounds = await _rounds_of(rounds_db, run_id)
        assert len(rounds) == 1 and rounds[0].status == "admitted"  # ONE child round
        dispatches_after_crash = len(implementer.dispatches)
        # the worker restarts — the dispatch leg is whole again
        monkeypatch.undo()

        # the reconciler pass re-drives it — exactly once, adopting any
        # journaled effect, never admitting a second child.
        await service.evaluate_review_rounds()
        await service.evaluate_review_rounds()
        rounds = await _rounds_of(rounds_db, run_id)
        assert len(rounds) == 1 and rounds[0].status == "dispatched"
        child = await _get_run(rounds_db, rounds[0].child_run_id)
        assert child.status == FlowStatus.WAITING_CI.value
        assert len(implementer.dispatches) == dispatches_after_crash + 1  # ONE new dispatch
        # at most ONE effect intent for the round's publication (R11)
        async with rounds_db() as session:
            intents = (
                (
                    await session.execute(
                        select(PublicationIntent).where(PublicationIntent.run_id == child.id)
                    )
                )
                .scalars()
                .all()
            )
        assert len(intents) <= 1

    async def test_two_authorized_corrections_racing_collapse_to_one_round(
        self, rounds_db, rounds_fake
    ):
        service = make_round_service(rounds_db, rounds_fake)
        run_id, candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9280, body="/fix cover `forge-demo/a.md`"
        )
        # The FIRST correction wins the round slot (the partial unique
        # index is the arbiter a true race would hit at the INSERT).
        await service.run_command(_round_command(mr_iid, "9280", "/fix cover `forge-demo/a.md`"))
        rounds = await _rounds_of(rounds_db, run_id)
        assert len(rounds) == 1 and rounds[0].note_id == "9280"

        # The SECOND authorized correction competes for the SAME head and
        # the SAME lineage — recorded on the parent, admitted directly, it
        # loses deterministically to the open round.
        from forge.adaptive.revisions import record_review_feedback_request

        second = ReviewFeedbackRequest(
            note_id="9281",
            run_id=run_id,
            discussion_id="d-fixb",
            mr_iid=mr_iid,
            actor="alice",
            head_sha=candidate,
            classification=IN_SCOPE_CORRECTION_CLASS,
            text="/fix also `forge-demo/b.md`",
            referenced_paths=("forge-demo/b.md",),
        )
        await record_review_feedback_request(rounds_db, run_id, second)
        await service._admit_review_round(PROJECT_ID, mr_iid, run_id, ISSUE_IID, second)

        rounds = await _rounds_of(rounds_db, run_id)
        assert len(rounds) == 1  # ONE outstanding round — deterministically the first
        assert rounds[0].note_id == "9280"
        parent = await _get_run(rounds_db, run_id)
        requests = review_feedback_requests_of(parent.evidence or {})
        assert requests["9281"].status == REQUEST_CONFLICTING
        assert requests["9281"].conflict_with == rounds[0].decision_id
        assert any("one outstanding correction" in n["body"] for n in rounds_fake.mr_notes)

    async def test_a_cancelled_child_leaves_delivery_one_and_the_mr_readable(
        self, rounds_db, rounds_fake
    ):
        service = make_round_service(rounds_db, rounds_fake)
        run_id, candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9290, body="/fix cover `forge-demo/a.md`"
        )
        await service.run_command(_round_command(mr_iid, "9290", "/fix cover `forge-demo/a.md`"))
        rounds = await _rounds_of(rounds_db, run_id)
        child = await _get_run(rounds_db, rounds[0].child_run_id)

        # the operator cancels the round child (the full id — the lineage
        # shares the branch prefix by design, so short forms would be
        # ambiguous across the lineage)
        await service.handle_cancel_note(
            PROJECT_ID, f"@forge /cancel {child.id}", "alice", ISSUE_IID
        )
        child = await _get_run(rounds_db, rounds[0].child_run_id)
        assert child.status == FlowStatus.CANCELLED.value
        await service.evaluate_review_rounds()  # closes the round row

        rounds = await _rounds_of(rounds_db, run_id)
        assert rounds[0].status == "ended"
        # delivery 1 and its MR are untouched and readable
        parent_after = await _get_run(rounds_db, run_id)
        assert parent_after.status == FlowStatus.READY_FOR_HUMAN.value
        assert ((parent_after.evidence or {}).get("review") or {}).get("sha") == candidate
        mr = await rounds_fake.get_merge_request(PROJECT_ID, mr_iid)
        assert mr.state == "opened"


# ----------------------------------------------------------------------
# R41-01 (#356) — the child exists BEFORE its finite budget opens
# ----------------------------------------------------------------------


class TestFiniteBudgetAdmission:
    """Every finite profile admits a round whose budget opened on an
    EXISTING child — under the old budget-before-child order each of
    these raised ``RunNotFound`` before the child row was created."""

    @pytest.mark.parametrize("profile", sorted(FINITE_BUDGET_PROFILES))
    async def test_every_finite_profile_admits_with_the_child_before_its_budget(
        self, rounds_db, rounds_fake, profile
    ):
        service = make_finite_round_service(rounds_db, rounds_fake, profile)
        run_id, _candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)

        # the PARENT's own budget opened from the frozen ceilings over the
        # production seam (FORGE_BUDGET_PROFILES → selection → spec)
        parent_budget = await _budget_of(rounds_db, run_id)
        assert parent_budget is not None

        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9300, body="/fix cover `forge-demo/a.md` empty input"
        )
        await service.run_command(
            _round_command(mr_iid, "9300", "/fix cover `forge-demo/a.md` empty input")
        )

        rounds = await _rounds_of(rounds_db, run_id)
        assert len(rounds) == 1 and rounds[0].status == "dispatched"
        child = await _get_run(rounds_db, rounds[0].child_run_id)
        assert child.status == FlowStatus.WAITING_CI.value

        # THE regression: the budget opened on an EXISTING child run
        budget = await _budget_of(rounds_db, child.id)
        assert budget is not None
        ceilings = FINITE_BUDGET_PROFILES[profile]
        assert budget.max_calls == ceilings.get("max_calls")
        assert budget.max_tokens == ceilings.get("max_tokens")
        assert budget.wallclock_s == ceilings.get("wallclock_s")
        assert budget.status == "open"

        # the closing partition rides the SAME frozen open (v1 policy)
        document = await _child_spec_document(rounds_db, child.id)
        limits = budget_limits_from_spec(document)
        partition = closing_partition(limits, ClosingReservePolicy.from_env())
        assert budget.closing_reserved_calls == (partition.calls if partition else None)
        assert budget.closing_reserved_tokens == (partition.tokens if partition else None)
        assert budget.closing_partition_policy == (partition.policy_version if partition else None)

        # one child, one round, ONE admission event — the atomic shape
        assert len(await _admission_events_of(rounds_db, child.id)) == 1

        # delivery 1 keeps its status and verdict untouched
        parent = await _get_run(rounds_db, run_id)
        assert parent.status == FlowStatus.READY_FOR_HUMAN.value

    async def test_the_unbudgeted_compatibility_profile_admits_with_no_budget_row(
        self, rounds_db, rounds_fake
    ):
        """The pre-#356 shape stays first-class: no ceilings in the spec →
        no budget row for parent OR child, and the round still admits."""
        service = make_round_service(rounds_db, rounds_fake)
        run_id, _candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        assert await _budget_of(rounds_db, run_id) is None
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9301, body="/fix cover `forge-demo/a.md` empty input"
        )
        await service.run_command(
            _round_command(mr_iid, "9301", "/fix cover `forge-demo/a.md` empty input")
        )
        rounds = await _rounds_of(rounds_db, run_id)
        assert len(rounds) == 1
        child = await _get_run(rounds_db, rounds[0].child_run_id)
        assert child.status == FlowStatus.WAITING_CI.value
        assert await _budget_of(rounds_db, child.id) is None


class TestAdmissionCrashRecovery:
    """A kill anywhere inside the admission transaction leaves nothing
    partial, and the replayed note admits exactly ONE round."""

    async def test_a_kill_after_the_child_flush_leaves_nothing_and_replays_once(
        self, rounds_db, rounds_fake, monkeypatch
    ):
        implementer = RecordingImplementer()
        service = make_finite_round_service(
            rounds_db, rounds_fake, "fully_finite", implementer=implementer
        )
        run_id, _candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        dispatches_before = len(implementer.dispatches)
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9310, body="/fix cover `forge-demo/a.md` empty input"
        )
        command = _round_command(mr_iid, "9310", "/fix cover `forge-demo/a.md` empty input")

        # The worker dies AFTER the child FlowRun's flush, BEFORE the
        # budget opens — the crash point the old order never reached (it
        # died earlier, in open_budget's run-existence guard).
        async def killed_after_child_flush(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("worker died after the child flush")

        monkeypatch.setattr("forge.durable.open_budget_from_spec", killed_after_child_flush)
        with pytest.raises(RuntimeError):
            await service.run_command(command)

        await _assert_nothing_partial(rounds_db, run_id, parent_budgeted=True)
        assert len(implementer.dispatches) == dispatches_before  # nothing dispatched
        parent = await _get_run(rounds_db, run_id)
        requests = review_feedback_requests_of(parent.evidence or {})
        assert requests["9310"].status == REQUEST_RECORDED  # not answered at all

        # The worker restarts; the SAME note replays through the real
        # entry and admits exactly one round with its budget.
        monkeypatch.undo()
        await service.run_command(command)
        rounds = await _rounds_of(rounds_db, run_id)
        assert len(rounds) == 1
        child = await _get_run(rounds_db, rounds[0].child_run_id)
        budget = await _budget_of(rounds_db, child.id)
        assert budget is not None and budget.max_calls == 20
        assert len(await _admission_events_of(rounds_db, child.id)) == 1
        assert len(implementer.dispatches) == dispatches_before + 1  # ONE allowance
        requests = review_feedback_requests_of((await _get_run(rounds_db, run_id)).evidence or {})
        assert requests["9310"].status == REQUEST_ROUND_ADMITTED

    async def test_a_kill_after_the_budget_creation_leaves_nothing_and_replays_once(
        self, rounds_db, rounds_fake, monkeypatch
    ):
        implementer = RecordingImplementer()
        service = make_finite_round_service(
            rounds_db, rounds_fake, "fully_finite", implementer=implementer
        )
        run_id, _candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        dispatches_before = len(implementer.dispatches)
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9311, body="/fix cover `forge-demo/a.md` empty input"
        )
        command = _round_command(mr_iid, "9311", "/fix cover `forge-demo/a.md` empty input")

        # The worker dies AFTER the child budget's INSERT, BEFORE the
        # admission commit: the flushed child, its budget, the round row
        # and the outbox event all roll back with the transaction.
        from forge.durable import Controller as RealController
        from forge.runs import service as runs_service_module

        class KilledAfterBudget(RealController):
            async def transition(self, *args: Any, **kwargs: Any) -> FlowRun:
                raise RuntimeError("worker died after the budget creation")

        monkeypatch.setattr(runs_service_module, "Controller", KilledAfterBudget)
        with pytest.raises(RuntimeError):
            await service.run_command(command)

        await _assert_nothing_partial(rounds_db, run_id, parent_budgeted=True)
        assert len(implementer.dispatches) == dispatches_before

        # Restart + replay: exactly one child, one budget, one round.
        monkeypatch.undo()
        await service.run_command(command)
        rounds = await _rounds_of(rounds_db, run_id)
        assert len(rounds) == 1
        child = await _get_run(rounds_db, rounds[0].child_run_id)
        budget = await _budget_of(rounds_db, child.id)
        assert budget is not None and budget.max_tokens == 50_000
        assert len(await _admission_events_of(rounds_db, child.id)) == 1
        assert len(implementer.dispatches) == dispatches_before + 1


class TestAdmissionIntegrityErrors:
    """The round-slot arbiter is answered as a conflict; an unrelated
    integrity failure is a genuine defect and never a false answer."""

    async def test_the_real_arbiter_race_is_answered_as_a_conflicting_correction(
        self, rounds_db, rounds_fake, monkeypatch
    ):
        service = make_round_service(rounds_db, rounds_fake)
        run_id, candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9330, body="/fix cover `forge-demo/a.md`"
        )
        command = _round_command(mr_iid, "9330", "/fix cover `forge-demo/a.md`")

        # A competing OPEN round commits BETWEEN this admission's lineage
        # precheck and its INSERT — the partial unique index is then the
        # arbiter a true race hits (SQLite: ``UNIQUE constraint failed:
        # review_rounds.root_run_id``).
        rival_child = f"{run_id[:8]}{'e' * 24}"
        reads = {"n": 0}
        real_read = service._read_mr_head

        async def read_then_seed_competitor(project_id, mr, issue, run_id_arg):
            reads["n"] += 1
            head = await real_read(project_id, mr, issue, run_id_arg)
            if reads["n"] == 2:  # the admission's own re-read — precheck passed
                async with rounds_db() as session:
                    session.add(
                        FlowRun(
                            id=rival_child,
                            project_id=PROJECT_ID,
                            issue_iid=ISSUE_IID,
                            provider="gitlab",
                        )
                    )
                    session.add(
                        ReviewRound(
                            id="e" * 32,
                            parent_run_id=run_id,
                            child_run_id=rival_child,
                            root_run_id=run_id,
                            round_number=2,
                            note_id="rival-note",
                            mr_iid=mr_iid,
                            base_head_sha=candidate,
                            decision_id="rd-rival",
                            requested_by="bob",
                            status="admitted",
                        )
                    )
                    await session.commit()
            return head

        monkeypatch.setattr(service, "_read_mr_head", read_then_seed_competitor)
        await service.run_command(command)

        # the loser's admission rolled back whole: the rival round alone
        rounds = await _rounds_of(rounds_db, run_id)
        assert len(rounds) == 1 and rounds[0].note_id == "rival-note"
        async with rounds_db() as session:
            run_ids = set((await session.execute(select(FlowRun.id))).scalars().all())
        assert run_ids == {run_id, rival_child}  # no orphan child of the loser
        parent = await _get_run(rounds_db, run_id)
        requests = review_feedback_requests_of(parent.evidence or {})
        assert requests["9330"].status == REQUEST_CONFLICTING
        assert requests["9330"].conflict_with == "rd-rival"
        assert any("one outstanding correction" in n["body"] for n in rounds_fake.mr_notes)

    async def test_an_unrelated_integrity_error_is_not_labeled_a_competing_request(
        self, rounds_db, rounds_fake, monkeypatch
    ):
        service = make_round_service(rounds_db, rounds_fake)
        run_id, _candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9331, body="/fix cover `forge-demo/a.md`"
        )
        command = _round_command(mr_iid, "9331", "/fix cover `forge-demo/a.md`")

        # A foreign-key-shaped integrity failure inside the admission
        # transaction — an unrelated constraint, NOT the round arbiter.
        from forge.runs import service as runs_service_module

        def fk_violation(*args: Any, **kwargs: Any) -> Any:
            raise IntegrityError(
                "INSERT INTO mr_reservations (flow_run_id, branch) VALUES (…)",
                {},
                RuntimeError(
                    'insert or update on table "mr_reservations" violates foreign '
                    'key constraint "mr_reservations_flow_run_id_fkey"'
                ),
            )

        monkeypatch.setattr(runs_service_module, "MRReservation", fk_violation)
        with pytest.raises(IntegrityError):
            await service.run_command(command)

        # nothing admitted, nothing partial — and the request is NOT
        # answered as a competing correction (no competitor exists)
        await _assert_nothing_partial(rounds_db, run_id, parent_budgeted=False)
        parent = await _get_run(rounds_db, run_id)
        requests = review_feedback_requests_of(parent.evidence or {})
        assert requests["9331"].status == REQUEST_RECORDED
        assert not any("one outstanding correction" in n["body"] for n in rounds_fake.mr_notes)


@pytest.mark.skipif(
    not os.environ.get("FORGE_PG_TEST_URL"),
    reason=(
        "FORGE_PG_TEST_URL not set — the finite-profile admission proof runs only"
        " against a disposable real PostgreSQL (FK-enforced inserts, named"
        " constraints); the fast sqlite layer covers the same admission"
    ),
)
class TestFiniteAdmissionOnPostgres:
    async def test_a_finite_profile_admits_over_the_real_entry_on_postgres(self):
        """The regression on real PostgreSQL: the REAL note-command entry
        with the REAL budget helper — nothing monkeypatched — over a fully
        finite spec. Under the old budget-before-child order Postgres
        fails twice over: ``open_budget``'s run-existence guard raises
        RunNotFound, and even without it the ``run_budgets.run_id``
        foreign key would reject a budget for a child that does not
        exist."""
        from sqlalchemy import text

        engine = create_async_engine(os.environ["FORGE_PG_TEST_URL"])
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                # a shared disposable database: clear earlier rounds FK-first
                await conn.execute(text("DELETE FROM review_rounds"))
                await conn.execute(text("TRUNCATE TABLE flow_runs CASCADE"))
            db = async_sessionmaker(engine, expire_on_commit=False)
            fake = ReviewFakeGitLab()
            fake.seed_issue(ISSUE_IID, ISSUE_TITLE, ISSUE_DESC)
            fake.seed_commit("main", "base-sha-1", "initial")
            fake.seed_file(".forge.yml", "implement:\n  paths:\n    - forge-demo/**\n")
            service = make_finite_round_service(db, fake, "fully_finite")

            run_id, _candidate, mr_iid = await _ready_run(db, fake, service)
            assert await _budget_of(db, run_id) is not None  # the parent's own

            fake.seed_discussion(
                mr_iid, "d-fix", note_id=9400, body="/fix cover `forge-demo/a.md` empty input"
            )
            await service.run_command(
                _round_command(mr_iid, "9400", "/fix cover `forge-demo/a.md` empty input")
            )

            rounds = await _rounds_of(db, run_id)
            assert len(rounds) == 1 and rounds[0].status == "dispatched"
            child = await _get_run(db, rounds[0].child_run_id)
            assert child.status == FlowStatus.WAITING_CI.value

            budget = await _budget_of(db, child.id)
            assert budget is not None
            assert budget.max_calls == 20
            assert budget.max_tokens == 50_000
            assert budget.wallclock_s == 3_600
            document = await _child_spec_document(db, child.id)
            partition = closing_partition(
                budget_limits_from_spec(document), ClosingReservePolicy.from_env()
            )
            assert budget.closing_reserved_calls == (partition.calls if partition else None)
            assert budget.closing_reserved_tokens == (partition.tokens if partition else None)
            assert len(await _admission_events_of(db, child.id)) == 1

            parent = await _get_run(db, run_id)
            assert parent.status == FlowStatus.READY_FOR_HUMAN.value
            requests = review_feedback_requests_of(parent.evidence or {})
            assert requests["9400"].status == REQUEST_ROUND_ADMITTED
        finally:
            await engine.dispose()


# ----------------------------------------------------------------------
# R41-03 (#358) — the round's OWN effects reconcile BEFORE the fence
# ----------------------------------------------------------------------


def _provider_commit_calls(fake: ReviewFakeGitLab) -> int:
    """How many real ``create_commit`` effects reached the provider."""
    return sum(1 for name, _ in fake.calls if name == "create_commit")


def _provider_mr_creations(fake: ReviewFakeGitLab) -> int:
    """How many ``create_merge_request`` effects reached the provider."""
    return sum(1 for name, _ in fake.calls if name == "create_merge_request")


async def _resolution_events(db, child_id: str) -> list[dict]:
    async with db() as session:
        rows = (
            (
                await session.execute(
                    select(Outbox).where(
                        Outbox.flow_run_id == child_id,
                        Outbox.event_type == "review_round.effect_resolution",
                    )
                )
            )
            .scalars()
            .all()
        )
    return [dict(row.payload or {}) for row in rows]


class TestRoundOwnEffectRecovery:
    """R41-03 (#358) — a crashed round's own effects, reconciled first.

    The verified defect: a round whose native commit landed while the
    child still sat in ``committing``/``ensuring_draft_mr`` was staled by
    the base-head fence BEFORE its own publication intent was ever
    classified. These arms kill the worker at every post-commit window
    and require the reconciler to ADOPT the exact own effect (no second
    commit, no regeneration, one MR), to keep a human commit after the
    candidate a TYPED conflict, and to settle round AND child together
    when the head is genuinely foreign.
    """

    from forge.repository.writer import ChangesetWriter as _RealWriter
    from forge.repository.writer import _commit_actions as _real_commit_actions

    class _CrashWriter(_RealWriter):
        """The REAL writer with a crash seam at a chosen publication window.

        ``after_journal`` — the native commit AND its journal landed; the
        worker dies before the bookkeeping (candidate list, MR update,
        ``waiting_ci``, the round's dispatched mark). ``on_response`` —
        the HTTP effect landed but the response and every journal write
        were lost (the open-intent window).
        """

        die_at: str | None = None

        async def _dispatch(
            self, flow_run_id, action_id, intent_id, commit_intent, cs, commit_message
        ):
            if type(self).die_at == "on_response":
                await self._gitlab.create_commit(
                    self._project_id,
                    cs.branch,
                    TestRoundOwnEffectRecovery._real_commit_actions(cs),
                    commit_message,
                )
                raise RuntimeError("worker died between the native commit and its journal")
            result = await super()._dispatch(
                flow_run_id, action_id, intent_id, commit_intent, cs, commit_message
            )
            if type(self).die_at == "after_journal":
                raise RuntimeError("worker died after the native commit, before bookkeeping")
            return result

    def _crash_service(self, db, fake, **overrides):
        overrides.setdefault("writer_class", self._CrashWriter)
        return make_round_service(db, fake, **overrides)

    async def test_a_kill_after_the_journal_adopts_the_own_effect_with_no_second_commit(
        self, rounds_db, rounds_fake, caplog
    ):
        import logging

        implementer = RecordingImplementer()
        service = self._crash_service(rounds_db, rounds_fake, implementer=implementer)
        run_id, candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        parent_before = await _get_run(rounds_db, run_id)
        commits_before_round = _provider_commit_calls(rounds_fake)
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9500, body="/fix cover `forge-demo/a.md` empty input"
        )

        # The worker dies AFTER the native commit and its journal, BEFORE
        # any bookkeeping: evidence, usage and the MR/state updates land
        # later — exactly the window the base-head fence misread foreign.
        type(self)._CrashWriter.die_at = "after_journal"
        try:
            with pytest.raises(RuntimeError, match="after the native commit"):
                await service.run_command(
                    _round_command(mr_iid, "9500", "/fix cover `forge-demo/a.md` empty input")
                )
        finally:
            type(self)._CrashWriter.die_at = None
        rounds = await _rounds_of(rounds_db, run_id)
        child_id = rounds[0].child_run_id
        child = await _get_run(rounds_db, child_id)
        assert child.status == FlowStatus.COMMITTING.value  # mid-advance, post-commit
        assert rounds[0].status == "admitted"  # the dispatched mark never landed
        own_sha = rounds_fake.branches[await _branch_of(rounds_db, child)][0]["sha"]
        assert own_sha != candidate
        commits_after_crash = _provider_commit_calls(rounds_fake)
        assert commits_after_crash == commits_before_round + 1  # the round's own native commit
        dispatches_after_crash = len(implementer.dispatches)

        # A NEW worker reconciles: the round's own effect is classified
        # BEFORE the fence and ADOPTED — the walk resumes post-publication.
        with caplog.at_level(logging.INFO, logger="forge.runs.service"):
            await service.evaluate_review_rounds()
        child = await _get_run(rounds_db, child_id)
        assert child.status == FlowStatus.WAITING_CI.value, child.status
        assert _candidate_of(child) == own_sha
        assert _provider_commit_calls(rounds_fake) == commits_after_crash  # NO second commit
        assert len(implementer.dispatches) == dispatches_after_crash  # no regeneration
        assert (await _rounds_of(rounds_db, run_id))[0].status == "dispatched"
        # the recovery log names the adopted round/attempt/effect
        assert child_id[:8] in caplog.text
        assert own_sha[:8] in caplog.text
        assert "OWN effect" in caplog.text
        events = await _resolution_events(rounds_db, child_id)
        assert [event["resolution"] for event in events] == ["own"]
        assert events[0]["effect_sha"] == own_sha
        assert events[0]["base_head_sha"] == candidate
        # the parent stays immutable history
        parent_after = await _get_run(rounds_db, run_id)
        assert parent_after.status == FlowStatus.READY_FOR_HUMAN.value
        assert list(parent_after.candidate_shas or []) == list(parent_before.candidate_shas or [])
        assert (parent_after.evidence or {})["review"] == (parent_before.evidence or {})["review"]

        # and the round reaches verification on the ADOPTED candidate —
        # still exactly one provider commit for the whole journey.
        rounds_fake.resolve(mr_iid, "d-fix")
        child = await _green_child(
            rounds_db, rounds_fake, service, (await _rounds_of(rounds_db, run_id))[0]
        )
        assert child.status == FlowStatus.READY_FOR_HUMAN.value
        assert _provider_commit_calls(rounds_fake) == commits_before_round + 1

    async def test_a_dropped_commit_response_is_adopted_by_identity_never_replayed(
        self, rounds_db, rounds_fake
    ):
        service = self._crash_service(rounds_db, rounds_fake)
        run_id, candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        commits_before_round = _provider_commit_calls(rounds_fake)
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9501, body="/fix cover `forge-demo/a.md` empty input"
        )

        # The HTTP effect landed; the response was dropped — the intent
        # stays OPEN, nothing is journaled, the run state never moved.
        type(self)._CrashWriter.die_at = "on_response"
        try:
            with pytest.raises(RuntimeError, match="between the native commit"):
                await service.run_command(
                    _round_command(mr_iid, "9501", "/fix cover `forge-demo/a.md` empty input")
                )
        finally:
            type(self)._CrashWriter.die_at = None
        rounds = await _rounds_of(rounds_db, run_id)
        child_id = rounds[0].child_run_id
        child = await _get_run(rounds_db, child_id)
        assert child.status == FlowStatus.COMMITTING.value
        own_sha = rounds_fake.branches[await _branch_of(rounds_db, child)][0]["sha"]
        async with rounds_db() as session:
            intents = (
                (
                    await session.execute(
                        select(PublicationIntent).where(PublicationIntent.run_id == child_id)
                    )
                )
                .scalars()
                .all()
            )
        assert len(intents) == 1 and intents[0].status in ("requested", "dispatched")

        # The reconciler classifies by identity (marker + parents), adopts
        # the landed effect through the writer's open-intent recovery, and
        # the round walks on — the provider saw exactly ONE commit.
        await service.evaluate_review_rounds()
        child = await _get_run(rounds_db, child_id)
        assert child.status == FlowStatus.WAITING_CI.value, child.status
        assert _candidate_of(child) == own_sha
        assert _provider_commit_calls(rounds_fake) == commits_before_round + 1  # never replayed
        async with rounds_db() as session:
            intents = (
                (
                    await session.execute(
                        select(PublicationIntent).where(PublicationIntent.run_id == child_id)
                    )
                )
                .scalars()
                .all()
            )
        assert len(intents) == 1  # one effect intent for the round, ever
        assert intents[0].status == "adopted"
        assert intents[0].provider_object_id == own_sha
        assert [
            _event["resolution"] for _event in (await _resolution_events(rounds_db, child_id))
        ] == ["own"]

    async def test_a_kill_in_ensuring_draft_mr_keeps_one_mr_and_one_candidate(
        self, rounds_db, rounds_fake
    ):
        service = make_round_service(rounds_db, rounds_fake, writer_class=self._RealWriter)
        run_id, candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        commits_before_round = _provider_commit_calls(rounds_fake)
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9502, body="/fix cover `forge-demo/a.md` empty input"
        )
        mrs_before = _provider_mr_creations(rounds_fake)

        # The worker dies INSIDE ensuring_draft_mr — the own commit is
        # journaled, the MR (the lineage's one collaboration surface)
        # already exists, the waiting_ci walk never ran.
        die = {"once": True}
        real_update = service._update_draft_mr

        async def crash_once(*args: Any, **kwargs: Any):
            if die["once"]:
                die["once"] = False
                raise RuntimeError("worker died ensuring the draft MR")
            return await real_update(*args, **kwargs)

        service._update_draft_mr = crash_once  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="ensuring the draft MR"):
            await service.run_command(
                _round_command(mr_iid, "9502", "/fix cover `forge-demo/a.md` empty input")
            )
        rounds = await _rounds_of(rounds_db, run_id)
        child_id = rounds[0].child_run_id
        child = await _get_run(rounds_db, child_id)
        assert child.status == FlowStatus.ENSURING_DRAFT_MR.value
        own_sha = rounds_fake.branches[await _branch_of(rounds_db, child)][0]["sha"]

        await service.evaluate_review_rounds()
        child = await _get_run(rounds_db, child_id)
        assert child.status == FlowStatus.WAITING_CI.value, child.status
        assert list(child.candidate_shas or []) == [own_sha]  # ONE candidate
        assert _provider_commit_calls(rounds_fake) == commits_before_round + 1  # no second commit
        # ONE collaboration surface: the MR the lineage already had
        assert _provider_mr_creations(rounds_fake) == mrs_before
        branch_mrs = [
            mr
            for mr in rounds_fake.merge_requests.values()
            if mr.get("source_branch") == await _branch_of(rounds_db, child)
        ]
        assert len(branch_mrs) == 1

    async def test_a_human_commit_after_the_candidate_is_a_typed_conflict_preserved(
        self, rounds_db, rounds_fake, monkeypatch
    ):
        monkeypatch.setenv(FORGE_MAX_REVIEW_ROUNDS_ENV, "2")
        implementer = RecordingImplementer()
        service = self._crash_service(rounds_db, rounds_fake, implementer=implementer)
        run_id, candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        parent_before = await _get_run(rounds_db, run_id)
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9503, body="/fix cover `forge-demo/a.md` empty input"
        )
        type(self)._CrashWriter.die_at = "after_journal"
        try:
            with pytest.raises(RuntimeError, match="after the native commit"):
                await service.run_command(
                    _round_command(mr_iid, "9503", "/fix cover `forge-demo/a.md` empty input")
                )
        finally:
            type(self)._CrashWriter.die_at = None
        rounds = await _rounds_of(rounds_db, run_id)
        child_id = rounds[0].child_run_id
        child = await _get_run(rounds_db, child_id)
        own_sha = rounds_fake.branches[await _branch_of(rounds_db, child)][0]["sha"]

        # A HUMAN commit lands on top of the round's candidate before the
        # recovery pass — preserved, a TYPED conflict, never auto-reset
        # and never force-pushed over.
        rounds_fake.seed_commit(
            await _branch_of(rounds_db, child),
            "late-human-sha",
            "human: late edit",
            parents=[own_sha],
        )

        await service.evaluate_review_rounds()

        child = await _get_run(rounds_db, child_id)
        assert child.status == FlowStatus.BLOCKED.value
        assert "review_round_foreign_head" in (child.status_reason or "")
        assert child.cancel_requested is True  # future publication rights revoked
        rounds = await _rounds_of(rounds_db, run_id)
        assert rounds[0].status == "stale"
        # BOTH commits still stand on the branch — nothing was reset
        branch_shas = [c["sha"] for c in rounds_fake.branches[await _branch_of(rounds_db, child)]]
        assert branch_shas[0] == "late-human-sha"
        assert own_sha in branch_shas
        # the parent request carries the TYPED conflict + the reply
        parent = await _get_run(rounds_db, run_id)
        requests = review_feedback_requests_of(parent.evidence or {})
        assert requests["9503"].status == REQUEST_STALE_HEAD
        assert any("stale_head" in note["body"] for note in rounds_fake.mr_notes)
        # the parent stays immutable history
        assert parent.status == FlowStatus.READY_FOR_HUMAN.value
        assert list(parent.candidate_shas or []) == list(parent_before.candidate_shas or [])
        assert (parent.evidence or {})["review"] == (parent_before.evidence or {})["review"]
        assert [
            event["resolution"] for event in (await _resolution_events(rounds_db, child_id))
        ] == ["foreign"]
        # the lineage's ONE outstanding-round slot freed at the settle:
        # a fresh authorized correction against the HUMAN head admits the
        # next round (routed at the ready parent, the /go-conflict shape)
        from forge.adaptive.revisions import record_review_feedback_request

        retry = ReviewFeedbackRequest(
            note_id="9504",
            run_id=run_id,
            discussion_id="d-fix2",
            mr_iid=mr_iid,
            actor="alice",
            head_sha="late-human-sha",
            classification=IN_SCOPE_CORRECTION_CLASS,
            text="/fix retry `forge-demo/a.md`",
            referenced_paths=("forge-demo/a.md",),
        )
        await record_review_feedback_request(rounds_db, run_id, retry)
        dispatches_before = len(implementer.dispatches)
        await service._admit_review_round(PROJECT_ID, mr_iid, run_id, ISSUE_IID, retry)
        rounds = await _rounds_of(rounds_db, run_id)
        assert [row.round_number for row in rounds] == [2, 3]
        assert rounds[1].base_head_sha == "late-human-sha"
        assert len(implementer.dispatches) == dispatches_before + 1

    async def test_an_unavailable_probe_leaves_uncertainty_visible_no_blind_publication(
        self, rounds_db, rounds_fake, monkeypatch, caplog
    ):
        import logging

        from forge.gitlab.client import GitLabAPIError

        service = self._crash_service(rounds_db, rounds_fake)
        run_id, candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        commits_before_round = _provider_commit_calls(rounds_fake)
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9505, body="/fix cover `forge-demo/a.md` empty input"
        )
        type(self)._CrashWriter.die_at = "on_response"
        try:
            with pytest.raises(RuntimeError, match="between the native commit"):
                await service.run_command(
                    _round_command(mr_iid, "9505", "/fix cover `forge-demo/a.md` empty input")
                )
        finally:
            type(self)._CrashWriter.die_at = None
        rounds = await _rounds_of(rounds_db, run_id)
        child_id = rounds[0].child_run_id

        # The identity probe is UNAVAILABLE: the open intent's outcome
        # cannot be arbitrated — uncertainty must stay visible, with no
        # stale, no dispatch and no publication derived from the blind spot.
        async def unavailable(project_id: int, ref: str) -> list[dict]:
            raise GitLabAPIError(503, "probe unavailable")

        monkeypatch.setattr(rounds_fake, "list_commits", unavailable)
        with caplog.at_level(logging.WARNING, logger="forge.runs.service"):
            await service.evaluate_review_rounds()
            await service.evaluate_review_rounds()

        child = await _get_run(rounds_db, child_id)
        assert child.status == FlowStatus.COMMITTING.value  # untouched, still mid-advance
        rounds = await _rounds_of(rounds_db, run_id)
        assert rounds[0].status == "admitted"  # neither staled nor dispatched
        assert (
            _provider_commit_calls(rounds_fake) == commits_before_round + 1
        )  # nothing published blindly
        async with rounds_db() as session:
            intents = (
                (
                    await session.execute(
                        select(PublicationIntent).where(PublicationIntent.run_id == child_id)
                    )
                )
                .scalars()
                .all()
            )
        assert len(intents) == 1 and intents[0].status in ("requested", "dispatched")
        # the uncertainty is journaled — one unknown resolution, not one per tick
        assert [
            event["resolution"] for event in (await _resolution_events(rounds_db, child_id))
        ] == ["unknown"]
        assert "unresolved" in caplog.text

    async def test_a_settled_round_settles_its_stranded_child_explicitly(
        self, rounds_db, rounds_fake
    ):
        service = make_round_service(rounds_db, rounds_fake)
        run_id, candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9506, body="/fix cover `forge-demo/a.md` empty input"
        )
        await service.run_command(
            _round_command(mr_iid, "9506", "/fix cover `forge-demo/a.md` empty input")
        )
        rounds = await _rounds_of(rounds_db, run_id)
        child_id = rounds[0].child_run_id

        # The pre-fix orphan shape, reproduced as a legacy database would
        # hold it: the round row closed STALE while the child stayed
        # mid-advance (the old stale path never settled its child).
        async with rounds_db() as session:
            row = await session.get(ReviewRound, rounds[0].id)
            row.status = "stale"
            child = await session.get(FlowRun, child_id)
            child.status = FlowStatus.COMMITTING.value
            await session.commit()

        await service.evaluate_review_rounds()
        child = await _get_run(rounds_db, child_id)
        assert child.status == FlowStatus.BLOCKED.value
        assert "review_round_settled" in (child.status_reason or "")
        assert child.cancel_requested is True  # publication rights revoked
        assert (await _rounds_of(rounds_db, run_id))[0].status == "stale"  # untouched

        # explicit and idempotent — the second pass is a no-op
        reason_after_first = (await _get_run(rounds_db, child_id)).status_reason
        await service.evaluate_review_rounds()
        child = await _get_run(rounds_db, child_id)
        assert child.status == FlowStatus.BLOCKED.value
        assert child.status_reason == reason_after_first

    async def test_a_completed_round_with_a_terminal_child_is_never_touched(
        self, rounds_db, rounds_fake
    ):
        service = make_round_service(rounds_db, rounds_fake)
        run_id, candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9507, body="/fix cover `forge-demo/a.md` empty input"
        )
        await service.run_command(
            _round_command(mr_iid, "9507", "/fix cover `forge-demo/a.md` empty input")
        )
        rounds = await _rounds_of(rounds_db, run_id)
        child = await _green_child(rounds_db, rounds_fake, service, rounds[0])
        rounds_fake.resolve(mr_iid, "d-fix")
        await service.evaluate_waiting_ci()
        await service.evaluate_review_rounds()  # closes the round
        assert (await _rounds_of(rounds_db, run_id))[0].status == "completed"
        child = await _get_run(rounds_db, rounds[0].child_run_id)
        assert child.status == FlowStatus.READY_FOR_HUMAN.value

        # the stranded inventory finds nothing to settle — a terminal
        # child of a settled round is the NORMAL completed shape
        await service.evaluate_review_rounds()
        child = await _get_run(rounds_db, rounds[0].child_run_id)
        assert child.status == FlowStatus.READY_FOR_HUMAN.value
        assert "review_round_settled" not in (child.status_reason or "")

    async def test_two_reconcilers_cannot_revive_a_settled_round(self, rounds_db, rounds_fake):
        implementer = RecordingImplementer()
        service = make_round_service(rounds_db, rounds_fake, implementer=implementer)
        run_id, candidate, mr_iid = await _ready_run(rounds_db, rounds_fake, service)
        rounds_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9508, body="/fix cover `forge-demo/a.md` empty input"
        )
        await service.run_command(
            _round_command(mr_iid, "9508", "/fix cover `forge-demo/a.md` empty input")
        )
        rounds = await _rounds_of(rounds_db, run_id)
        child_id = rounds[0].child_run_id

        # the round settles (the terminal close a racing reconciler won)
        assert await service._mark_review_round(child_id, "completed", "child terminal")
        # the expected-state CAS refuses EVERY revival from a settled row —
        # an old dispatcher cannot overwrite completed with dispatched
        for revived in ("dispatched", "admitted", "stale", "ended"):
            assert await service._mark_review_round(child_id, revived) is False
        assert (await _rounds_of(rounds_db, run_id))[0].status == "completed"

        # and the dispatch ENTRY GUARD stands a stale dispatcher down: no
        # advance leg runs for a settled round's child
        child = await _get_run(rounds_db, child_id)
        request = review_feedback_requests_of(child.evidence or {})["9508"]
        dispatches_before = len(implementer.dispatches)
        await service._dispatch_review_round(child_id, PROJECT_ID, request)
        assert len(implementer.dispatches) == dispatches_before


# ----------------------------------------------------------------------
# R41-08 (#363) — the round-schema rollback guard
# ----------------------------------------------------------------------


class TestRoundSchemaDowngradeGuard:
    """A rollback attempted while a child round executes refuses typed.

    The 031 downgrade's guard (the 030/029 honest-downgrade precedent,
    and the twin of 032's tested guard): ``review_rounds`` rows are the
    supersession linkage — dropping them while any exist would leave
    child runs reading as independent deliveries with no lineage. The
    guard runs BEFORE any change, so a refusal leaves the schema
    untouched (the refusal IS the safe state — documented as the
    cold-install runbook §8a table).
    """

    def test_the_downgrade_refuses_while_a_round_row_exists(self):
        import importlib.util
        from pathlib import Path

        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import create_engine, text

        repo_root = Path(__file__).resolve().parents[1]
        path = repo_root / "alembic" / "versions" / "031_review_rounds.py"
        spec = importlib.util.spec_from_file_location("migration_031_under_test", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        engine = create_engine("sqlite://")
        with engine.connect() as conn:
            conn.execute(
                text(
                    "CREATE TABLE review_rounds (id INTEGER PRIMARY KEY,"
                    " root_run_id VARCHAR(32) NOT NULL)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO review_rounds (id, root_run_id)"
                    " VALUES (1, 'a1b2c3d4' || '000000000000000000000000a1b2c3d4')"
                )
            )
            conn.commit()
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                with pytest.raises(RuntimeError, match="review_rounds holds 1 round row"):
                    module.downgrade()
            # the guard refused BEFORE any change — the linkage row survives
            assert conn.execute(text("SELECT count(*) FROM review_rounds")).scalar_one() == 1

    def test_the_downgrade_proceeds_only_when_the_table_is_empty(self):
        import importlib.util
        from pathlib import Path

        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import create_engine, text

        repo_root = Path(__file__).resolve().parents[1]
        path = repo_root / "alembic" / "versions" / "031_review_rounds.py"
        spec = importlib.util.spec_from_file_location("migration_031_empty_under_test", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        engine = create_engine("sqlite://")
        with engine.connect() as conn:
            conn.execute(
                text(
                    "CREATE TABLE review_rounds (id INTEGER PRIMARY KEY,"
                    " root_run_id VARCHAR(32) NOT NULL)"
                )
            )
            conn.execute(
                text("CREATE INDEX ix_review_rounds_root_run_id ON review_rounds (root_run_id)")
            )
            conn.execute(
                text("CREATE INDEX uq_review_round_open_per_root ON review_rounds (root_run_id)")
            )
            conn.execute(
                text("CREATE INDEX ix_review_rounds_child_run_id ON review_rounds (root_run_id)")
            )
            conn.execute(
                text("CREATE INDEX ix_review_rounds_parent_run_id ON review_rounds (root_run_id)")
            )
            conn.commit()
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                module.downgrade()  # empty: the honest path drops the table
            # the table (and its indexes) are gone once no linkage rows exist
            tables = {
                row[0]
                for row in conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'table'")
                ).fetchall()
            }
            assert "review_rounds" not in tables
