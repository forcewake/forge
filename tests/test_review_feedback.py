"""Q39-13 (#332) — post-MR review feedback as a bounded revision.

The review's most practical next scenario, as rules first and as a lane
second: a reviewer names a specific edit on the Draft MR; the request
binds to the CURRENT MR head; human changes are preserved; only the
permitted thing is fixed; the affected checks rerun; a new reviewable
result comes back — and the bot never merges, never resolves the
discussion, never marks the human decision complete.

Three layers:

- the CLASSIFICATION + record rules (pure, over the revisions module):
  the closed three-way matrix (a question is answered with no dispatch;
  an edit provably inside the approved write scope is a bounded
  correction; anything else is a MATERIAL proposal — never a permission
  expansion), the note-id idempotency (one reviewer comment → ONE
  durable request despite webhook replay), the head binding and the
  precise evidence invalidation.
- the DURABLE staging + approval (sqlite): the correction stages through
  the EXISTING human gate and the EXISTING activation makes it the
  active revision TEXT — the #321 ApprovedInput machinery briefs from it.
- the SERVICE lane (the REAL RunService over the note ingress): the
  ``run_command`` dispatch a GitLab MR note travels, the typed refusals
  (unauthorized actor, deleted discussion, conflicting instructions,
  stale head, closed window), the correction re-dispatch through the
  repair edge, the required-discussion readiness gate and the final
  summary naming resolved + still-open discussions and the tested
  candidate.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive.models import PlanRevision, PlanStep
from forge.adaptive.revisions import (
    ACTIVE_PLAN_KEY,
    CLARIFICATION_CLASS,
    IN_SCOPE_CORRECTION_CLASS,
    MATERIAL_CHANGE_CLASS,
    PENDING_PROPOSAL_KEY,
    REQUEST_CLARIFICATION_OPEN,
    REQUEST_CONFLICTING,
    REQUEST_DELETED_DISCUSSION,
    REQUEST_DISPATCHED,
    REQUEST_MATERIALIZED,
    REQUEST_REFUSED_UNAUTHORIZED,
    REQUEST_STAGED,
    REQUEST_STALE_HEAD,
    REQUEST_WINDOW_CLOSED,
    REVISION_CONTENT_KEY,
    ReviewFeedbackRefused,
    ReviewFeedbackRequest,
    activate_pending_revision,
    classify_review_feedback,
    correction_decision_id,
    correction_invalidation_set,
    head_binding_guard,
    mark_review_feedback_request,
    parse_review_feedback_note,
    plan_digest,
    read_active_plan,
    read_review_feedback_requests,
    record_review_feedback_request,
    referenced_paths_of,
    review_correction_revision,
    review_feedback_requests_of,
    review_feedback_summary_section,
    resolve_approved_input,
    stage_review_correction,
)
from forge.durable import FlowRun, FlowStatus
from forge.gitlab.client import GitLabAPIError
from forge.gitlab.schemas import Discussion
from forge.models.base import Base
from forge.runs.stubs import StubImplementer

from tests.fixtures.fake_gitlab import FakeGitLab
from tests.test_runs_service import (
    ISSUE_DESC,
    ISSUE_IID,
    ISSUE_TITLE,
    PROJECT_ID,
    FakeWriter,
    make_settings,
)

D_CONTRACT = "3" * 64
D_SNAPSHOT = "5" * 64
HEAD = "a" * 40
OTHER_HEAD = "b" * 40


def _request(**overrides) -> ReviewFeedbackRequest:
    base = dict(
        note_id="9001",
        run_id="r" * 32,
        discussion_id="d-abc",
        mr_iid=17,
        actor="alice",
        head_sha=HEAD,
        classification=IN_SCOPE_CORRECTION_CLASS,
        text="rename `src/entry.py` to validate_email",
        referenced_paths=("src/entry.py",),
        diff_context="src/entry.py -> src/entry.py",
        created_at="2026-09-25T10:00:00+00:00",
    )
    base.update(overrides)
    return ReviewFeedbackRequest(**base)


# ----------------------------------------------------------------------
# The classification matrix — fail closed, never a permission expansion
# ----------------------------------------------------------------------


class TestClassification:
    def test_a_question_is_a_clarification(self):
        assert classify_review_feedback("ask", "why is the retry idempotent?", ("src/",)) == (
            CLARIFICATION_CLASS
        )

    def test_an_edit_provably_inside_the_scope_is_a_bounded_correction(self):
        assert (
            classify_review_feedback(
                "fix", "rename `src/entry.py` to validate_email", ("src/", "tests/")
            )
            == IN_SCOPE_CORRECTION_CLASS
        )

    def test_a_path_outside_the_approved_scope_is_material(self):
        assert (
            classify_review_feedback("fix", "also update `docs/runbook.md`", ("src/",))
            == MATERIAL_CHANGE_CLASS
        )

    def test_an_unscoped_edit_is_material_scope_is_never_guessed(self):
        assert (
            classify_review_feedback("fix", "rename the entrypoint", ("src/",))
            == MATERIAL_CHANGE_CLASS
        )

    def test_exact_file_matches_an_exact_scope_entry(self):
        assert (
            classify_review_feedback("fix", "fix `src/app.py` typo", ("src/app.py",))
            == IN_SCOPE_CORRECTION_CLASS
        )

    def test_an_empty_scope_authorizes_nothing(self):
        assert classify_review_feedback("fix", "fix `src/app.py` typo", ()) == MATERIAL_CHANGE_CLASS

    def test_an_unknown_kind_refuses_typed(self):
        with pytest.raises(ReviewFeedbackRefused) as caught:
            classify_review_feedback("merge", "just merge it", ("src/",))
        assert caught.value.code == "unknown_kind"

    def test_the_note_parser_takes_only_the_two_verbs(self):
        assert parse_review_feedback_note("/fix rename `src/a.py`") == (
            "fix",
            "rename `src/a.py`",
        )
        assert parse_review_feedback_note("@forge /ask why?") == ("ask", "why?")
        assert parse_review_feedback_note("lgtm, just a normal comment") is None
        assert parse_review_feedback_note("/go 0123abcd") is None

    def test_referenced_paths_come_only_from_backticks(self):
        assert referenced_paths_of("fix `src/a.py` and `src/b.py`, not src/c.py") == (
            "src/a.py",
            "src/b.py",
        )
        assert referenced_paths_of("fix the entrypoint") == ()


# ----------------------------------------------------------------------
# The head binding + the precise evidence invalidation
# ----------------------------------------------------------------------


class TestHeadBindingAndInvalidation:
    def test_a_matching_head_passes(self):
        head_binding_guard(HEAD, HEAD)  # no refusal

    def test_a_moved_head_is_the_typed_stale_head_conflict(self):
        with pytest.raises(ReviewFeedbackRefused) as caught:
            head_binding_guard(HEAD, OTHER_HEAD)
        assert caught.value.code == "stale_head"
        assert "preserved" in caught.value.detail

    def test_an_empty_binding_never_dispatches_blind(self):
        with pytest.raises(ReviewFeedbackRefused) as caught:
            head_binding_guard("", HEAD)
        assert caught.value.code == "stale_head"

    def test_only_the_evidence_bound_to_the_corrected_head_invalidates(self):
        partition = correction_invalidation_set(
            HEAD,
            {
                "review-1": {"applicability": HEAD},
                "verification-1": {"applicability": HEAD},
                "review-0": {"applicability": OTHER_HEAD},
                "pipeline-0": {"applicability": ""},
            },
        )
        assert dict(partition["invalidated"])["review-1"]
        assert dict(partition["invalidated"])["verification-1"]
        assert partition["preserved"] == ["review-0", "pipeline-0"]
        # superseded, never deleted
        assert partition["superseded"]["review-1"]


# ----------------------------------------------------------------------
# The bounded correction revision — steps byte-identical, provenance full
# ----------------------------------------------------------------------


def _step(step_id: str, objective: str, *, writes: str | None = None) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        objective=objective,
        write_repository_id=writes,
        impact=["internal"],
        acceptance_refs=["AC-1"],
    )


def _revision(revision: int, parent: int | None, *, summary: str) -> PlanRevision:
    return PlanRevision(
        plan_id="plan-rf",
        work_id="wp-rf",
        revision=revision,
        parent_revision=parent,
        work_contract_digest=D_CONTRACT,
        snapshot_set_digest=D_SNAPSHOT,
        summary=summary,
        steps=[
            _step("S1", "Inspect the existing entrypoint."),
            _step("S2", "Implement the entrypoint.", writes="forge/validators"),
            _step("S3", "Check the result."),
        ],
    )


class TestCorrectionRevision:
    def test_steps_stay_byte_identical_so_the_wip_survives(self):
        active = _revision(1, None, summary="Land the validator.")
        proposed = review_correction_revision(active, _request())
        assert proposed.steps == active.steps
        assert proposed.revision == 2
        assert proposed.parent_revision == 1

    def test_the_summary_carries_the_full_provenance_the_agent_needs(self):
        active = _revision(1, None, summary="Land the validator.")
        proposed = review_correction_revision(active, _request())
        text = proposed.summary
        assert "d-abc" in text  # the originating discussion
        assert "9001" in text  # the note
        assert HEAD[:12] in text  # the CURRENT head binding
        assert "rename `src/entry.py` to validate_email" in text  # the permitted change
        assert "src/entry.py" in text  # the referenced paths
        assert "src/entry.py -> src/entry.py" in text  # the referenced diff context
        assert "preserve every other human edit" in text

    def test_the_decision_id_is_derived_from_the_note_identity(self):
        assert correction_decision_id("r" * 32, "9001") == correction_decision_id("r" * 32, "9001")
        assert correction_decision_id("r" * 32, "9001") != correction_decision_id("r" * 32, "9002")

    def test_the_summary_section_names_both_sides_and_the_tested_candidate(self):
        requests = {
            "9001": _request(),
            "9002": _request(
                note_id="9002",
                discussion_id="d-resolved",
                classification=CLARIFICATION_CLASS,
                text="why?",
            ),
        }
        section = review_feedback_summary_section(requests, {"d-resolved": True}, HEAD)
        assert "Tested candidate" in section and HEAD in section
        assert "d-resolved" in section  # resolved
        assert "d-abc" in section  # still open
        assert review_feedback_summary_section({}, {}, HEAD) == ""


# ----------------------------------------------------------------------
# The durable record — one reviewer comment is one request
# ----------------------------------------------------------------------


class _World:
    """A sqlite-backed FlowRun evidence store (the activation-test shape)."""

    def __init__(self) -> None:
        self.run_id = "f" * 32
        self.factory: Any = None
        self._engine = None

    async def start(self) -> None:
        self._engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(self._engine, expire_on_commit=False)
        async with self.factory() as session:
            session.add(FlowRun(id=self.run_id, project_id=1, status="waiting_ci"))
            await session.commit()

    async def stop(self) -> None:
        await self._engine.dispose()

    async def evidence(self) -> dict:
        async with self.factory() as session:
            run = await session.get(FlowRun, self.run_id)
            return dict(run.evidence or {})

    async def seed_active(self, revision: PlanRevision, *, activated_by: str = "rd-one") -> None:
        evidence = await self.evidence()
        evidence[ACTIVE_PLAN_KEY] = {
            "schema": "forge.revision.active-plan/1",
            "work_id": revision.work_id,
            "plan_id": revision.plan_id,
            "active_revision": revision.revision,
            "plan_digest": plan_digest(revision),
            "revised_from_digest": "",
            "work_contract_digest": revision.work_contract_digest,
            "authorization_epoch": 4,
            "publication_epoch": 1,
            "activated_by_decision": activated_by,
            REVISION_CONTENT_KEY: revision.model_dump(),
        }
        async with self.factory() as session:
            run = await session.get(FlowRun, self.run_id)
            run.evidence = evidence
            await session.commit()


@pytest.fixture()
async def world():
    instance = _World()
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()


class TestDurableRequestRecord:
    async def test_one_note_records_exactly_one_request_despite_replay(self, world):
        request = _request()
        first = await record_review_feedback_request(world.factory, world.run_id, request)
        again = await record_review_feedback_request(
            world.factory, world.run_id, _request(created_at="2026-09-25T11:00:00+00:00")
        )
        assert first.note_id == again.note_id
        requests = await read_review_feedback_requests(world.factory, world.run_id)
        assert list(requests) == ["9001"]  # ONE durable request
        assert requests["9001"].document()["schema"] == "forge.review.feedback/1"

    async def test_a_different_request_under_a_used_note_id_refuses_typed(self, world):
        await record_review_feedback_request(world.factory, world.run_id, _request())
        with pytest.raises(ReviewFeedbackRefused) as caught:
            await record_review_feedback_request(
                world.factory, world.run_id, _request(actor="mallory")
            )
        assert caught.value.code == "note_id_conflict"

    async def test_a_lifecycle_transition_returns_a_new_record(self, world):
        await record_review_feedback_request(world.factory, world.run_id, _request())
        updated = await mark_review_feedback_request(
            world.factory, world.run_id, "9001", REQUEST_STAGED, decision_id="rd-x"
        )
        assert updated is not None and updated.status == REQUEST_STAGED
        assert updated.decision_id == "rd-x"
        stored = (await read_review_feedback_requests(world.factory, world.run_id))["9001"]
        assert stored.status == REQUEST_STAGED


class TestStagedCorrection:
    async def test_the_correction_stages_through_the_existing_approval_route(self, world):
        active = _revision(1, None, summary="Land the validator.")
        await world.seed_active(active)
        decision_id = await stage_review_correction(world.factory, world.run_id, _request())
        assert decision_id == correction_decision_id(world.run_id, "9001")
        evidence = await world.evidence()
        pending = evidence[PENDING_PROPOSAL_KEY]
        assert pending["decision"]["decision_id"] == decision_id
        proposed = PlanRevision.model_validate(pending["proposed"])
        assert proposed.steps == active.steps  # the WIP stays compatible
        assert "validate_email" in proposed.summary
        request = review_feedback_requests_of(evidence)["9001"]
        assert request.status == REQUEST_STAGED
        # the precise invalidation was recorded beside the staging
        invalidation = (await read_review_feedback_requests(world.factory, world.run_id))["9001"]
        assert invalidation is not None

    async def test_the_approval_makes_the_correction_the_active_revision_text(self, world):
        active = _revision(1, None, summary="Land the validator.")
        await world.seed_active(active)
        decision_id = await stage_review_correction(world.factory, world.run_id, _request())
        outcome = await activate_pending_revision(
            world.factory, world.run_id, decision_id, decided_by="alice"
        )
        assert outcome.status == "activated", outcome.reason
        pointer = await read_active_plan(world.factory, world.run_id)
        assert pointer is not None and pointer["activated_by_decision"] == decision_id
        resolved = await resolve_approved_input(
            world.factory,
            world.run_id,
            task_title="T",
            task_description="D",
            spec_plan_text="the frozen spec brief",
            spec_plan_digest="s" * 64,
        )
        assert resolved.source == "revision"
        # the agent sees the correction + the current head, never an
        # obsolete source version
        assert "validate_email" in resolved.brief()
        assert HEAD[:12] in resolved.brief()
        assert "the frozen spec brief" not in resolved.brief()

    async def test_without_an_active_plan_the_correction_refuses_typed(self, world):
        with pytest.raises(ReviewFeedbackRefused) as caught:
            await stage_review_correction(world.factory, world.run_id, _request())
        assert caught.value.code == "no_active_plan"

    async def test_only_an_in_scope_correction_stages(self, world):
        with pytest.raises(ReviewFeedbackRefused) as caught:
            await stage_review_correction(
                world.factory,
                world.run_id,
                _request(classification=CLARIFICATION_CLASS),
            )
        assert caught.value.code == "not_a_correction"


# ----------------------------------------------------------------------
# The service lane — the REAL note ingress (run_command) over FakeGitLab
# ----------------------------------------------------------------------


class ReviewFakeGitLab(FakeGitLab):
    """FakeGitLab plus the MR-discussions surface the ingress consults."""

    def __init__(self) -> None:
        super().__init__()
        self.discussions: dict[int, list[dict]] = {}
        self.resolve_calls: list[tuple[int, str]] = []
        self.merge_calls: list[tuple] = []

    async def list_discussions(self, project_id: int, mr_iid: int) -> list[Discussion]:
        self.calls.append(("list_discussions", (project_id, mr_iid)))
        if mr_iid not in self.merge_requests:
            raise GitLabAPIError(404, "mr not found")  # pragma: no cover
        return [Discussion.model_validate(entry) for entry in self.discussions.get(mr_iid, [])]

    async def resolve_discussion(self, project_id, mr_iid, discussion_id, resolved=True):
        self.resolve_calls.append((mr_iid, discussion_id))
        raise AssertionError("forge never resolves a reviewer discussion")

    def seed_discussion(
        self,
        mr_iid: int,
        discussion_id: str,
        *,
        note_id: int,
        body: str,
        resolvable: bool = True,
        resolved: bool = False,
        position: dict | None = None,
    ) -> None:
        note = {
            "id": note_id,
            "body": body,
            "resolvable": resolvable,
            "resolved": resolved if resolvable else None,
        }
        if position:
            note["position"] = position
        self.discussions.setdefault(mr_iid, []).append(
            {"id": discussion_id, "individual_note": False, "notes": [note]}
        )

    def resolve(self, mr_iid: int, discussion_id: str) -> None:
        for entry in self.discussions.get(mr_iid, []):
            if entry["id"] == discussion_id:
                entry["notes"][0]["resolved"] = True

    def mr_discussion_bodies(self) -> list[str]:
        return [
            note["body"]
            for notes in self.discussions.values()
            for entry in notes
            for note in entry["notes"]
        ]


def make_review_service(db, fake, **overrides):
    from forge.runs import RunService
    from forge.runs.stubs import StubPlanner, StubReviewer

    values = dict(
        session_factory=db,
        gitlab=fake,
        settings=make_settings(),
        writer_class=FakeWriter,
        planner=StubPlanner(),
        implementer=StubImplementer(),
        reviewer=StubReviewer(),
    )
    values.update(overrides)
    return RunService(**values)


class RecordingImplementer(StubImplementer):
    """The stub implementer, remembering the brief inputs it was dispatched with."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.dispatches: list[dict] = []

    async def propose(self, run, issue_title: str, **kwargs) -> Any:
        self.dispatches.append({"issue_title": issue_title, **kwargs})
        return await super().propose(run, issue_title)


