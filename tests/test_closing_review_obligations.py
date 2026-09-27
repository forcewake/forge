"""R41-06 (#361) — the closing review bound to the active obligations.

Two verified bases (external review 68f22b8, item R41-06):

1. **The captured input.** The review-round child carries an ACTIVE_PLAN
   seed (the folded correction revision — #338) and the inherited
   correction request, but the closing reviewer's ``plan_summary`` came
   from the legacy ``_read_plan_evidence`` path — the ``evidence.plan``
   section ONLY the planning leg writes. A round child is admitted
   straight into ``proposing``: it never runs planning, so the capture
   below measures what the reviewer ACTUALLY received for (a) an
   ordinary parent and (b) an adaptive-revision parent with a new
   correction, before any fix is claimed.

2. **The policy split.** ``_review_feedback_unresolved`` treated an
   UNREADABLE discussions surface as nonblocking even for code-requesting
   feedback. Unknown must render as unknown: in a required-resolution
   profile it BLOCKS readiness (a typed ``evidence_unavailable`` reason);
   a best-effort profile hands off with the unknown NAMED — never a
   verified-human-resolution claim.

Plus the persistence identity: a review's applicability is the OBLIGATION
digest (the reviewer brief + the candidate + the code-requesting
corrections' text/head/policy), so a materially changed correction on the
same source sha can never reuse an earlier review merely because the sha
matches.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive.revisions import (
    IN_SCOPE_CORRECTION_CLASS,
    REVIEW_FEEDBACK_KEY,
    ReviewFeedbackRequest,
    review_feedback_requests_of,
)
from forge.durable import FlowRun
from forge.gateway.feedback import FORGE_MAX_REVIEW_ROUNDS_ENV
from forge.gitlab.client import GitLabAPIError
from forge.models.base import Base
from forge.runs.stubs import StubReviewer
from tests.test_review_feedback import (
    ISSUE_DESC,
    ISSUE_IID,
    ISSUE_TITLE,
    PROJECT_ID,
    ReviewFakeGitLab,
    _feedback_command,
)
from tests.test_review_rounds import (
    _candidate_of,
    _get_run,
    _green_child,
    _ready_run,
    _round_command,
    _rounds_of,
    make_round_service,
)
from tests.test_runs_service import FakeWriter

_CORRECTION_TEXT = "/fix also cover `forge-demo/a.md` empty input"


class RecordingReviewer(StubReviewer):
    """The closing reviewer, capturing every request verbatim (R41-06)."""

    async def review(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.result


class UnreadableDiscussions(ReviewFakeGitLab):
    """The discussions surface answers 404 while ``failing`` — unavailable,
    not empty (the typed degradation ``_discussions_or_none`` performs)."""

    def __init__(self) -> None:
        super().__init__()
        self.failing = False

    async def list_discussions(self, project_id: int, mr_iid: int):
        if self.failing:
            raise GitLabAPIError(404, "discussions surface unavailable")
        return await super().list_discussions(project_id, mr_iid)


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
def fake():
    fake = ReviewFakeGitLab()
    fake.seed_issue(ISSUE_IID, ISSUE_TITLE, ISSUE_DESC)
    fake.seed_commit("main", "base-sha-1", "initial")
    fake.seed_file(".forge.yml", "implement:\n  paths:\n    - forge-demo/**\n")
    return fake


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.delenv(FORGE_MAX_REVIEW_ROUNDS_ENV, raising=False)
    FakeWriter.reset()
    yield
    FakeWriter.reset()


async def _admit_round(db, fake, service, run_id: str, mr_iid: int, *, note_id: str):
    fake.seed_discussion(mr_iid, "d-fix", note_id=9400, body=_CORRECTION_TEXT)
    await service.run_command(_round_command(mr_iid, note_id, _CORRECTION_TEXT))
    rounds = await _rounds_of(db, run_id)
    assert len(rounds) == 1
    return rounds[0]


async def _strand_back_into_reviewing(db, run_id: str) -> None:
    """The crash-resume shape: the review landed but the run never reached
    ready — the reconciler's ``reviewing`` pass re-enters the leg."""
    async with db() as session:
        row = await session.get(FlowRun, run_id)
        row.status = "reviewing"
        await session.commit()


