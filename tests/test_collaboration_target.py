"""R41-04 (#359) — the explicit collaboration target.

The disclosed debt under test: child round ids deliberately reused the
parent's first 8 hex chars so ``factory_branch`` derivations collided
onto one branch — work identity, collaboration identity and display
shorthand coupled, short command ids ambiguous within a lineage.

The contract under test:

- a canonical PERSISTED target (connection/repository, source branch,
  target branch, MR identity) linked from root and child work;
- INDEPENDENT run ids for new rounds — branch identity resolves through
  the target in the publisher, the drift checks, the harness dispatch,
  CI collection and the operator commands;
- a legacy adapter deriving the target ONCE for existing runs,
  validated against recorded MR/source information (an unresolved
  topology records as a REFUSAL, never an inferred branch);
- concise human round references (``round N of <root-short>``) that are
  unambiguous within the repository and NOT authorization tokens.

Negative/recovery (the review's bar): shared-prefix legacy resolution;
two repositories sharing an MR number and branch label; a source branch
renamed/deleted between reads; an old in-flight run restarted during a
staged rollout; a target mismatch refused BEFORE writes; display-label
and round-number tampering cannot redirect publication; the migration
records unresolved legacy topology as refusal.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive.revisions import (
    REQUEST_TARGET_REFUSED,
    review_feedback_requests_of,
)
from forge.durable import ActionLog, CollaborationTarget, FlowRun, FlowStatus, MRReservation
from forge.durable.collaboration import (
    ROUND_REFERENCE_RE,
    CollaborationTargetError,
    LegacyTargetEvidence,
    TARGET_MISMATCH_EVENT,
    TARGET_REFUSED_EVENT,
    derive_legacy_target,
    round_reference,
    target_for_run,
)
from forge.durable.identity import factory_branch
from forge.durable.models import Outbox, PublicationIntent, ReviewRound
from forge.models.base import Base
from tests.test_review_feedback import (
    ISSUE_DESC,
    ISSUE_IID,
    ISSUE_TITLE,
    PROJECT_ID,
    RecordingImplementer,
    ReviewFakeGitLab,
)
from tests.test_review_rounds import (
    _green_child,
    _ready_run,
    _round_command,
    _rounds_of,
    make_round_service,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _round_policy_default(monkeypatch):
    """Deterministic round bound: unset in the environment (default 1),
    raised explicitly by the multi-round arms."""
    from forge.gateway.feedback import FORGE_MAX_REVIEW_ROUNDS_ENV

    monkeypatch.delenv(FORGE_MAX_REVIEW_ROUNDS_ENV, raising=False)


# ----------------------------------------------------------------------
# The harness (the review fixtures, reused)
# ----------------------------------------------------------------------


@pytest.fixture()
async def target_db():
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
def target_fake():
    fake = ReviewFakeGitLab()
    fake.seed_issue(ISSUE_IID, ISSUE_TITLE, ISSUE_DESC)
    fake.seed_commit("main", "base-sha-1", "initial")
    fake.seed_file(".forge.yml", "implement:\n  paths:\n    - forge-demo/**\n")
    return fake


async def _get_run(db, run_id: str) -> FlowRun:
    async with db() as session:
        return await session.get(FlowRun, run_id)


async def _targets_of(db) -> list[CollaborationTarget]:
    async with db() as session:
        return list((await session.execute(select(CollaborationTarget))).scalars().all())


async def _outbox_of(db, event_type: str) -> list[dict]:
    async with db() as session:
        rows = (
            (await session.execute(select(Outbox).where(Outbox.event_type == event_type)))
            .scalars()
            .all()
        )
        return [dict(row.payload or {}) for row in rows]


async def _branch_of(db, run: FlowRun) -> str:
    """The run's recorded collaboration branch (the persisted target)."""
    async with db() as session:
        if run.target_id:
            target = await session.get(CollaborationTarget, run.target_id)
            if target is not None and target.source_branch:
                return str(target.source_branch)
    raise AssertionError("run has no materialized target — fixture broken")


# ----------------------------------------------------------------------
# The identity contract: independent ids, ONE recorded target
# ----------------------------------------------------------------------


