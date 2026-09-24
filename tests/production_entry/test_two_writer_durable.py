"""R36-18 / #277 — one two-writer change through DURABLE provider
publication and recovery, over REAL PostgreSQL.

The production-entry discipline applied to the two-writer change: every
collaborator is real — the coordinators are the SHIPPED
``SagaCoordinator``/``WorkPackageCoordinator`` entries, the durable
state is a REAL database (real PostgreSQL under ``FORGE_PG_TEST_URL``,
an aiosqlite file otherwise), a "restarted process" is a genuinely
fresh engine/session factory over the same rows, and the remote is the
provider-shaped :class:`NativeShapedRemote` whose duplicate behavior is
NATIVE (no marker dedup — safety must come from probe-first recovery).

The heavy proofs are PostgreSQL-gated (the FI convention) because they
are the issue's negative-test core — separate processes, real durable
rows, a crash after every pre-effect intent and provider response:

- **the kill matrix**: death at the store's COMMIT BOUNDARY after every
  saga step of BOTH writers (the real coordinator's own saves — fence
  check and commit intent share one save there, ``prepare`` is the
  pass's first save). Every cell must restart-and-converge with exactly
  ONE logical review per writer, no destructive history rewrite, and an
  inert third pass.
- **the post-pivot matrix**: a human MERGED the producer's PR and EDITED
  the consumer's branch while the coordinator was dead. Recovery is
  forward-only: the merged publication stands; the collision PARKS for a
  human decision inside the open effect window and completes forward
  from the already-verified rungs — never a rollback.
- **lost response**: adopted only through ESTABLISHED native
  correlation (the listed branch history carries the marker and the
  expected parent); an effect the surface cannot prove STAYS unknown
  across two recovery passes while the consumer stays gated.
- **durable-state crash recovery**: killed between coordinator saves,
  the persisted saga is EXACTLY the last committed boundary, and a
  fresh process converges from it.

One dual-backend trace keeps the layer alive in the plain suite: the
full two-writer drive over whatever durable backend ``pe_db`` provides.
"""

from __future__ import annotations

import os

import pytest

from forge.adaptive.saga_durable import (
    DurablePublicationEntry,
    NativeShapedRemote,
    ProcessDied,
    kill_at_boundary,
)
from forge.adaptive.two_writer_qualification import PUBLICATION_STEPS, default_scenario
from forge.adaptive.workpackage import read_workpackage_state

pytestmark = pytest.mark.production_entry

SCENARIO = default_scenario()
PRODUCER = SCENARIO.producer()
CONSUMER = SCENARIO.consumer()

#: The pivot arms that die INSIDE the consumer's open effect window —
#: the human edit there must PARK (a decision is owed), while deaths on
#: the already-verified rungs complete FORWARD (the effect was verified
#: before the edit; the review simply carries it).
_EFFECT_WINDOW = frozenset({"fence_check", "commit_intent", "provider_commit"})
_FORWARD = frozenset({"verify", "record"})

_REQUIRE_PG = pytest.mark.skipif(
    not os.environ.get("FORGE_PG_TEST_URL"),
    reason=(
        "FORGE_PG_TEST_URL not set — the R36-18 two-writer crash matrix runs only "
        "against a disposable real Postgres (separate processes, durable rows)"
    ),
)


def _seeded_remote() -> NativeShapedRemote:
    remote = NativeShapedRemote()
    for repo in SCENARIO.writer_repos():
        remote.seed(repo.repository_id, repo.branch, repo.base_oid)
    return remote


def _histories(remote: NativeShapedRemote) -> dict[str, tuple[str, ...]]:
    return {
        repo.repository_id: remote.branch_history(repo.repository_id, repo.branch)
        for repo in SCENARIO.writer_repos()
    }


async def _drive_one_pass(pe_db, remote, **entry_kwargs) -> DurablePublicationEntry:
    """One 'process': a fresh engine over the durable rows, one entry."""
    entry = DurablePublicationEntry(pe_db.worker_factory(), SCENARIO, remote=remote, **entry_kwargs)
    await entry.start()
    await entry.drive()
    return entry


