"""R36-18 / #277 — durable two-writer publication over provider-shaped
native semantics.

The pins, in the order the issue states them:

- **the store is durable for real**: the saga document survives the
  process that wrote it (a FRESH engine over the same rows loads it),
  one publication saga per parent run, and every observability
  transition lands as an outbox row in the SAME transaction;
- **the remote's duplicate behavior is NATIVE**: a repeated identical
  commit creates a SECOND commit (the marker is a message trailer, not
  an idempotency key), merge requests are idempotent only by the
  provider-native key (repository, source branch), the refs surface
  422s on a moved head, protected branches refuse direct commits, and
  every effect is journaled with its native identity;
- **the consumer is gated on PERSISTED outcomes**: while the producer is
  silent, failed, or proven only from a foreign tested world, the
  consumer never launches and its publication cannot even persist an
  intent (the store's admission guard);
- **crash + lost-response discipline**: dying at the store's commit
  boundary and restarting converges with exactly ONE logical review per
  writer; a lost response is adopted only through native correlation
  (the listed branch history carries the marker); an effect the surface
  cannot prove STAYS unknown across recovery passes (fail closed); a
  human edit is preserved and parked, never force-overwritten; a moved
  head under the publication's CAS pin is a 422 policy outcome;
- **read-only dependencies are structurally untouched**: no remote
  effect ever names them, no writer credential is ever staged into
  their lanes;
- **a converged package is inert**: further recovery passes spend no
  model work (no child launches), no remote effects, no outbox rows.

The FULL kill matrix (every saga step × both writers) and the
post-pivot matrix run against real PostgreSQL in
``tests/production_entry/test_two_writer_durable.py``; here a
representative boundary proves the same discipline on aiosqlite, plus
the real-PostgreSQL class at the bottom (skipped without
``FORGE_PG_TEST_URL``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import forge.durable.models  # noqa: F401 — the tables create_all needs
from forge.adaptive.publication_saga import (
    ProviderRejectedError,
    ProviderUnavailableError,
    begin_saga,
    outcome_unknown,
    park_for_human,
    provider_committed,
    record_commit_intent,
    review_opened,
    verified,
)
from forge.adaptive.saga_durable import (
    OUTCOME_OF_PUBLICATION_STATUS,
    DurablePublicationEntry,
    NativeShapedRemote,
    PostgresSagaStore,
    ProcessDied,
    SagaDurabilityError,
    PhaseAdmissionRefused,
    journal_tail,
    kill_at_boundary,
    saga_from_document,
    saga_to_document,
)
from forge.adaptive.two_writer_qualification import (
    default_scenario,
    lane_assignment,
    verify_credential_scope,
)
from forge.durable import FlowRun, Outbox
from forge.models.base import Base

SCENARIO = default_scenario()
PRODUCER = SCENARIO.producer().repository_id
CONSUMER = SCENARIO.consumer().repository_id
PINNED = SCENARIO.pinned().repository_id
NEIGHBOR = SCENARIO.neighbor().repository_id
WORLD = SCENARIO.freeze().tested_world_digest or ""


# ---------------------------------------------------------------------------
# Fixtures: one aiosqlite FILE = one durable database; a fresh engine is a
# fresh "process" (the production-entry convention).
# ---------------------------------------------------------------------------

#: Engines minted by ``_process``/``_fresh_worker`` during the CURRENT
#: test, drained by the autouse disposer below (R37-18): the inline
#: ``await engine.dispose()`` calls carry the process-death semantics on
#: the happy path, but a test dying mid-assertion must not leave its
#: aiosqlite worker thread to finalize against the CLOSED loop — the
#: leak that smeared "Event loop is closed" tracebacks across whatever
#: test the GC happened to interrupt.
_pending_engines: list[AsyncEngine] = []


@pytest.fixture(autouse=True)
async def _dispose_every_owned_engine():
    """Dispose, in a finally block, every engine this test minted.

    ``AsyncEngine.dispose()`` is idempotent — the inline process-death
    disposes keep their meaning; this fixture only guarantees the ones
    a failing test never reached.
    """
    try:
        yield
    finally:
        while _pending_engines:
            await _pending_engines.pop().dispose()


async def _process(
    db_path: Path,
) -> tuple[async_sessionmaker[AsyncSession], AsyncEngine]:
    """A fresh engine + session factory over the file — one 'process'."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    _pending_engines.append(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False), engine


async def _seed_parent(factory: async_sessionmaker[AsyncSession], run_id: str) -> None:
    async with factory() as session:
        session.add(FlowRun(id=run_id, project_id=99, status="planning"))
        await session.commit()


def _seeded_remote() -> NativeShapedRemote:
    remote = NativeShapedRemote()
    for repo in SCENARIO.writer_repos():
        remote.seed(repo.repository_id, repo.branch, repo.base_oid)
    return remote


# ---------------------------------------------------------------------------
# The persisted saga document.
# ---------------------------------------------------------------------------