@pytest.fixture()
async def review_db():
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
def review_fake():
    fake = ReviewFakeGitLab()
    fake.seed_issue(ISSUE_IID, ISSUE_TITLE, ISSUE_DESC)
    fake.seed_commit("main", "base-sha-1", "initial")
    # The approved work scope the classification checks against: the
    # project's frozen ``implement.paths`` allowlist (the RunSpec's
    # ``allowed_paths``) — the stub lane proposes inside ``forge-demo/``.
    fake.seed_file(".forge.yml", "implement:\n  paths:\n    - forge-demo/**\n")
    return fake


async def _get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        return await session.get(FlowRun, run_id)


async def _landed_run(db, fake, service) -> tuple[str, str]:
    """A run parked in ``waiting_ci`` with its Draft MR and candidate.

    The post-approval builtin lane: the candidate is committed, the Draft
    MR opened, the branch head aligned (the FakeWriter does not touch the
    fake repository — the drift checks read the seeded head).
    """
    from forge.runs.stubs import factory_branch

    FakeWriter.reset()
    run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
    await service.handle_command_note(
        PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
    )
    run = await _get_run(db, run_id)
    assert run.status == FlowStatus.WAITING_CI.value
    branch = factory_branch(ISSUE_IID, run_id)
    fake.seed_commit(branch, "fake-sha-1", "forge commit")
    fake.merge_requests[run.mr_iid]["sha"] = "fake-sha-1"
    # The revision world the correction revises (the #321 disclosure: the
    # live planner does not emit plan revisions — revision 1 is staged
    # through the app's own durable shape).
    active = _revision(1, None, summary="Land the validator.")
    evidence = dict(run.evidence or {})
    evidence[ACTIVE_PLAN_KEY] = {
        "schema": "forge.revision.active-plan/1",
        "work_id": active.work_id,
        "plan_id": active.plan_id,
        "active_revision": 1,
        "plan_digest": plan_digest(active),
        "revised_from_digest": "",
        "work_contract_digest": active.work_contract_digest,
        "authorization_epoch": 4,
        "publication_epoch": 1,
        "activated_by_decision": "rd-one",
        REVISION_CONTENT_KEY: active.model_dump(),
    }
    async with db() as session:
        row = await session.get(FlowRun, run_id)
        row.evidence = evidence
        await session.commit()
    return run_id, "fake-sha-1"


