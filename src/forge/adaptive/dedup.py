"""Database-enforced deduplication for checkpoints and control events (FND-06).

Why the database is the arbiter: interactive retries mean two workers can
record the same checkpoint, or replay the same command delivery, at the
same moment. A query-then-insert convention has a select-then-insert
window that both racers slip through; the unique constraint closes it.
Whichever transaction commits first wins, and the loser READS the
winner's row instead of writing its own — first-result-wins, enforced by
the index rather than by discipline. One authoritative row per identity
is what lets a redelivered command avoid spending another iteration or
granting another approval.

Why independent epochs: publication ("what the world has seen"),
checkpoint ("how far execution is persisted") and control ("how many
command deliveries were consumed") advance for different reasons. A
checkpoint between publications, or a control epoch bump without new
output, must not be misread as progress on the other axes — so they are
three counters, never one.

Why projections are rebuilt, not trusted: event history is immutable and
append-only; the mutable projection (status, last sequence, spent kinds)
is DERIVED from it. After a crash, replaying the history reconstructs
exactly where the stream stood without rerunning any side effect, and a
rejected decision from the past can never be overwritten by a summary.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def insert_or_read(
    session_factory: async_sessionmaker[AsyncSession],
    model_class: type,
    unique_key_fields: dict[str, Any],
    values: dict[str, Any],
) -> tuple[Any, bool]:
    """Transactional insert-or-read with FIRST-RESULT-WINS semantics.

    Try the INSERT; when the unique constraint rejects it, roll back and
    SELECT the surviving row by *unique_key_fields*. Returns ``(row,
    created)`` — ``created=False`` means another transaction got there
    first and the CALLER'S values were discarded, which is the point: the
    earliest record of an event stays authoritative. Callers must ensure
    the unique key is the only constraint the insert can violate (a
    CHECK failure would surface as an IntegrityError here too, and the
    follow-up SELECT would then find no winner).
    """
    async with session_factory() as session:
        row = model_class(**unique_key_fields, **values)
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:
            # Lost the race: the constraint is the dedup arbiter, so roll
            # back the losing write and adopt the winner's row verbatim.
            await session.rollback()
            winner: object = (
                await session.execute(select(model_class).filter_by(**unique_key_fields))
            ).scalar_one()
            return winner, False
        return row, True


@dataclass(frozen=True)
class Epochs:
    """Three independent monotonic counters, persisted as int columns.

    The caller supplies the row (a flow run, a work package); these fields
    map one-to-one onto ``publication_epoch`` / ``checkpoint_epoch`` /
    ``control_epoch`` integer columns. Frozen: a bump returns a NEW value
    object to record, so an in-flight reader can never observe a torn
    counter update, and each ``next_*`` advances exactly one axis.
    """

    publication_epoch: int = 0
    checkpoint_epoch: int = 0
    control_epoch: int = 0

    def next_publication(self) -> Epochs:
        """Record one more publication; checkpoint and control stand still."""
        return replace(self, publication_epoch=self.publication_epoch + 1)

    def next_checkpoint(self) -> Epochs:
        """Record one more checkpoint; publication and control stand still."""
        return replace(self, checkpoint_epoch=self.checkpoint_epoch + 1)

    def next_control(self) -> Epochs:
        """Record one more consumed control delivery; the rest stand still."""
        return replace(self, control_epoch=self.control_epoch + 1)


def rebuild_projection(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Rebuild the mutable projection from IMMUTABLE event history.

    Each event is ``{"sequence": int, "kind": str, "status": "applied" |
    "rejected"}``. The projection records: the status of the HIGHEST
    sequence event (the decision that actually won, not the last delivered
    row), the last sequence seen (the replay watermark), and the kinds
    that were APPLIED — only applied events spent an effect, so a rejected
    command replays as unspent. An empty history projects to the pristine
    ``"idle"`` state. Recovery reruns no side effects; it only re-derives.
    """
    ordered = sorted(events, key=lambda event: event["sequence"])
    latest = ordered[-1] if ordered else None
    return {
        "status": latest["status"] if latest is not None else "idle",
        "last_sequence": latest["sequence"] if latest is not None else 0,
        "applied_kinds": {event["kind"] for event in ordered if event["status"] == "applied"},
    }
