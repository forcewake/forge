"""NXT-09 — the durable control mailbox: dedup-first, restart-safe, NXT-12 ladder.

Every test runs against SQLite (aiosqlite, the fast profile) AND against
real Postgres when ``FORGE_PG_TEST_URL`` is set (the FI convention: the
schema comes from the REAL migration chain via ``tests.fi_os.lab``, and
only a disposable lab database may be pointed at). The invariants pinned
here are the ones the review states as the difference between durable
control and dictionaries:

- a duplicate delivery is refused by the database BEFORE any state
  change — one row, the winner's bytes, ``created=False``;
- the dedup key is WORK-scoped: the same provider event number in two
  works is two commands;
- a new instance over the same database recovers the pending commands
  and the sequence continues monotonically (restart with an empty heap);
- the ladder cannot skip rungs, ``dispatching`` leaves ``pending()``, and
  ``applied`` is only reachable through the vendor rungs (NXT-12);
- a duplicate pause does not bump the publication epoch.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import forge.adaptive.mailbox_db  # noqa: F401  (registers control_commands in metadata)
from forge.adaptive.control import PauseState, new_publication_epoch, request_pause, send_interrupt
from forge.adaptive.mailbox_db import (
    ControlCommandDeliveryRow,
    ControlCommandRow,
    PostgresMailbox,
)
from forge.adaptive.models import ControlCommand
from forge.models.base import Base

SQLITE_URL = "sqlite+aiosqlite:///:memory:"

SCOPES = {
    "server_authenticated_human": ("human:reviewer-17",),
    "operator_token": ("token:ops-1",),
    "automation_reconciler": ("svc:reconciler",),
}


def _command(**overrides) -> ControlCommand:
    base = {
        "schema": "forge.proposal.control-command/1",
        "command_id": "cmd-1",
        "work_id": "wp-demo-1",
        "sequence": 1,
        "kind": "pause",
        "actor_ref": "human:reviewer-17",
        "actor_origin": "server_authenticated_human",
        "idempotency_key": "gitlab-note:1",
        "status": "received",
    }
    base.update(overrides)
    return ControlCommand.model_validate(base)


def _database_urls() -> list[str]:
    """SQLite always; real Postgres too when the FI lab URL is set."""
    urls = [SQLITE_URL]
    if os.environ.get("FORGE_PG_TEST_URL"):
        urls.append(os.environ["FORGE_PG_TEST_URL"])
    return urls


def _url_id(url: str) -> str:
    return "postgres" if url.startswith("postgres") else "sqlite"


class _Lab:
    """One database with reopenable mailboxes — the restart simulation.

    ``reopen()`` builds a FRESH :class:`PostgresMailbox` over its own
    session factory: a new process with an empty heap and the same
    database. ``row()`` reads the durable row itself (epoch, journal) —
    the columns the contract object does not carry.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self.mailbox = self.reopen()

    def reopen(self) -> PostgresMailbox:
        return PostgresMailbox(async_sessionmaker(self._engine, expire_on_commit=False))

    async def row(self, command_id: str) -> ControlCommandRow | None:
        factory = async_sessionmaker(self._engine, expire_on_commit=False)
        async with factory() as session:
            return await session.scalar(
                select(ControlCommandRow).where(ControlCommandRow.id == command_id)
            )