# ----------------------------------------------------------------------
# THE CAPTURE — what the closing reviewer actually receives
# ----------------------------------------------------------------------


class TestTheCapturedReviewerInput:
    async def test_an_ordinary_parent_briefs_the_reviewer_from_its_frozen_plan(self, db, fake):
        reviewer = RecordingReviewer()
        service = make_round_service(db, fake, reviewer=reviewer)
        run_id, candidate, _mr_iid = await _ready_run(db, fake, service)

        call = reviewer.calls[-1]
        assert call["candidate_sha"] == candidate
        # the plan the approver saw — the planning leg wrote evidence.plan
        assert "Implementation plan" in call["plan_summary"]
        assert call["plan_summary"].strip()

    async def test_a_round_child_briefs_the_reviewer_with_the_new_correction(self, db, fake):
        """The capture that motivated the fix: an adaptive-revision parent
        takes a correction; the round child's closing review must show the
        NEW correction text, the current candidate and the inherited
        constraints (the parent revision the round folded)."""
        reviewer = RecordingReviewer()
        service = make_round_service(db, fake, reviewer=reviewer)
        run_id, _candidate, mr_iid = await _ready_run(db, fake, service, seed_revision=True)

        round_row = await _admit_round(db, fake, service, run_id, mr_iid, note_id="9400")
        child = await _get_run(db, round_row.child_run_id)
        child_candidate = _candidate_of(child)
        # the child NEVER ran the planning leg — the legacy reader's source
        # (the parent-style evidence.plan section) does not exist on it
        assert "plan" not in (child.evidence or {})

        fake.resolve(mr_iid, "d-fix")
        child = await _green_child(db, fake, service, round_row)
        assert child.status == "ready_for_human"

        call = next(c for c in reviewer.calls if c["candidate_sha"] == child_candidate)
        assert "empty input" in call["plan_summary"]  # the NEW correction text
        assert "Land the validator." in call["plan_summary"]  # inherited constraint
        assert "Reviewer correction" in call["plan_summary"]  # full provenance
        assert call["candidate_sha"] == child_candidate

    async def test_a_classic_round_child_briefs_the_correction_and_frozen_plan(self, db, fake):
        """The ordinary (never-staged-revision) parent: the verified
        adapter derives the round's plan from the frozen spec + request —
        the reviewer sees BOTH the correction and the approved plan."""
        reviewer = RecordingReviewer()
        service = make_round_service(db, fake, reviewer=reviewer)
        run_id, _candidate, mr_iid = await _ready_run(db, fake, service)

        round_row = await _admit_round(db, fake, service, run_id, mr_iid, note_id="9401")
        child = await _get_run(db, round_row.child_run_id)
        child_candidate = _candidate_of(child)

        fake.resolve(mr_iid, "d-fix")
        child = await _green_child(db, fake, service, round_row)
        assert child.status == "ready_for_human"

        call = next(c for c in reviewer.calls if c["candidate_sha"] == child_candidate)
        assert "empty input" in call["plan_summary"]  # the correction text
        assert "Implementation plan" in call["plan_summary"]  # the frozen plan


# ----------------------------------------------------------------------
# The obligation digest — review applicability persisted by identity
# ----------------------------------------------------------------------