class TestIndependentIdentityOneTarget:
    async def test_two_rounds_have_independent_ids_but_one_recorded_mr_and_branch(
        self, target_db, target_fake, monkeypatch
    ):
        """The review's primary acceptance: two rounds, independent run
        ids, ONE explicitly recorded MR/source branch — the target row
        every delivery of the lineage links."""
        from forge.gateway.feedback import FORGE_MAX_REVIEW_ROUNDS_ENV

        monkeypatch.setenv(FORGE_MAX_REVIEW_ROUNDS_ENV, "2")
        service = make_round_service(target_db, target_fake)
        run_id, candidate, mr_iid = await _ready_run(target_db, target_fake, service)

        target_fake.seed_discussion(
            mr_iid, "d-r2", note_id=7101, body="/fix cover `forge-demo/a.md` empty input"
        )
        await service.run_command(
            _round_command(
                mr_iid, "7101", "/fix cover `forge-demo/a.md` empty input", discussion="d-r2"
            )
        )
        rounds = await _rounds_of(target_db, run_id)
        assert len(rounds) == 1 and rounds[0].round_number == 2
        child_two = await _get_run(target_db, rounds[0].child_run_id)
        await _green_child(target_db, target_fake, service, rounds[0])
        target_fake.resolve(mr_iid, "d-r2")
        await service.evaluate_waiting_ci()
        child_two = await _get_run(target_db, rounds[0].child_run_id)
        assert child_two.status == FlowStatus.READY_FOR_HUMAN.value
        await service.evaluate_review_rounds()  # closes round 2

        target_fake.seed_discussion(
            mr_iid, "d-r3", note_id=7102, body="/fix tighten `forge-demo/a.md` wording"
        )
        await service.run_command(
            _round_command(
                mr_iid, "7102", "/fix tighten `forge-demo/a.md` wording", discussion="d-r3"
            )
        )
        rounds = await _rounds_of(target_db, run_id)
        assert [row.round_number for row in rounds] == [2, 3]
        child_three = await _get_run(target_db, rounds[1].child_run_id)

        # INDEPENDENT ids: no shared 8-hex prefix anywhere in the lineage.
        prefixes = {run_id[:8], child_two.id[:8], child_three.id[:8]}
        assert len(prefixes) == 3
        assert len({run_id, child_two.id, child_three.id}) == 3

        # ONE recorded target: all three runs link the SAME row, whose
        # single source branch + MR identity is the lineage's surface.
        parent = await _get_run(target_db, run_id)
        assert parent.target_id == child_two.target_id == child_three.target_id
        targets = await _targets_of(target_db)
        active = [row for row in targets if row.status == "active"]
        assert len(active) == 1
        target = active[0]
        assert target.root_run_id == run_id
        assert target.mr_iid == mr_iid
        assert target.provenance == "live"
        # the MR identity never forked: exactly ONE merge request exists.
        assert len(target_fake.merge_requests) == 1

    async def test_the_rounds_candidate_lands_on_the_target_branch(self, target_db, target_fake):
        """Branch identity resolves through the target in the publisher:
        the round's commit lands on the RECORDED branch, never on a
        re-derivation from the independent child id."""
        implementer = RecordingImplementer()
        service = make_round_service(target_db, target_fake, implementer=implementer)
        run_id, candidate, mr_iid = await _ready_run(target_db, target_fake, service)
        parent = await _get_run(target_db, run_id)
        target_branch = await _branch_of(target_db, parent)
        assert target_branch == factory_branch(ISSUE_IID, run_id)  # minted at admission

        target_fake.seed_discussion(
            mr_iid, "d-r2", note_id=7201, body="/fix add tests for `forge-demo/a.md`"
        )
        await service.run_command(
            _round_command(
                mr_iid, "7201", "/fix add tests for `forge-demo/a.md`", discussion="d-r2"
            )
        )
        rounds = await _rounds_of(target_db, run_id)
        child = await _get_run(target_db, rounds[0].child_run_id)
        # the proposer's id-derived default was REBOUND before persisting
        child_candidate = str(list(child.candidate_shas or [])[-1])
        assert child_candidate
        assert target_fake.branches[target_branch][0]["sha"] == child_candidate
        # ...and nothing landed on the child-id derivation
        wrong_branch = factory_branch(ISSUE_IID, child.id)
        assert wrong_branch != target_branch
        assert wrong_branch not in target_fake.branches
        # the recorded MR still targets the one branch
        mr = target_fake.merge_requests[mr_iid]
        assert mr["source_branch"] == target_branch


# ----------------------------------------------------------------------
# Command resolution: legacy shared prefixes vs modern independent ids
# ----------------------------------------------------------------------


async def _seed_legacy_lineage(
    db,
    fake,
    *,
    project_id: int = PROJECT_ID,
    issue_iid: int = ISSUE_IID,
    root_status: str = FlowStatus.READY_FOR_HUMAN.value,
    child_status: str = FlowStatus.READY_FOR_HUMAN.value,
) -> tuple[str, str, int, str]:
    """A PRE-#359 lineage, hand-seeded: the child id SHARES the root's
    8-hex prefix (the disclosed debt's shape), the round row and the
    child's confirmed reservation recorded on the shared branch."""
    root_id = uuid4().hex
    child_id = f"{root_id[:8]}{uuid4().hex[8:]}"
    mr_iid = next(iter(fake.merge_requests)) if fake.merge_requests else 31
    branch = factory_branch(issue_iid, root_id)
    async with db() as session:
        session.add(
            FlowRun(
                id=root_id,
                project_id=project_id,
                issue_iid=issue_iid,
                mr_iid=mr_iid,
                status=root_status,
            )
        )
        session.add(
            FlowRun(
                id=child_id,
                project_id=project_id,
                issue_iid=issue_iid,
                mr_iid=mr_iid,
                status=child_status,
            )
        )
        session.add(
            MRReservation(flow_run_id=child_id, branch=branch, status="confirmed", mr_iid=mr_iid)
        )
        session.add(
            ReviewRound(
                id=uuid4().hex,
                parent_run_id=root_id,
                child_run_id=child_id,
                root_run_id=root_id,
                round_number=2,
                note_id="legacy-1",
                mr_iid=mr_iid,
                base_head_sha="legacy-base",
                decision_id="legacy-decision",
                status="completed",
            )
        )
        await session.commit()
    return root_id, child_id, mr_iid, branch