def _feedback_command(
    run_mr_iid: int,
    note_text: str,
    *,
    note_id: str,
    author: str = "alice",
    discussion_id: str = "",
) -> dict:
    """The run_command payload a GitLab MR note produces at the ingress."""
    return {
        "command": "review_feedback",
        "project_id": PROJECT_ID,
        "issue_iid": ISSUE_IID,
        "mr_iid": run_mr_iid,
        "note_id": note_id,
        "discussion_id": discussion_id,
        "note_text": note_text,
        "author_username": author,
    }


async def _approve_via_router(db, fake, run_id: str, decision_id: str, *, note_id: int = 9100):
    """The REAL /approve-revision ingress (ControlCommandRouter)."""
    from forge.adaptive.command_router import ControlCommandRouter

    async def post_note(body: str) -> dict:
        return await fake.create_issue_note(PROJECT_ID, ISSUE_IID, body)

    router = ControlCommandRouter(
        session_factory=db,
        settings=make_settings(),
        post_note=post_note,
    )
    return await router.handle(
        {
            "command": "adaptive_control",
            "provider": "gitlab",
            "adaptive_verb": "approve-revision",
            "project_id": PROJECT_ID,
            "issue_iid": ISSUE_IID,
            "author_username": "alice",
            "note_text": f"/approve-revision {run_id} {decision_id}",
            "note_id": note_id,
        }
    )


