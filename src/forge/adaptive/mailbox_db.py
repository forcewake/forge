"""The Postgres-backed durable control mailbox (NXT-09, the NXT-12 ladder).

The in-memory :class:`forge.adaptive.control.Mailbox` is the reference
implementation of CTL-04 — but its commands, dedup index and per-work
sequences live in process dictionaries, so a restart forgets every
accepted command, idempotency holds only as long as the object lives, and
a bare idempotency key is only conventionally work-scoped. This module is
the same mailbox on the database (Postgres in production, SQLite in
tests): one ``control_commands`` row per logical command, with the
identity in the schema rather than in caller discipline.

Dedup BEFORE any state change (NXT-09): :meth:`PostgresMailbox.submit`
INSERTs against ``UNIQUE (work_id, dedup_key)`` — a redelivered command
loses the insert race and READS the winner (first-result-wins, the
:func:`forge.adaptive.dedup.insert_or_read` semantics), so a duplicate is
refused atomically BEFORE the caller bumps an epoch, spends an iteration,
or records a pause. The key is WORK-SCOPED: the same provider event
number in two works is two commands, never a collision.

The ladder is NXT-12's distinction between "intended to send" and "the
agent applied it"::

    received -> authorized -> dispatching
        -> vendor_accepted | outcome_unknown
        -> applied -> checkpointed

with ``expired`` (a CAS miss at dispatch) and ``rejected`` as exits.
``dispatching`` means the effect was INTENDED — the vendor correlation
and the execution epoch are persisted on the row BEFORE the vendor call;
``vendor_accepted`` means the vendor took it; ``outcome_unknown`` is the
lost-response window a recovery pass must reconcile (never silently
retry); ``applied`` is the APPLICATION observed — the bridge's old habit
of marking ``applied`` before the vendor effect is exactly what this
split removes; ``checkpointed`` means the durable checkpoint carries the
command. Every transition is a guarded ``UPDATE ... WHERE status =
<expected>`` compare-and-set: a skipped rung raises, a concurrent mover
loses to the WHERE clause, and each hop appends an auditable ``journal``
entry. :meth:`PostgresMailbox.pending` returns received/authorized ONLY
— once a command is ``dispatching`` it has left the queue and its fate is
read from the row, never guessed.

The surface mirrors :class:`forge.adaptive.control.Mailbox` (see
:class:`forge.adaptive.control.MailboxSurface`): ``submit`` /
``authorize`` / ``checkpoint`` / ``pending`` keep their names, arguments
and semantics — as awaitables, because production I/O is async and the
gateway seam that consumes this mailbox (NXT-10) is async. The coarse
in-memory ``apply`` becomes the finer ``dispatch -> vendor_accepted |
outcome_unknown -> observe`` ladder above; that split IS the review's
fix, so it is not collapsible back into one call.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Final, cast

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    select,
    update,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from forge.adaptive.models import ControlCommand
from forge.models.base import Base

__all__ = ["CONTROL_COMMAND_STATUSES", "ControlCommandRow", "PostgresMailbox"]

#: The ordered delivery ladder (NXT-12) plus the two exits. ``rejected``
#: and ``expired`` are exits, never rungs — the ladder only climbs.
CONTROL_COMMAND_STATUSES: Final = (
    "received",
    "authorized",
    "dispatching",
    "vendor_accepted",
    "outcome_unknown",
    "applied",
    "checkpointed",
    "rejected",
    "expired",
)

_KINDS: Final = ("pause", "resume", "steer", "answer", "amend", "approve-revision")
_ORIGINS: Final = (
    "server_authenticated_human",
    "operator_token",
    "automation_reconciler",
)

#: Human-readable rung order for ladder-refusal messages.
_RUNG_ORDER: Final = (
    "received",
    "authorized",
    "dispatching",
    "vendor_accepted/outcome_unknown",
    "applied",
    "checkpointed",
)

_JSONType = JSON().with_variant(postgresql.JSONB(), "postgresql")
"""JSONB on Postgres, JSON on SQLite — same bytes, dialect-native type."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ControlCommandRow(Base):
    """One durable mailbox record of human control (the table of NXT-09).

    Mirrors migration 020 exactly: the work-scoped dedup identity and the
    per-work sequence uniqueness are constraints, the status ladder is a
    CHECK, and ``journal`` is the append-only audit of every guarded
    transition. ``run_id`` is the denormalized run scope (from
    ``payload["run_id"]``) so a lane can index its own commands; the
    payload remains authoritative.
    """

    __tablename__ = "control_commands"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('pause', 'resume', 'steer', 'answer', 'amend', 'approve-revision')",
            name="ck_control_commands_kind",
        ),
        CheckConstraint(
            "actor_origin IN ('server_authenticated_human', 'operator_token', "
            "'automation_reconciler')",
            name="ck_control_commands_actor_origin",
        ),
        CheckConstraint(
            "status IN ('received', 'authorized', 'dispatching', 'vendor_accepted', "
            "'outcome_unknown', 'applied', 'checkpointed', 'rejected', 'expired')",
            name="ck_control_commands_status",
        ),
        UniqueConstraint("work_id", "dedup_key", name="uq_control_command_dedup_scope"),
        UniqueConstraint("work_id", "sequence", name="uq_control_command_work_sequence"),
        Index("ix_control_commands_work_status", "work_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    work_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(_JSONType, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="received")
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_origin: Mapped[str] = mapped_column(String(40), nullable=False)
    expected_plan_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_execution_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The execution epoch the command was dispatched under — persisted
    #: BEFORE the vendor call (NXT-12), so a late acknowledgement from an
    #: old epoch can never be booked as current.
    epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    journal: Mapped[list[dict[str, Any]]] = mapped_column(_JSONType, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def _to_command(row: ControlCommandRow) -> ControlCommand:
    """Rebuild the contract object from the durable row."""
    # kind / actor_origin / status are CHECK-constrained closed sets on the
    # row; the str-typed column cannot prove them to the type checker.
    return ControlCommand(
        command_id=row.id,
        work_id=row.work_id,
        sequence=row.sequence,
        kind=row.kind,  # type: ignore[arg-type]
        actor_ref=row.actor_ref,
        actor_origin=row.actor_origin,  # type: ignore[arg-type]
        idempotency_key=row.dedup_key,
        expected_plan_revision=row.expected_plan_revision,
        expected_execution_epoch=row.expected_execution_epoch,
        payload=dict(row.payload or {}),
        status=row.status,  # type: ignore[arg-type]
    )


def _journal_entry(from_status: str | None, to_status: str, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"at": _utcnow().isoformat(), "to": to_status}
    if from_status is not None:
        entry["from"] = from_status
    entry.update({key: value for key, value in extra.items() if value is not None})
    return entry


class PostgresMailbox:
    """The durable command mailbox over ``control_commands`` (CTL-04).

    Same surface as the in-memory
    :class:`~forge.adaptive.control.Mailbox`, as awaitables:
    :meth:`submit` / :meth:`authorize` / :meth:`checkpoint` /
    :meth:`pending` / :meth:`get` keep their names and semantics, and the
    coarse ``apply`` becomes the NXT-12 ladder
    ``dispatch -> vendor_accepted | outcome_unknown -> observe``. A new
    instance over the same database sees every committed command —
    restart recovery IS a fresh query, not a rebuild.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # -- entry ------------------------------------------------------------

    async def submit(self, command: ControlCommand) -> tuple[ControlCommand, bool]:
        """Enter a command into the durable mailbox; redelivery is a no-op.

        The sequence must be strictly greater than every prior sequence of
        the same work (checked against ``max(sequence)`` first; the
        ``UNIQUE (work_id, sequence)`` constraint is the race backstop the
        check cannot be). A NEW command is stored at ``received`` — the
        ladder starts here whatever the caller believed. A REDELIVERED
        command (same work-scoped ``dedup_key``) loses the insert to the
        unique constraint and READS the winner: it returns the existing
        record with ``created=False``, having changed nothing — the
        duplicate is refused BEFORE any epoch bump or pause-state mutation
        the caller might perform next.
        """
        run_scope = command.payload.get("run_id")
        async with self._session_factory() as session:
            last = await session.scalar(
                select(func.max(ControlCommandRow.sequence)).where(
                    ControlCommandRow.work_id == command.work_id
                )
            )
            if command.sequence <= (last or 0):
                raise ValueError(
                    f"sequence {command.sequence} for work {command.work_id!r} is not "
                    f"strictly increasing (last accepted: {last or 0})"
                )
            row = ControlCommandRow(
                id=command.command_id,
                work_id=command.work_id,
                run_id=run_scope if isinstance(run_scope, str) else None,
                kind=command.kind,
                payload=dict(command.payload),
                status="received",
                sequence=command.sequence,
                dedup_key=command.idempotency_key,
                actor_ref=command.actor_ref,
                actor_origin=command.actor_origin,
                expected_plan_revision=command.expected_plan_revision,
                expected_execution_epoch=command.expected_execution_epoch,
                journal=[_journal_entry(None, "received")],
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                return await self._adopt_or_refuse(session, command, exc)
            return _to_command(row), True

    async def next_sequence(self, work_id: str) -> int:
        """The next per-work sequence to submit (``max + 1``).

        Transactional allocation is the caller's INSERT; this helper plus
        the ``UNIQUE (work_id, sequence)`` constraint give NXT-09's
        "concurrent inserts preserve one logical command and a monotonic
        work sequence": a racer that slips past the read loses the insert
        and is refused.
        """
        async with self._session_factory() as session:
            last = await session.scalar(
                select(func.max(ControlCommandRow.sequence)).where(
                    ControlCommandRow.work_id == work_id
                )
            )
            return (last or 0) + 1

    # -- the ladder ---------------------------------------------------------

    async def authorize(
        self, command_id: str, actor_scopes: dict[str, tuple[str, ...]]
    ) -> ControlCommand:
        """Check the actor against its OWN origin's scopes; authorize.

        ``actor_scopes`` maps origin -> the actor refs that origin may
        speak for; an actor listed under a different origin is
        impersonation, not a typo — :class:`PermissionError`. The
        transition itself is a guarded ``received -> authorized`` CAS.
        """
        async with self._session_factory() as session:
            row = await self._require(session, command_id)
            if row.status != "received":
                raise self._skip_error(command_id, row.status, ("received",))
            allowed = actor_scopes.get(row.actor_origin, ())
            if row.actor_ref not in allowed:
                raise PermissionError(
                    f"actor {row.actor_ref!r} is not listed under its own origin "
                    f"{row.actor_origin!r}"
                )
            return await self._advance(session, row, ("received",), "authorized")

    async def dispatch(
        self,
        command_id: str,
        *,
        current_plan_revision: int,
        current_execution_epoch: int,
        vendor_correlation_id: str = "",
    ) -> ControlCommand:
        """Compare-and-set the command's world, then record the dispatch INTENT.

        The CTL-04 CAS gate, moved to where NXT-12 needs it — BEFORE the
        vendor call: when ``expected_plan_revision`` /
        ``expected_execution_epoch`` are set and differ from the current
        values, the command EXPIRES instead of dispatching (it was
        written against a world that no longer exists). A live command
        moves to ``dispatching`` with the execution epoch and the vendor
        correlation persisted on the row FIRST — a crash after this point
        leaves an honest ``dispatching`` record to reconcile, never an
        ``applied`` claim the record cannot support.
        """
        async with self._session_factory() as session:
            row = await self._require(session, command_id)
            if row.status != "authorized":
                raise self._skip_error(command_id, row.status, ("authorized",))
            stale = (
                row.expected_plan_revision is not None
                and row.expected_plan_revision != current_plan_revision
            ) or (
                row.expected_execution_epoch is not None
                and row.expected_execution_epoch != current_execution_epoch
            )
            if stale:
                return await self._advance(
                    session,
                    row,
                    ("authorized",),
                    "expired",
                    reason="stale expectations: the command was written against a "
                    "plan revision / execution epoch that no longer holds",
                )
            return await self._advance(
                session,
                row,
                ("authorized",),
                "dispatching",
                epoch=current_execution_epoch,
                vendor_correlation_id=vendor_correlation_id or None,
            )

    async def vendor_accepted(self, command_id: str) -> ControlCommand:
        """The vendor took the effect (``dispatching -> vendor_accepted``)."""
        async with self._session_factory() as session:
            row = await self._require(session, command_id)
            if row.status != "dispatching":
                raise self._skip_error(command_id, row.status, ("dispatching",))
            return await self._advance(session, row, ("dispatching",), "vendor_accepted")

    async def outcome_unknown(self, command_id: str) -> ControlCommand:
        """The response was lost (``dispatching -> outcome_unknown``).

        The lost-response window: the request may or may not have landed.
        The record says UNKNOWN — never ``applied`` (unproven) and never
        silently retried (a non-idempotent steer). Recovery probes session
        / turn evidence and either climbs to ``applied`` via :meth:`observe`
        or surfaces the uncertainty to the operator.
        """
        async with self._session_factory() as session:
            row = await self._require(session, command_id)
            if row.status != "dispatching":
                raise self._skip_error(command_id, row.status, ("dispatching",))
            return await self._advance(
                session,
                row,
                ("dispatching",),
                "outcome_unknown",
                reason="vendor response lost — reconcile before any retry",
            )

    async def observe(self, command_id: str) -> ControlCommand:
        """Record the APPLICATION observed — the transition to ``applied``.

        Reachable only from ``vendor_accepted`` / ``outcome_unknown``:
        ``applied`` is booked only after the vendor rung, so "intended to
        send" can never be misread as "the agent applied it". Reaching it
        from ``outcome_unknown`` is the reconciliation path — evidence
        that the effect did land.
        """
        async with self._session_factory() as session:
            row = await self._require(session, command_id)
            if row.status not in ("vendor_accepted", "outcome_unknown"):
                raise self._skip_error(
                    command_id, row.status, ("vendor_accepted", "outcome_unknown")
                )
            return await self._advance(
                session, row, ("vendor_accepted", "outcome_unknown"), "applied"
            )

    async def checkpoint(self, command_id: str) -> ControlCommand:
        """Mark an applied command as carried into the durable checkpoint."""
        async with self._session_factory() as session:
            row = await self._require(session, command_id)
            if row.status != "applied":
                raise self._skip_error(command_id, row.status, ("applied",))
            return await self._advance(session, row, ("applied",), "checkpointed")

    # -- reads ----------------------------------------------------------------

    async def pending(self, work_id: str) -> list[ControlCommand]:
        """The work's received/authorized commands, in durable sequence order.

        ``dispatching`` and beyond are deliberately absent: a command that
        left the queue has a vendor-side fate recorded on its row, and the
        drain must not re-deliver it (the review's premature-``applied``
        ordering fix — the queue view stops at the dispatch boundary).
        """
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ControlCommandRow)
                        .where(
                            ControlCommandRow.work_id == work_id,
                            ControlCommandRow.status.in_(("received", "authorized")),
                        )
                        .order_by(ControlCommandRow.sequence)
                    )
                )
                .scalars()
                .all()
            )
        return [_to_command(row) for row in rows]

    async def get(self, command_id: str) -> ControlCommand | None:
        """One command by id, whatever rung it sits on (or ``None``)."""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ControlCommandRow).where(ControlCommandRow.id == command_id)
            )
        return _to_command(row) if row is not None else None

    # -- internals ------------------------------------------------------------

    async def _adopt_or_refuse(
        self, session: AsyncSession, command: ControlCommand, exc: IntegrityError
    ) -> tuple[ControlCommand, bool]:
        """Classify a lost insert race: adopt the dedup winner or refuse.

        The unique constraint is the arbiter, so a lost INSERT is the
        NORMAL duplicate path, not an error: when the winner is the row
        with the same ``(work_id, dedup_key)``, adopt it verbatim
        (``created=False`` — the caller's values are discarded, which is
        the point). Any other constraint loss (a concurrent sequence
        racer, a command_id reuse) is a protocol violation and raises.
        """
        winner = await session.scalar(
            select(ControlCommandRow).where(
                ControlCommandRow.work_id == command.work_id,
                ControlCommandRow.dedup_key == command.idempotency_key,
            )
        )
        if winner is not None:
            return _to_command(winner), False
        racer = await session.scalar(
            select(ControlCommandRow).where(
                ControlCommandRow.work_id == command.work_id,
                ControlCommandRow.sequence == command.sequence,
            )
        )
        if racer is not None:
            raise ValueError(
                f"sequence {command.sequence} for work {command.work_id!r} is not "
                f"strictly increasing (concurrent submit won: {racer.id!r})"
            ) from exc
        squatter = await session.scalar(
            select(ControlCommandRow).where(ControlCommandRow.id == command.command_id)
        )
        if squatter is not None:
            raise ValueError(
                f"command_id {command.command_id!r} already exists under a "
                f"different idempotency key"
            ) from exc
        raise exc

    @staticmethod
    async def _require(session: AsyncSession, command_id: str) -> ControlCommandRow:
        row = await session.scalar(
            select(ControlCommandRow).where(ControlCommandRow.id == command_id)
        )
        if row is None:
            raise KeyError(f"unknown command_id {command_id!r}")
        return row

    @staticmethod
    def _skip_error(command_id: str, status: str, expected: tuple[str, ...]) -> ValueError:
        return ValueError(
            f"command {command_id!r} is {status!r}; the ladder {_RUNG_ORDER} "
            f"refuses to skip from anything but {' or '.join(repr(rung) for rung in expected)}"
        )

    @staticmethod
    async def _advance(
        session: AsyncSession,
        row: ControlCommandRow,
        from_statuses: tuple[str, ...],
        to_status: str,
        **journal_extra: Any,
    ) -> ControlCommand:
        """One guarded ``UPDATE ... WHERE status IN <expected>`` transition.

        The WHERE clause is the compare-and-set: if a concurrent writer
        moved the command between the read and the write, zero rows
        match and the transition is REFUSED (the caller reloads and
        retries — it never writes on top of a state it did not see). The
        journal append rides the same statement, so the audit entry and
        the transition are one atomic change.
        """
        journal = list(row.journal or [])
        journal.append(_journal_entry(row.status, to_status, **journal_extra))
        values: dict[str, Any] = {"status": to_status, "journal": journal}
        if to_status == "applied":
            values["applied_at"] = _utcnow()
        if "epoch" in journal_extra:
            values["epoch"] = journal_extra["epoch"]
        result = await session.execute(
            update(ControlCommandRow)
            .where(
                ControlCommandRow.id == row.id,
                ControlCommandRow.status.in_(from_statuses),
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        # A DML execute returns a CursorResult (the typed base Result has
        # no rowcount); one matched row is the CAS win.
        matched = cast("CursorResult[Any]", result).rowcount
        if matched != 1:
            raise ValueError(
                f"command {row.id!r} lost the compare-and-set to "
                f"{to_status!r}: it moved concurrently — reload and retry"
            )
        await session.commit()
        # The CAS wrote exactly ``values`` on top of the state we read, so
        # the row's contract-visible fields are the ones we hold (a commit
        # with expire_on_commit simply re-reads them); patch the status we
        # just wrote and return.
        return _to_command(row).model_copy(update={"status": to_status})