class TestCommandResolution:
    async def test_status_with_a_shared_legacy_prefix_adopts_nothing(self, target_db, target_fake):
        """Legacy: the 8-char prefix matches root AND child — the raw
        prefix resolves NOTHING (deterministic refusal, never a guess)."""
        service = make_round_service(target_db, target_fake)
        root_id, child_id, mr_iid, branch = await _seed_legacy_lineage(target_db, target_fake)
        notes_before = len(target_fake.notes)
        await service.handle_status_note(
            PROJECT_ID, f"@forge /status {root_id[:8]}", "alice", ISSUE_IID
        )
        posted = [n for n in target_fake.notes[notes_before:]]
        assert posted, "the ambiguous prefix is answered, not ignored"
        assert "No forge run found" in posted[0]["body"]

    async def test_status_round_reference_resolves_the_lineage_child(self, target_db, target_fake):
        """The round reference resolves UNAMBIGUOUSLY on the shared-prefix
        legacy lineage (every prefix match agrees on the lineage root),
        names the ROUND's child — and round 1 names the root."""
        service = make_round_service(target_db, target_fake)
        root_id, child_id, mr_iid, branch = await _seed_legacy_lineage(target_db, target_fake)
        notes_before = len(target_fake.notes)
        await service.handle_status_note(
            PROJECT_ID, f"@forge /status round 2 of {root_id[:8]}", "alice", ISSUE_IID
        )
        body = target_fake.notes[notes_before]["body"]
        assert f"`{child_id[:8]}`" in body and child_id[:8] != root_id[:8] or child_id in body

        notes_before = len(target_fake.notes)
        await service.handle_status_note(
            PROJECT_ID, f"@forge /status round 1 of {root_id[:8]}", "alice", ISSUE_IID
        )
        body = target_fake.notes[notes_before]["body"]
        assert root_id[:8] in body

    async def test_cancel_with_a_shared_legacy_prefix_touches_nothing(self, target_db, target_fake):
        service = make_round_service(target_db, target_fake)
        root_id, child_id, mr_iid, branch = await _seed_legacy_lineage(target_db, target_fake)
        await service.handle_cancel_note(
            PROJECT_ID, f"@forge /cancel {root_id[:8]}", "alice", ISSUE_IID
        )
        root = await _get_run(target_db, root_id)
        child = await _get_run(target_db, child_id)
        assert root.cancel_requested is False and child.cancel_requested is False
        assert root.status == FlowStatus.READY_FOR_HUMAN.value

    async def test_full_ids_resolve_unambiguously_on_the_legacy_lineage(
        self, target_db, target_fake
    ):
        """The full 32-hex id is never ambiguous — even where the 8-char
        forms collide, the FULL ids pin exactly one run each."""
        service = make_round_service(target_db, target_fake)
        root_id, child_id, mr_iid, branch = await _seed_legacy_lineage(target_db, target_fake)
        # child is non-terminal in this arm: cancel it by full id
        async with target_db() as session:
            row = await session.get(FlowRun, child_id)
            row.status = FlowStatus.WAITING_CI.value
            await session.commit()
        await service.handle_cancel_note(
            PROJECT_ID, f"@forge /cancel {child_id}", "alice", ISSUE_IID
        )
        child = await _get_run(target_db, child_id)
        root = await _get_run(target_db, root_id)
        assert child.status == FlowStatus.CANCELLED.value
        assert root.status == FlowStatus.READY_FOR_HUMAN.value

    async def test_modern_independent_prefixes_resolve_directly(self, target_db, target_fake):
        """Modern: the child's 8-char prefix is UNIQUE — /status and
        /cancel resolve it directly, no round reference needed."""
        service = make_round_service(target_db, target_fake)
        run_id, candidate, mr_iid = await _ready_run(target_db, target_fake, service)
        target_fake.seed_discussion(
            mr_iid, "d-r2", note_id=7301, body="/fix tweak `forge-demo/a.md`"
        )
        await service.run_command(
            _round_command(mr_iid, "7301", "/fix tweak `forge-demo/a.md`", discussion="d-r2")
        )
        rounds = await _rounds_of(target_db, run_id)
        child_id = rounds[0].child_run_id
        assert child_id[:8] != run_id[:8]

        notes_before = len(target_fake.notes)
        await service.handle_status_note(
            PROJECT_ID, f"@forge /status {child_id[:8]}", "alice", ISSUE_IID
        )
        assert child_id[:8] in target_fake.notes[notes_before]["body"]

        child = await _get_run(target_db, child_id)
        assert child.status == FlowStatus.WAITING_CI.value
        await service.handle_cancel_note(
            PROJECT_ID, f"@forge /cancel {child_id[:8]}", "alice", ISSUE_IID
        )
        child = await _get_run(target_db, child_id)
        assert child.status == FlowStatus.CANCELLED.value

    async def test_the_round_reference_is_not_an_authorization_token(self, target_db, target_fake):
        """`/cancel round 2 of <root>` carries NO targeting power of its
        own: with no ACTIVE run on the issue (both deliveries terminal)
        the reference selects nothing — the destructive command requires
        the full run id exactly as forge prints it."""
        service = make_round_service(target_db, target_fake)
        run_id, candidate, mr_iid = await _ready_run(target_db, target_fake, service)
        target_fake.seed_discussion(
            mr_iid, "d-r2", note_id=7302, body="/fix tweak `forge-demo/a.md`"
        )
        await service.run_command(
            _round_command(mr_iid, "7302", "/fix tweak `forge-demo/a.md`", discussion="d-r2")
        )
        child_id = (await _rounds_of(target_db, run_id))[0].child_run_id
        # the child reaches its terminal delivery — nothing active remains
        await _green_child(
            target_db, target_fake, service, (await _rounds_of(target_db, run_id))[0]
        )
        target_fake.resolve(mr_iid, "d-r2")
        await service.evaluate_waiting_ci()
        child = await _get_run(target_db, child_id)
        assert child.status == FlowStatus.READY_FOR_HUMAN.value

        await service.handle_cancel_note(
            PROJECT_ID, f"@forge /cancel round 2 of {run_id[:8]}", "alice", ISSUE_IID
        )
        child = await _get_run(target_db, child_id)
        parent = await _get_run(target_db, run_id)
        assert child.status == FlowStatus.READY_FOR_HUMAN.value
        assert parent.status == FlowStatus.READY_FOR_HUMAN.value
        assert child.cancel_requested is False and parent.cancel_requested is False

    async def test_the_admitted_reply_prints_the_reference_and_full_id_commands(
        self, target_db, target_fake
    ):
        """Commands printed by forge resolve to the intended round: the
        reply carries the round reference for reads and the FULL child id
        for the destructive command."""
        service = make_round_service(target_db, target_fake)
        run_id, candidate, mr_iid = await _ready_run(target_db, target_fake, service)
        target_fake.seed_discussion(
            mr_iid, "d-r2", note_id=7303, body="/fix tweak `forge-demo/a.md`"
        )
        await service.run_command(
            _round_command(mr_iid, "7303", "/fix tweak `forge-demo/a.md`", discussion="d-r2")
        )
        child_id = (await _rounds_of(target_db, run_id))[0].child_run_id
        reply = next(
            n["body"] for n in target_fake.mr_notes if "Review round 2 opened" in n["body"]
        )
        assert round_reference(2, run_id) in reply
        assert f"/status round 2 of {run_id[:8]}" in reply
        assert f"/cancel {child_id}" in reply


