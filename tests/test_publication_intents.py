"""Publication-intent tests (R11 + A12): durable intent BEFORE the remote
effect, adoption of a lost outcome by identity — never a blind replay.

The review acceptance, per provider:

1. the remote ACCEPTS the commit and the response is LOST → a new process /
   reconciler pass ADOPTS the existing effect without duplicating it (the
   probe finds the intent's ``(forge-op:<key>)`` marker at the expected
   parent);
2. a PREVIOUS repair cycle's commit (different operation key, repeating
   human message) is never mistaken for the new attempt;
3. an unresolvable outcome (≥2 marker matches) blocks as
   ``unknown_outcome`` with an operator instruction — never guessed;
4. the intent row is durably written BEFORE the HTTP call (order asserted
   through the journal at dispatch time).

A12 adds the effect-certainty contract: a NEGATIVE probe (zero marker hits,
head intact) is not proof that no remote effect is pending — the provider
may have accepted the first request and be applying it slowly. On GitLab
(no branch-wide CAS) the intent settles in the bounded ``probing`` window
and an exhausted window parks a visible unknown; on GitHub/Azure the CAS
makes the released redispatch inherently duplicate-safe.

Also covered: the pure probe decision table
(:func:`forge.durable.intents.classify_probe`), the R10 superseded
interplay (a cancelled/terminal run's intent resolves ``duplicated``, never
adopted-into-READY), the migration-relevant model state machine, and the
Azure stale-CAS adoption.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from forge.durable import (
    ActionLog,
    DEFAULT_SETTLE_WINDOW_SECONDS,
    FlowRun,
    FlowStatus,
    PublicationIntent,
    ProbeObservation,
    ProbeVerdict,
    RedispatchGuarantee,
    SettleDecision,
    classify_probe,
    commit_matches,
    complete_intent,
    mark_dispatched,
    message_with_marker,
    op_marker,
    record_intent,
    redispatch_guarantee,
    settle_negative_probe,
    settle_state,
)
from forge.durable.intents import InvalidIntentTransition
from forge.integrations.github_flow import GitHubPublishFlow, github_factory_branch
from forge.models.base import Base
from forge.repository import Change, ChangeSet, ChangesetWriter, Operation, WriteOutcome
from forge.repository.writer import BranchDriftError, WriteResult
from forge.runs.azure_service import AzureRunService, _changeset_to_commits
from forge.runs.github_service import GitHubRunService
from forge.runs.publisher import publish_candidate
from forge.repository.changeset import ChangeSet as RepoChangeSet
from tests.fixtures.fake_github import FakeGitHub
from tests.fixtures.fake_gitlab import FakeGitLab

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

RUN_ID = uuid4().hex
PROJECT_ID = 42
BRANCH = "factory/7/" + RUN_ID[:8]
REPO = "acme/acme-widget"
OWNER, REPO_NAME = REPO.split("/", 1)
GITHUB_ISSUE = 42
BASE_HEAD = "1" * 40
GBRANCH = github_factory_branch(GITHUB_ISSUE, RUN_ID)


@pytest.fixture()
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            FlowRun(
                id=RUN_ID,
                project_id=PROJECT_ID,
                issue_iid=7,
                provider="gitlab",
                base_sha=BASE_SHA,
            )
        )
        await session.commit()
    yield factory
    await engine.dispose()


@pytest.fixture()
async def gh_session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            FlowRun(
                id=RUN_ID,
                project_id=70010,
                issue_iid=GITHUB_ISSUE,
                provider="github",
                github_repo_full_name=REPO,
                base_sha=BASE_HEAD,
            )
        )
        await session.commit()
    yield factory
    await engine.dispose()


def gitlab_changeset() -> ChangeSet:
    return ChangeSet(
        branch=BRANCH,
        commit_message=f"forge: implement 7 (run {RUN_ID[:8]})",
        changes=[
            Change(
                path=f"forge-demo/run-{RUN_ID[:8]}.md",
                operation=Operation.CREATE,
                content=f"# run {RUN_ID}\n",
            )
        ],
    )


async def open_or_create_intent(
    session_factory, *, status: str, key: str, expected_parent: str | None, branch: str = BRANCH
) -> PublicationIntent:
    """Seed a crashed attempt's intent exactly as the writer would have."""
    async with session_factory() as session:
        intent = await record_intent(
            session,
            run_id=RUN_ID,
            provider="gitlab",
            repo=str(PROJECT_ID),
            target_ref=branch,
            idempotency_scope="cycle-1",
            operation_key=key,
            expected_parent_oid=expected_parent,
            expected_head=expected_parent,
        )
        if status == "dispatched":
            await mark_dispatched(session, intent.id)
        await session.commit()
        return intent


async def intent_of(session_factory, intent_id: str) -> PublicationIntent:
    async with session_factory() as session:
        return await session.get(PublicationIntent, intent_id)


async def expire_settle_window(session_factory, intent_id: str) -> None:
    """Force the A12 certainty window open-ended (the wall clock stood in for)."""
    async with session_factory() as session:
        row = await session.get(PublicationIntent, intent_id)
        assert row is not None
        row.next_probe_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()


async def _apply_after_expired_window(writer, session_factory, intent_id: str) -> WriteResult:
    """Expire the certainty window, then re-enter the writer's recovery probe."""
    await expire_settle_window(session_factory, intent_id)
    return await writer.apply(RUN_ID, gitlab_changeset(), start_ref="main")


class DelayedApplyGitLab(FakeGitLab):
    """A12 accept-then-delay-apply fake: the provider ACCEPTED the write but
    its application is still in flight — every branch read shows the OLD
    head until :meth:`complete_delayed_apply` runs (the review's acceptance
    shape: accept, delay the application past the negative probe, complete
    after the redispatch attempt would have happened)."""

    def __init__(self) -> None:
        super().__init__()
        self.pending: dict | None = None

    def accept_delayed_commit(
        self, branch: str, sha: str, message: str, parents: list[str]
    ) -> None:
        """Seed the accepted-but-unapplied effect (the lost-response shape)."""
        self.pending = {
            "branch": branch,
            "record": {
                "sha": sha,
                "short_id": sha[:8],
                "message": message,
                "parent_ids": list(parents),
            },
        }

    def complete_delayed_apply(self) -> str:
        """The provider's slow application lands — the commit appears."""
        assert self.pending is not None, "no delayed apply is in flight"
        pending = self.pending
        self.pending = None
        self.branches.setdefault(pending["branch"], []).insert(0, dict(pending["record"]))
        return str(pending["record"]["sha"])


# ---------------------------------------------------------------------------
# The pure probe decision table
# ---------------------------------------------------------------------------