# ---------------------------------------------------------------------------
# The dual-backend trace: the layer runs everywhere, PostgreSQL included.
# ---------------------------------------------------------------------------


class TestTwoWriterDurableDrive:
    async def test_the_full_change_publishes_and_completes_over_pe_db(self, pe_db):
        remote = _seeded_remote()
        entry = DurablePublicationEntry(pe_db.worker_factory(), SCENARIO, remote=remote)
        state = await entry.start()
        assert state["phases"] == [["pinned-baseline", "producer"], ["consumer"]]
        report = await entry.drive()
        saga = await entry._saga_state()
        assert report.package_state == "complete"
        assert str(saga.status) == "complete"
        for repo in SCENARIO.writer_repos():
            assert (
                len(remote.commits_carrying(repo.repository_id, repo.branch, saga.commit_marker))
                == 1
            )
            assert remote.merge_request_creates(repo.repository_id) == 1
        # the consumer child launched only after the producer's recorded outcome
        assert [launch["item_id"] for launch in entry.child_launches] == [
            SCENARIO.PRODUCER_ITEM,
            SCENARIO.CONSUMER_ITEM,
        ]


# ---------------------------------------------------------------------------
# PG-gated: the kill matrix — every saga step × both writers.
# ---------------------------------------------------------------------------


@_REQUIRE_PG
class TestKillMatrixOverRealPostgres:
    @pytest.mark.parametrize("step", PUBLICATION_STEPS)
    @pytest.mark.parametrize("repository_kind", ["producer", "consumer"])
    async def test_death_after_every_step_restarts_and_converges(
        self, pe_db, repository_kind, step
    ):
        target = PRODUCER if repository_kind == "producer" else CONSUMER
        remote = _seeded_remote()

        # process 1: the pre-crash coordinator, dying at its own save boundary
        entry_a = DurablePublicationEntry(
            pe_db.worker_factory(),
            SCENARIO,
            remote=remote,
            on_boundary=kill_at_boundary(target.repository_id, step),
        )
        await entry_a.start()
        with pytest.raises(ProcessDied):
            await entry_a.drive()
        histories_at_death = _histories(remote)

        # process 2: a genuinely fresh engine over the same rows — recovery
        entry_b = DurablePublicationEntry(pe_db.worker_factory(), SCENARIO, remote=remote)
        report = await entry_b.drive()
        saga = await entry_b._saga_state()
        assert report.package_state == "complete", report.refusal
        assert str(saga.status) == "complete"

        for repo in SCENARIO.writer_repos():
            landed = remote.commits_carrying(repo.repository_id, repo.branch, saga.commit_marker)
            assert len(landed) == 1, f"{repo.repository_id}: duplicate logical effect"
            assert landed[0].author == "forge-bot"
            assert remote.merge_request_creates(repo.repository_id) == 1
        assert remote.destructive_operations() == []
        now = _histories(remote)
        for repository_id, was in histories_at_death.items():
            assert now[repository_id][: len(was)] == was  # prefix-preserved histories
        # no duplicate MODEL work either: across the dead process and the
        # recovering one, each child launched exactly once — and the durable
        # record proves the consumer's launch came after the producer's
        # recorded outcome (the coordinator's own ordering).
        launched = [
            launch["item_id"] for launch in (*entry_a.child_launches, *entry_b.child_launches)
        ]
        assert launched.count(SCENARIO.PRODUCER_ITEM) == 1
        assert launched.count(SCENARIO.CONSUMER_ITEM) == 1
        factory = pe_db.worker_factory()
        state = await read_workpackage_state(factory, entry_b.parent_run_id)
        children = state["children"]
        assert children[SCENARIO.CONSUMER_ITEM]["intent_status"] == "launched"
        assert children[SCENARIO.PRODUCER_ITEM]["outcome"]["status"] == "succeeded"
        trail = await entry_b.outbox_events()
        consumer_intent = next(
            index
            for index, event in enumerate(trail)
            if event["event_type"] == "workpackage.child_intent"
            and event["payload"]["item_id"] == SCENARIO.CONSUMER_ITEM
        )
        producer_outcome = next(
            index
            for index, event in enumerate(trail)
            if event["event_type"] == "workpackage.outcome_recorded"
            and event["payload"]["item_id"] == SCENARIO.PRODUCER_ITEM
        )
        assert producer_outcome < consumer_intent

        # process 3: an inert third pass — no work, no effects, no outbox rows
        journal_size = len(remote.journal)
        digest = saga.steps_digest
        events = await entry_b.outbox_events()
        entry_c = DurablePublicationEntry(pe_db.worker_factory(), SCENARIO, remote=remote)
        await entry_c.drive()
        await entry_c.drive()
        assert len(remote.journal) == journal_size
        assert entry_c.child_launches == ()
        assert (await entry_c._saga_state()).steps_digest == digest
        assert await entry_c.outbox_events() == events


