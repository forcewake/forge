"""The durable publication pause fence (R28-08, review 1ae5290 §6).

The reviewer's contract: a pause's epoch check must be verified as ONE
persisted authority state on BOTH sides — in control processing AND
immediately before publication. Before this module the pause authority
lived in two places that a publisher never saw: ``OperatorControlService.
pause_states`` (process-local dict, CTL-05) and the lane process's own
drain of the pause command. The classic publisher's
:func:`forge.integrations.github_flow.GitHubPublishFlow.publish_validated`
re-checked only run cancellation — a lane or API restart erased the
in-memory pause, and a stale worker holding a previously valid candidate
could still land a commit.

This module is the persisted fence both sides share:

- :func:`raise_pause_fence` — when a pause lands (the command is recorded
  and the publication epoch bumped), ONE row per work records
  ``(work_id, publication_epoch_bumped, fenced_at)``. The row is the
  authority: killing the API process, the worker, or both does not erase
  it, and a late candidate callback from the old lane is refused by the
  row's mere existence.
- :func:`pause_fence_decision` — the publisher-side read, evaluated at
  the NATIVE-effect boundary (immediately before the commit-API call).
  A fenced work yields a refused publication with an actionable reason;
  a missing or cleared row publishes.
- :func:`clear_pause_fence` — resume clears the fence under a NEW
  publication epoch (``resumed_publication_epoch``): the old epoch's
  grants stay dead, and the resume that reopens publication is the same
  durable decision an operator can audit.

Shape mirrors the control tables (migration 022): one row per work, CAS
at the application layer (read-modify-write inside one session; the row
is per-work so writers serialize on the primary key), timestamps
timezone-aware. The epoch column is monotonic — re-arming a cleared
fence (pause after resume) keeps the HIGHER epoch, so a replayed pause
command can never roll the authority backwards.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from forge.models.base import Base

__all__ = [
    "PauseFenceDecision",
    "PauseFenceRow",
    "clear_pause_fence",
    "pause_fence_decision",
    "raise_pause_fence",
]

#: Anything that yields sessions — ``async_sessionmaker`` duck-types here
#: (the same seam :mod:`forge.adaptive.discovery_stage` codes against).
SessionFactory = Callable[[], AbstractAsyncContextManager[Any]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PauseFenceRow(Base):
    """One work's durable pause fence (R28-08, migration 022).

    A row with ``cleared_at IS NULL`` means publication for this work is
    FENCED: no new provider effects may be authorized, whatever a stale
    process presents. ``publication_epoch_bumped`` is the epoch the pause
    bumped TO (grants of every lower epoch are dead). Resume writes
    ``cleared_at`` + ``resumed_publication_epoch`` (the new epoch the
    resumed work runs under) — the row is kept, never deleted, so the
    pause/resume history stays auditable.
    """

    __tablename__ = "pause_fences"

    work_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    publication_epoch_bumped: Mapped[int] = mapped_column(Integer, nullable=False)
    fenced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    #: The pause command's id (when the raiser knows it) — audit lineage.
    raised_by_command: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The NEW publication epoch the resume opened (never reused from the
    #: paused epoch — the old publication grant stays revoked).
    resumed_publication_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cleared_by_command: Mapped[str | None] = mapped_column(String(64), nullable=True)


@dataclass(frozen=True)
class PauseFenceDecision:
    """The fence's answer for one work, as the publisher consumes it.

    ``fenced`` is the whole contract for the publisher: True means refuse
    the native effect. The remaining fields make the refusal actionable
    (which epoch fenced the work, when) and ride the blocked outcome's
    reason so an operator sees the persisted authority, not a guess.
    """

    work_id: str
    fenced: bool
    publication_epoch_bumped: int = 0
    fenced_at: datetime | None = None
    resumed_publication_epoch: int | None = None

    @property
    def reason(self) -> str:
        """The actionable refusal sentence ('' when not fenced)."""
        if not self.fenced:
            return ""
        stamp = self.fenced_at.isoformat() if self.fenced_at else "?"
        return (
            f"pause fence active for work {self.work_id}: publication epoch "
            f"{self.publication_epoch_bumped} fenced at {stamp} — resume "
            "clears it under a new epoch"
        )


async def raise_pause_fence(
    session_factory: SessionFactory,
    work_id: str,
    *,
    publication_epoch_bumped: int,
    raised_by_command: str = "",
) -> PauseFenceDecision:
    """Persist (or re-arm) the work's pause fence; return the decision.

    Idempotent and monotonic: an already-active fence keeps the HIGHER
    epoch (a replayed pause command must not move the authority back),
    and a cleared fence re-arms with a fresh ``fenced_at`` (pause after
    resume). Called by control processing the moment the pause is on
    record — the same transaction boundary ordering CTL-05 uses (the
    fence is durable BEFORE the interrupt matters, because the publisher
    reads the row, not the interrupt's fate).
    """
    epoch = int(publication_epoch_bumped)
    async with session_factory() as session:
        row = await session.get(PauseFenceRow, work_id)
        now = _utcnow()
        if row is None:
            row = PauseFenceRow(
                work_id=work_id,
                publication_epoch_bumped=epoch,
                fenced_at=now,
                raised_by_command=raised_by_command or None,
            )
            session.add(row)
        else:
            row.publication_epoch_bumped = max(row.publication_epoch_bumped, epoch)
            row.fenced_at = now
            row.cleared_at = None
            row.resumed_publication_epoch = None
            row.cleared_by_command = None
            if raised_by_command:
                row.raised_by_command = raised_by_command
        await session.commit()
    return PauseFenceDecision(
        work_id=work_id,
        fenced=True,
        publication_epoch_bumped=epoch,
        fenced_at=now,
    )


async def pause_fence_decision(session_factory: SessionFactory, work_id: str) -> PauseFenceDecision:
    """The publisher-side read: is publication for *work_id* fenced?

    Durable by construction — the answer comes from the committed row,
    never from process memory, so a restarted API/worker sees the same
    fence the pause left. A missing or cleared row is not fenced (the
    resume's new epoch owns publication from there).
    """
    async with session_factory() as session:
        row = await session.get(PauseFenceRow, work_id)
    if row is None:
        return PauseFenceDecision(work_id=work_id, fenced=False)
    if row.cleared_at is not None:
        return PauseFenceDecision(
            work_id=work_id,
            fenced=False,
            publication_epoch_bumped=row.publication_epoch_bumped,
            resumed_publication_epoch=row.resumed_publication_epoch,
        )
    return PauseFenceDecision(
        work_id=work_id,
        fenced=True,
        publication_epoch_bumped=row.publication_epoch_bumped,
        fenced_at=row.fenced_at,
    )


async def clear_pause_fence(
    session_factory: SessionFactory,
    work_id: str,
    *,
    resumed_publication_epoch: int | None = None,
    cleared_by_command: str = "",
) -> bool:
    """Resume clears the fence under a NEW publication epoch.

    ``resumed_publication_epoch`` defaults to the fenced epoch + 1 (the
    CTL-06 fresh-epoch rule): the old epoch's publication grants stay
    dead and the resumed work runs under a distinguishable one. Returns
    whether an active fence was actually cleared (False for a missing or
    already-cleared fence — idempotent, never an error).
    """
    async with session_factory() as session:
        row = await session.get(PauseFenceRow, work_id)
        if row is None or row.cleared_at is not None:
            return False
        row.cleared_at = _utcnow()
        row.resumed_publication_epoch = (
            row.publication_epoch_bumped + 1
            if resumed_publication_epoch is None
            else int(resumed_publication_epoch)
        )
        row.cleared_by_command = cleared_by_command or None
        await session.commit()
        return True