class TestSagaDocument:
    async def test_round_trip_preserves_state_and_journal_digest(self):
        saga = begin_saga(
            "wp-1",
            publication_epoch=3,
            candidate_digest="d" * 64,
            repo_plans={"repo-a": ("main", "base-a"), "repo-b": ("main", "base-b")},
        )
        saga = record_commit_intent(saga, "repo-a")
        saga = provider_committed(saga, "repo-a", "head-a-1")
        saga = verified(saga, "repo-a")
        saga = review_opened(saga, "repo-a", "https://example.test/repo-a/mr/1")
        saga = park_for_human(saga, "repo-b", "a human edit stands")
        rebuilt = saga_from_document(saga_to_document(saga))
        assert rebuilt == saga
        assert rebuilt.steps_digest == saga.steps_digest
        assert journal_tail(rebuilt) == ("repo-b", "park")

    async def test_the_document_is_plain_json(self):
        saga = begin_saga(
            "wp-1", publication_epoch=1, candidate_digest="d" * 64, repo_plans={"r": ("b", "o")}
        )
        import json

        document = saga_to_document(saga)
        encoded = json.dumps(document)  # JSON-safe end to end
        assert saga_from_document(json.loads(encoded)) == saga


# ---------------------------------------------------------------------------
# The durable store.
# ---------------------------------------------------------------------------


class TestPostgresSagaStore:
    async def test_a_fresh_process_loads_what_the_dead_one_saved(self, tmp_path):
        factory_a, engine_a = await _process(tmp_path / "saga.db")
        await _seed_parent(factory_a, "run-1")
        store_a = PostgresSagaStore(factory_a, parent_run_id="run-1")
        saga = begin_saga(
            "wp-1", publication_epoch=2, candidate_digest="d" * 64, repo_plans={"r": ("b", "o")}
        )
        saga = record_commit_intent(saga, "r")
        await store_a.save(saga)
        await engine_a.dispose()  # the process dies

        factory_b, engine_b = await _process(tmp_path / "saga.db")  # no create_all harm
        store_b = PostgresSagaStore(factory_b, parent_run_id="run-1")
        loaded = await store_b.load(saga.saga_id)
        assert loaded == saga
        await engine_b.dispose()

    async def test_a_missing_parent_run_refuses_loudly(self, tmp_path):
        factory, engine = await _process(tmp_path / "saga.db")
        store = PostgresSagaStore(factory, parent_run_id="run-none")
        saga = begin_saga(
            "wp-1", publication_epoch=1, candidate_digest="d" * 64, repo_plans={"r": ("b", "o")}
        )
        with pytest.raises(SagaDurabilityError, match="not found"):
            await store.save(saga)
        with pytest.raises(SagaDurabilityError, match="not found"):
            await store.load(saga.saga_id)
        await engine.dispose()

    async def test_one_publication_saga_per_parent_run(self, tmp_path):
        factory, engine = await _process(tmp_path / "saga.db")
        await _seed_parent(factory, "run-1")
        store = PostgresSagaStore(factory, parent_run_id="run-1")
        first = begin_saga(
            "wp-1", publication_epoch=1, candidate_digest="d" * 64, repo_plans={"r": ("b", "o")}
        )
        await store.save(first)
        second = begin_saga(
            "wp-1", publication_epoch=1, candidate_digest="e" * 64, repo_plans={"r": ("b", "o")}
        )
        with pytest.raises(SagaDurabilityError, match="one publication saga per parent run"):
            await store.save(second)
        with pytest.raises(SagaDurabilityError, match="not"):
            await store.load(second.saga_id)
        await engine.dispose()

    async def test_transitions_land_outbox_rows_in_the_same_transaction(self, tmp_path):
        factory, engine = await _process(tmp_path / "saga.db")
        await _seed_parent(factory, "run-1")
        store = PostgresSagaStore(factory, parent_run_id="run-1")
        saga = begin_saga(
            "wp-1",
            publication_epoch=1,
            candidate_digest="d" * 64,
            repo_plans={"repo-a": ("main", "base-a"), "repo-b": ("main", "base-b")},
        )
        saga = record_commit_intent(saga, "repo-a")
        await store.save(saga)
        parked = park_for_human(saga, "repo-b", "a human edit stands")
        await store.save(parked)
        unknown = outcome_unknown(parked, "repo-a", "commit response lost")
        await store.save(unknown)
        adopted = provider_committed(unknown, "repo-a", "head-a-9", adopted=True)
        await store.save(adopted)
        verified_saga = verified(adopted, "repo-a")
        await store.save(verified_saga)
        recorded = review_opened(verified_saga, "repo-a", "https://example.test/mr/1")
        await store.save(recorded)

        async with factory() as session:
            events = (
                (
                    await session.execute(
                        select(Outbox).where(Outbox.flow_run_id == "run-1").order_by(Outbox.id)
                    )
                )
                .scalars()
                .all()
            )
        kinds = [row.event_type for row in events]
        assert "saga.unknown_effects" in kinds
        assert "recovery.native_adoption" in kinds
        assert "human_edit.conflicts" in kinds
        assert kinds.count("workpackage.partial_publication") >= 1
        adoption = next(row for row in events if row.event_type == "recovery.native_adoption")
        assert adoption.payload["repository_id"] == "repo-a"
        assert adoption.payload["correlation"] == (
            "commit-message marker over the listed branch history"
        )
        partial = next(row for row in events if row.event_type == "workpackage.partial_publication")
        assert {effect["repository_id"] for effect in partial.payload["outstanding"]} == {"repo-b"}
        await engine.dispose()