class TestReviewObligationDigest:
    async def test_the_persisted_review_carries_its_obligation_digest(self, db, fake):
        reviewer = RecordingReviewer()
        service = make_round_service(db, fake, reviewer=reviewer)
        run_id, _candidate, mr_iid = await _ready_run(db, fake, service, seed_revision=True)
        parent_review = dict((await _get_run(db, run_id)).evidence or {}).get("review")

        round_row = await _admit_round(db, fake, service, run_id, mr_iid, note_id="9402")
        fake.resolve(mr_iid, "d-fix")
        child = await _green_child(db, fake, service, round_row)
        assert child.status == "ready_for_human"

        child_review = (child.evidence or {}).get("review") or {}
        assert re.fullmatch(r"[0-9a-f]{64}", str(child_review.get("obligation_digest")))
        # the parent's delivery-1 review answered a DIFFERENT obligation
        assert parent_review is not None
        assert parent_review.get("obligation_digest") != child_review["obligation_digest"]

    async def _green_ready_child(self, db, fake, reviewer, *, note_id: str):
        service = make_round_service(db, fake, reviewer=reviewer)
        run_id, _candidate, mr_iid = await _ready_run(db, fake, service)
        round_row = await _admit_round(db, fake, service, run_id, mr_iid, note_id=note_id)
        fake.resolve(mr_iid, "d-fix")
        child = await _green_child(db, fake, service, round_row)
        assert child.status == "ready_for_human"
        return service, child, mr_iid

    async def test_an_unchanged_obligation_replays_without_a_second_model_call(self, db, fake):
        """The reconciler's ``reviewing`` re-drive (the crash-resume
        shape): the same candidate under the same obligation replays the
        persisted review — the digest never breaks the R07 contract."""
        reviewer = RecordingReviewer()
        service, child, _mr_iid = await self._green_ready_child(db, fake, reviewer, note_id="9403")
        calls_before = len(reviewer.calls)
        digest_before = (child.evidence or {})["review"]["obligation_digest"]

        await _strand_back_into_reviewing(db, child.id)
        await service.evaluate_waiting_ci()

        assert len(reviewer.calls) == calls_before  # replayed, never re-called
        after = await _get_run(db, child.id)
        assert after.status == "ready_for_human"
        assert (after.evidence or {})["review"]["obligation_digest"] == digest_before

    async def test_a_materially_changed_correction_on_the_same_sha_re_reviews(self, db, fake):
        """The digest's AC: the same candidate sha under a MATERIALLY
        changed correction obligation refuses the earlier review — the
        sha alone never proves applicability."""
        reviewer = RecordingReviewer()
        service, child, _mr_iid = await self._green_ready_child(db, fake, reviewer, note_id="9404")
        calls_before = len(reviewer.calls)
        digest_before = (child.evidence or {})["review"]["obligation_digest"]
        child_candidate = _candidate_of(child)

        # the obligation MATERIALLY changes against the SAME source head:
        # the recorded correction text is a different ask while the
        # candidate sha stands. The stored review's applicability no
        # longer holds — the sha match alone must not reuse it.
        async with db() as session:
            row = await session.get(FlowRun, child.id)
            evidence = dict(row.evidence or {})
            requests = dict(evidence[REVIEW_FEEDBACK_KEY])
            document = dict(requests["9404"])
            document["text"] = "/fix also cover `forge-demo/a.md` TRAILING NEWLINE"
            requests["9404"] = document
            evidence[REVIEW_FEEDBACK_KEY] = requests
            row.evidence = evidence
            await session.commit()

        await _strand_back_into_reviewing(db, child.id)
        await service.evaluate_waiting_ci()

        assert len(reviewer.calls) == calls_before + 1  # refused reuse, re-reviewed
        after = await _get_run(db, child.id)
        assert after.status == "ready_for_human"
        assert _candidate_of(after) == child_candidate  # the SAME sha re-reviewed
        new_digest = (after.evidence or {})["review"]["obligation_digest"]
        assert new_digest != digest_before
        assert re.fullmatch(r"[0-9a-f]{64}", new_digest)


# ----------------------------------------------------------------------
# Required-resolution vs best-effort — the policy split
# ----------------------------------------------------------------------