class TestIngressClassificationAndIdempotency:
    async def test_one_comment_records_one_request_and_one_reply_despite_replay(
        self, review_db, review_fake
    ):
        implementer = RecordingImplementer()
        service = make_review_service(review_db, review_fake, implementer=implementer)
        run_id, _ = await _landed_run(review_db, review_fake, service)
        mr_iid = (await _get_run(review_db, run_id)).mr_iid
        review_fake.seed_discussion(
            mr_iid,
            "d-abc",
            note_id=9001,
            body="/fix rename `forge-demo/x.md` handler",
            resolvable=True,
        )
        payload = _feedback_command(
            mr_iid,
            "/fix rename `forge-demo/x.md` handler",
            note_id="9001",
            discussion_id="d-abc",
        )
        await service.run_command(dict(payload))
        await service.run_command(dict(payload))  # the webhook replay

        run = await _get_run(review_db, run_id)
        requests = review_feedback_requests_of(run.evidence or {})
        assert list(requests) == ["9001"]  # ONE durable request
        assert requests["9001"].classification == IN_SCOPE_CORRECTION_CLASS
        assert requests["9001"].status == REQUEST_STAGED
        assert requests["9001"].head_sha == "fake-sha-1"
        # exactly ONE operator reply (the A11 one-reply-per-note rule)
        replies = [n for n in review_fake.mr_notes if "/approve-revision" in n["body"]]
        assert len(replies) == 1
        # nothing NEW dispatched — the human gate owns the activation
        # (the one dispatch on record is /go's own advance)
        assert len(implementer.dispatches) == 1
        assert len(FakeWriter.instances) == 1

    async def test_an_unauthorized_actor_is_refused_and_records_no_staging(
        self, review_db, review_fake
    ):
        service = make_review_service(review_db, review_fake)
        run_id, _ = await _landed_run(review_db, review_fake, service)
        mr_iid = (await _get_run(review_db, run_id)).mr_iid
        await service.run_command(
            _feedback_command(
                mr_iid, "/fix rename `forge-demo/x.md`", note_id="9100", author="stranger"
            )
        )
        run = await _get_run(review_db, run_id)
        requests = review_feedback_requests_of(run.evidence or {})
        assert requests["9100"].status == REQUEST_REFUSED_UNAUTHORIZED
        assert PENDING_PROPOSAL_KEY not in (run.evidence or {})
        assert any("ignored" in n["body"] for n in review_fake.mr_notes)
        assert len(FakeWriter.instances) == 1  # only /go's own advance

    async def test_a_deleted_discussion_is_refused_typed(self, review_db, review_fake):
        service = make_review_service(review_db, review_fake)
        run_id, _ = await _landed_run(review_db, review_fake, service)
        mr_iid = (await _get_run(review_db, run_id)).mr_iid
        # the note references a discussion that no longer exists
        await service.run_command(
            _feedback_command(
                mr_iid,
                "/fix rename `forge-demo/x.md`",
                note_id="9101",
                discussion_id="d-gone",
            )
        )
        run = await _get_run(review_db, run_id)
        requests = review_feedback_requests_of(run.evidence or {})
        assert requests["9101"].status == REQUEST_DELETED_DISCUSSION
        assert PENDING_PROPOSAL_KEY not in (run.evidence or {})
        assert any("deleted_discussion" in n["body"] for n in review_fake.mr_notes)

    async def test_a_transient_provider_failure_is_retried_never_read_as_deletion(
        self, review_db, review_fake
    ):
        """R40-01 (#337): a 5xx from the discussions surface is TRANSIENT —
        the command fails (the step runtime retries it with backoff) and
        NOTHING is recorded; a confirmed deletion (the readable list without
        the id, above) is the typed ``deleted_discussion`` refusal. The two
        outcomes must never be confused."""

        class FlakyDiscussions(ReviewFakeGitLab):
            def __init__(self) -> None:
                super().__init__()
                self.failing = True

            async def list_discussions(self, project_id: int, mr_iid: int) -> list[Discussion]:
                if self.failing:
                    raise GitLabAPIError(503, "discussions temporarily unavailable")
                return await super().list_discussions(project_id, mr_iid)

        fake = FlakyDiscussions()
        fake.seed_issue(ISSUE_IID, ISSUE_TITLE, ISSUE_DESC)
        fake.seed_commit("main", "base-sha-1", "initial")
        fake.seed_file(".forge.yml", "implement:\n  paths:\n    - forge-demo/**\n")
        service = make_review_service(review_db, fake)
        run_id, _ = await _landed_run(review_db, fake, service)
        mr_iid = (await _get_run(review_db, run_id)).mr_iid
        fake.seed_discussion(mr_iid, "d-flaky", note_id=9102, body="/fix rename `forge-demo/x.md`")

        with pytest.raises(GitLabAPIError):
            await service.run_command(
                _feedback_command(
                    mr_iid,
                    "/fix rename `forge-demo/x.md`",
                    note_id="9102",
                    discussion_id="d-flaky",
                )
            )
        # nothing recorded — a transient failure is retried, never guessed
        run = await _get_run(review_db, run_id)
        assert review_feedback_requests_of(run.evidence or {}) == {}
        assert fake.mr_notes == []

        # the recovery: the SAME delivery re-enters once the surface is back
        fake.failing = False
        await service.run_command(
            _feedback_command(
                mr_iid,
                "/fix rename `forge-demo/x.md`",
                note_id="9102",
                discussion_id="d-flaky",
            )
        )
        requests = review_feedback_requests_of((await _get_run(review_db, run_id)).evidence or {})
        assert list(requests) == ["9102"]
        assert requests["9102"].status == REQUEST_STAGED  # NOT deleted_discussion

    async def test_conflicting_reviewer_instructions_are_refused_typed(
        self, review_db, review_fake
    ):
        service = make_review_service(review_db, review_fake)
        run_id, _ = await _landed_run(review_db, review_fake, service)
        mr_iid = (await _get_run(review_db, run_id)).mr_iid
        review_fake.seed_discussion(mr_iid, "d-1", note_id=9001, body="/fix a `forge-demo/a.md`")
        review_fake.seed_discussion(mr_iid, "d-2", note_id=9002, body="/fix b `forge-demo/b.md`")
        await service.run_command(
            _feedback_command(
                mr_iid, "/fix a `forge-demo/a.md`", note_id="9001", discussion_id="d-1"
            )
        )
        await service.run_command(
            _feedback_command(
                mr_iid, "/fix b `forge-demo/b.md`", note_id="9002", discussion_id="d-2"
            )
        )
        run = await _get_run(review_db, run_id)
        requests = review_feedback_requests_of(run.evidence or {})
        assert requests["9001"].status == REQUEST_STAGED
        assert requests["9002"].status == REQUEST_CONFLICTING
        assert any(
            "conflicts with the correction already staged" in n["body"]
            for n in review_fake.mr_notes
        )

    async def test_a_clarification_never_dispatches_code(self, review_db, review_fake):
        implementer = RecordingImplementer()
        service = make_review_service(review_db, review_fake, implementer=implementer)
        run_id, _ = await _landed_run(review_db, review_fake, service)
        mr_iid = (await _get_run(review_db, run_id)).mr_iid
        review_fake.seed_discussion(mr_iid, "d-q", note_id=9003, body="/ask why no retry?")
        await service.run_command(
            _feedback_command(mr_iid, "/ask why no retry?", note_id="9003", discussion_id="d-q")
        )
        run = await _get_run(review_db, run_id)
        requests = review_feedback_requests_of(run.evidence or {})
        assert requests["9003"].classification == CLARIFICATION_CLASS
        assert requests["9003"].status == REQUEST_CLARIFICATION_OPEN
        # reviewer-only recovery: no staging, no NEW dispatch, no budget
        assert PENDING_PROPOSAL_KEY not in (run.evidence or {})
        assert len(implementer.dispatches) == 1  # only /go's own advance
        assert len(FakeWriter.instances) == 1
        assert any("No code change is dispatched" in n["body"] for n in review_fake.mr_notes)

    async def test_an_out_of_scope_request_becomes_a_material_proposal_not_a_permission(
        self, review_db, review_fake
    ):
        service = make_review_service(review_db, review_fake)
        run_id, _ = await _landed_run(review_db, review_fake, service)
        mr_iid = (await _get_run(review_db, run_id)).mr_iid
        await service.run_command(
            _feedback_command(mr_iid, "/fix rewrite `ops/runbook.md`", note_id="9004")
        )
        run = await _get_run(review_db, run_id)
        requests = review_feedback_requests_of(run.evidence or {})
        assert requests["9004"].classification == MATERIAL_CHANGE_CLASS
        assert requests["9004"].status == REQUEST_MATERIALIZED
        # never a permission expansion: nothing staged, nothing NEW dispatched
        assert PENDING_PROPOSAL_KEY not in (run.evidence or {})
        assert len(FakeWriter.instances) == 1  # only /go's own advance
        assert any("material proposal" in n["body"] for n in review_fake.mr_notes)

    async def test_feedback_after_the_window_records_but_never_reenters(
        self, review_db, review_fake
    ):
        # R40-02 (#338): the round route owns the READY case (a linked
        # round, see test_review_rounds.py); every OTHER terminal status
        # keeps the honest window-closed refusal — no re-entry, no staging.
        service = make_review_service(review_db, review_fake)
        run_id, _ = await _landed_run(review_db, review_fake, service)
        async with review_db() as session:
            row = await session.get(FlowRun, run_id)
            row.status = FlowStatus.FAILED.value  # terminal, not round-eligible
            await session.commit()
        mr_iid = (await _get_run(review_db, run_id)).mr_iid
        await service.run_command(
            _feedback_command(mr_iid, "/fix a `forge-demo/a.md`", note_id="9005")
        )
        run = await _get_run(review_db, run_id)
        requests = review_feedback_requests_of(run.evidence or {})
        assert requests["9005"].status == REQUEST_WINDOW_CLOSED
        assert PENDING_PROPOSAL_KEY not in (run.evidence or {})
        assert run.status == FlowStatus.FAILED.value