# ---------------------------------------------------------------------------
# The native-shaped remote: duplicate behavior IS the provider's.
# ---------------------------------------------------------------------------


class TestNativeShapedRemote:
    async def test_the_reference_remote_satisfies_the_effect_interface(self):
        """#295 adapter-compat: the in-process reference implements the
        effect Protocol the native adapters (``saga_native``) also satisfy —
        one seam, reference and live spellings."""
        from forge.adaptive.publication_saga import PublicationProvider
        from forge.adaptive.saga_native import SagaEffectSurface

        remote = NativeShapedRemote()
        assert isinstance(remote, SagaEffectSurface)
        assert isinstance(remote, PublicationProvider)

    async def test_a_repeated_identical_commit_creates_a_second_commit(self):
        remote = _seeded_remote()
        first = await remote.commit(PRODUCER, "main", "forge-saga:m1")
        second = await remote.commit(PRODUCER, "main", "forge-saga:m1")
        assert first != second  # NO marker dedup — the provider is not an idempotency key
        assert len(remote.commits_carrying(PRODUCER, "main", "forge-saga:m1")) == 2

    async def test_the_marker_is_a_message_trailer_the_listing_finds(self):
        remote = _seeded_remote()
        await remote.commit(PRODUCER, "main", "forge-saga:m1")
        assert await remote.head_carries_marker(PRODUCER, "main", "forge-saga:m1")
        assert not await remote.head_carries_marker(PRODUCER, "main", "forge-saga:other")

    async def test_update_ref_is_cas_protected(self):
        remote = _seeded_remote()
        sha = await remote.commit(PRODUCER, "main", "forge-saga:m1")
        remote.update_ref(PRODUCER, "main", sha, expected_head=sha)
        remote.human_commit(PRODUCER, "main")
        with pytest.raises(ProviderRejectedError, match="422"):
            remote.update_ref(
                PRODUCER, "main", sha, expected_head=sha
            )  # the head moved — the refs API refuses

    async def test_a_pinned_head_makes_the_commit_itself_refuse_422(self):
        remote = _seeded_remote()
        base = SCENARIO.producer().base_oid
        remote.pin_expected_head(PRODUCER, "main", base)
        remote.human_commit(PRODUCER, "main")  # the branch moved under the pin
        with pytest.raises(ProviderRejectedError, match="422.*expected head"):
            await remote.commit(PRODUCER, "main", "forge-saga:m1")
        assert not remote.effects_for(PRODUCER) or all(
            entry["author"] == "human" for entry in remote.effects_for(PRODUCER)
        )  # the refusal left nothing of ours behind

    async def test_protected_branches_refuse_direct_commits(self):
        remote = _seeded_remote()
        remote.protect_branch(PRODUCER, "main")
        with pytest.raises(ProviderRejectedError, match="protected"):
            await remote.commit(PRODUCER, "main", "forge-saga:m1")

    async def test_merge_requests_are_idempotent_by_source_branch_only(self):
        remote = _seeded_remote()
        first = await remote.open_review(PRODUCER, "feat/x", "forge-saga:m1")
        again = await remote.open_review(PRODUCER, "feat/x", "forge-saga:m1-different-marker")
        assert first == again  # the provider-native key (repo, source branch) governs
        assert remote.merge_request_creates(PRODUCER) == 1
        assert remote.review_calls[PRODUCER] == 2
        other = await remote.open_review(PRODUCER, "feat/y", "forge-saga:m1")
        assert other != first
        assert remote.merge_request_creates(PRODUCER) == 2

    async def test_every_effect_is_journaled_with_its_native_identity(self):
        remote = _seeded_remote()
        sha = await remote.commit(PRODUCER, "main", "forge-saga:m1")
        url = await remote.open_review(PRODUCER, "main", "forge-saga:m1")
        commit_entry = remote.effects_for(PRODUCER)[0]
        assert commit_entry["op"] == "create_commit"
        assert commit_entry["sha"] == sha
        assert commit_entry["parent"] == SCENARIO.producer().base_oid
        assert commit_entry["author"] == "forge-bot"
        mr_entry = next(entry for entry in remote.journal if entry["op"] == "create_merge_request")
        assert mr_entry["url"] == url
        assert mr_entry["iid"]
        human = remote.human_commit(PRODUCER, "main")
        human_entry = remote.effects_for(PRODUCER)[-1]
        assert human_entry["sha"] == human
        assert human_entry["author"] == "human"  # authorship separates people from the bot

    async def test_unavailability_raises_before_any_effect(self):
        remote = _seeded_remote()
        remote.unavailable = {PRODUCER}
        with pytest.raises(ProviderUnavailableError):
            await remote.commit(PRODUCER, "main", "forge-saga:m1")
        assert remote.effects_for(PRODUCER) == []

    async def test_a_lost_response_lands_the_effect_and_kills_the_answer(self):
        remote = _seeded_remote()
        remote.lose_commit_response = {PRODUCER}
        with pytest.raises(TimeoutError):
            await remote.commit(PRODUCER, "main", "forge-saga:m1")
        assert len(remote.commits_carrying(PRODUCER, "main", "forge-saga:m1")) == 1

    async def test_the_surface_cannot_express_destruction(self):
        remote = _seeded_remote()
        await remote.commit(PRODUCER, "main", "forge-saga:m1")
        remote.human_commit(PRODUCER, "main")
        assert remote.destructive_operations() == []
        assert remote.branch_history(PRODUCER, "main")[0] == SCENARIO.producer().base_oid


