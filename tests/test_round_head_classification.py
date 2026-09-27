"""R41-03 (#358) — the round-head classification decision table.

Units of :meth:`forge.runs.service.RunService._classify_round_head` and
its probe leg: the FOUR outcomes (``not_dispatched`` / ``own`` /
``unknown`` / ``foreign``) are decided by the round's EXISTING
publication identities — the ``commit`` intents' operation key and
expected parents, the journal's succeeded actions, the recorded effect
shas — and NEVER by a commit message alone (F07: the human message
repeats across cycles and proves nothing).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.adaptive.revisions import REQUEST_ROUND_ADMITTED, ReviewFeedbackRequest
from forge.durable import FlowRun, FlowStatus, Outbox, PublicationIntent
from forge.durable.models import ActionLog, ReviewRound
from forge.gitlab.client import GitLabAPIError
from forge.models.base import Base
from forge.runs.stubs import factory_branch
from tests.test_review_feedback import (
    ISSUE_DESC,
    ISSUE_IID,
    ISSUE_TITLE,
    PROJECT_ID,
    ReviewFakeGitLab,
    make_review_service,
)

PARENT_ID = "9" * 32
CHILD_ID = "9" * 8 + "c" * 24  # shares the lineage's 8-hex branch prefix
BRANCH = factory_branch(ISSUE_IID, CHILD_ID)
BASE = "b" * 40
OWN = "o" * 40
HUMAN = "h" * 40
OPERATION_KEY = "op-key-classify-1"
MARKER = f"(forge-op:{OPERATION_KEY})"


@pytest.fixture()
async def classify_db():
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
def classify_fake():
    fake = ReviewFakeGitLab()
    fake.seed_issue(ISSUE_IID, ISSUE_TITLE, ISSUE_DESC)
    fake.seed_commit("main", BASE, "initial")
    fake.seed_file(".forge.yml", "implement:\n  paths:\n    - forge-demo/**\n")
    # the round's approved base is the branch head at admission
    fake.seed_commit(BRANCH, BASE, "the approved human head")
    return fake


def _intent(
    *,
    status: str = "dispatched",
    operation_key: str = OPERATION_KEY,
    expected_parent: str | None = BASE,
    sha: str | None = None,
) -> PublicationIntent:
    return PublicationIntent(
        run_id=CHILD_ID,
        provider="gitlab",
        repo=str(PROJECT_ID),
        operation="commit",
        target_ref=BRANCH,
        idempotency_scope="cycle-1",
        operation_key=operation_key,
        expected_parent_oid=expected_parent,
        status=status,
        provider_object_id=sha,
    )


async def _seed(
    db,
    *,
    child_status: str = "committing",
    child_evidence: dict | None = None,
    intents: list[PublicationIntent] | None = None,
    journal_shas: list[str] | None = None,
) -> None:
    child_evidence = dict(child_evidence or {})
    child_evidence.setdefault("review_round", {"round_number": 2})
    async with db() as session:
        session.add(
            FlowRun(
                id=PARENT_ID,
                project_id=PROJECT_ID,
                issue_iid=ISSUE_IID,
                provider="gitlab",
                status="ready_for_human",
            )
        )
        session.add(
            FlowRun(
                id=CHILD_ID,
                project_id=PROJECT_ID,
                issue_iid=ISSUE_IID,
                provider="gitlab",
                status=child_status,
                evidence=child_evidence,
            )
        )
        session.add(
            ReviewRound(
                id="r" * 32,
                parent_run_id=PARENT_ID,
                child_run_id=CHILD_ID,
                root_run_id=PARENT_ID,
                round_number=2,
                note_id="n-classify",
                mr_iid=17,
                base_head_sha=BASE,
                decision_id="rd-classify",
                status="dispatched",
            )
        )
        for intent in intents or []:
            session.add(intent)
        for sha in journal_shas or []:
            session.add(
                ActionLog(
                    flow_run_id=CHILD_ID,
                    action_kind="commit",
                    correlation_id=BRANCH,
                    status="succeeded",
                    remote_result={"sha": sha},
                )
            )
        await session.commit()


async def _classify(db, fake, head: str) -> tuple[str, str]:
    service = make_review_service(db, fake)
    async with db() as session:
        row = (
            (
                await session.execute(
                    select(ReviewRound).where(ReviewRound.child_run_id == CHILD_ID).limit(1)
                )
            )
            .scalars()
            .first()
        )
        child = await session.get(FlowRun, CHILD_ID)
    assert row is not None and child is not None
    return await service._classify_round_head(row, child, head)


class TestNotDispatched:
    async def test_no_identities_with_the_head_at_the_base_is_not_dispatched(
        self, classify_db, classify_fake
    ):
        await _seed(classify_db)
        assert await _classify(classify_db, classify_fake, BASE) == ("not_dispatched", "")

    async def test_a_moved_head_without_any_own_identity_is_foreign(
        self, classify_db, classify_fake
    ):
        await _seed(classify_db)
        assert await _classify(classify_db, classify_fake, HUMAN) == ("foreign", "")


class TestOwnEffect:
    async def test_the_journaled_effect_at_the_head_is_own(self, classify_db, classify_fake):
        await _seed(classify_db, journal_shas=[OWN])
        assert await _classify(classify_db, classify_fake, OWN) == ("own", OWN)

    async def test_an_intent_recorded_effect_at_the_head_is_own(self, classify_db, classify_fake):
        await _seed(classify_db, intents=[_intent(status="committed", sha=OWN)])
        assert await _classify(classify_db, classify_fake, OWN) == ("own", OWN)

    async def test_the_published_candidate_evidence_at_the_head_is_own(
        self, classify_db, classify_fake
    ):
        await _seed(
            classify_db,
            child_evidence={"published_candidate": {"sha": OWN, "base": BASE}},
        )
        assert await _classify(classify_db, classify_fake, OWN) == ("own", OWN)

    async def test_an_open_intent_with_the_head_intact_resumes_probe_mediated(
        self, classify_db, classify_fake
    ):
        # Nothing landed yet (or it is in flight): the head is still the
        # approved base, so the writer's probe-mediated recovery — adopt
        # or the A12 certainty window — is the correct resumption, never
        # the fence, never a blind second dispatch.
        await _seed(classify_db, intents=[_intent(status="dispatched")])
        assert await _classify(classify_db, classify_fake, BASE) == ("own", "")

    async def test_an_open_intent_marker_and_parent_match_at_the_head_is_own(
        self, classify_db, classify_fake
    ):
        classify_fake.seed_commit(BRANCH, OWN, f"the correction {MARKER}", parents=[BASE])
        await _seed(classify_db, intents=[_intent(status="dispatched")])
        assert await _classify(classify_db, classify_fake, OWN) == ("own", OWN)


class TestForeignEffect:
    async def test_a_foreign_tip_above_the_own_effect_is_foreign(self, classify_db, classify_fake):
        classify_fake.seed_commit(BRANCH, OWN, f"the correction {MARKER}", parents=[BASE])
        classify_fake.seed_commit(BRANCH, HUMAN, "human: late edit", parents=[OWN])
        await _seed(classify_db, intents=[_intent(status="dispatched")])
        # the round's effect is preserved in the history; the TIP is foreign
        assert await _classify(classify_db, classify_fake, HUMAN) == ("foreign", OWN)

    async def test_zero_identity_matches_with_a_moved_head_is_foreign(
        self, classify_db, classify_fake
    ):
        classify_fake.seed_commit(BRANCH, HUMAN, "human: unrelated edit", parents=[BASE])
        await _seed(classify_db, intents=[_intent(status="dispatched")])
        assert await _classify(classify_db, classify_fake, HUMAN) == ("foreign", "")

    async def test_a_repeated_message_without_the_marker_proves_nothing(
        self, classify_db, classify_fake
    ):
        # F07: the human message repeats across repair cycles — a commit
        # with the SAME message but no operation marker is NEVER adopted.
        classify_fake.seed_commit(BRANCH, HUMAN, "the correction (no marker)", parents=[BASE])
        await _seed(classify_db, intents=[_intent(status="dispatched")])
        assert await _classify(classify_db, classify_fake, HUMAN) == ("foreign", "")

    async def test_a_marker_match_with_the_wrong_parents_is_not_own(
        self, classify_db, classify_fake
    ):
        classify_fake.seed_commit(BRANCH, OWN, f"the correction {MARKER}", parents=[HUMAN])
        await _seed(classify_db, intents=[_intent(status="dispatched")])
        assert await _classify(classify_db, classify_fake, OWN) == ("foreign", "")

    async def test_terminal_intents_only_with_a_moved_head_is_foreign(
        self, classify_db, classify_fake
    ):
        # the round's own effect provably never landed (failed/duplicated)
        # and the head moved — a foreign change owns the tip
        await _seed(classify_db, intents=[_intent(status="duplicated")])
        assert await _classify(classify_db, classify_fake, HUMAN) == ("foreign", "")


class TestUnknownOutcome:
    async def test_an_unavailable_probe_stays_unknown(
        self, classify_db, classify_fake, monkeypatch
    ):
        async def unavailable(project_id: int, ref: str) -> list[dict]:
            raise GitLabAPIError(503, "probe unavailable")

        monkeypatch.setattr(classify_fake, "list_commits", unavailable)
        await _seed(classify_db, intents=[_intent(status="dispatched")])
        assert await _classify(classify_db, classify_fake, HUMAN) == ("unknown", "")

    async def test_ambiguous_matches_stay_unknown(self, classify_db, classify_fake):
        classify_fake.seed_commit(BRANCH, OWN, f"the correction {MARKER}", parents=[BASE])
        classify_fake.seed_commit(BRANCH, HUMAN, f"the correction {MARKER}", parents=[BASE])
        await _seed(classify_db, intents=[_intent(status="dispatched")])
        assert await _classify(classify_db, classify_fake, HUMAN) == ("unknown", "")


class TestResolutionEvent:
    """The journaled ``review_round.effect_resolution`` is one row per
    DISTINCT ``(resolution, head)`` — a repeating uncertainty does not
    spam the outbox, a change re-journals."""

    async def test_a_repeating_resolution_journals_once(self, classify_db, classify_fake):
        from forge.runs.service import RunService

        service: RunService = make_review_service(classify_db, classify_fake)
        await _seed(classify_db)
        async with classify_db() as session:
            row = (
                (
                    await session.execute(
                        select(ReviewRound).where(ReviewRound.child_run_id == CHILD_ID).limit(1)
                    )
                )
                .scalars()
                .first()
            )
            child = await session.get(FlowRun, CHILD_ID)
        for _ in range(3):
            await service._emit_round_effect_resolution(row, child, HUMAN, "unknown", "")
        await service._emit_round_effect_resolution(row, child, OWN, "own", OWN)
        async with classify_db() as session:
            events = (
                (
                    await session.execute(
                        select(Outbox).where(
                            Outbox.flow_run_id == CHILD_ID,
                            Outbox.event_type == "review_round.effect_resolution",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert [event.payload["resolution"] for event in events] == ["unknown", "own"]
        assert events[-1].payload["effect_sha"] == OWN
        assert events[-1].payload["round_id"] == "r" * 32
        assert events[-1].payload["attempt_cycle"] == 1


class TestRequestDocumentStaysUsable:
    """A guard against fixture drift: the classification never mutates the
    round's request linkage or the child row it was handed."""

    async def test_classification_touches_no_durable_state(self, classify_db, classify_fake):
        await _seed(classify_db, journal_shas=[OWN])
        await _classify(classify_db, classify_fake, OWN)
        async with classify_db() as session:
            child = await session.get(FlowRun, CHILD_ID)
            intents = (
                (
                    await session.execute(
                        select(PublicationIntent).where(PublicationIntent.run_id == CHILD_ID)
                    )
                )
                .scalars()
                .all()
            )
            row = (
                (
                    await session.execute(
                        select(ReviewRound).where(ReviewRound.child_run_id == CHILD_ID)
                    )
                )
                .scalars()
                .first()
            )
        assert child.status == FlowStatus.COMMITTING.value
        assert len(intents) == 0  # the journal seeded shas, not intent rows
        assert row is not None and row.status == "dispatched"
        request = ReviewFeedbackRequest(
            note_id="n-classify",
            run_id=PARENT_ID,
            discussion_id="d",
            mr_iid=17,
            actor="alice",
            head_sha=BASE,
            classification="in_scope_correction",
            text="/fix",
            status=REQUEST_ROUND_ADMITTED,
        )
        assert request.note_id == "n-classify"