# ----------------------------------------------------------------------
# Repository isolation: same MR number + branch label, two repositories
# ----------------------------------------------------------------------


class TestRepositoryIsolation:
    async def test_two_repositories_with_the_same_mr_and_branch_never_cross(
        self, target_db, target_fake
    ):
        """Two projects, runs whose ids SHARE the 8-hex prefix (same
        derived branch label) and the SAME MR number: the targets key
        apart on (provider, repository) — never crossed."""
        shared_prefix = "deadbeef"
        run_a = f"{shared_prefix}{'a' * 24}"
        run_b = f"{shared_prefix}{'b' * 24}"
        label = f"factory/{ISSUE_IID}/{shared_prefix}"
        async with target_db() as session:
            session.add(FlowRun(id=run_a, project_id=11, issue_iid=ISSUE_IID, mr_iid=7))
            session.add(FlowRun(id=run_b, project_id=22, issue_iid=ISSUE_IID, mr_iid=7))
            session.add(
                MRReservation(flow_run_id=run_a, branch=label, status="confirmed", mr_iid=7)
            )
            session.add(
                MRReservation(flow_run_id=run_b, branch=label, status="confirmed", mr_iid=7)
            )
            await session.commit()

        run_a_row = await _get_run(target_db, run_a)
        async with target_db() as session:
            target_a = await target_for_run(session, run_a_row)
        run_b_row = await _get_run(target_db, run_b)
        async with target_db() as session:
            target_b = await target_for_run(session, run_b_row)

        assert target_a.id != target_b.id
        assert target_a.project_ref == "11" and target_b.project_ref == "22"
        assert target_a.source_branch == label == target_b.source_branch
        assert target_a.mr_iid == 7 == target_b.mr_iid
        # and the DB invariant holds: two active rows, same branch label,
        # distinct repositories.
        actives = [row for row in await _targets_of(target_db) if row.status == "active"]
        assert len(actives) == 2


# ----------------------------------------------------------------------
# Typed refusals
# ----------------------------------------------------------------------