# ---------------------------------------------------------------------------
# PG-gated: the post-pivot matrix — human merge + colliding human edit.
# ---------------------------------------------------------------------------


@_REQUIRE_PG
class TestPivotMatrixOverRealPostgres:
    @pytest.mark.parametrize("step", tuple(_EFFECT_WINDOW | _FORWARD))
    async def test_recovery_past_the_pivot_is_forward_only(self, pe_db, step):
        remote = _seeded_remote()

        entry_a = DurablePublicationEntry(
            pe_db.worker_factory(),
            SCENARIO,
            remote=remote,
            on_boundary=kill_at_boundary(CONSUMER.repository_id, step),
        )
        await entry_a.start()
        with pytest.raises(ProcessDied):
            await entry_a.drive()
        histories_at_death = _histories(remote)

        # while the coordinator is dead: the human MERGES the producer's PR
        # and EDITS the consumer's branch — the pivot and the collision.
        remote.human_merge(PRODUCER.repository_id, PRODUCER.branch)
        remote.human_commit(CONSUMER.repository_id, CONSUMER.branch)

        entry_b = DurablePublicationEntry(pe_db.worker_factory(), SCENARIO, remote=remote)
        await entry_b.observe_merge(PRODUCER.repository_id)  # the OBSERVED merge
        report = await entry_b.drive()
        saga = await entry_b._saga_state()
        assert saga.repo(PRODUCER.repository_id).status == "human_merged"  # it STANDS
        consumer_status = saga.repo(CONSUMER.repository_id).status
        if step in _EFFECT_WINDOW:
            assert consumer_status == "parked_human"  # a decision is owed
            assert report.package_state != "complete"
            events = [event["event_type"] for event in await entry_b.outbox_events()]
            assert "human_edit.conflicts" in events
            partial = await entry_b.partial_publication()
            assert [effect.repository_id for effect in partial.standing] == [PRODUCER.repository_id]
            assert [effect.repository_id for effect in partial.outstanding] == [
                CONSUMER.repository_id
            ]
        else:
            assert consumer_status in {"ready_for_review", "human_merged"}
            assert report.package_state == "complete"  # forward from decided rungs
        # whatever the outcome: the histories only grew, nothing was rewritten
        now = _histories(remote)
        for repository_id, was in histories_at_death.items():
            assert now[repository_id][: len(was)] == was
        assert remote.destructive_operations() == []
        # the merged publication is never rolled back or duplicated
        assert (
            len(
                remote.commits_carrying(PRODUCER.repository_id, PRODUCER.branch, saga.commit_marker)
            )
            == 1
        )


# ---------------------------------------------------------------------------
# PG-gated: lost responses — adoption by evidence, unknown stays unknown.
# ---------------------------------------------------------------------------