class TestCorrectionRedistribution:
    async def test_the_approved_correction_redispatches_through_the_repair_edge(
        self, review_db, review_fake
    ):
        implementer = RecordingImplementer()
        service = make_review_service(review_db, review_fake, implementer=implementer)
        run_id, _ = await _landed_run(review_db, review_fake, service)
        run = await _get_run(review_db, run_id)
        mr_iid = run.mr_iid
        review_fake.seed_discussion(
            mr_iid, "d-fix", note_id=9006, body="/fix rename `forge-demo/x.md` handler"
        )
        await service.run_command(
            _feedback_command(
                mr_iid,
                "/fix rename `forge-demo/x.md` handler",
                note_id="9006",
                discussion_id="d-fix",
            )
        )
        staged = review_feedback_requests_of((await _get_run(review_db, run_id)).evidence or {})[
            "9006"
        ]
        outcome = await _approve_via_router(review_db, review_fake, run_id, staged.decision_id)
        assert outcome["status"] == "applied", outcome

        await service.evaluate_review_corrections()

        run = await _get_run(review_db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value  # the cycle re-ran, checks rerun
        assert run.commit_cycle == 2
        requests = review_feedback_requests_of(run.evidence or {})
        assert requests["9006"].status == REQUEST_DISPATCHED
        # the correction context rode the executor's dispatch input (the
        # /go advance's dispatch precedes it)
        implementer_dispatch = implementer.dispatches[-1]
        assert "rename `forge-demo/x.md` handler" in (
            implementer_dispatch.get("repair_context") or ""
        )
        # the active revision IS the correction (the #321 join)
        active = (run.evidence or {})[ACTIVE_PLAN_KEY]
        assert active["activated_by_decision"] == staged.decision_id
        assert "rename" in str(active[REVISION_CONTENT_KEY]["summary"])
        # the bot never resolves the reviewer's discussion
        assert review_fake.resolve_calls == []

    async def test_a_moved_mr_head_is_the_typed_stale_head_conflict(self, review_db, review_fake):
        implementer = RecordingImplementer()
        service = make_review_service(review_db, review_fake, implementer=implementer)
        run_id, _ = await _landed_run(review_db, review_fake, service)
        run = await _get_run(review_db, run_id)
        mr_iid = run.mr_iid
        review_fake.seed_discussion(mr_iid, "d-fix", note_id=9007, body="/fix a `forge-demo/a.md`")
        await service.run_command(
            _feedback_command(
                mr_iid, "/fix a `forge-demo/a.md`", note_id="9007", discussion_id="d-fix"
            )
        )
        staged = review_feedback_requests_of((await _get_run(review_db, run_id)).evidence or {})[
            "9007"
        ]
        await _approve_via_router(review_db, review_fake, run_id, staged.decision_id, note_id=9101)

        # a HUMAN EDIT lands between request and publication: the branch
        # head moves past the reviewed candidate.
        from forge.runs.stubs import factory_branch

        review_fake.seed_commit(factory_branch(ISSUE_IID, run_id), "human-edit-sha", "human edit")
        await service.evaluate_review_corrections()

        run = await _get_run(review_db, run_id)
        requests = review_feedback_requests_of(run.evidence or {})
        assert requests["9007"].status == REQUEST_STALE_HEAD
        # human edits preserved: nothing NEW dispatched, the candidate stands
        assert run.status == FlowStatus.WAITING_CI.value
        assert len(implementer.dispatches) == 1  # only /go's own advance
        assert len(FakeWriter.instances) == 1
        assert any("stale_head" in n["body"] for n in review_fake.mr_notes)

    async def test_the_unapproved_correction_never_dispatches(self, review_db, review_fake):
        implementer = RecordingImplementer()
        service = make_review_service(review_db, review_fake, implementer=implementer)
        run_id, _ = await _landed_run(review_db, review_fake, service)
        mr_iid = (await _get_run(review_db, run_id)).mr_iid
        review_fake.seed_discussion(mr_iid, "d-fix", note_id=9008, body="/fix a `forge-demo/a.md`")
        await service.run_command(
            _feedback_command(
                mr_iid, "/fix a `forge-demo/a.md`", note_id="9008", discussion_id="d-fix"
            )
        )
        await service.evaluate_review_corrections()  # nobody approved yet
        assert len(implementer.dispatches) == 1  # only /go's own advance
        assert len(FakeWriter.instances) == 1
        assert (await _get_run(review_db, run_id)).status == FlowStatus.WAITING_CI.value


class TestReadinessGateAndSummary:
    async def _green(self, review_db, review_fake, service, run_id: str) -> None:
        from forge.runs.stubs import factory_branch

        branch = factory_branch(ISSUE_IID, run_id)
        pipeline_id = (await review_fake.create_pipeline(PROJECT_ID, branch))["id"]
        review_fake.set_pipeline_status(pipeline_id, "success", "fake-sha-1")

    async def test_a_green_candidate_with_an_open_required_discussion_is_not_ready(
        self, review_db, review_fake
    ):
        service = make_review_service(review_db, review_fake)
        run_id, _ = await _landed_run(review_db, review_fake, service)
        mr_iid = (await _get_run(review_db, run_id)).mr_iid
        review_fake.seed_discussion(
            mr_iid,
            "d-fix",
            note_id=9009,
            body="/fix rename `forge-demo/x.md` handler",
            resolvable=True,
            position={"old_path": "forge-demo/x.md", "new_path": "forge-demo/x.md"},
        )
        await service.run_command(
            _feedback_command(
                mr_iid,
                "/fix rename `forge-demo/x.md` handler",
                note_id="9009",
                discussion_id="d-fix",
            )
        )
        staged = review_feedback_requests_of((await _get_run(review_db, run_id)).evidence or {})[
            "9009"
        ]
        await _approve_via_router(review_db, review_fake, run_id, staged.decision_id, note_id=9102)
        await service.evaluate_review_corrections()
        assert (await _get_run(review_db, run_id)).status == FlowStatus.WAITING_CI.value

        await self._green(review_db, review_fake, service, run_id)
        await service.evaluate_waiting_ci()

        # repair passed its tests, but the required discussion is OPEN —
        # the run is NOT ready and says exactly what blocks it.
        run = await _get_run(review_db, run_id)
        assert run.status == FlowStatus.EVALUATING_CI.value
        held = [n for n in review_fake.mr_notes if "held for human review feedback" in n["body"]]
        assert len(held) == 1
        assert "d-fix" in held[0]["body"]
        assert "Still-open discussions" in held[0]["body"]
        # the held note is journaled once per candidate (A11)
        await service.evaluate_waiting_ci()
        assert (
            len([n for n in review_fake.mr_notes if "held for human review feedback" in n["body"]])
            == 1
        )

        # the REVIEWER resolves the discussion (never forge) — readiness
        # follows on the next pass, and the final summary names the sides.
        review_fake.resolve(mr_iid, "d-fix")
        await service.evaluate_waiting_ci()
        run = await _get_run(review_db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        summary = [n for n in review_fake.notes if "ready for human review" in n["body"]][-1]
        assert "Review feedback" in summary["body"]
        assert "Resolved discussions" in summary["body"]
        assert "d-fix" in summary["body"]
        assert "fake-sha-1" in summary["body"]  # the tested candidate
        # the bot never merged and never resolved anything itself
        assert review_fake.resolve_calls == []
        assert review_fake.calls_of("resolve_discussion") == []

    async def test_a_resolvable_thread_from_a_plain_comment_never_gates(
        self, review_db, review_fake
    ):
        service = make_review_service(review_db, review_fake)
        run_id, _ = await _landed_run(review_db, review_fake, service)
        mr_iid = (await _get_run(review_db, run_id)).mr_iid
        # a non-resolvable thread (a plain comment): GitLab itself says it
        # can never resolve, so it can never gate readiness.
        review_fake.seed_discussion(
            mr_iid, "d-plain", note_id=9010, body="/fix a `forge-demo/a.md`", resolvable=False
        )
        await service.run_command(
            _feedback_command(
                mr_iid, "/fix a `forge-demo/a.md`", note_id="9010", discussion_id="d-plain"
            )
        )
        staged = review_feedback_requests_of((await _get_run(review_db, run_id)).evidence or {})[
            "9010"
        ]
        await _approve_via_router(review_db, review_fake, run_id, staged.decision_id, note_id=9103)
        await service.evaluate_review_corrections()
        await self._green(review_db, review_fake, service, run_id)
        await service.evaluate_waiting_ci()
        run = await _get_run(review_db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value
        summary = [n for n in review_fake.notes if "ready for human review" in n["body"]][-1]
        assert "Still-open discussions" in summary["body"]  # named, never gating


class TestTheBotNeverMergesOrDecides:
    async def test_the_ready_run_stops_at_the_human_and_merges_nothing(
        self, review_db, review_fake
    ):
        service = make_review_service(review_db, review_fake)
        run_id, _ = await _landed_run(review_db, review_fake, service)
        await service.run_command(
            _feedback_command(
                (await _get_run(review_db, run_id)).mr_iid,
                "/ask is the retry idempotent?",
                note_id="9011",
                discussion_id="d-q2",
            )
        )
        from forge.runs.stubs import factory_branch

        branch = factory_branch(ISSUE_IID, run_id)
        pipeline_id = (await review_fake.create_pipeline(PROJECT_ID, branch))["id"]
        review_fake.set_pipeline_status(pipeline_id, "success", "fake-sha-1")
        await service.evaluate_waiting_ci()
        run = await _get_run(review_db, run_id)
        assert run.status == FlowStatus.READY_FOR_HUMAN.value  # terminal FOR THE BOT
        # no merge surface exists on the fake and none was invented
        assert review_fake.resolve_calls == []
        assert not hasattr(review_fake, "accept_merge_request")
        # the requests stay durable for the audit
        requests = review_feedback_requests_of(run.evidence or {})
        assert requests["9011"].classification == CLARIFICATION_CLASS


# ----------------------------------------------------------------------
# R40-01 (#337): the GATEWAY ingress — the parser, the flag, the typed
# refusal and the durable inbox identity the note travels through
# ----------------------------------------------------------------------


def _mr_note_event(
    body: str,
    *,
    note_id: int = 500,
    project_id: int = PROJECT_ID,
    mr_iid: int = 7,
    discussion_id: str = "abc123def456",
    author: str = "alice",
):
    """A GitLab MR Note Hook, typed the way ``parse_webhook`` produces it."""
    from forge.gateway.parser import parse_webhook

    payload = {
        "object_kind": "note",
        "event_type": "note",
        "user": {"id": 11, "name": "Alice", "username": author, "email": ""},
        "project": {
            "id": project_id,
            "name": "forge-demo",
            "path_with_namespace": "acme/forge-demo",
            "web_url": "https://gitlab.test/acme/forge-demo",
        },
        "object_attributes": {
            "id": note_id,
            "note": body,
            "noteable_type": "MergeRequest",
            "noteable_id": 100,
            "author_id": 11,
            "discussion_id": discussion_id,
        },
        "merge_request": {
            "id": 100,
            "iid": mr_iid,
            "title": "Draft: the candidate",
            "source_branch": "forge/factory-1",
            "target_branch": "main",
            "state": "opened",
        },
    }
    return parse_webhook("Note Hook", payload)


def _issue_note_event(body: str, *, author: str = "alice"):
    from forge.gateway.parser import parse_webhook

    payload = {
        "object_kind": "note",
        "event_type": "note",
        "user": {"id": 11, "name": "Alice", "username": author, "email": ""},
        "project": {
            "id": PROJECT_ID,
            "name": "forge-demo",
            "path_with_namespace": "acme/forge-demo",
            "web_url": "https://gitlab.test/acme/forge-demo",
        },
        "object_attributes": {
            "id": 501,
            "note": body,
            "noteable_type": "Issue",
            "noteable_id": 12,
            "author_id": 11,
        },
        "issue": {"id": 12, "iid": ISSUE_IID, "title": ISSUE_TITLE},
    }
    return parse_webhook("Note Hook", payload)


class TestFeedbackGatewayParsing:
    """The parser widening: MR-bound verbs behind the capability flag."""

    def test_the_flag_defaults_to_zero_routing(self, monkeypatch):
        from forge.gateway.router import _match_run_command

        monkeypatch.delenv("FORGE_REVIEW_FEEDBACK_ENABLED", raising=False)
        assert (
            _match_run_command(_mr_note_event("/fix rename `src/app.py`"), make_settings()) is None
        )
        assert _match_run_command(_mr_note_event("/ask why?"), make_settings()) is None

    def test_a_flagged_mr_fix_note_matches_review_feedback(self, monkeypatch):
        from forge.gateway.router import _match_run_command

        monkeypatch.setenv("FORGE_REVIEW_FEEDBACK_ENABLED", "1")
        command = _match_run_command(
            _mr_note_event("/fix rename the helper in `src/app.py` to validate_email"),
            make_settings(),
        )
        assert command is not None
        assert command["command"] == "review_feedback"
        assert command["provider"] == "gitlab"
        assert command["project_id"] == PROJECT_ID
        assert command["mr_iid"] == 7
        assert command["note_id"] == 500
        assert command["discussion_id"] == "abc123def456"
        assert command["author_username"] == "alice"
        assert command["note_text"].startswith("/fix rename")

    def test_a_flagged_mr_ask_note_matches_review_feedback(self, monkeypatch):
        from forge.gateway.router import _match_run_command

        monkeypatch.setenv("FORGE_REVIEW_FEEDBACK_ENABLED", "1")
        command = _match_run_command(
            _mr_note_event("/ask why is the retry idempotent?"), make_settings()
        )
        assert command is not None
        assert command["command"] == "review_feedback"
        assert command["mr_iid"] == 7

    def test_a_mentioned_verb_parses_too(self, monkeypatch):
        from forge.gateway.router import _match_run_command

        monkeypatch.setenv("FORGE_REVIEW_FEEDBACK_ENABLED", "1")
        command = _match_run_command(
            _mr_note_event("@forge /fix tighten the guard in `src/app.py`"), make_settings()
        )
        assert command is not None and command["command"] == "review_feedback"

    def test_a_malformed_verb_is_the_typed_ingress_refusal(self, monkeypatch):
        """``/fix`` with no description: recognized, refused TYPED at the
        ingress — never a run command, never a 4xx (hook auto-disable)."""
        from forge.gateway.router import _match_run_command

        monkeypatch.setenv("FORGE_REVIEW_FEEDBACK_ENABLED", "1")
        for body in ("/fix", "/fix   ", "/ask"):
            command = _match_run_command(_mr_note_event(body), make_settings())
            assert command is not None, body
            assert command["command"] == "review_feedback_refused"
            assert command["refusal_reason"] == "malformed_feedback_command"
            assert command["note_id"] == 500  # the identity is still carried

    def test_an_issue_bound_verb_is_not_a_run_command(self, monkeypatch):
        """Review feedback lives on MR discussions only — zero routing on
        issues, whatever the flag says."""
        from forge.gateway.router import _match_run_command

        monkeypatch.setenv("FORGE_REVIEW_FEEDBACK_ENABLED", "1")
        assert (
            _match_run_command(_issue_note_event("/fix rename `src/app.py`"), make_settings())
            is None
        )
        assert _match_run_command(_issue_note_event("/ask why?"), make_settings()) is None

    def test_a_commit_bound_verb_without_an_mr_is_ignored(self, monkeypatch):
        from forge.gateway.parser import parse_webhook
        from forge.gateway.router import _match_run_command

        monkeypatch.setenv("FORGE_REVIEW_FEEDBACK_ENABLED", "1")
        payload = {
            "object_kind": "note",
            "user": {"id": 11, "name": "A", "username": "alice"},
            "project": {
                "id": PROJECT_ID,
                "name": "d",
                "path_with_namespace": "a/d",
                "web_url": "u",
            },
            "object_attributes": {
                "id": 502,
                "note": "/fix rename `src/app.py`",
                "noteable_type": "Commit",
                "discussion_id": "c1",
            },
        }
        event = parse_webhook("Note Hook", payload)
        assert _match_run_command(event, make_settings()) is None

    def test_a_bot_authored_feedback_note_is_never_a_trigger(self, monkeypatch):
        from forge.gateway.router import _match_run_command

        monkeypatch.setenv("FORGE_REVIEW_FEEDBACK_ENABLED", "1")
        settings = make_settings(FORGE_BOT_USERNAME="forge-bot")
        assert _match_run_command(_mr_note_event("/fix x", author="forge-bot"), settings) is None

    # -- the regression arms: the classic surface is untouched -----------

    def test_mr_security_keeps_its_route_whatever_the_feedback_flag(self, monkeypatch):
        from forge.gateway.router import _match_run_command

        for value in (None, "1"):
            if value is None:
                monkeypatch.delenv("FORGE_REVIEW_FEEDBACK_ENABLED", raising=False)
            else:
                monkeypatch.setenv("FORGE_REVIEW_FEEDBACK_ENABLED", value)
            command = _match_run_command(_mr_note_event("/security"), make_settings())
            assert command is not None
            assert command["command"] == "security_triage"
            assert command["mr_iid"] == 7

    def test_issue_bound_implement_go_retry_keep_their_routes(self, monkeypatch):
        from forge.gateway.router import _match_run_command

        monkeypatch.setenv("FORGE_REVIEW_FEEDBACK_ENABLED", "1")
        assert _match_run_command(_issue_note_event("/implement"), make_settings()) == {
            "project_id": PROJECT_ID,
            "issue_iid": ISSUE_IID,
            "author_username": "alice",
            "author_user_id": 11,
            "note_id": 501,
            "command": "start_run",
        }
        go = _match_run_command(_issue_note_event("/go ab12cd34"), make_settings())
        assert go is not None and go["command"] == "go"
        retry = _match_run_command(_issue_note_event("/retry"), make_settings())
        assert retry is not None and retry["command"] == "retry"


class _SetNXQueue:
    """The Redis TaskQueue's dedup semantics (SET-NX, 5-minute window) with
    the submit swallowed — the production shape where the queue is only a
    wake-up accelerator and the wake-up is LOST (the worker died)."""

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self.submitted: list[dict] = []

    async def is_duplicate(self, fingerprint: str, ttl: int = 300) -> bool:
        if fingerprint in self._seen:
            return True
        self._seen.add(fingerprint)
        return False

    async def submit(self, task) -> None:
        self.submitted.append(dict(getattr(task, "metadata", {}) or {}))


class TestFeedbackIngressDurability:
    """The ASGI ingress: inbox + step in ONE transaction, the two dedup
    layers, and the typed malformed refusal — the ADR-0017 §1 contract the
    production-entry trace then walks end to end."""

    @pytest.fixture()
    async def app(self, tmp_path, monkeypatch):
        from forge.database import reset_engine
        from forge.main import create_app

        monkeypatch.setenv("FORGE_REVIEW_FEEDBACK_ENABLED", "1")
        reset_engine()
        application = create_app(
            settings=make_settings(DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'ingress.db'}")
        )
        async with application.router.lifespan_context(application):
            application.state.task_queue = _SetNXQueue()
            yield application
        reset_engine()

    @pytest.fixture()
    async def client(self, app):
        from httpx import ASGITransport, AsyncClient

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    async def _post(self, client, body: str, *, uuid: str, note_id: int = 500):
        return await client.post(
            "/webhook",
            json={
                "object_kind": "note",
                "event_type": "note",
                "user": {"id": 11, "name": "Alice", "username": "alice"},
                "project": {
                    "id": PROJECT_ID,
                    "name": "forge-demo",
                    "path_with_namespace": "acme/forge-demo",
                    "web_url": "https://gitlab.test/acme/forge-demo",
                },
                "object_attributes": {
                    "id": note_id,
                    "note": body,
                    "noteable_type": "MergeRequest",
                    "noteable_id": 100,
                    "author_id": 11,
                    "discussion_id": "abc123def456",
                },
                "merge_request": {
                    "id": 100,
                    "iid": 7,
                    "title": "Draft: the candidate",
                    "source_branch": "forge/factory-1",
                    "target_branch": "main",
                    "state": "opened",
                },
            },
            headers={
                "X-Gitlab-Token": "whsec",
                "X-Gitlab-Event": "Note Hook",
                "X-Gitlab-Event-UUID": uuid,
            },
        )

    async def _rows(self, app):
        from sqlalchemy import select

        from forge.durable import EventInbox, StepRun

        async with app.state.session_factory() as session:
            inbox = list((await session.execute(select(EventInbox))).scalars().all())
            steps = list((await session.execute(select(StepRun))).scalars().all())
        return inbox, steps

    async def test_a_feedback_note_ingests_inbox_and_step_in_one_transaction(self, app, client):
        response = await self._post(client, "/fix rename the helper in `src/app.py`", uuid="d-111")
        assert response.status_code == 202
        assert response.json() == {
            "status": "accepted",
            "event": "note",
            "queued": True,
            "run_command": True,
        }
        inbox, steps = await self._rows(app)
        assert len(inbox) == 1 and len(steps) == 1
        payload = inbox[0].payload
        assert payload["command"] == "review_feedback"
        assert payload["project_id"] == PROJECT_ID
        assert payload["mr_iid"] == 7
        assert payload["note_id"] == "500"
        assert payload["discussion_id"] == "abc123def456"
        assert payload["delivery_uuid"] == "d-111"  # the transport identity rides along
        assert inbox[0].event_type == "run_command"
        assert steps[0].source_event_id == inbox[0].source_event_id
        assert steps[0].step_name == "review_feedback"
        assert steps[0].status == "scheduled"

    async def test_an_exact_replay_collapses_at_the_transport_layer(self, app, client):
        await self._post(client, "/fix rename `src/app.py`", uuid="d-222")
        replay = await self._post(client, "/fix rename `src/app.py`", uuid="d-222")
        assert replay.status_code == 202
        assert replay.json()["deduplicated"] is True
        inbox, steps = await self._rows(app)
        assert len(inbox) == 1 and len(steps) == 1

    async def test_a_manual_redelivery_collapses_at_the_logical_layer(self, app, client):
        """A different delivery uuid, the SAME note id — one logical request."""
        await self._post(client, "/fix rename `src/app.py`", uuid="d-333")
        redelivery = await self._post(client, "/fix rename `src/app.py`", uuid="d-444")
        assert redelivery.status_code == 202
        assert redelivery.json()["deduplicated"] is True
        inbox, steps = await self._rows(app)
        assert len(inbox) == 1 and len(steps) == 1

    async def test_a_malformed_note_is_refused_typed_without_any_row(self, app, client):
        response = await self._post(client, "/fix", uuid="d-555")
        assert response.status_code == 202  # never a 4xx — hook auto-disable
        assert response.json() == {
            "status": "accepted",
            "event": "note",
            "feedback": "refused",
            "refusal_reason": "malformed_feedback_command",
        }
        inbox, steps = await self._rows(app)
        assert inbox == [] and steps == []

    async def test_the_disabled_flag_leaves_no_trace_at_all(self, app, client, monkeypatch):
        monkeypatch.delenv("FORGE_REVIEW_FEEDBACK_ENABLED", raising=False)
        response = await self._post(client, "/fix rename `src/app.py`", uuid="d-666")
        assert response.status_code == 202
        assert "run_command" not in response.json()
        inbox, steps = await self._rows(app)
        assert inbox == [] and steps == []