# ---------------------------------------------------------------------------
# The commit-boundary kill seam.
# ---------------------------------------------------------------------------


class TestKillAtBoundary:
    async def test_prepare_dies_at_the_first_save_and_fence_check_maps_to_the_intent_save(
        self,
    ):
        saga = begin_saga(
            "wp-1",
            publication_epoch=1,
            candidate_digest="d" * 64,
            repo_plans={"repo-a": ("main", "base-a"), "repo-b": ("main", "base-b")},
        )
        prepare_killer = kill_at_boundary("repo-a", "prepare")
        with pytest.raises(ProcessDied, match="prepare"):
            await prepare_killer(saga)  # save #1 — the begun intents

        intended = record_commit_intent(saga, "repo-a")
        fence_killer = kill_at_boundary("repo-a", "fence_check")
        with pytest.raises(ProcessDied, match="commit_intent"):
            await fence_killer(intended)  # the REAL coordinator's batched intent save
        # a boundary that has not been reached does not fire
        await kill_at_boundary("repo-a", "verify")(intended)

    async def test_step_deaths_fire_on_their_own_save(self):
        saga = begin_saga(
            "wp-1", publication_epoch=1, candidate_digest="d" * 64, repo_plans={"r": ("b", "o")}
        )
        saga = record_commit_intent(saga, "r")
        committed = provider_committed(saga, "r", "head-1")
        with pytest.raises(ProcessDied, match="provider_commit of r"):
            await kill_at_boundary("r", "provider_commit")(committed)
        recorded = review_opened(verified(committed, "r"), "r", "u")
        with pytest.raises(ProcessDied, match="record of r"):
            await kill_at_boundary("r", "record")(recorded)


# ---------------------------------------------------------------------------
# The durable entry: gating, crash, lost response, policy outcomes.
# ---------------------------------------------------------------------------


class TestAdmissionVocabulary:
    def test_undecided_statuses_prove_nothing(self):
        assert dict(OUTCOME_OF_PUBLICATION_STATUS) == {
            "ready_for_review": "succeeded",
            "human_merged": "succeeded",
            "failed": "failed",
        }
        for undecided in (
            "preparing",
            "intent_recorded",
            "committed",
            "verified",
            "outcome_unknown",
            "parked_human",
            "superseded",
        ):
            assert undecided not in OUTCOME_OF_PUBLICATION_STATUS

    def test_the_guard_refuses_an_unadmitted_climb_but_allows_the_plan(self):
        saga = begin_saga(
            "wp-1",
            publication_epoch=1,
            candidate_digest="d" * 64,
            repo_plans={"repo-a": ("main", "base-a"), "repo-b": ("main", "base-b")},
        )
        # the PLAN (begin's prepare entries, statuses preparing) is persistable
        DurablePublicationEntry._assert_admission(frozenset({"repo-a"}), saga)
        intended = record_commit_intent(saga, "repo-b")
        with pytest.raises(PhaseAdmissionRefused, match="repo-b"):
            DurablePublicationEntry._assert_admission(frozenset({"repo-a"}), intended)
        # an admitted repository's climb is unconstrained
        DurablePublicationEntry._assert_admission(frozenset({"repo-a", "repo-b"}), intended)


class TestDurableHappyPath:
    async def test_both_writers_publish_exactly_once_and_the_package_completes(self, tmp_path):
        factory, engine = await _process(tmp_path / "tw.db")
        remote = _seeded_remote()
        entry = DurablePublicationEntry(factory, SCENARIO, remote=remote)
        await entry.start()
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
        # the consumer's child launched only AFTER the producer's recorded outcome
        events = [event["event_type"] for event in await entry.outbox_events()]
        assert events.index("workpackage.outcome_recorded") < events.index(
            "workpackage.child_intent"
        ) or [launch["item_id"] for launch in entry.child_launches] == [
            SCENARIO.PRODUCER_ITEM,
            SCENARIO.CONSUMER_ITEM,
        ]
        await engine.dispose()