class TestRequiredResolutionProfile:
    async def test_an_unreadable_surface_blocks_readiness_with_evidence_unavailable(self, db):
        """A code-requesting correction gates the candidate; the surface
        that must carry the human's resolution is UNREADABLE — unknown
        renders as unknown and BLOCKS, never a silent ready."""
        fake = UnreadableDiscussions()
        fake.seed_issue(ISSUE_IID, ISSUE_TITLE, ISSUE_DESC)
        fake.seed_commit("main", "base-sha-1", "initial")
        fake.seed_file(".forge.yml", "implement:\n  paths:\n    - forge-demo/**\n")
        reviewer = RecordingReviewer()
        service = make_round_service(db, fake, reviewer=reviewer)
        run_id, _candidate, mr_iid = await _ready_run(db, fake, service)

        round_row = await _admit_round(db, fake, service, run_id, mr_iid, note_id="9405")
        child = await _green_child(db, fake, service, round_row)
        assert child.status == "evaluating_ci"  # held: the discussion is open
        assert [n for n in fake.mr_notes if "held for human review feedback" in n["body"]]

        # the surface goes down (404) while the discussion stays required
        fake.failing = True
        await service.evaluate_waiting_ci()
        child = await _get_run(db, round_row.child_run_id)
        assert child.status == "evaluating_ci"  # still held — unknown BLOCKS
        blocked = ((child.evidence or {}).get("readiness") or {}).get("blocked_by") or {}
        assert blocked.get("evidence_unavailable")
        assert blocked.get("code") == ""
        # the handoff NAMES the unknown — it never claims resolution
        unknown = [n for n in fake.mr_notes if "resolution unknown" in n["body"]]
        assert unknown
        assert "Still-open discussions" not in unknown[-1]["body"]
        assert "Resolved discussions" not in unknown[-1]["body"]

        # recovery: a fresh process re-reads the live surface, the human
        # resolves the thread, readiness follows
        fake.failing = False
        fresh = make_round_service(db, fake, reviewer=reviewer)
        fake.resolve(mr_iid, "d-fix")
        await fresh.evaluate_waiting_ci()
        child = await _get_run(db, round_row.child_run_id)
        assert child.status == "ready_for_human"
        released = ((child.evidence or {}).get("readiness") or {}).get("blocked_by") or {}
        assert released.get("evidence_unavailable") == []
        assert released.get("feedback") == []
        assert fake.resolve_calls == []

    async def test_a_deleted_required_discussion_blocks_readiness(self, db, fake):
        """The thread that carried the required correction is deleted
        after admission: the resolution signal is GONE — blocked with the
        typed evidence-unavailable reason, never a silent release."""
        service = make_round_service(db, fake)
        run_id, _candidate, mr_iid = await _ready_run(db, fake, service)

        round_row = await _admit_round(db, fake, service, run_id, mr_iid, note_id="9406")
        fake.discussions[mr_iid] = []  # the discussion vanished from the MR
        child = await _green_child(db, fake, service, round_row)

        assert child.status == "evaluating_ci"
        blocked = ((child.evidence or {}).get("readiness") or {}).get("blocked_by") or {}
        assert blocked.get("evidence_unavailable")
        assert fake.resolve_calls == []

    async def test_a_foreign_mr_request_never_gates_this_run(self, db, fake):
        """A required request recorded on ANOTHER MR cannot borrow this
        MR's discussion id to hold readiness — the join is scoped to the
        run's own collaboration surface."""
        service = make_round_service(db, fake)
        run_id, _candidate, mr_iid = await _ready_run(db, fake, service)

        round_row = await _admit_round(db, fake, service, run_id, mr_iid, note_id="9407")
        child = await _get_run(db, round_row.child_run_id)
        # the inherited request is re-bound to a FOREIGN MR (an evidence
        # shape only surgery produces — the point is the gate's join)
        async with db() as session:
            row = await session.get(FlowRun, child.id)
            evidence = dict(row.evidence or {})
            requests = dict(evidence[REVIEW_FEEDBACK_KEY])
            document = dict(requests["9407"])
            document["mr_iid"] = 999999
            requests["9407"] = document
            evidence[REVIEW_FEEDBACK_KEY] = requests
            row.evidence = evidence
            await session.commit()

        # the run's OWN MR carries the same discussion id, UNRESOLVED —
        # the foreign request must not hold this delivery
        child = await _green_child(db, fake, service, round_row)
        assert child.status == "ready_for_human"
        assert fake.resolve_calls == []

    async def test_a_resolution_on_another_mr_never_releases_this_one(self, db, fake):
        """The same discussion id RESOLVED on a foreign MR releases
        nothing here: the gate reads only this MR's surface."""
        service = make_round_service(db, fake)
        run_id, _candidate, mr_iid = await _ready_run(db, fake, service)

        round_row = await _admit_round(db, fake, service, run_id, mr_iid, note_id="9408")
        fake.seed_merge_request(PROJECT_ID, "other-branch", "foreign", iid=999999)
        fake.seed_discussion(999999, "d-fix", note_id=8800, body=_CORRECTION_TEXT, resolved=True)
        child = await _green_child(db, fake, service, round_row)

        assert child.status == "evaluating_ci"  # this MR's d-fix still open
        fake.resolve(mr_iid, "d-fix")  # the CORRECT surface resolves it
        await service.evaluate_waiting_ci()
        child = await _get_run(db, round_row.child_run_id)
        assert child.status == "ready_for_human"