class TestProbeDecisionTable:
    """docs/research/2026-09-17-remote-effect-reconciliation.md, decision table."""

    COMMITS = [
        {"sha": "a" * 40, "message": "forge: fix (forge-op:ka12bug45678)", "parent_ids": ["p"]},
        {"sha": "b" * 40, "message": "forge: fix (forge-op:otherkey1234)", "parent_ids": ["p"]},
        {"sha": "c" * 40, "message": "forge: fix", "parent_ids": ["p"]},
        # root commit: marker present, no parents
        {"sha": "d" * 40, "message": "root (forge-op:kroot1234567)", "parent_ids": []},
    ]

    def test_exactly_one_marker_and_parent_match_adopts(self):
        hits = commit_matches(self.COMMITS, operation_key="ka12bug45678", expected_parent_oid="p")
        assert hits == ["a" * 40]
        verdict = classify_probe(
            ProbeObservation(marker_hits=tuple(hits), head_oid="x", expected_parent_oid="p")
        )
        assert verdict is ProbeVerdict.ADOPT

    def test_previous_repair_key_is_never_mistaken_for_this_intent(self):
        """A different intent's key (same repeating human message) matches nothing."""
        hits = commit_matches(self.COMMITS, operation_key="currentkey01", expected_parent_oid="p")
        assert hits == []

    def test_right_marker_wrong_parent_is_not_a_match(self):
        hits = commit_matches(
            self.COMMITS, operation_key="ka12bug45678", expected_parent_oid="other"
        )
        assert hits == []

    def test_two_matches_are_unknown(self):
        twins = [
            {"sha": "t1", "message": "m (forge-op:doublekey12)", "parent_ids": ["p"]},
            {"sha": "t2", "message": "m (forge-op:doublekey12)", "parent_ids": ["p"]},
        ]
        hits = commit_matches(twins, operation_key="doublekey12", expected_parent_oid="p")
        assert len(hits) == 2
        assert (
            classify_probe(
                ProbeObservation(marker_hits=tuple(hits), head_oid="t1", expected_parent_oid="p")
            )
            is ProbeVerdict.UNKNOWN
        )

    def test_zero_matches_head_intact_is_redispatch(self):
        verdict = classify_probe(
            ProbeObservation(marker_hits=(), head_oid="p", expected_parent_oid="p")
        )
        assert verdict is ProbeVerdict.REDISPATCH

    def test_zero_matches_head_moved_is_duplicated(self):
        verdict = classify_probe(
            ProbeObservation(marker_hits=(), head_oid="human", expected_parent_oid="p")
        )
        assert verdict is ProbeVerdict.DUPLICATED

    def test_root_commit_intent_with_empty_branch_is_redispatch(self):
        verdict = classify_probe(
            ProbeObservation(marker_hits=(), head_oid=None, expected_parent_oid=None)
        )
        assert verdict is ProbeVerdict.REDISPATCH

    def test_marker_format_is_the_frozen_contract(self):
        assert op_marker("abcd1234efgh") == "(forge-op:abcd1234efgh)"
        assert (
            message_with_marker("forge: implement 7", "abcd1234efgh")
            == "forge: implement 7 (forge-op:abcd1234efgh)"
        )


class TestRedispatchGuaranteeTable:
    """A12 per-adapter guarantees (research doc §summary matrix)."""

    def test_github_and_azure_are_cas_protected(self):
        assert (
            redispatch_guarantee("github") is RedispatchGuarantee.CAS_PROTECTED
        )  # createCommitOnBranch expectedHeadOid — a branch-wide CAS
        assert (
            redispatch_guarantee("azure_devops") is RedispatchGuarantee.CAS_PROTECTED
        )  # pushes oldObjectId → staleOldObjectId — a branch-wide CAS

    def test_gitlab_has_no_cas(self):
        assert redispatch_guarantee("gitlab") is RedispatchGuarantee.UNPROTECTED

    def test_default_settle_window_is_bounded(self):
        assert DEFAULT_SETTLE_WINDOW_SECONDS == 30


class TestA12SettleMachine:
    """The effect-certainty machine over a seeded open intent."""

    async def _seed(self, session_factory, *, provider: str, status: str) -> PublicationIntent:
        async with session_factory() as session:
            intent = await record_intent(
                session,
                run_id=RUN_ID,
                provider=provider,
                repo=str(PROJECT_ID),
                target_ref=BRANCH,
                idempotency_scope="cycle-1",
                operation_key="settlek12345",
                expected_parent_oid=None,
                expected_head=None,
            )
            if status == "dispatched":
                await mark_dispatched(session, intent.id)
            await session.commit()
            return intent

    async def test_first_negative_probe_opens_the_window(self, session_factory):
        intent = await self._seed(session_factory, provider="gitlab", status="dispatched")
        async with session_factory() as session:
            decision = await settle_negative_probe(
                session, intent, now=datetime.now(timezone.utc), window_seconds=30
            )
            await session.commit()
        assert decision is SettleDecision.WAIT
        settled = await intent_of(session_factory, intent.id)
        assert settled.status == "probing"
        assert settled.next_probe_at is not None
        state = settle_state(settled)
        assert state.rounds == 0 and state.window_seconds == 30
        assert state.reprobe_at is not None

    async def test_open_window_waits_without_touching_the_row(self, session_factory):
        intent = await self._seed(session_factory, provider="gitlab", status="dispatched")
        async with session_factory() as session:
            await settle_negative_probe(session, intent, window_seconds=30)
            await session.commit()
        settled = await intent_of(session_factory, intent.id)
        before = (settled.status, settled.next_probe_at, settle_state(settled).rounds)
        async with session_factory() as session:
            decision = await settle_negative_probe(session, settled, window_seconds=30)
            await session.commit()
        assert decision is SettleDecision.WAIT  # window still open — no hot re-probe
        after = await intent_of(session_factory, intent.id)
        assert (after.status, after.next_probe_at, settle_state(after).rounds) == before

    async def test_gitlab_window_exhaustion_parks_unknown(self, session_factory):
        intent = await self._seed(session_factory, provider="gitlab", status="dispatched")
        decisions: list[SettleDecision] = []
        for round_index in range(4):
            await expire_settle_window(session_factory, intent.id)
            current = await intent_of(session_factory, intent.id)
            async with session_factory() as session:
                decisions.append(
                    await settle_negative_probe(
                        session, current, now=datetime.now(timezone.utc), window_seconds=30
                    )
                )
                await session.commit()
        assert decisions == [
            SettleDecision.WAIT,
            SettleDecision.WAIT,
            SettleDecision.WAIT,
            SettleDecision.PARK_UNKNOWN,
        ]
        exhausted = await intent_of(session_factory, intent.id)
        assert exhausted.status == "probing"  # the PARK write stays with the caller
        assert settle_state(exhausted).rounds == 2  # 3 windows total: 30s, 60s, 120s

    async def test_cas_provider_window_release_returns_to_dispatched(self, session_factory):
        """On a CAS-protected adapter the exhausted window RELEASES the same-key
        redispatch (``probing → dispatched``): the branch-wide CAS itself
        refuses any duplicate, so the release is inherently safe."""
        intent = await self._seed(session_factory, provider="github", status="dispatched")
        attempts_before = intent.attempt_count
        async with session_factory() as session:
            first = await settle_negative_probe(session, intent, window_seconds=30)
            await session.commit()
        assert first is SettleDecision.WAIT
        await expire_settle_window(session_factory, intent.id)
        for _ in range(2):  # rounds 1 and 2 extend the window
            current = await intent_of(session_factory, intent.id)
            async with session_factory() as session:
                extended = await settle_negative_probe(session, current, window_seconds=30)
                await session.commit()
            assert extended is SettleDecision.WAIT
            await expire_settle_window(session_factory, intent.id)
        released = await intent_of(session_factory, intent.id)
        async with session_factory() as session:
            decision = await settle_negative_probe(session, released, window_seconds=30)
            await session.commit()
        assert decision is SettleDecision.REDISPATCH
        final = await intent_of(session_factory, intent.id)
        assert final.status == "dispatched"  # released to the CAS-protected leg
        assert int(final.attempt_count) == attempts_before + 1