class TestConsumerGating:
    async def test_a_silent_producer_blocks_the_consumer(self, tmp_path):
        factory, engine = await _process(tmp_path / "tw.db")
        remote = _seeded_remote()
        entry = DurablePublicationEntry(factory, SCENARIO, remote=remote)
        await entry.start()  # phase 0 dispatches the producer child only
        await engine.dispose()

        factory_b, engine_b = await _process(tmp_path / "tw.db")
        entry_b = DurablePublicationEntry(factory_b, SCENARIO, remote=remote)
        from forge.adaptive.workpackage import PhaseAdvanceRefused, WorkPackageCoordinator

        coordinator = WorkPackageCoordinator(factory_b, entry_b._dispatch_child)
        with pytest.raises(PhaseAdvanceRefused) as refusal:
            await coordinator.advance(entry_b.parent_run_id, SCENARIO.package())
        assert refusal.value.awaiting == (SCENARIO.PRODUCER_ITEM,)
        assert [launch["item_id"] for launch in entry_b.child_launches] == []
        assert remote.effects_for(CONSUMER) == []  # invocation-completion is not proof
        await engine_b.dispose()

    async def test_an_outcome_from_a_foreign_tested_world_proves_nothing(self, tmp_path):
        factory, engine = await _process(tmp_path / "tw.db")
        remote = _seeded_remote()
        entry = DurablePublicationEntry(factory, SCENARIO, remote=remote)
        await entry.start()
        await engine.dispose()

        factory_b, engine_b = await _process(tmp_path / "tw.db")
        entry_b = DurablePublicationEntry(factory_b, SCENARIO, remote=remote)
        from forge.adaptive.workpackage import WorkPackageCoordinator

        coordinator = WorkPackageCoordinator(factory_b, entry_b._dispatch_child)
        stale = await coordinator.record_outcome(
            entry_b.parent_run_id,
            SCENARIO.PRODUCER_ITEM,
            "succeeded",
            tested_world_digest="f" * 64,
        )
        assert stale.status == "rejected"
        assert "different world" in stale.reason
        assert [launch["item_id"] for launch in entry_b.child_launches] == []
        await engine_b.dispose()

    async def test_a_failed_producer_blocks_the_consumer(self, tmp_path):
        factory, engine = await _process(tmp_path / "tw.db")
        remote = _seeded_remote()
        remote.refuse_commits = {PRODUCER}
        entry = DurablePublicationEntry(factory, SCENARIO, remote=remote)
        await entry.start()
        report = await entry.drive()
        assert report.package_state == "failed"
        assert [launch["item_id"] for launch in entry.child_launches] == [
            SCENARIO.PRODUCER_ITEM
        ]  # the consumer was never launched
        assert remote.effects_for(CONSUMER) == []
        partial = await entry.partial_publication()
        outstanding = {effect.repository_id: effect for effect in partial.outstanding}
        assert outstanding[PRODUCER].status == "failed"  # the refusal is identified
        assert outstanding[CONSUMER].status == "preparing"  # so is the never-started writer
        assert not partial.standing
        await engine.dispose()

    async def test_the_unadmitted_writer_cannot_even_persist_its_intent(self, tmp_path):
        factory, engine = await _process(tmp_path / "tw.db")
        remote = _seeded_remote()
        entry = DurablePublicationEntry(factory, SCENARIO, remote=remote)
        await entry.start()
        await engine.dispose()

        factory_b, engine_b = await _process(tmp_path / "tw.db")
        entry_b = DurablePublicationEntry(factory_b, SCENARIO, remote=remote)
        # kill the pre-crash process after the producer's intent save: the
        # restart's first pass must leave the consumer PREPARING — the
        # admission guard refuses its write-ahead intent until phase 1.
        saga = await entry_b.publish_pass()
        assert saga.repo(PRODUCER).status == "ready_for_review"
        assert saga.repo(CONSUMER).status == "preparing"
        assert remote.effects_for(CONSUMER) == []
        await engine_b.dispose()


