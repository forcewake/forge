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

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive.models import PlanRevision, PlanStep
from forge.adaptive.revisions import (
    ACTIVE_PLAN_KEY,
    IN_SCOPE_CORRECTION_CLASS,
    REQUEST_CONFLICTING,
    REQUEST_MR_CLOSED,
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
from forge.durable import FlowRun, FlowStatus, PublicationIntent
from forge.durable.models import ReviewRound
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
from tests.test_runs_service import FakeWriter


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


def _branch_of(run: FlowRun) -> str:
    from forge.runs.stubs import factory_branch

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
    pipeline_id = (await fake.create_pipeline(PROJECT_ID, _branch_of(child)))["id"]
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
        assert child.id != run_id and child.id[:8] == run_id[:8]  # same branch lineage
        parent = await _get_run(rounds_db, run_id)
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
        branch = _branch_of(parent)

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
        branch = _branch_of(parent)
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
        rounds_fake.seed_commit(_branch_of(child), "late-human-sha", "human: late edit")
        rounds_fake.resolve(mr_iid, "d-fix")
        pipeline_id = (await rounds_fake.create_pipeline(PROJECT_ID, _branch_of(child)))["id"]
        rounds_fake.set_pipeline_status(pipeline_id, "success", candidate)
        await service.evaluate_waiting_ci()
        child = await _get_run(rounds_db, rounds[0].child_run_id)
        assert child.status == FlowStatus.BLOCKED.value
        assert "external_change" in (child.status_reason or "") or "candidate_drift" in (
            child.status_reason or ""
        )
        # the human commit stands on the branch
        assert rounds_fake.branches[_branch_of(child)][0]["sha"] == "late-human-sha"

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