# ---------------------------------------------------------------------------
# GitLab writer: intent before I/O + adoption by identity
# ---------------------------------------------------------------------------


class TestGitLabWriterIntents:
    async def test_intent_and_action_written_before_http(self, session_factory):
        """THE order assert: when the HTTP call fires, the durable intent row
        (stable key + expected parent) and the action journal row both exist."""
        fake = FakeGitLab()
        seen: dict = {}

        original = fake.create_commit

        async def inspecting(*args, **kwargs):
            async with session_factory() as session:
                action = (
                    (
                        await session.execute(
                            select(ActionLog)
                            .where(ActionLog.flow_run_id == RUN_ID)
                            .order_by(ActionLog.id.desc())
                        )
                    )
                    .scalars()
                    .first()
                )
                intent = (
                    (
                        await session.execute(
                            select(PublicationIntent).where(PublicationIntent.run_id == RUN_ID)
                        )
                    )
                    .scalars()
                    .first()
                )
            seen["action_status"] = action.status if action else None
            seen["intent"] = intent
            return await original(*args, **kwargs)

        fake.create_commit = inspecting  # type: ignore[method-assign]
        writer = ChangesetWriter(fake, session_factory, project_id=PROJECT_ID)

        result = await writer.apply(
            RUN_ID, gitlab_changeset(), start_ref="main", expected_head=None
        )

        assert result.outcome is WriteOutcome.COMMITTED
        assert seen["action_status"] == "requested"  # journal intent, pre-outcome
        intent = seen["intent"]
        assert intent is not None
        assert intent.status == "dispatched"  # marked dispatched BEFORE the POST
        assert intent.operation_key == writer.operation_key
        assert op_marker(intent.operation_key) in fake.calls_of("create_commit")[0][1][3]

    async def test_intent_key_is_stable_across_retries(self, session_factory):
        """The open intent's key is reused on the re-drive — never re-minted:
        a crashed attempt's key must survive process death (that IS the
        recovery identity), while a fresh intent mints a fresh key."""
        fake = FakeGitLab()
        writer = ChangesetWriter(fake, session_factory, project_id=PROJECT_ID)
        cs = gitlab_changeset()

        await writer.apply(RUN_ID, cs, start_ref="main")
        first_key = writer.operation_key
        assert first_key is not None
        # A DIFFERENT intent mints a DIFFERENT key (repair cycles never share).
        fake.branches[BRANCH] = []
        fake.seed_commit(BRANCH, "sha-newbase", "previous cycle's candidate")

        async with session_factory() as session:
            repair_intent = await record_intent(
                session,
                run_id=RUN_ID,
                provider="gitlab",
                repo=str(PROJECT_ID),
                target_ref=BRANCH,
                idempotency_scope="cycle-2",
                operation_key=None,  # minted at creation — once
                commit_cycle=2,
                expected_parent_oid="sha-newbase",
            )
            await mark_dispatched(session, repair_intent.id)
            await session.commit()
            repair_key = repair_intent.operation_key
        assert repair_key != first_key

        writer2 = ChangesetWriter(fake, session_factory, project_id=PROJECT_ID)
        await writer2.apply(
            RUN_ID,
            gitlab_changeset(),
            start_ref="sha-newbase",
            expected_head="sha-newbase",
            commit_cycle=2,  # the resumed repair leg derives the same scope
        )
        assert writer2.operation_key == repair_key  # the open intent's key wins

    async def test_lost_response_is_adopted_by_a_new_process(self, session_factory):
        """THE acceptance: the remote accepted the commit, the response was
        lost, the process died before the journal completed — a NEW writer
        (fresh process) ADOPTS the landed commit and never duplicates it."""
        fake = FakeGitLab()
        key = "lostrespk1234"

        # Previous process: intent dispatched, effect landed, journal lost.
        intent = await open_or_create_intent(
            session_factory, status="dispatched", key=key, expected_parent=None
        )
        fake.seed_commit(
            BRANCH,
            "sha-landed",
            f"forge: implement 7 (run {RUN_ID[:8]}) {op_marker(key)}",
            parents=[],
        )

        # New process: a fresh writer re-runs apply for the same identity.
        writer = ChangesetWriter(fake, session_factory, project_id=PROJECT_ID)
        result = await writer.apply(RUN_ID, gitlab_changeset(), start_ref="main")

        assert result.outcome is WriteOutcome.COMMITTED
        assert result.commit_sha == "sha-landed"  # the PREVIOUS attempt's commit
        assert fake.calls_of("create_commit") == []  # zero new writes — no duplicate
        assert writer.operation_key == key  # same key forever

        resolved = await intent_of(session_factory, intent.id)
        assert resolved.status == "adopted"
        assert resolved.provider_object_id == "sha-landed"
        # The action journal records the adoption for `_committed_candidate`.
        async with session_factory() as session:
            action = (
                (
                    await session.execute(
                        select(ActionLog)
                        .where(ActionLog.flow_run_id == RUN_ID)
                        .order_by(ActionLog.id.desc())
                    )
                )
                .scalars()
                .first()
            )
        assert action.status == "succeeded"
        assert action.remote_result["adopted"] is True
        assert action.remote_result["sha"] == "sha-landed"

    async def test_response_lost_and_nothing_landed_settles_then_parks_unknown(
        self, session_factory
    ):
        """Lost response, nothing landed, head intact (A12): the FIRST
        negative probe must NOT re-dispatch and must not call the outcome
        unknown either — the intent settles in the certainty window; only
        the window's exhaustion with consistently-negative probes parks the
        conservative unknown (the run blocks, never a blind retry)."""
        fake = FakeGitLab()
        intent = await open_or_create_intent(
            session_factory, status="dispatched", key="droppedkk123", expected_parent=None
        )

        writer = ChangesetWriter(fake, session_factory, project_id=PROJECT_ID)
        first = await writer.apply(RUN_ID, gitlab_changeset(), start_ref="main")

        assert first == WriteResult(WriteOutcome.SETTLING, None)
        assert fake.calls_of("create_commit") == []  # zero new writes — no duplicate
        settled = await intent_of(session_factory, intent.id)
        assert settled.status == "probing"
        assert settled.next_probe_at is not None  # the window-end re-probe

        # Windows expire with the probe still negative (nothing ever lands):
        # the ladder extends twice, then parks the honest unknown.
        second = await _apply_after_expired_window(writer, session_factory, intent.id)
        assert second == WriteResult(WriteOutcome.SETTLING, None)
        third = await _apply_after_expired_window(writer, session_factory, intent.id)
        assert third == WriteResult(WriteOutcome.SETTLING, None)
        fourth = await _apply_after_expired_window(writer, session_factory, intent.id)
        assert fourth == WriteResult(WriteOutcome.UNKNOWN, None)
        assert fake.calls_of("create_commit") == []  # never re-dispatched

        resolved = await intent_of(session_factory, intent.id)
        assert resolved.status == "unknown"
        assert resolved.remote_result["reason"] == "settle_window_exhausted"
        assert "operator" in resolved.remote_result["operator_instruction"]
        assert settle_state(resolved).rounds == 2  # three certainty windows served

    async def test_previous_repair_commit_not_mistaken_for_new_attempt(self, session_factory):
        """A previous cycle's commit (different operation key) is never
        adopted by this intent: nothing landed FOR THIS KEY and the head is
        intact at the new attempt base — but A12 forbids deriving a dispatch
        from that one read, so the intent settles first and (nothing ever
        landing) parks the honest unknown. The old cycle's commit is never
        adopted and never re-written."""
        fake = FakeGitLab()
        fake.branches[BRANCH] = []  # branch pre-exists at the new base
        fake.seed_commit(BRANCH, "sha-newbase", "previous cycle's candidate (forge-op:oldcycle1)")
        intent = await open_or_create_intent(
            session_factory,
            status="dispatched",
            key="newcyclek123",
            expected_parent="sha-newbase",
        )

        writer = ChangesetWriter(fake, session_factory, project_id=PROJECT_ID)
        first = await writer.apply(
            RUN_ID,
            gitlab_changeset(),
            start_ref="sha-newbase",
            expected_head="sha-newbase",
        )

        # The old commit was NOT adopted; nothing was dispatched either —
        # the negative probe opened the certainty window.
        assert first == WriteResult(WriteOutcome.SETTLING, None)
        assert fake.calls_of("create_commit") == []
        settled = await intent_of(session_factory, intent.id)
        assert settled.status == "probing"

        second = await _apply_after_expired_window(writer, session_factory, intent.id)
        third = await _apply_after_expired_window(writer, session_factory, intent.id)
        fourth = await _apply_after_expired_window(writer, session_factory, intent.id)
        assert (second, third, fourth) == (
            WriteResult(WriteOutcome.SETTLING, None),
            WriteResult(WriteOutcome.SETTLING, None),
            WriteResult(WriteOutcome.UNKNOWN, None),
        )
        resolved = await intent_of(session_factory, intent.id)
        assert resolved.status == "unknown"
        # The branch still holds ONLY the previous cycle's work — no duplicate
        # was ever dispatched and the old commit was never mis-adopted.
        assert [c["sha"] for c in fake.branches[BRANCH]] == ["sha-newbase"]

    async def test_unresolvable_two_matches_block_unknown(self, session_factory):
        """≥2 marker+parent matches is a double write — inconclusive: the
        writer stays UNKNOWN and the intent resolves unknown (run blocks)."""
        fake = FakeGitLab()
        intent = await open_or_create_intent(
            session_factory, status="dispatched", key="twinkiesk123", expected_parent=None
        )
        fake.seed_commit(
            BRANCH, "sha-twin-2", f"forge: implement 7 {op_marker('twinkiesk123')}", parents=[]
        )
        fake.seed_commit(
            BRANCH, "sha-twin-1", f"forge: implement 7 {op_marker('twinkiesk123')}", parents=[]
        )

        writer = ChangesetWriter(fake, session_factory, project_id=PROJECT_ID)
        result = await writer.apply(RUN_ID, gitlab_changeset(), start_ref="main")

        assert result == WriteResult(WriteOutcome.UNKNOWN, None)
        resolved = await intent_of(session_factory, intent.id)
        assert resolved.status == "unknown"
        assert resolved.remote_result["matches"] == ["sha-twin-1", "sha-twin-2"]

    async def test_head_moved_by_human_resolves_duplicated_and_drifts(self, session_factory):
        """Zero marker matches + a moved head: someone else owns the ref —
        the intent resolves duplicated and the writer raises BranchDriftError
        (never force, never adopt)."""
        fake = FakeGitLab()
        fake.branches[BRANCH] = []
        fake.seed_commit(BRANCH, "sha-human", "a human push")
        intent = await open_or_create_intent(
            session_factory,
            status="dispatched",
            key="driftedkk1234",
            expected_parent="original-base",
        )

        writer = ChangesetWriter(fake, session_factory, project_id=PROJECT_ID)
        with pytest.raises(BranchDriftError) as exc_info:
            await writer.apply(RUN_ID, gitlab_changeset(), start_ref="main")

        assert exc_info.value.actual == "sha-human"
        resolved = await intent_of(session_factory, intent.id)
        assert resolved.status == "duplicated"
        assert fake.calls_of("create_commit") == []