class TestCrashRecovery:
    async def test_death_between_coordinator_saves_restarts_and_converges(self, tmp_path):
        db = tmp_path / "tw.db"
        factory, engine = await _process(db)
        remote = _seeded_remote()
        entry = DurablePublicationEntry(
            factory,
            SCENARIO,
            remote=remote,
            on_boundary=kill_at_boundary(PRODUCER, "commit_intent"),
        )
        await entry.start()
        with pytest.raises(ProcessDied):
            await entry.drive()
        died_at = await entry._saga_state()
        assert died_at.repo(PRODUCER).status == "intent_recorded"  # write-ahead, no effect yet
        assert remote.effects_for(PRODUCER) == []
        await engine.dispose()

        factory_b, engine_b = await _process(db)  # the restarted process
        entry_b = DurablePublicationEntry(factory_b, SCENARIO, remote=remote)
        report = await entry_b.drive()
        saga = await entry_b._saga_state()
        assert report.package_state == "complete" and str(saga.status) == "complete"
        for repo in SCENARIO.writer_repos():
            assert (
                len(remote.commits_carrying(repo.repository_id, repo.branch, saga.commit_marker))
                == 1
            )
            assert remote.commit_calls[repo.repository_id] == 1  # probe-first: no duplicate
            assert remote.merge_request_creates(repo.repository_id) == 1
        await engine_b.dispose()


class TestLostResponse:
    async def test_adoption_only_through_native_correlation(self, tmp_path):
        factory, engine = await _process(tmp_path / "tw.db")
        remote = _seeded_remote()
        entry = DurablePublicationEntry(factory, SCENARIO, remote=remote)
        await entry.start()
        remote.lose_commit_response = {PRODUCER}
        report = await entry.drive()  # the drive reconciles within its own passes
        saga = await entry._saga_state()
        assert report.package_state == "complete"
        producer_repo = saga.repo(PRODUCER)
        assert producer_repo.adopted is True  # adopted, never re-published
        assert remote.commit_calls[PRODUCER] == 1  # the lost call is the ONLY call
        landed = remote.commits_carrying(PRODUCER, SCENARIO.producer().branch, saga.commit_marker)
        assert len(landed) == 1  # exactly one distinct effect
        # the adoption is ESTABLISHED: the listed commit's message carries the
        # marker and its parent is the expected base (content/parent checks).
        assert saga.commit_marker in landed[0].message
        assert landed[0].parent == SCENARIO.producer().base_oid
        assert landed[0].author == "forge-bot"
        events = [event["event_type"] for event in await entry.outbox_events()]
        assert "saga.unknown_effects" in events  # the window was honestly booked
        assert "recovery.native_adoption" in events
        await engine.dispose()

    async def test_a_genuinely_unknown_effect_stays_unknown_across_two_passes(self, tmp_path):
        db = tmp_path / "tw.db"
        factory, engine = await _process(db)
        remote = _seeded_remote()
        entry = DurablePublicationEntry(
            factory,
            SCENARIO,
            remote=remote,
            on_boundary=kill_at_boundary(PRODUCER, "outcome_unknown"),
        )
        await entry.start()
        remote.lose_commit_response = {PRODUCER}
        with pytest.raises(ProcessDied):
            await entry.drive()
        await engine.dispose()

        remote.lose_commit_response = set()
        remote.unavailable = {PRODUCER}  # the surface cannot prove anything
        for _pass in range(2):
            factory_b, engine_b = await _process(db)
            entry_b = DurablePublicationEntry(factory_b, SCENARIO, remote=remote)
            await entry_b.drive()
            saga = await entry_b._saga_state()
            assert saga.repo(PRODUCER).status == "outcome_unknown"  # never assumed
            assert remote.commit_calls[PRODUCER] == 1  # fail closed: no blind retry
            assert remote.effects_for(CONSUMER) == []  # and the consumer stays gated
            assert [effect.repository_id for effect in await entry_b.unknown_effects()] == [
                PRODUCER
            ]
            await engine_b.dispose()
        events = [event["event_type"] for event in await entry_b.outbox_events()]
        assert "saga.unknown_effects" in events

        remote.unavailable = set()  # the provider heals
        factory_c, engine_c = await _process(db)
        entry_c = DurablePublicationEntry(factory_c, SCENARIO, remote=remote)
        report = await entry_c.drive()
        saga = await entry_c._saga_state()
        assert report.package_state == "complete"
        assert saga.repo(PRODUCER).adopted is True  # adopted via the LISTED history
        assert (
            len(remote.commits_carrying(PRODUCER, SCENARIO.producer().branch, saga.commit_marker))
            == 1
        )
        await engine_c.dispose()