@_REQUIRE_PG
class TestLostResponseOverRealPostgres:
    async def test_a_lost_commit_response_is_adopted_through_native_correlation(self, pe_db):
        remote = _seeded_remote()
        entry_a = DurablePublicationEntry(pe_db.worker_factory(), SCENARIO, remote=remote)
        await entry_a.start()
        remote.lose_commit_response = {PRODUCER.repository_id}
        report = await entry_a.drive()  # the drive reconciles within its passes
        saga = await entry_a._saga_state()
        assert report.package_state == "complete"
        repo = saga.repo(PRODUCER.repository_id)
        assert repo.adopted is True
        assert remote.commit_calls[PRODUCER.repository_id] == 1  # never re-published
        landed = remote.commits_carrying(
            PRODUCER.repository_id, PRODUCER.branch, saga.commit_marker
        )
        assert len(landed) == 1
        # the adoption is ESTABLISHED by native correlation: the listed
        # commit carries the marker AND its parent is the expected base.
        assert saga.commit_marker in landed[0].message
        assert landed[0].parent == PRODUCER.base_oid
        events = [event["event_type"] for event in await entry_a.outbox_events()]
        assert "saga.unknown_effects" in events
        assert "recovery.native_adoption" in events

    async def test_an_unprovable_effect_stays_unknown_across_two_passes(self, pe_db):
        remote = _seeded_remote()
        entry_a = DurablePublicationEntry(
            pe_db.worker_factory(),
            SCENARIO,
            remote=remote,
            on_boundary=kill_at_boundary(PRODUCER.repository_id, "outcome_unknown"),
        )
        await entry_a.start()
        remote.lose_commit_response = {PRODUCER.repository_id}
        with pytest.raises(ProcessDied):
            await entry_a.drive()

        remote.lose_commit_response = set()
        remote.unavailable = {PRODUCER.repository_id}  # the surface proves nothing
        entry_b = None
        for _pass in range(2):
            entry_b = DurablePublicationEntry(pe_db.worker_factory(), SCENARIO, remote=remote)
            await entry_b.drive()
            saga = await entry_b._saga_state()
            assert saga.repo(PRODUCER.repository_id).status == "outcome_unknown"
            assert remote.commit_calls[PRODUCER.repository_id] == 1  # no blind retry
            assert remote.effects_for(CONSUMER.repository_id) == []  # gated, not started
        assert [effect.repository_id for effect in await entry_b.unknown_effects()] == [
            PRODUCER.repository_id
        ]
        events = [event["event_type"] for event in await entry_b.outbox_events()]
        assert "saga.unknown_effects" in events

        remote.unavailable = set()  # the provider heals — and ONLY now resolves
        entry_c = DurablePublicationEntry(pe_db.worker_factory(), SCENARIO, remote=remote)
        report = await entry_c.drive()
        saga = await entry_c._saga_state()
        assert report.package_state == "complete"
        assert saga.repo(PRODUCER.repository_id).adopted is True
        assert (
            len(
                remote.commits_carrying(PRODUCER.repository_id, PRODUCER.branch, saga.commit_marker)
            )
            == 1
        )


# ---------------------------------------------------------------------------
# PG-gated: durable-state crash recovery — the state at death IS the boundary.
# ---------------------------------------------------------------------------


@_REQUIRE_PG
class TestCrashBetweenSavesOverRealPostgres:
    async def test_the_persisted_state_is_exactly_the_last_committed_boundary(self, pe_db):
        remote = _seeded_remote()
        entry_a = DurablePublicationEntry(
            pe_db.worker_factory(),
            SCENARIO,
            remote=remote,
            on_boundary=kill_at_boundary(PRODUCER.repository_id, "commit_intent"),
        )
        await entry_a.start()
        with pytest.raises(ProcessDied):
            await entry_a.drive()
        saga_at_death = await entry_a._saga_state()
        # write-ahead held: the intent is durable, the effect never happened
        assert saga_at_death.repo(PRODUCER.repository_id).status == "intent_recorded"
        assert remote.effects_for(PRODUCER.repository_id) == []
        assert saga_at_death.repo(CONSUMER.repository_id).status == "preparing"

        entry_b = DurablePublicationEntry(pe_db.worker_factory(), SCENARIO, remote=remote)
        report = await entry_b.drive()
        saga = await entry_b._saga_state()
        assert report.package_state == "complete" and str(saga.status) == "complete"
        for repo in SCENARIO.writer_repos():
            assert (
                len(remote.commits_carrying(repo.repository_id, repo.branch, saga.commit_marker))
                == 1
            )
            assert remote.merge_request_creates(repo.repository_id) == 1