# ---------------------------------------------------------------------------
# A12: delayed apply on GitLab — a negative probe never duplicates
# ---------------------------------------------------------------------------


class TestA12DelayedApplyGitLab:
    async def test_negative_probe_never_redispatches_and_late_commit_is_adopted(
        self, session_factory
    ):
        """THE A12 acceptance: the provider ACCEPTED the first request and is
        applying it SLOWLY. The recovery's negative probe must not redispatch
        — the intent settles in the certainty window, and the window-end
        re-probe adopts the late-landing commit. One write, one logical
        candidate, a consistent history."""
        fake = DelayedApplyGitLab()
        key = "slowapplyk12"
        intent = await open_or_create_intent(
            session_factory, status="dispatched", key=key, expected_parent=None
        )
        # The provider accepted A; its application is still in flight — the
        # branch reads show only the old (empty) head.
        fake.accept_delayed_commit(
            BRANCH,
            "sha-inflight",
            f"forge: implement 7 (run {RUN_ID[:8]}) {op_marker(key)}",
            parents=[],
        )

        writer = ChangesetWriter(fake, session_factory, project_id=PROJECT_ID)
        result = await writer.apply(RUN_ID, gitlab_changeset(), start_ref="main")

        assert result == WriteResult(WriteOutcome.SETTLING, None)
        assert fake.calls_of("create_commit") == [], "no duplicate dispatched off one read"
        assert fake.branches.get(BRANCH, []) == [], "the probe saw the old head"
        settled = await intent_of(session_factory, intent.id)
        assert settled.status == "probing"
        assert settled.next_probe_at is not None

        # The provider's slow application completes AFTER the negative probe
        # (and after the point where the old code would have redispatched).
        sha = fake.complete_delayed_apply()
        assert [c["sha"] for c in fake.branches[BRANCH]] == [sha]
        assert op_marker(key) in fake.branches[BRANCH][0]["message"]

        # Window-end re-probe: the settled intent is due again — the recovery
        # adopts the late-landing commit (same key, parent intact), it never
        # dispatches a second candidate.
        writer2 = ChangesetWriter(fake, session_factory, project_id=PROJECT_ID)
        result2 = await _apply_after_expired_window(writer2, session_factory, intent.id)

        assert result2 == WriteResult(WriteOutcome.COMMITTED, sha)
        assert fake.calls_of("create_commit") == []  # zero writes across the whole recovery
        resolved = await intent_of(session_factory, intent.id)
        assert resolved.status == "adopted"
        assert resolved.provider_object_id == sha
        assert [c["sha"] for c in fake.branches[BRANCH]] == [sha], "one candidate, no duplicate"