class TestHumanPivot:
    async def test_a_human_merge_stands_and_a_colliding_edit_parks_forward_only(self, tmp_path):
        db = tmp_path / "tw.db"
        factory, engine = await _process(db)
        remote = _seeded_remote()
        entry = DurablePublicationEntry(
            factory,
            SCENARIO,
            remote=remote,
            on_boundary=kill_at_boundary(CONSUMER, "commit_intent"),
        )
        await entry.start()
        with pytest.raises(ProcessDied):
            await entry.drive()
        await engine.dispose()

        # while the coordinator is dead: the human MERGES the producer's MR
        # and EDITS the consumer's branch.
        remote.human_merge(PRODUCER, SCENARIO.producer().branch)
        remote.human_commit(CONSUMER, SCENARIO.consumer().branch)
        history_at_death = remote.branch_history(CONSUMER, SCENARIO.consumer().branch)

        factory_b, engine_b = await _process(db)
        entry_b = DurablePublicationEntry(factory_b, SCENARIO, remote=remote)
        await entry_b.observe_merge(PRODUCER)  # the OBSERVED merge — never performed
        report = await entry_b.drive()
        saga = await entry_b._saga_state()
        assert saga.repo(PRODUCER).status == "human_merged"  # forward-only: it stands
        assert saga.repo(CONSUMER).status == "parked_human"  # the collision parks
        assert "human edit" in saga.repo(CONSUMER).note or "moved past" in saga.repo(CONSUMER).note
        assert report.package_state == "running"  # awaiting the human decision
        # the human's work is preserved — prefix-intact, never force-pushed
        history_now = remote.branch_history(CONSUMER, SCENARIO.consumer().branch)
        assert history_now[: len(history_at_death)] == history_at_death
        assert remote.destructive_operations() == []
        # the conflict decision is surfaced, with each outstanding effect identified
        partial = await entry_b.partial_publication()
        assert [effect.repository_id for effect in partial.standing] == [PRODUCER]
        assert [effect.repository_id for effect in partial.outstanding] == [CONSUMER]
        events = [event["event_type"] for event in await entry_b.outbox_events()]
        assert "human_edit.conflicts" in events
        await engine_b.dispose()

    async def test_a_moved_head_before_the_second_publication_is_a_422_policy_outcome(
        self,
        tmp_path,
    ):
        db = tmp_path / "tw.db"
        factory, engine = await _process(db)
        remote = _seeded_remote()
        entry = DurablePublicationEntry(
            factory,
            SCENARIO,
            remote=remote,
            on_boundary=kill_at_boundary(PRODUCER, "record"),
        )
        await entry.start()
        with pytest.raises(ProcessDied):
            await entry.drive()  # dies between the two publications
        await engine.dispose()

        remote.human_commit(CONSUMER, SCENARIO.consumer().branch)  # the head moves first
        factory_b, engine_b = await _process(db)
        entry_b = DurablePublicationEntry(factory_b, SCENARIO, remote=remote)
        report = await entry_b.drive()
        saga = await entry_b._saga_state()
        consumer_repo = saga.repo(CONSUMER)
        assert consumer_repo.status == "failed"  # the CAS pin: an explicit policy outcome
        assert "422" in consumer_repo.note
        assert saga.repo(PRODUCER).status == "ready_for_review"  # the first PR stands
        assert report.package_state == "failed"
        # the human edit is preserved and reported as the outstanding effect
        assert len(remote.branch_history(CONSUMER, SCENARIO.consumer().branch)) == 2
        partial = await entry_b.partial_publication()
        assert [effect.repository_id for effect in partial.standing] == [PRODUCER]
        await engine_b.dispose()


class TestProviderOutage:
    async def test_unavailability_fails_closed_then_succeeds_late(self, tmp_path):
        db = tmp_path / "tw.db"
        factory, engine = await _process(db)
        remote = _seeded_remote()
        entry = DurablePublicationEntry(factory, SCENARIO, remote=remote)
        await entry.start()
        remote.unavailable = {CONSUMER}
        report = await entry.drive()
        saga = await entry._saga_state()
        assert remote.effects_for(CONSUMER) == []  # nothing created blind
        assert saga.repo(CONSUMER).status in {"preparing", "intent_recorded"}
        assert report.saga_status in {"running", "failed"}  # honest, never "complete"
        await engine.dispose()

        remote.unavailable = set()  # delayed success after the timeout window
        factory_b, engine_b = await _process(db)
        entry_b = DurablePublicationEntry(factory_b, SCENARIO, remote=remote)
        report_b = await entry_b.drive()
        assert report_b.package_state == "complete"
        await engine_b.dispose()


class TestReadOnlyDependencies:
    async def test_read_only_repositories_never_receive_effects_or_writer_credentials(
        self,
        tmp_path,
    ):
        factory, engine = await _process(tmp_path / "tw.db")
        remote = _seeded_remote()
        entry = DurablePublicationEntry(factory, SCENARIO, remote=remote)
        await entry.start()
        await entry.drive()
        touched = {entry["repository_id"] for entry in remote.journal}
        assert touched == {PRODUCER, CONSUMER}  # no effect ever names a read-only repo
        assert remote.commit_calls.keys() <= {PRODUCER, CONSUMER}
        assert remote.review_calls.keys() <= {PRODUCER, CONSUMER}
        staging = entry.credential_staging()
        assert set(staging) == {PRODUCER, CONSUMER, PINNED, NEIGHBOR}
        assert all(name.startswith("cred-read-") for name in staging[PINNED] + staging[NEIGHBOR])
        assert verify_credential_scope(lane_assignment(SCENARIO.package()), staging) == []
        await engine.dispose()


