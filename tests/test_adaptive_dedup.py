"""FND-06 — database-enforced dedup, independent epochs, projection rebuild.

The constraint is the arbiter: two workers recording the same replay key
produce ONE row, and the loser reads the winner's bytes (first-result-wins
— a redelivered command never spends another iteration). Epochs prove the
three counters move on independent axes, and the projection rebuild
derives mutable state from immutable history without rerunning effects.
"""

from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy import JSON, Integer, String, UniqueConstraint, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import StaticPool

from forge.adaptive.dedup import Epochs, insert_or_read, rebuild_projection
from forge.models.base import Base


class DedupRow(Base):
    """A minimal replay-key table: (run_id, delivery_id) is the identity."""

    __tablename__ = "dedup_test_rows"
    __table_args__ = (UniqueConstraint("run_id", "delivery_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(32), nullable=False)
    delivery_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


@pytest.fixture()
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class TestInsertOrRead:
    async def test_second_call_reads_the_first_result_cross_transaction(self, session_factory):
        """Two sequential worker attempts, two separate sessions: one row.

        The second insert loses the unique-constraint race and must read
        the winner's row — including the winner's payload. First result
        wins; the loser's values are discarded, not merged.
        """
        first, created_first = await insert_or_read(
            session_factory,
            DedupRow,
            {"run_id": "run-1", "delivery_id": "delivery-1"},
            {"payload": {"attempt": "first"}},
        )
        second, created_second = await insert_or_read(
            session_factory,
            DedupRow,
            {"run_id": "run-1", "delivery_id": "delivery-1"},
            {"payload": {"attempt": "second"}},
        )

        assert created_first is True
        assert created_second is False
        assert second.id == first.id  # same authoritative row
        assert second.payload == {"attempt": "first"}  # the winner's bytes survive

        async with session_factory() as session:
            count = await session.scalar(select(func.count()).select_from(DedupRow))
        assert count == 1

    async def test_distinct_keys_both_create(self, session_factory):
        row_a, created_a = await insert_or_read(
            session_factory,
            DedupRow,
            {"run_id": "run-1", "delivery_id": "delivery-a"},
            {"payload": {}},
        )
        row_b, created_b = await insert_or_read(
            session_factory,
            DedupRow,
            {"run_id": "run-1", "delivery_id": "delivery-b"},
            {"payload": {}},
        )

        assert created_a and created_b
        assert row_a.id != row_b.id
        async with session_factory() as session:
            count = await session.scalar(select(func.count()).select_from(DedupRow))
        assert count == 2


class TestEpochs:
    def test_counters_start_at_zero_and_bump_independently(self):
        epochs = Epochs()
        assert (epochs.publication_epoch, epochs.checkpoint_epoch, epochs.control_epoch) == (
            0,
            0,
            0,
        )

        published = epochs.next_publication()
        assert (
            published.publication_epoch,
            published.checkpoint_epoch,
            published.control_epoch,
        ) == (
            1,
            0,
            0,
        )
        checkpointed = published.next_checkpoint()
        assert (
            checkpointed.publication_epoch,
            checkpointed.checkpoint_epoch,
            checkpointed.control_epoch,
        ) == (1, 1, 0)
        controlled = checkpointed.next_control()
        assert (
            controlled.publication_epoch,
            controlled.checkpoint_epoch,
            controlled.control_epoch,
        ) == (1, 1, 1)

    def test_bumping_one_axis_never_moves_the_others(self):
        epochs = Epochs(publication_epoch=3, checkpoint_epoch=5, control_epoch=7)
        assert epochs.next_publication() == Epochs(4, 5, 7)
        assert epochs.next_checkpoint() == Epochs(3, 6, 7)
        assert epochs.next_control() == Epochs(3, 5, 8)
        assert epochs == Epochs(3, 5, 7)  # the source value is never mutated

    def test_frozen_no_torn_counter_updates(self):
        epochs = Epochs()
        with pytest.raises(dataclasses.FrozenInstanceError):
            epochs.publication_epoch = 99  # type: ignore[misc]


class TestRebuildProjection:
    def test_mixed_history_projects_latest_decision_and_spent_kinds(self):
        projection = rebuild_projection(
            [
                {"sequence": 1, "kind": "pause", "status": "applied"},
                {"sequence": 2, "kind": "resume", "status": "rejected"},
                {"sequence": 3, "kind": "steer", "status": "applied"},
            ]
        )

        assert projection == {
            "status": "applied",  # the highest-sequence decision won
            "last_sequence": 3,
            "applied_kinds": {"pause", "steer"},  # rejected spent nothing
        }

    def test_sequence_not_delivery_order_decides_the_latest(self):
        projection = rebuild_projection(
            [
                {"sequence": 9, "kind": "steer", "status": "applied"},
                {"sequence": 4, "kind": "pause", "status": "rejected"},
                {"sequence": 6, "kind": "resume", "status": "applied"},
            ]
        )

        assert projection["status"] == "applied"
        assert projection["last_sequence"] == 9
        assert projection["applied_kinds"] == {"steer", "resume"}

    def test_history_ending_in_rejection_projects_rejected(self):
        projection = rebuild_projection(
            [
                {"sequence": 1, "kind": "pause", "status": "applied"},
                {"sequence": 2, "kind": "amend", "status": "rejected"},
            ]
        )
        assert projection == {
            "status": "rejected",
            "last_sequence": 2,
            "applied_kinds": {"pause"},
        }

    def test_empty_history_projects_the_pristine_state(self):
        assert rebuild_projection([]) == {
            "status": "idle",
            "last_sequence": 0,
            "applied_kinds": set(),
        }