# ---------------------------------------------------------------------------
# Publisher (GitLab transport): intent metadata flows through the boundary
# ---------------------------------------------------------------------------


BASE_SHA = "base-sha-1"


def _create_diff(path: str, content: str) -> str:
    """A `git diff` fragment creating one file (mirrors the artifact shape)."""
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n" + body
    )


class TestPublisherIntentMetadata:
    async def test_publish_candidate_journals_intent_before_effect(self, session_factory):
        """The GitLab publisher transport carries the R11 intent identity:
        the writer journals the durable intent (provider/repo/scope) BEFORE
        the commit effect, and the outcome completes it."""
        from forge.durable.identity import factory_branch
        from forge.runs.candidate import parse_unified_diff

        fake = FakeGitLab()
        fake.seed_commit("main", BASE_SHA, "initial")
        async with session_factory() as session:
            run = await session.get(FlowRun, RUN_ID)
        assert run is not None
        bundle = parse_unified_diff(
            _create_diff("forge-demo/x.md", "hello\n"), BASE_SHA, "completed"
        )
        writer = ChangesetWriter(fake, session_factory, project_id=PROJECT_ID)
        result = await publish_candidate(
            gitlab=fake,
            session_factory=session_factory,
            writer=writer,
            run=run,
            bundle=bundle,
        )

        assert result.ok is True
        async with session_factory() as session:
            intent = (
                (
                    await session.execute(
                        select(PublicationIntent).where(PublicationIntent.run_id == RUN_ID)
                    )
                )
                .scalars()
                .first()
            )
        assert intent is not None
        assert intent.provider == "gitlab"
        assert intent.repo == str(PROJECT_ID)
        assert intent.idempotency_scope == "cycle-1"
        assert intent.status == "committed"
        assert intent.provider_object_id == result.commit_sha
        # The commit message carries the intent's stable marker.
        branch = factory_branch(7, RUN_ID)
        head = fake.branches[branch][0]  # newest first
        assert op_marker(intent.operation_key) in head["message"]


# ---------------------------------------------------------------------------
# GitHub flow: stale CAS adopts by marker
# ---------------------------------------------------------------------------


def gh_changeset(path: str = "src/feature.py") -> RepoChangeSet:
    return RepoChangeSet(
        branch=GBRANCH,
        commit_message=f"forge: implement {GITHUB_ISSUE} (run {RUN_ID[:8]})",
        changes=[Change(path=path, operation=Operation.CREATE, content="VALUE = 1\n")],
    )


class TestGitHubFlowAdoption:
    def _fake(self) -> FakeGitHub:
        github = FakeGitHub()
        github.seed_repo(REPO, {"src/app.py": "print('hi')\n"})
        github.heads[REPO]["main"] = BASE_HEAD
        return github

    async def test_lost_response_adopted_on_stale_cas(self):
        """The first attempt LANDED (response lost); the re-drive with the
        SAME operation key hits the stale CAS, probes the tip, finds the
        marker + expected parent, and ADOPTS — no duplicate commit."""
        fake = self._fake()
        flow = GitHubPublishFlow(fake, base_branch="main")
        key = "ghlostk12345"

        first = await flow.publish_changeset(
            OWNER,
            REPO_NAME,
            issue_number=GITHUB_ISSUE,
            run_id=RUN_ID,
            changeset=gh_changeset("src/feature.py"),
            expected_head=BASE_HEAD,
            operation_key=key,
        )
        assert first.ok is True and first.adopted is False
        landed = first.commit_oid
        # The commit message carries the frozen marker contract.
        (commit,) = fake.commits[REPO][GBRANCH]
        assert op_marker(key) in commit["message"]

        # Re-drive: SAME key, base pinned to the ORIGINAL frozen base (the
        # branch head has moved past it — the lost-response shape).
        flow2 = GitHubPublishFlow(fake, base_branch="main")
        second = await flow2.publish_changeset(
            OWNER,
            REPO_NAME,
            issue_number=GITHUB_ISSUE,
            run_id=RUN_ID,
            changeset=gh_changeset("src/other.py"),  # new path: validation passes
            expected_head=BASE_HEAD,
            operation_key=key,
        )

        assert second.ok is True
        assert second.adopted is True
        assert second.commit_oid == landed  # the previous attempt's commit
        assert len(fake.commits[REPO][GBRANCH]) == 1  # one commit, no duplicate

    async def test_different_key_is_not_adopted_and_stays_drift(self):
        """A previous attempt with a DIFFERENT operation key proves nothing
        for this intent — the CAS refusal stays a drift outcome."""
        fake = self._fake()
        flow = GitHubPublishFlow(fake, base_branch="main")
        await flow.publish_changeset(
            OWNER,
            REPO_NAME,
            issue_number=GITHUB_ISSUE,
            run_id=RUN_ID,
            changeset=gh_changeset("src/feature.py"),
            expected_head=BASE_HEAD,
            operation_key="firstkey1234",
        )

        flow2 = GitHubPublishFlow(fake, base_branch="main")
        second = await flow2.publish_changeset(
            OWNER,
            REPO_NAME,
            issue_number=GITHUB_ISSUE,
            run_id=RUN_ID,
            changeset=gh_changeset("src/other.py"),
            expected_head=BASE_HEAD,
            operation_key="secondkey23",
        )

        assert second.ok is False
        assert second.drift is True
        assert second.adopted is False
        assert len(fake.commits[REPO][GBRANCH]) == 1  # the refusal wrote nothing

    async def test_ambiguous_marker_matches_stay_drift(self):
        """Two commits with this intent's marker are inconclusive — the flow
        must NOT adopt (drift / block instead), never guess."""
        fake = self._fake()
        flow = GitHubPublishFlow(fake, base_branch="main")
        key = "ambiguokey123"
        # The branch head is beyond the pinned base (stale CAS guaranteed)
        # and TWO commits carry the marker with the expected parent.
        fake.seed_commit(
            REPO, GBRANCH, "t" * 40, f"forge: implement 42 {op_marker(key)}", parents=[BASE_HEAD]
        )
        fake.seed_commit(
            REPO, GBRANCH, "s" * 40, f"forge: implement 42 {op_marker(key)}", parents=[BASE_HEAD]
        )

        outcome = await flow.publish_changeset(
            OWNER,
            REPO_NAME,
            issue_number=GITHUB_ISSUE,
            run_id=RUN_ID,
            changeset=gh_changeset("src/feature.py"),
            expected_head=BASE_HEAD,
            operation_key=key,
        )

        assert outcome.ok is False
        assert outcome.drift is True
        assert outcome.adopted is False