class TestConvergedInertness:
    async def test_third_and_fourth_passes_spend_nothing(self, tmp_path):
        db = tmp_path / "tw.db"
        factory, engine = await _process(db)
        remote = _seeded_remote()
        entry = DurablePublicationEntry(factory, SCENARIO, remote=remote)
        await entry.start()
        await entry.drive()
        journal_size = len(remote.journal)
        saga = await entry._saga_state()
        steps_digest = saga.steps_digest
        events = await entry.outbox_events()
        await engine.dispose()

        from forge.adaptive.workpackage import read_workpackage_state

        factory_b, engine_b = await _process(db)
        entry_b = DurablePublicationEntry(factory_b, SCENARIO, remote=remote)
        children_before = (await read_workpackage_state(factory_b, entry_b.parent_run_id))[
            "children"
        ]
        for _ in range(3):  # third pass and beyond
            await entry_b.drive()
        assert len(remote.journal) == journal_size  # no remote effects
        assert entry_b.child_launches == ()  # no model work
        assert (await entry_b._saga_state()).steps_digest == steps_digest
        assert await entry_b.outbox_events() == events  # no outbox rows
        children_after = (await read_workpackage_state(factory_b, entry_b.parent_run_id))[
            "children"
        ]
        assert children_before == children_after
        await engine_b.dispose()


# ---------------------------------------------------------------------------
# PG-gated: real PostgreSQL, fresh engines as fresh processes (FI convention).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("FORGE_PG_TEST_URL"),
    reason=(
        "FORGE_PG_TEST_URL not set — the R36-18 real-PostgreSQL durability proof "
        "runs only against a disposable real Postgres (rows are dropped per test)"
    ),
)
class TestRealPostgresDurability:
    @staticmethod
    async def _fresh_worker(
        *, reset: bool = False
    ) -> tuple[async_sessionmaker[AsyncSession], AsyncEngine]:
        """A fresh engine over the lab URL — one 'process'. With *reset*
        (the first call of each test), the public schema is dropped and
        recreated first: the FI convention — a lab URL is DISPOSABLE, so
        no test inherits another's rows."""
        url = os.environ["FORGE_PG_TEST_URL"]
        setup = create_async_engine(url)
        _pending_engines.append(setup)
        async with setup.begin() as conn:
            if reset:
                from sqlalchemy import text

                names = (
                    (
                        await conn.execute(
                            text("select tablename from pg_tables where schemaname = 'public'")
                        )
                    )
                    .scalars()
                    .all()
                )
                for name in names:
                    await conn.execute(text(f'drop table if exists "{name}" cascade'))
            await conn.run_sync(Base.metadata.create_all)
        await setup.dispose()
        engine = create_async_engine(url)
        _pending_engines.append(engine)
        return async_sessionmaker(engine, expire_on_commit=False), engine

    async def test_the_saga_survives_its_process_on_real_postgres(self, tmp_path):
        factory_a, engine_a = await self._fresh_worker(reset=True)
        await _seed_parent(factory_a, "run-pg-1")
        store_a = PostgresSagaStore(factory_a, parent_run_id="run-pg-1")
        saga = begin_saga(
            "wp-1",
            publication_epoch=4,
            candidate_digest="d" * 64,
            repo_plans={"repo-a": ("main", "base-a"), "repo-b": ("main", "base-b")},
        )
        saga = record_commit_intent(saga, "repo-a")
        await store_a.save(saga)
        await engine_a.dispose()  # the writer's process is gone

        factory_b, engine_b = await self._fresh_worker()
        try:
            loaded = await PostgresSagaStore(factory_b, parent_run_id="run-pg-1").load(saga.saga_id)
            assert loaded == saga
            async with factory_b() as session:
                events = (
                    (await session.execute(select(Outbox).where(Outbox.flow_run_id == "run-pg-1")))
                    .scalars()
                    .all()
                )
            assert events == []  # an intent alone is not yet an observability transition
        finally:
            await engine_b.dispose()

    async def test_the_durable_entry_converges_over_real_postgres(self, tmp_path):
        remote = _seeded_remote()
        factory_a, engine_a = await self._fresh_worker(reset=True)
        entry = DurablePublicationEntry(
            factory_a,
            SCENARIO,
            remote=remote,
            on_boundary=kill_at_boundary(PRODUCER, "provider_commit"),
        )
        await entry.start()
        with pytest.raises(ProcessDied):
            await entry.drive()
        await engine_a.dispose()  # the process dies mid-publication

        factory_b, engine_b = await self._fresh_worker()  # a genuinely fresh process
        try:
            entry_b = DurablePublicationEntry(factory_b, SCENARIO, remote=remote)
            report = await entry_b.drive()
            saga = await entry_b._saga_state()
            assert report.package_state == "complete" and str(saga.status) == "complete"
            for repo in SCENARIO.writer_repos():
                assert (
                    len(
                        remote.commits_carrying(repo.repository_id, repo.branch, saga.commit_marker)
                    )
                    == 1
                )
                assert remote.merge_request_creates(repo.repository_id) == 1
        finally:
            await engine_b.dispose()