@pytest.fixture(params=_database_urls(), ids=_url_id)
async def lab(request) -> _Lab:
    url = request.param
    if url.startswith("postgres"):
        from sqlalchemy import inspect

        from tests.fi_os.lab import run_migrations

        engine = create_async_engine(url)
        async with engine.begin() as conn:
            present = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).has_table("control_commands")
            )
        await engine.dispose()
        if not present:
            run_migrations(url)  # the REAL chain — never a second schema factory
            engine = create_async_engine(url)
        # Fresh control state per test; every other table is left alone
        # (this suite coexists with the FI lab's own runs).
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM control_commands"))
    else:
        engine = create_async_engine(
            url, connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    yield _Lab(engine)
    await engine.dispose()


class TestSubmitDedup:
    async def test_submit_stores_a_new_command_at_received(self, lab):
        stored, created = await lab.mailbox.submit(_command(status="authorized"))

        assert created is True
        assert stored.status == "received"  # the ladder starts here, whatever the caller believed

    async def test_a_duplicate_native_event_is_one_command(self, lab):
        """Redelivery (same work-scoped key, even a new command_id) reads the winner."""
        original, created_first = await lab.mailbox.submit(_command())
        replayed, created_second = await lab.mailbox.submit(
            _command(command_id="cmd-replayed", sequence=2, status="checkpointed")
        )

        assert created_first is True
        assert created_second is False
        assert replayed.command_id == original.command_id
        assert replayed.status == "received"  # the replay's claimed status is discarded
        reopened = lab.reopen()
        recovered = await reopened.get(original.command_id)
        assert recovered is not None
        assert recovered.status == "received"  # ONE row — the winner's bytes

    async def test_the_dedup_key_is_scoped_to_the_work(self, lab):
        """The same provider event number in two works is two commands."""
        first, created_first = await lab.mailbox.submit(_command(work_id="wp-a"))
        second, created_second = await lab.mailbox.submit(
            _command(command_id="cmd-b", work_id="wp-b")
        )

        assert created_first is True
        assert created_second is True
        assert first.command_id != second.command_id

    async def test_sequences_must_be_strictly_increasing_per_work(self, lab):
        await lab.mailbox.submit(_command(sequence=2))

        with pytest.raises(ValueError, match="not strictly increasing"):
            await lab.mailbox.submit(
                _command(command_id="cmd-1b", idempotency_key="k-other", sequence=1)
            )
        # ...and independent per work:
        stored, created = await lab.mailbox.submit(
            _command(command_id="cmd-b", work_id="wp-b", sequence=1)
        )
        assert created is True

    async def test_a_command_id_under_another_key_is_refused(self, lab):
        await lab.mailbox.submit(_command())

        with pytest.raises(ValueError, match="already exists under a different idempotency key"):
            await lab.mailbox.submit(_command(idempotency_key="k-other", sequence=2))

    async def test_next_sequence_allocates_after_the_durable_history(self, lab):
        assert await lab.mailbox.next_sequence("wp-demo-1") == 1
        await lab.mailbox.submit(_command())
        assert await lab.mailbox.next_sequence("wp-demo-1") == 2
        reopened = lab.reopen()
        assert await reopened.next_sequence("wp-demo-1") == 2  # the restart sees the history


class TestRestartRecovery:
    async def test_a_new_instance_recovers_pending_commands_and_the_sequence(self, lab):
        """Restart with an empty Python heap and the same database (NXT-09)."""
        await lab.mailbox.submit(
            _command(
                sequence=1,
                command_id="cmd-a",
                idempotency_key="k1",
                kind="steer",
                payload={"text": "fix the parser first"},
            )
        )
        await lab.mailbox.submit(_command(sequence=2, command_id="cmd-b", idempotency_key="k2"))
        await lab.mailbox.submit(
            _command(
                sequence=3,
                command_id="cmd-c",
                idempotency_key="k3",
                kind="steer",
                payload={"text": "then the CLI"},
            )
        )
        await lab.mailbox.authorize("cmd-b", SCOPES)
        await lab.mailbox.authorize("cmd-a", SCOPES)

        reopened = lab.reopen()  # a new process, same database

        pending = await reopened.pending("wp-demo-1")
        # received/authorized only — in durable sequence order; the
        # authorized rung survives the restart exactly as it stood.
        assert [command.command_id for command in pending] == ["cmd-a", "cmd-b", "cmd-c"]
        assert [command.status for command in pending] == ["authorized", "authorized", "received"]
        # the sequence continues monotonically from the durable history:
        stored, created = await reopened.submit(
            _command(sequence=4, command_id="cmd-d", idempotency_key="k4")
        )
        assert created is True
        with pytest.raises(ValueError, match="not strictly increasing"):
            await reopened.submit(_command(sequence=3, command_id="cmd-e", idempotency_key="k5"))

    async def test_a_consumed_command_is_recovered_at_its_last_rung(self, lab):
        mailbox = lab.mailbox
        await mailbox.submit(_command())
        await mailbox.authorize("cmd-1", SCOPES)
        await mailbox.dispatch(
            "cmd-1",
            current_plan_revision=1,
            current_execution_epoch=1,
            vendor_correlation_id="sess-42",
        )
        await mailbox.vendor_accepted("cmd-1")
        await mailbox.observe("cmd-1")

        reopened = lab.reopen()

        recovered = await reopened.get("cmd-1")
        assert recovered is not None
        assert recovered.status == "applied"  # never re-delivered as pending...
        assert await reopened.pending("wp-demo-1") == []
        # ...and the new process finishes the ladder from the durable rung:
        await reopened.checkpoint("cmd-1")
        final = await reopened.get("cmd-1")
        assert final is not None
        assert final.status == "checkpointed"


class TestDeliveryLadder:
    async def test_the_full_ladder_reaches_checkpointed(self, lab):
        mailbox = lab.mailbox
        await mailbox.submit(_command())
        await mailbox.authorize("cmd-1", SCOPES)

        dispatching = await mailbox.dispatch(
            "cmd-1", current_plan_revision=1, current_execution_epoch=2
        )
        assert dispatching.status == "dispatching"

        accepted = await mailbox.vendor_accepted("cmd-1")
        assert accepted.status == "vendor_accepted"

        applied = await mailbox.observe("cmd-1")
        assert applied.status == "applied"

        checkpointed = await mailbox.checkpoint("cmd-1")
        assert checkpointed.status == "checkpointed"

    async def test_the_ladder_refuses_to_skip_rungs(self, lab):
        mailbox = lab.mailbox
        await mailbox.submit(_command())

        with pytest.raises(ValueError, match="refuses to skip"):
            await mailbox.dispatch("cmd-1", current_plan_revision=1, current_execution_epoch=1)
        with pytest.raises(ValueError, match="refuses to skip"):
            await mailbox.checkpoint("cmd-1")

        await mailbox.authorize("cmd-1", SCOPES)
        with pytest.raises(ValueError, match="refuses to skip"):
            await mailbox.observe("cmd-1")  # applied requires a vendor rung first
        with pytest.raises(ValueError, match="refuses to skip"):
            await mailbox.checkpoint("cmd-1")  # checkpointing requires applied

    async def test_dispatching_leaves_the_pending_queue(self, lab):
        """The review's ordering fix: intended-to-send is NOT pending."""
        mailbox = lab.mailbox
        await mailbox.submit(
            _command(
                sequence=1,
                command_id="cmd-keep",
                idempotency_key="k0",
                kind="steer",
                payload={"text": "hold my rung"},
            )
        )
        await mailbox.submit(_command(sequence=2, command_id="cmd-1", idempotency_key="k1"))
        await mailbox.authorize("cmd-1", SCOPES)
        await mailbox.authorize("cmd-keep", SCOPES)

        await mailbox.dispatch("cmd-1", current_plan_revision=1, current_execution_epoch=1)

        pending = [command.command_id for command in await mailbox.pending("wp-demo-1")]
        assert pending == ["cmd-keep"]  # dispatching and beyond never re-deliver

    async def test_outcome_unknown_is_the_lost_response_window(self, lab):
        mailbox = lab.mailbox
        await mailbox.submit(_command())
        await mailbox.authorize("cmd-1", SCOPES)
        await mailbox.dispatch("cmd-1", current_plan_revision=1, current_execution_epoch=1)

        unknown = await mailbox.outcome_unknown("cmd-1")

        assert unknown.status == "outcome_unknown"
        assert await mailbox.pending("wp-demo-1") == []
        # reconciliation may later observe the application — never a blind retry:
        observed = await mailbox.observe("cmd-1")
        assert observed.status == "applied"

    async def test_stale_expectations_expire_at_dispatch(self, lab):
        mailbox = lab.mailbox
        await mailbox.submit(_command(expected_execution_epoch=2))
        await mailbox.authorize("cmd-1", SCOPES)

        expired = await mailbox.dispatch(
            "cmd-1", current_plan_revision=1, current_execution_epoch=5
        )

        assert expired.status == "expired"
        with pytest.raises(ValueError, match="refuses to skip"):
            # an expired command is an exit — it cannot dispatch again:
            await mailbox.dispatch("cmd-1", current_plan_revision=1, current_execution_epoch=2)

    async def test_matching_expectations_dispatch_and_persist_the_correlation(self, lab):
        mailbox = lab.mailbox
        await mailbox.submit(_command(expected_plan_revision=1, expected_execution_epoch=2))
        await mailbox.authorize("cmd-1", SCOPES)

        dispatching = await mailbox.dispatch(
            "cmd-1",
            current_plan_revision=1,
            current_execution_epoch=2,
            vendor_correlation_id="sess-42",
        )

        assert dispatching.status == "dispatching"
        row = await lab.row("cmd-1")
        assert row is not None
        assert row.epoch == 2  # the execution epoch persisted BEFORE the vendor call
        assert [entry["to"] for entry in row.journal] == ["received", "authorized", "dispatching"]
        last = row.journal[-1]
        assert last["vendor_correlation_id"] == "sess-42"
        assert last["epoch"] == 2

    async def test_every_transition_is_journaled_onto_the_audit_trail(self, lab):
        mailbox = lab.mailbox
        await mailbox.submit(_command())
        await mailbox.authorize("cmd-1", SCOPES)
        await mailbox.dispatch("cmd-1", current_plan_revision=1, current_execution_epoch=1)
        await mailbox.vendor_accepted("cmd-1")
        await mailbox.observe("cmd-1")
        await mailbox.checkpoint("cmd-1")

        row = await lab.row("cmd-1")
        assert row is not None
        assert [entry["to"] for entry in row.journal] == [
            "received",
            "authorized",
            "dispatching",
            "vendor_accepted",
            "applied",
            "checkpointed",
        ]
        assert all(entry["at"] for entry in row.journal)  # auditable: every hop is stamped
        assert row.applied_at is not None

    async def test_wrong_origin_actor_is_a_permission_error(self, lab):
        await lab.mailbox.submit(
            _command(actor_ref="human:reviewer-17", actor_origin="operator_token")
        )

        with pytest.raises(PermissionError, match="not listed under its own origin"):
            await lab.mailbox.authorize("cmd-1", SCOPES)

    async def test_an_unknown_command_id_is_a_key_error(self, lab):
        with pytest.raises(KeyError, match="unknown command_id"):
            await lab.mailbox.checkpoint("cmd-nope")


class TestPauseOrdering:
    async def test_a_duplicate_pause_does_not_bump_the_epoch(self, lab):
        """NXT-09: dedup BEFORE the epoch mutation — the durable-mailbox order.

        The durable composition of :func:`forge.adaptive.control.recorded_pause`:
        submit first (the row is the dedup arbiter); fence the epoch ONLY
        on ``created``. A redelivered pause leaves the publication epoch
        where the first delivery put it — one generation per logical
        command, never per retry.
        """
        mailbox = lab.mailbox
        state = PauseState(work_id="wp-demo-1")

        stored, created = await mailbox.submit(
            _command(idempotency_key="note:pause:1", kind="pause")
        )
        assert created is True
        fenced = new_publication_epoch(request_pause(state))
        assert fenced.publication_epoch == 1
        assert send_interrupt(fenced).interrupt_sent is True

        replayed, created_again = await mailbox.submit(
            _command(command_id="cmd-replay", sequence=2, idempotency_key="note:pause:1")
        )
        assert created_again is False
        if created_again:  # the recorded_pause gate — dead on a refused duplicate
            fenced = new_publication_epoch(request_pause(fenced))

        assert fenced.publication_epoch == 1  # NOT bumped on the duplicate
        pauses = [
            command
            for command in [await mailbox.get(stored.command_id)]
            if command is not None and command.kind == "pause"
        ]
        assert len(pauses) == 1


@pytest.mark.skipif(
    not os.environ.get("FORGE_PG_TEST_URL"),
    reason=(
        "the concurrent same-sequence race needs real Postgres row locks and "
        "separate connection pools (aiosqlite serializes on one connection)"
    ),
)
class TestConcurrentSubmit:
    async def test_two_racers_produce_one_logical_command(self):
        """NXT-09 negative: two gateway processes accept the same event number."""
        url = os.environ["FORGE_PG_TEST_URL"]
        engine = create_async_engine(url, pool_size=4)
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM control_commands"))
        first = PostgresMailbox(async_sessionmaker(engine, expire_on_commit=False))
        second = PostgresMailbox(async_sessionmaker(engine, expire_on_commit=False))

        results = await asyncio.gather(
            first.submit(_command(command_id="cmd-racer-a", idempotency_key="race:a")),
            second.submit(_command(command_id="cmd-racer-b", idempotency_key="race:b")),
            return_exceptions=True,
        )

        created = [result[1] for result in results if isinstance(result, tuple)]
        refused = [result for result in results if isinstance(result, ValueError)]
        assert len(created) == 1  # exactly one logical command survived...
        assert len(refused) == 1  # ...the sequence racer was refused, not duplicated
        assert "not strictly increasing" in str(refused[0])
        async with engine.begin() as conn:
            rows = (await conn.execute(text("SELECT count(*) FROM control_commands"))).scalar()
        assert rows == 1
        await engine.dispose()


class TestWorkWideBroadcasts:
    """NXT-13 — the durable twin: the parent is ONE control_commands row,
    the per-lane state is control_command_deliveries, and each lane
    consumes its OWN acknowledgement row. The first lane to checkpoint a
    work-wide pause no longer removes it from the second lane's queue."""

    def _broadcast(self, **overrides) -> ControlCommand:
        return _command(kind="pause", **overrides)

    async def test_two_lanes_ack_independently_and_the_parent_reflects_both(self, lab):
        mailbox = lab.mailbox
        broadcast, created = await mailbox.submit_broadcast(self._broadcast(), ("run-a", "run-b"))

        assert created is True
        assert broadcast.status == "pending"
        assert broadcast.recipients == ("run-a", "run-b")

        first = await mailbox.acknowledge(broadcast.command_id, "run-a", note="ckpt:aaa")
        assert first.status == "pending"  # run-b has not spoken

        second = await mailbox.acknowledge(broadcast.command_id, "run-b", note="ckpt:bbb")
        assert second.status == "completed"
        assert second.acknowledged_recipients == ("run-a", "run-b")

        row = await lab.row(broadcast.command_id)
        assert row is not None
        assert row.kind == "pause"  # ONE parent row — the logical intent

    async def test_the_second_lane_still_sees_a_pause_the_first_acked(self, lab):
        mailbox = lab.mailbox
        broadcast, _ = await mailbox.submit_broadcast(self._broadcast(), ("run-a", "run-b"))

        await mailbox.acknowledge(broadcast.command_id, "run-a", note="ckpt:aaa")

        own = await mailbox.pending_for("wp-demo-1", "run-a")
        other = await mailbox.pending_for("wp-demo-1", "run-b")

        assert own == []  # its own view is consumed...
        assert [command.command_id for command in other] == [broadcast.command_id]
        assert other[0].payload["run_id"] == "run-b"  # ...the sibling's is not
        assert other[0].payload["broadcast_id"] == broadcast.command_id

    async def test_a_restart_recovers_the_delivery_state_verbatim(self, lab):
        """A new process over the same database: the acknowledged row stays
        acknowledged, the outstanding row is still pending for its lane."""
        mailbox = lab.mailbox
        broadcast, _ = await mailbox.submit_broadcast(self._broadcast(), ("run-a", "run-b"))
        await mailbox.acknowledge(broadcast.command_id, "run-a", note="ckpt:aaa")
        await mailbox.mark_uncertain(
            broadcast.command_id, "run-b", "probe inconclusive: session gone"
        )

        reopened = lab.reopen()

        recovered = await reopened.broadcast(broadcast.command_id)
        assert recovered is not None
        assert recovered.status == "completed_with_uncertain"
        assert recovered.acknowledgement("run-a").status == "acknowledged"
        assert recovered.uncertain_recipients == ("run-b",)
        # the new process continues from the durable rows:
        resolved = await reopened.resolve_uncertain(
            broadcast.command_id, "run-b", note="late receipt verified"
        )
        assert resolved.status == "completed"

    async def test_broadcast_parents_leave_the_single_consumer_pending(self, lab):
        """A lane draining the ordinary queue can never consume (and thereby
        hide) a work-wide command — it is delivered per recipient only."""
        mailbox = lab.mailbox
        broadcast, _ = await mailbox.submit_broadcast(
            self._broadcast(sequence=1, command_id="cmd-wide", idempotency_key="k-wide"),
            ("run-a", "run-b"),
        )
        await mailbox.submit(
            _command(
                sequence=2,
                command_id="cmd-plain",
                idempotency_key="k-plain",
                kind="steer",
                payload={"text": "fix the parser first"},
            )
        )

        assert [command.command_id for command in await mailbox.pending("wp-demo-1")] == [
            "cmd-plain"
        ]
        assert len(await mailbox.pending_for("wp-demo-1", "run-a")) == 1

    async def test_redelivery_adopts_the_winner_and_heals_missing_rows(self, lab):
        """The mr_reservations shape: the delivery rows are derived from the
        winner's frozen payload, so a crash between the parent INSERT and
        the delivery INSERTs heals on resubmission without resetting state."""
        mailbox = lab.mailbox
        broadcast, created_first = await mailbox.submit_broadcast(
            self._broadcast(), ("run-a", "run-b")
        )
        await mailbox.acknowledge(broadcast.command_id, "run-a", note="ckpt:aaa")

        replayed, created_second = await mailbox.submit_broadcast(
            self._broadcast(command_id="cmd-replayed", sequence=2), ("run-a", "run-b")
        )

        assert created_first is True
        assert created_second is False
        assert replayed.command_id == broadcast.command_id
        assert replayed.acknowledgement("run-a").status == "acknowledged"  # never reset
        assert replayed.acknowledgement("run-b").status == "pending"

    async def test_the_ack_ladder_refuses_undeciding(self, lab):
        mailbox = lab.mailbox
        broadcast, _ = await mailbox.submit_broadcast(self._broadcast(), ("run-a",))

        with pytest.raises(KeyError, match="not in the frozen recipient set"):
            await mailbox.acknowledge(broadcast.command_id, "run-stranger")

        await mailbox.mark_uncertain(broadcast.command_id, "run-a", "probe inconclusive")
        with pytest.raises(ValueError, match="refuses to move"):
            await mailbox.acknowledge(broadcast.command_id, "run-a")

        # idempotent re-ack after resolution:
        await mailbox.resolve_uncertain(broadcast.command_id, "run-a", note="evidence arrived")
        again = await mailbox.acknowledge(broadcast.command_id, "run-a", note="again")
        assert again.status == "completed"

    async def test_every_transition_is_journaled_onto_the_delivery_row(self, lab):
        mailbox = lab.mailbox
        broadcast, _ = await mailbox.submit_broadcast(self._broadcast(), ("run-a",))

        async with async_sessionmaker(lab._engine, expire_on_commit=False)() as session:
            row = await session.scalar(
                select(ControlCommandDeliveryRow).where(
                    ControlCommandDeliveryRow.command_id == broadcast.command_id,
                    ControlCommandDeliveryRow.recipient == "run-a",
                )
            )
        assert row is not None
        assert [entry["to"] for entry in row.journal] == ["pending"]  # submit journals it
        assert row.acknowledged_at is None

        await mailbox.acknowledge(broadcast.command_id, "run-a", note="ckpt:aaa")

        async with async_sessionmaker(lab._engine, expire_on_commit=False)() as session:
            row = await session.scalar(
                select(ControlCommandDeliveryRow).where(
                    ControlCommandDeliveryRow.command_id == broadcast.command_id,
                    ControlCommandDeliveryRow.recipient == "run-a",
                )
            )
        assert row is not None
        assert [entry["to"] for entry in row.journal] == ["pending", "acknowledged"]
        assert row.journal[-1]["note"] == "ckpt:aaa"
        assert row.acknowledged_at is not None