# ---------------------------------------------------------------------------
# A12: delayed apply on GitHub/Azure CAS — the branch-wide CAS refuses the
# duplicate, so a redispatch off a negative probe is inherently safe
# ---------------------------------------------------------------------------


class TestA12DelayedApplyGitHubCAS:
    def _fake(self) -> FakeGitHub:
        github = FakeGitHub()
        github.seed_repo(REPO, {"src/app.py": "print('hi')\n"})
        github.heads[REPO]["main"] = BASE_HEAD
        return github

    async def test_slow_first_write_cannot_double_the_redispatch(self):
        """The first write is ACCEPTED but its application is delayed; the
        re-drive's negative probe sees the unchanged head and redispatches.
        The CAS makes both landing impossible: whichever write applies
        against a moved head is refused — exactly one effect survives."""
        fake = self._fake()
        fake.delayed_apply = True  # A is accepted, application in flight
        flow = GitHubPublishFlow(fake, base_branch="main")
        key = "casslowk1234"

        first = await flow.publish_changeset(
            OWNER,
            REPO_NAME,
            issue_number=GITHUB_ISSUE,
            run_id=RUN_ID,
            changeset=gh_changeset("src/feature.py"),
            expected_head=BASE_HEAD,
            operation_key=key,
        )
        assert first.ok is True
        assert fake.heads[REPO][GBRANCH] == BASE_HEAD, "accepted, not yet applied"
        assert len(fake.pending_delayed_commits[GBRANCH]) == 1

        # The negative probe would see exactly this old head. The redispatch
        # is applied immediately (the CAS evaluates against the same head).
        fake.delayed_apply = False
        second = await flow.publish_changeset(
            OWNER,
            REPO_NAME,
            issue_number=GITHUB_ISSUE,
            run_id=RUN_ID,
            changeset=gh_changeset("src/other.py"),  # new path: validation passes
            expected_head=BASE_HEAD,
            operation_key=key,
        )
        assert second.ok is True
        assert fake.heads[REPO][GBRANCH] == second.commit_oid

        # The slow first write's application finally races in — its CAS
        # (expected BASE_HEAD, actual = the re-drive's commit) REFUSES it.
        fake.flush_delayed_apply(GBRANCH)
        assert fake.delayed_refused == 1
        assert len(fake.commits[REPO][GBRANCH]) == 1, "single effect — no duplicate"
        assert fake.heads[REPO][GBRANCH] == second.commit_oid

    async def test_late_landing_first_write_is_adopted_not_duplicated(self):
        """The delayed application completes BEFORE the redispatch: the
        re-drive's CAS is refused (the head moved), the stale-CAS probe finds
        THIS intent's marker + parent and ADOPTS the late-landing commit —
        one effect, no duplicate."""
        fake = self._fake()
        fake.delayed_apply = True
        flow = GitHubPublishFlow(fake, base_branch="main")
        key = "caslatek1234"

        first = await flow.publish_changeset(
            OWNER,
            REPO_NAME,
            issue_number=GITHUB_ISSUE,
            run_id=RUN_ID,
            changeset=gh_changeset("src/feature.py"),
            expected_head=BASE_HEAD,
            operation_key=key,
        )
        assert first.ok is True
        fake.flush_delayed_apply(GBRANCH)  # the slow application lands
        landed = fake.heads[REPO][GBRANCH]
        assert landed != BASE_HEAD
        assert op_marker(key) in fake.commits[REPO][GBRANCH][0]["message"]

        fake.delayed_apply = False
        second = await flow.publish_changeset(
            OWNER,
            REPO_NAME,
            issue_number=GITHUB_ISSUE,
            run_id=RUN_ID,
            changeset=gh_changeset("src/other.py"),
            expected_head=BASE_HEAD,
            operation_key=key,
        )

        assert second.ok is True
        assert second.adopted is True
        assert second.commit_oid == landed  # the previous attempt's commit
        assert len(fake.commits[REPO][GBRANCH]) == 1  # one commit, no duplicate


# ---------------------------------------------------------------------------
# GitHub service: the recovery scanner
# ---------------------------------------------------------------------------


def gh_marker_message(key: str) -> str:
    return f"forge: implement {GITHUB_ISSUE} (run {RUN_ID[:8]}) {op_marker(key)}"


def gh_repo_suffix(full: str, branch: str) -> str:
    return branch