class _BranchDeletingReviewer:
    """A reviewer whose in-flight review RENAMES AWAY the source branch —
    the between-reads deletion arm: the pre-review drift check passed,
    the branch is gone by the post-review freshness read."""

    def __init__(self, fake, branch: str) -> None:
        self._fake = fake
        self._branch = branch
        self.calls = 0

    async def review(self, **kwargs):
        self.calls += 1
        self._fake.branches.pop(self._branch, None)
        from forge.runs.stubs import StubReviewResult

        return StubReviewResult()


class TestTypedRefusals:
    async def test_a_source_branch_deleted_between_reads_is_a_typed_refusal(
        self, target_db, target_fake
    ):
        """The post-review freshness fence reads the branch head LIVE —
        a branch renamed/deleted between reads parks the run typed
        (never a silent pass, never a guess)."""
        service = make_round_service(target_db, target_fake)
        run_id = await service.start_run(PROJECT_ID, ISSUE_IID, ISSUE_TITLE, ISSUE_DESC, "alice")
        await service.handle_command_note(
            PROJECT_ID, f"@forge /go {run_id}", "alice", ISSUE_IID, author_user_id=11
        )
        run = await _get_run(target_db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value
        parent = await _get_run(target_db, run_id)
        target_branch = await _branch_of(target_db, parent)
        sha = str(list(run.candidate_shas or [])[-1])

        service._reviewer = _BranchDeletingReviewer(target_fake, target_branch)
        pipeline_id = (await target_fake.create_pipeline(PROJECT_ID, target_branch))["id"]
        target_fake.set_pipeline_status(pipeline_id, "success", sha)

        await service.evaluate_waiting_ci()

        run = await _get_run(target_db, run_id)
        assert run.status == FlowStatus.BLOCKED.value
        assert "candidate_drift_after_review" in (run.status_reason or "")
        assert "branch head read failed" in (run.status_reason or "")

    async def test_a_target_mismatch_between_run_round_and_provider_is_refused_before_writes(
        self, target_db, target_fake
    ):
        """The recorded handles disagree (the TARGET row's MR was moved
        off the run's recorded MR — the corruption the pre-write guard
        exists for): the mismatch is observed, the request recorded as
        refused, and ZERO writes (no child run, no round row)."""
        service = make_round_service(target_db, target_fake)
        run_id, candidate, mr_iid = await _ready_run(target_db, target_fake, service)
        parent = await _get_run(target_db, run_id)
        assert parent.mr_iid == mr_iid

        # the drifted handle: the target row says another MR
        async with target_db() as session:
            target = await session.get(CollaborationTarget, parent.target_id)
            assert target is not None
            target.mr_iid = mr_iid + 99
            await session.commit()

        target_fake.seed_discussion(
            mr_iid, "d-r2", note_id=7401, body="/fix tweak `forge-demo/a.md`"
        )
        await service.run_command(
            _round_command(mr_iid, "7401", "/fix tweak `forge-demo/a.md`", discussion="d-r2")
        )

        rounds = await _rounds_of(target_db, run_id)
        assert rounds == []  # nothing admitted
        mismatches = await _outbox_of(target_db, TARGET_MISMATCH_EVENT)
        assert mismatches and mismatches[0]["run_id"] == run_id
        # the request on the parent is recorded as TARGET-refused
        parent = await _get_run(target_db, run_id)
        requests = review_feedback_requests_of(parent.evidence or {})
        assert requests["7401"].status == REQUEST_TARGET_REFUSED
        # the operator reply names the disagreement
        assert any("disagree" in n["body"] for n in target_fake.mr_notes)
        # no new runs exist at all
        async with target_db() as session:
            runs = list((await session.execute(select(FlowRun))).scalars().all())
        assert len(runs) == 1

    async def test_a_provider_mr_source_branch_disagreeing_with_the_target_refuses(
        self, target_db, target_fake
    ):
        """The provider's live MR document says a different source branch
        than the recorded target — a handle mismatch, refused before
        writes (the MR the round would publish to is pinned by the
        target, never re-picked)."""
        service = make_round_service(target_db, target_fake)
        run_id, candidate, mr_iid = await _ready_run(target_db, target_fake, service)
        target_fake.merge_requests[mr_iid]["source_branch"] = "factory/7/somewhere-else"
        target_fake.seed_discussion(
            mr_iid, "d-r2", note_id=7402, body="/fix tweak `forge-demo/a.md`"
        )
        await service.run_command(
            _round_command(mr_iid, "7402", "/fix tweak `forge-demo/a.md`", discussion="d-r2")
        )

        assert (await _rounds_of(target_db, run_id)) == []
        mismatches = await _outbox_of(target_db, TARGET_MISMATCH_EVENT)
        assert mismatches and "source branch" in mismatches[0]["mismatches"][0]

    async def test_display_label_or_round_number_tampering_cannot_redirect_publication(
        self, target_db, target_fake
    ):
        """Publication keys on the target's recorded branch + run ids —
        renumbering the round (or rewriting the display reference) moves
        nothing; the reference degrades honestly for reads only."""
        implementer = RecordingImplementer()
        service = make_round_service(target_db, target_fake, implementer=implementer)
        run_id, candidate, mr_iid = await _ready_run(target_db, target_fake, service)
        parent = await _get_run(target_db, run_id)
        target_branch = await _branch_of(target_db, parent)

        target_fake.seed_discussion(
            mr_iid, "d-r2", note_id=7501, body="/fix tweak `forge-demo/a.md`"
        )
        await service.run_command(
            _round_command(mr_iid, "7501", "/fix tweak `forge-demo/a.md`", discussion="d-r2")
        )
        rounds = await _rounds_of(target_db, run_id)
        child_id = rounds[0].child_run_id

        # tamper: renumber the round row AFTER admission, and rewind it to
        # `admitted` so the reconciler pass re-drives the dispatch
        async with target_db() as session:
            row = await session.get(ReviewRound, rounds[0].id)
            row.round_number = 99
            row.status = "admitted"
            await session.commit()

        # the re-drive still publishes to the recorded target branch
        await service.evaluate_review_rounds()
        child = await _get_run(target_db, child_id)
        child_candidate = str(list(child.candidate_shas or [])[-1])
        assert child_candidate
        assert target_fake.branches[target_branch][0]["sha"] == child_candidate
        # exactly one MR still — nothing forked
        assert len(target_fake.merge_requests) == 1

        # the read-side reference degrades (round 2 no longer recorded),
        # but the child stays reachable by its full id
        notes_before = len(target_fake.notes)
        await service.handle_status_note(
            PROJECT_ID, f"@forge /status round 2 of {run_id[:8]}", "alice", ISSUE_IID
        )
        assert "matched no round" in target_fake.notes[notes_before]["body"]
        notes_before = len(target_fake.notes)
        await service.handle_status_note(
            PROJECT_ID, f"@forge /status {child_id}", "alice", ISSUE_IID
        )
        assert child_id[:8] in target_fake.notes[notes_before]["body"]


# ----------------------------------------------------------------------
# The legacy adapter: derive ONCE, validate, refuse unresolved
# ----------------------------------------------------------------------


class TestLegacyAdapterPure:
    def test_agreeing_evidence_materializes(self) -> None:
        decision = derive_legacy_target(
            provider="gitlab",
            project_id=5,
            issue_iid=7,
            run_id="ab" * 16,
            run_mr_iid=31,
            evidence=LegacyTargetEvidence(branches=("factory/7/abababab",), mr_iids=(31,)),
            target_branch="main",
        )
        assert not decision.refused
        assert decision.source_branch == "factory/7/abababab"
        assert decision.mr_iid == 31

    def test_no_evidence_keeps_the_own_derivation(self) -> None:
        """A run that never recorded a branch (delivery 1 pre-commit, or
        an MR row alone — delivery 1 records no reservations): its own
        deterministic derivation IS the topology its admission minted."""
        decision = derive_legacy_target(
            provider="gitlab",
            project_id=5,
            issue_iid=7,
            run_id="cd" * 16,
            run_mr_iid=None,
            evidence=LegacyTargetEvidence(),
            target_branch="main",
        )
        assert decision.source_branch == "factory/7/cdcdcdcd"

    def test_a_disagreeing_recorded_branch_is_refused(self) -> None:
        decision = derive_legacy_target(
            provider="gitlab",
            project_id=5,
            issue_iid=7,
            run_id="ef" * 16,
            run_mr_iid=None,
            evidence=LegacyTargetEvidence(branches=("factory/7/zzzzzzzz",)),
            target_branch="main",
        )
        assert decision.refused
        assert decision.source_branch is None  # never an inferred branch
        assert "recorded_branch_mismatch" in decision.refusal_reason

    def test_disagreeing_recorded_mrs_are_refused(self) -> None:
        decision = derive_legacy_target(
            provider="gitlab",
            project_id=5,
            issue_iid=7,
            run_id="12" * 16,
            run_mr_iid=31,
            evidence=LegacyTargetEvidence(branches=("factory/7/12121212",), mr_iids=(44,)),
            target_branch="main",
        )
        assert decision.refused
        assert "recorded_mr_mismatch" in decision.refusal_reason

    def test_non_gitlab_providers_have_no_legacy_adapter_yet(self) -> None:
        decision = derive_legacy_target(
            provider="github",
            project_id=5,
            issue_iid=7,
            run_id="34" * 16,
            run_mr_iid=None,
            evidence=LegacyTargetEvidence(),
            target_branch="main",
        )
        assert decision.refused and "no_legacy_adapter_for_provider" in decision.refusal_reason


class TestLegacyAdapterRuntime:
    async def test_an_old_in_flight_run_restarted_during_staged_rollout_keeps_its_topology(
        self, target_db, target_fake
    ):
        """A pre-#359 run, mid-flight (waiting_ci, its intent recorded on
        its historical branch), restarted on the new code: the adapter
        materializes the target ONCE from the recorded evidence — same
        branch, same MR, the run continues in place."""
        run_id = uuid4().hex
        branch = factory_branch(ISSUE_IID, run_id)
        mr_iid = 31
        async with target_db() as session:
            session.add(
                FlowRun(
                    id=run_id,
                    project_id=PROJECT_ID,
                    issue_iid=ISSUE_IID,
                    mr_iid=mr_iid,
                    status=FlowStatus.WAITING_CI.value,
                    status_reason="pipeline for cand-1",
                    candidate_shas=["cand-1"],
                )
            )
            session.add(
                PublicationIntent(
                    id=uuid4().hex,
                    run_id=run_id,
                    provider="gitlab",
                    repo=str(PROJECT_ID),
                    target_ref=branch,
                    idempotency_scope="cycle-1",
                    operation_key=f"legacy-op-{run_id[:8]}",
                    content_digest="d" * 64,
                    status="committed",
                )
            )
            await session.commit()
        target_fake.seed_commit(branch, "cand-1", "forge: implement 7")
        pipeline_id = (await target_fake.create_pipeline(PROJECT_ID, branch))["id"]
        target_fake.set_pipeline_status(pipeline_id, "success", "cand-1")

        # the NEW code (a fresh service — the staged rollout's shape)
        # resolves the branch through the ONE seam every leg uses
        service = make_round_service(target_db, target_fake)
        resolved = await service._collaboration_branch(run_id)
        assert resolved == branch  # the SAME recorded branch, nothing re-minted
        run = await _get_run(target_db, run_id)
        assert run.status == FlowStatus.WAITING_CI.value  # untouched, not blocked
        assert run.target_id  # adopted exactly once
        targets = [row for row in await _targets_of(target_db) if row.status == "active"]
        assert len(targets) == 1
        target = targets[0]
        assert target.provenance == "legacy"
        assert target.source_branch == branch  # the SAME branch
        assert target.mr_iid == mr_iid  # the SAME MR
        assert target.root_run_id == run_id

    async def test_an_unresolved_legacy_topology_records_a_refusal_never_a_branch(
        self, target_db, target_fake
    ):
        """Recorded evidence CONTRADICTS the derivation: a refusal row is
        recorded (typed reason, linked run, observed outbox event) — and
        the retry of that run refuses typed instead of dispatching."""
        run_id = uuid4().hex
        async with target_db() as session:
            session.add(
                FlowRun(
                    id=run_id,
                    project_id=PROJECT_ID,
                    issue_iid=ISSUE_IID,
                    mr_iid=31,
                    status=FlowStatus.FAILED.value,
                    status_reason="planning_failed: boom",
                )
            )
            # the journal recorded commits on a DIFFERENT branch than the
            # run's own derivation — hand-migrated topology corruption
            session.add(
                ActionLog(
                    flow_run_id=run_id,
                    action_kind="commit",
                    correlation_id="factory/7/someone-else",
                    status="succeeded",
                    remote_result={"commit_sha": "c" * 40},
                )
            )
            await session.commit()

        service = make_round_service(target_db, target_fake)
        notes_before = len(target_fake.notes)
        await service.handle_retry_note(PROJECT_ID, f"@forge /retry {run_id}", "alice", ISSUE_IID)
        assert any("refused" in n["body"] for n in target_fake.notes[notes_before:])

        run = await _get_run(target_db, run_id)
        assert run.target_id  # linked to the refusal row
        async with target_db() as session:
            refusal = await session.get(CollaborationTarget, run.target_id)
        assert refusal is not None and refusal.status == "refused"
        assert refusal.source_branch is None  # NEVER an inferred branch
        assert "recorded_branch_mismatch" in (refusal.refusal_reason or "")
        assert await _outbox_of(target_db, TARGET_REFUSED_EVENT)

        # the refusal is durable: a second resolution does not re-derive
        with pytest.raises(CollaborationTargetError, match="refused"):
            async with target_db() as session:
                row = await session.get(FlowRun, run_id)
                await target_for_run(session, row)


# ----------------------------------------------------------------------
# Migration 032: the backfill over a pre-032 database shape
# ----------------------------------------------------------------------


def _load_migration_032():
    path = REPO_ROOT / "alembic" / "versions" / "032_collaboration_targets.py"
    spec = importlib.util.spec_from_file_location("migration_032_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_PRE_032_SCHEMA = """
CREATE TABLE flow_runs (
    id VARCHAR(32) PRIMARY KEY,
    project_id INTEGER NOT NULL,
    issue_iid INTEGER,
    provider VARCHAR(20) DEFAULT 'gitlab' NOT NULL,
    mr_iid INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
);
CREATE TABLE mr_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_run_id VARCHAR(32) NOT NULL,
    branch VARCHAR(200) NOT NULL,
    status VARCHAR(20) DEFAULT 'open' NOT NULL,
    mr_iid INTEGER
);
CREATE TABLE publication_intents (
    id VARCHAR(32) PRIMARY KEY,
    run_id VARCHAR(32) NOT NULL,
    operation VARCHAR(20) DEFAULT 'commit' NOT NULL,
    target_ref VARCHAR(255) NOT NULL
);
CREATE TABLE action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_run_id VARCHAR(32),
    action_kind VARCHAR(50) NOT NULL,
    status VARCHAR(20) DEFAULT 'requested' NOT NULL,
    correlation_id VARCHAR(100)
);
"""


class TestMigrationBackfill:
    @pytest.fixture()
    def migrated_db(self):
        """A pre-032 database shape, seeded with one valid legacy lineage
        and one unresolved topology, run through 032's upgrade."""

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        migration = _load_migration_032()
        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            for statement in _PRE_032_SCHEMA.split(";"):
                if statement.strip():
                    conn.exec_driver_sql(statement)
            root = "aa" * 16
            child = root[:8] + "bb" * 12  # the prefix-sharing legacy child
            branch = f"factory/7/{root[:8]}"
            conn.execute(
                text(
                    "INSERT INTO flow_runs (id, project_id, issue_iid, mr_iid)"
                    " VALUES (:id, 5, 7, 31)"
                ),
                [{"id": root}, {"id": child}],
            )
            conn.execute(
                text(
                    "INSERT INTO mr_reservations (flow_run_id, branch, status, mr_iid)"
                    " VALUES (:run, :branch, 'confirmed', 31)"
                ),
                [{"run": child, "branch": branch}],
            )
            conn.execute(
                text(
                    "INSERT INTO publication_intents (id, run_id, target_ref)"
                    " VALUES ('i1', :run, :branch)"
                ),
                [{"run": root, "branch": branch}],
            )
            corrupt = "cc" * 16
            conn.execute(
                text(
                    "INSERT INTO flow_runs (id, project_id, issue_iid, mr_iid)"
                    " VALUES (:id, 5, 7, 44)"
                ),
                [{"id": corrupt}],
            )
            conn.execute(
                text(
                    "INSERT INTO mr_reservations (flow_run_id, branch, status, mr_iid)"
                    " VALUES (:run, 'factory/7/hand-mangled', 'confirmed', 44)"
                ),
                [{"run": corrupt}],
            )
            conn.commit()

            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()
            yield engine, root, child, corrupt
        engine.dispose()

    def test_the_valid_lineage_collapses_onto_one_active_target(self, migrated_db):
        engine, root, child, corrupt = migrated_db
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, source_branch, mr_iid, root_run_id, status, provenance"
                    " FROM collaboration_targets WHERE status = 'active'"
                )
            ).fetchall()
            assert len(rows) == 1
            target_id, source, mr, root_id, status, provenance = rows[0]
            assert source == f"factory/7/{root[:8]}"
            assert mr == 31 and root_id == root
            assert provenance == "legacy"
            # BOTH runs link the one row (delivery 1 is the anchor)
            linked = conn.execute(
                text("SELECT id, target_id FROM flow_runs WHERE id IN (:a, :b)"),
                {"a": root, "b": child},
            ).fetchall()
            assert {row[1] for row in linked} == {target_id}
            # existing branch and MR untouched: the reservation still says
            # exactly what it said before the migration.
            reservation = conn.execute(
                text("SELECT branch, mr_iid FROM mr_reservations WHERE flow_run_id = :c"),
                {"c": child},
            ).fetchone()
            assert reservation == (f"factory/7/{root[:8]}", 31)

    def test_the_unresolved_topology_records_a_refusal_never_a_branch(self, migrated_db):
        engine, root, child, corrupt = migrated_db
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT t.status, t.source_branch, t.refusal_reason, f.target_id"
                    " FROM collaboration_targets t JOIN flow_runs f ON f.target_id = t.id"
                    " WHERE f.id = :corrupt"
                ),
                {"corrupt": corrupt},
            ).fetchone()
            assert row is not None
            status, source, reason, _target_id = row
            assert status == "refused"
            assert source is None  # never an inferred branch
            assert "recorded_branch_mismatch" in reason

    def test_the_downgrade_guard_refuses_while_rows_exist(self, migrated_db):

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        engine, root, child, corrupt = migrated_db
        migration = _load_migration_032()
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                with pytest.raises(RuntimeError, match="collaboration_targets holds"):
                    migration.downgrade()


# ----------------------------------------------------------------------
# The reference format itself
# ----------------------------------------------------------------------


class TestRoundReferenceFormat:
    def test_the_reference_is_round_n_of_the_root_short(self) -> None:
        assert round_reference(2, "a1b2c3d4" + "f" * 24) == "round 2 of a1b2c3d4"
        assert round_reference(1, "a1b2c3d4" + "f" * 24) == "round 1 of a1b2c3d4"

    def test_the_reference_regex_parses_both_id_lengths(self) -> None:
        match = ROUND_REFERENCE_RE.search("/status round 12 of a1b2c3d4")
        assert match and match.group(1) == "12" and match.group(2) == "a1b2c3d4"
        match = ROUND_REFERENCE_RE.search(f"/status round 2 of {'a' * 32}")
        assert match and match.group(2) == "a" * 32
        assert ROUND_REFERENCE_RE.search("/status a1b2c3d4") is None