class TestBestEffortProfile:
    async def test_an_unreadable_surface_hands_off_with_the_unknown_named(self, db):
        """No code-requesting correction gates this candidate (a recorded
        clarification only): the unreadable surface is best-effort — the
        run reaches the human and the summary NAMES the unknown, never a
        resolved/still-open claim it cannot back."""
        fake = UnreadableDiscussions()
        fake.seed_issue(ISSUE_IID, ISSUE_TITLE, ISSUE_DESC)
        fake.seed_commit("main", "base-sha-1", "initial")
        fake.seed_file(".forge.yml", "implement:\n  paths:\n    - forge-demo/**\n")
        service = make_round_service(db, fake)
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )
        mr_iid = (await _get_run(db, run_id)).mr_iid
        fake.seed_discussion(mr_iid, "d-ask", note_id=9500, body="/ask is the retry idempotent?")
        await service.run_command(
            _feedback_command(
                mr_iid,
                "/ask is the retry idempotent?",
                note_id="9500",
                discussion_id="d-ask",
            )
        )
        requests = review_feedback_requests_of((await _get_run(db, run_id)).evidence or {})
        assert requests["9500"].classification == "clarification"  # best-effort world

        fake.failing = True  # the surface drops before the handoff
        child_candidate = _candidate_of(await _get_run(db, run_id))
        branch = f"forge/issue-{ISSUE_IID}-{run_id[:8]}"
        pipeline_id = (await fake.create_pipeline(PROJECT_ID, branch))["id"]
        fake.set_pipeline_status(pipeline_id, "success", child_candidate)
        await service.evaluate_waiting_ci()

        run = await _get_run(db, run_id)
        assert run.status == "ready_for_human"  # the best-effort handoff
        summary = [n for n in fake.notes if "ready for human review" in n["body"]][-1]
        assert "unknown" in summary["body"].lower()
        assert "Still-open discussions" not in summary["body"]
        assert "Resolved discussions" not in summary["body"]
        assert "d-ask" in summary["body"]
        assert fake.resolve_calls == []


# ----------------------------------------------------------------------
# The required-set definition stays shared with the gate (unit level)
# ----------------------------------------------------------------------


def _request(**overrides) -> ReviewFeedbackRequest:
    values = dict(
        note_id="1",
        run_id="r",
        discussion_id="d-1",
        mr_iid=7,
        actor="alice",
        head_sha="h" * 40,
        classification=IN_SCOPE_CORRECTION_CLASS,
        text="/fix rename `forge-demo/x.md`",
        status="dispatched",
    )
    values.update(overrides)
    return ReviewFeedbackRequest(**values)


class TestTheRequiredSet:
    def test_only_code_requesting_live_obligations_are_required(self):
        from forge.runs.service import _required_feedback_requests

        requests = {
            "1": _request(),
            "2": _request(note_id="2", classification="clarification"),
            "3": _request(note_id="3", status="staged"),
            "4": _request(note_id="4", status="correction_window_closed"),
            "5": _request(note_id="5", classification="material_change"),
            "6": _request(note_id="6", status="deleted_discussion"),
        }
        required = _required_feedback_requests(requests, 7)
        assert [r.note_id for r in required] == ["1", "3"]

    def test_a_foreign_mr_request_is_never_required_for_this_mr(self):
        from forge.runs.service import _required_feedback_requests

        assert _required_feedback_requests({"1": _request(mr_iid=999999)}, 7) == []
        # an MR-less gate keeps every live obligation (nothing to scope by)
        assert [r.note_id for r in _required_feedback_requests({"1": _request()}, None)] == ["1"]