class TestGitHubIntentScanner:
    async def _seed_dispatched_intent(
        self, session_factory, *, key: str, commit_sha: str | None = None, twins: int = 0
    ) -> PublicationIntent:
        async with session_factory() as session:
            intent = await record_intent(
                session,
                run_id=RUN_ID,
                provider="github",
                repo=REPO,
                target_ref=GBRANCH,
                idempotency_scope="cycle-1",
                operation_key=key,
                commit_cycle=1,
                expected_parent_oid=BASE_HEAD,
                expected_head=BASE_HEAD,
            )
            await mark_dispatched(session, intent.id)
            await session.commit()
        return intent

    def _fake_with_landed_commit(self, key: str, twins: int = 0) -> FakeGitHub:
        fake = FakeGitHub()
        fake.seed_repo(REPO, {"src/app.py": "print('hi')\n"})
        fake.heads[REPO]["main"] = BASE_HEAD
        fake.seed_commit(REPO, GBRANCH, "f" * 40, gh_marker_message(key), parents=[BASE_HEAD])
        for i in range(twins):
            fake.seed_commit(
                REPO,
                GBRANCH,
                f"{i + 2:x}" * 10,
                gh_marker_message(key),
                parents=[BASE_HEAD],
            )
        return fake

    def _service(self, session_factory, fake: FakeGitHub) -> GitHubRunService:
        return GitHubRunService(
            session_factory,
            SimpleNamespace(),
            None,
            stack=SimpleNamespace(client=fake, flow=GitHubPublishFlow(fake, base_branch="main")),
            repo_full_name=REPO,
        )

    async def test_scanner_adopts_landed_commit_and_advances_run(
        self, gh_session_factory, monkeypatch
    ):
        """THE live case: the push LANDED while the worker was stalled, the
        journal never completed — the post-restart scanner ADOPTS the commit
        and the run advances to waiting_ci (not branch_drift → blocked)."""

        key = "scanadoptk123"
        fake = self._fake_with_landed_commit(key)
        intent = await self._seed_dispatched_intent(gh_session_factory, key=key)
        # The crash shape: the publish leg was in flight — the run sits
        # mid-publication when the scanner picks the stranded intent up.
        async with gh_session_factory() as session:
            run = await session.get(FlowRun, RUN_ID)
            run.status = FlowStatus.COMMITTING.value
            await session.commit()
        service = self._service(gh_session_factory, fake)

        resolved = await service.resolve_publication_intents()

        assert resolved == 1
        row = await intent_of(gh_session_factory, intent.id)
        assert row.status == "adopted"
        assert row.provider_object_id == "f" * 40
        async with gh_session_factory() as session:
            run = await session.get(FlowRun, RUN_ID)
        assert run.status == FlowStatus.WAITING_CI.value  # advanced, not blocked
        assert "f" * 40 in list(run.candidate_shas or [])

    async def test_scanner_resolves_superseded_run_as_duplicated(self, gh_session_factory):
        """R10 interplay: a cancelled run's landed commit is superseded
        evidence — the intent resolves duplicated, the run is NEVER revived."""
        key = "supersedk1234"
        fake = self._fake_with_landed_commit(key)
        intent = await self._seed_dispatched_intent(gh_session_factory, key=key)
        async with gh_session_factory() as session:
            run = await session.get(FlowRun, RUN_ID)
            run.cancel_requested = True
            await session.commit()
        service = self._service(gh_session_factory, fake)

        await service.resolve_publication_intents()

        row = await intent_of(gh_session_factory, intent.id)
        assert row.status == "duplicated"
        assert row.remote_result["reason"] == "run_superseded"
        async with gh_session_factory() as session:
            run = await session.get(FlowRun, RUN_ID)
        assert run.status == FlowStatus.ACCEPTED.value  # untouched — never revived

    async def test_scanner_unknown_blocks_run(self, gh_session_factory):
        key = "scanunknk123"
        fake = self._fake_with_landed_commit(key, twins=1)  # two marker matches
        intent = await self._seed_dispatched_intent(gh_session_factory, key=key)
        async with gh_session_factory() as session:
            run = await session.get(FlowRun, RUN_ID)
            run.status = FlowStatus.COMMITTING.value
            await session.commit()
        service = self._service(gh_session_factory, fake)

        await service.resolve_publication_intents()

        row = await intent_of(gh_session_factory, intent.id)
        assert row.status == "unknown"
        async with gh_session_factory() as session:
            run = await session.get(FlowRun, RUN_ID)
        assert run.status == FlowStatus.BLOCKED.value

    async def test_scanner_negative_probe_opens_certainty_window_without_posting(
        self, gh_session_factory
    ):
        """Nothing landed and the head intact (A12): the scanner does NOT
        dispatch (it holds no candidate) and does NOT call the outcome
        resolved — the negative probe parks the intent in the effect-
        certainty window for the window-end re-probe."""
        key = "scanredisk123"
        fake = FakeGitHub()
        fake.seed_repo(REPO, {"src/app.py": "print('hi')\n"})
        fake.heads[REPO][GBRANCH] = BASE_HEAD  # branch exists at the base
        intent = await self._seed_dispatched_intent(gh_session_factory, key=key)
        service = self._service(gh_session_factory, fake)

        resolved = await service.resolve_publication_intents()

        assert resolved == 0  # not resolved — intentionally left open
        row = await intent_of(gh_session_factory, intent.id)
        assert row.status == "probing"  # the A12 certainty window
        assert row.next_probe_at is not None  # the window-end re-probe
        assert fake.calls_of("create_commit_on_branch") == []

    async def test_scanner_exhausted_window_releases_cas_protected_redispatch(
        self, gh_session_factory
    ):
        """Window expired still-negative on GitHub: the release returns the
        intent to ``dispatched`` for the run's probe-first leg — the
        branch-wide CAS of createCommitOnBranch refuses any duplicate, so
        the redispatch is inherently safe. The scanner still never POSTs."""
        key = "scanrelek123"
        fake = FakeGitHub()
        fake.seed_repo(REPO, {"src/app.py": "print('hi')\n"})
        fake.heads[REPO][GBRANCH] = BASE_HEAD
        intent = await self._seed_dispatched_intent(gh_session_factory, key=key)
        service = self._service(gh_session_factory, fake)

        first = await service.resolve_publication_intents()
        assert first == 0
        await expire_settle_window(gh_session_factory, intent.id)
        for _ in range(2):  # rounds 1 and 2 extend the certainty window
            again = await service.resolve_publication_intents()
            assert again == 0
            await expire_settle_window(gh_session_factory, intent.id)

        resolved = await service.resolve_publication_intents()

        assert resolved == 0  # released, not resolved
        row = await intent_of(gh_session_factory, intent.id)
        assert row.status == "dispatched"  # the release: probing → dispatched
        assert row.next_probe_at is not None  # the scanner's probe backoff
        assert fake.calls_of("create_commit_on_branch") == []


# ---------------------------------------------------------------------------
# Azure DevOps: marker + stale-CAS adoption
# ---------------------------------------------------------------------------


class TestAzureAdoption:
    def test_changeset_to_commits_embeds_marker(self):
        from forge.repository import Change as RepoChange, Operation as RepoOperation

        cs = RepoChangeSet(
            branch="forge/42/abcd1234",
            commit_message="forge: implement 42",
            changes=[RepoChange(path="src/app.py", operation=RepoOperation.UPDATE, content="x")],
        )
        (commit,) = _changeset_to_commits(cs, operation_key="azureopk1234")
        assert commit.comment == "forge: implement 42 (forge-op:azureopk1234)"
        (bare,) = _changeset_to_commits(cs)
        assert bare.comment == "forge: implement 42"

    async def test_stale_cas_adopts_previous_push(self, session_factory):
        """staleOldObjectId → probe: the previous attempt's landed commit
        (marker + expected parent) is adopted, never re-pushed."""
        from tests.test_azure_runs import FakeAzureDevOps

        fake = FakeAzureDevOps()
        key = "azurestalek12"
        # Previous attempt landed with its response lost:
        fake.seed_commit(
            GBRANCH, "a" * 40, f"forge: implement 42 {op_marker(key)}", parents=[BASE_HEAD]
        )

        service = AzureRunService(
            session_factory,
            SimpleNamespace(),
            None,
            stack=SimpleNamespace(client=fake),
            repo_full_name="proj/repo",
        )
        outcome = await service._publish_changeset(
            RUN_ID,
            issue_number=GITHUB_ISSUE,
            changeset=gh_changeset("src/feature.py"),
            base_branch="main",
            expected_head=BASE_HEAD,
            operation_key=key,
        )

        assert outcome.ok is True
        assert outcome.adopted is True
        assert outcome.commit_oid == "a" * 40
        # Exactly ONE push attempt — the CAS-refused POST of this re-drive —
        # and it landed nothing (the adopted commit is the previous
        # attempt's). No duplicate push survived.
        assert len(fake.calls_of("push_commits")) == 1
        assert len(fake.commits[GBRANCH]) == 1


class TestA12DelayedApplyAzureCAS:
    def _service(self, session_factory, fake) -> AzureRunService:
        return AzureRunService(
            session_factory,
            SimpleNamespace(),
            None,
            stack=SimpleNamespace(client=fake),
            repo_full_name="proj/repo",
        )

    async def test_slow_first_push_cannot_double_the_redispatch(self, session_factory):
        """The first push is ACCEPTED but its application is delayed; the
        re-drive's negative probe sees the unchanged head and redispatches.
        The pushes API's branch-wide CAS (``oldObjectId``) makes both landing
        impossible — exactly one effect survives."""
        from tests.test_azure_runs import FakeAzureDevOps

        fake = FakeAzureDevOps()
        key = "azslowpk1234"
        service = self._service(session_factory, fake)

        fake.delayed_apply = True  # A accepted, application in flight
        first = await service._publish_changeset(
            RUN_ID,
            issue_number=GITHUB_ISSUE,
            changeset=gh_changeset("src/feature.py"),
            base_branch="main",
            expected_head=BASE_HEAD,
            operation_key=key,
        )
        assert first.ok is True
        assert fake.heads[GBRANCH] == BASE_HEAD, "accepted, not yet applied"
        assert len(fake.pending_pushes) == 1

        # The negative probe would see exactly this old head. The redispatch
        # is applied immediately (the CAS evaluates against the same head).
        fake.delayed_apply = False
        second = await service._publish_changeset(
            RUN_ID,
            issue_number=GITHUB_ISSUE,
            changeset=gh_changeset("src/other.py"),
            base_branch="main",
            expected_head=BASE_HEAD,
            operation_key=key,
        )
        assert second.ok is True
        assert fake.heads[GBRANCH] == second.commit_oid

        # The slow first push's application finally races in — its CAS
        # (expected BASE_HEAD, actual = the re-drive's commit) REFUSES it.
        fake.flush_delayed_pushes()
        assert fake.delayed_refused == 1
        assert len(fake.commits[GBRANCH]) == 1, "single effect — no duplicate"
        assert fake.heads[GBRANCH] == second.commit_oid

    async def test_late_landing_first_push_is_adopted_not_duplicated(self, session_factory):
        """The delayed application completes BEFORE the redispatch: the
        re-drive's CAS is refused (``staleObjectId`` — the head moved), the
        probe finds THIS intent's marker + parent and ADOPTS the
        late-landing push — one effect, no duplicate."""
        from tests.test_azure_runs import FakeAzureDevOps

        fake = FakeAzureDevOps()
        key = "azlatepk1234"
        service = self._service(session_factory, fake)

        fake.delayed_apply = True
        first = await service._publish_changeset(
            RUN_ID,
            issue_number=GITHUB_ISSUE,
            changeset=gh_changeset("src/feature.py"),
            base_branch="main",
            expected_head=BASE_HEAD,
            operation_key=key,
        )
        assert first.ok is True
        fake.flush_delayed_pushes()  # the slow application lands
        landed = fake.heads[GBRANCH]
        assert landed != BASE_HEAD
        assert op_marker(key) in fake.commits[GBRANCH][0]["comment"]

        fake.delayed_apply = False
        second = await service._publish_changeset(
            RUN_ID,
            issue_number=GITHUB_ISSUE,
            changeset=gh_changeset("src/other.py"),
            base_branch="main",
            expected_head=BASE_HEAD,
            operation_key=key,
        )

        assert second.ok is True
        assert second.adopted is True
        assert second.commit_oid == landed  # the previous attempt's push
        assert len(fake.commits[GBRANCH]) == 1  # one push, no duplicate


# ----------------------------------------------------------------------
# Idempotent completion: re-publishing an already-recorded effect
# ----------------------------------------------------------------------


class TestIdempotentCompletion:
    """A re-publication of an already-terminally-recorded effect is a
    no-op, not a conflict (LIVE-found 2026-09-20: a repair cycle re-pushed
    the candidate the probe had already ``adopted``; the direct push then
    tried ``committed`` over the terminal row, the raise aborted the
    publish step mid-transaction, and the run's PR reference was never
    journaled — which degraded the review to "(diff unavailable)")."""

    async def _adopted_intent(self, session_factory) -> PublicationIntent:
        intent = await TestA12SettleMachine._seed(
            self, session_factory, provider="github", status="dispatched"
        )
        async with session_factory() as session:
            await complete_intent(session, intent.id, "adopted", provider_object_id="b" * 40)
            await session.commit()
        return intent

    async def test_completing_the_same_effect_again_is_a_no_op(self, session_factory):
        intent = await self._adopted_intent(session_factory)

        async with session_factory() as session:
            settled = await complete_intent(
                session, intent.id, "committed", provider_object_id="b" * 40
            )
            await session.commit()

        assert settled.status == "adopted"  # terminal row untouched
        assert settled.provider_object_id == "b" * 40

    async def test_completing_a_different_effect_still_raises(self, session_factory):
        intent = await self._adopted_intent(session_factory)

        with pytest.raises(InvalidIntentTransition):
            async with session_factory() as session:
                await complete_intent(session, intent.id, "committed", provider_object_id="c" * 40)
