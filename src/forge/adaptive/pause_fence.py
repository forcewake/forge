"""The durable publication pause fence (R28-08, review 1ae5290 §6; NEXT-02).

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
  row's mere existence. One atomic conditional UPDATE (NEXT-02): the
  epoch only ever RISES and a cleared fence re-arms with a fresh
  ``fenced_at`` — there is no read-modify-write window that a concurrent
  clear or raise could interleave with.
- :func:`pause_fence_decision` — the publisher-side read, evaluated at
  the NATIVE-effect boundary (immediately before the commit-API call).
  A fenced work yields a refused publication with an actionable reason;
  a missing or cleared row publishes — UNLESS the caller pins the
  CANDIDATE's grant generation (NEXT-02's ``expected_generation``): a
  candidate whose grant is below the fence's current generation stays
  refused even after the clear, because the resume that reopened
  publication killed the old attempt's authority.
- :func:`clear_pause_fence` — resume clears the fence under a NEW
  publication epoch (``resumed_publication_epoch``) with ONE conditional
  UPDATE: ``WHERE`` the fence is still active ``AND`` — when the caller
  pins ``expected_publication_epoch`` — its epoch is exactly the one the
  caller decided to clear. Two concurrent clears cannot both win, a
  pause that re-armed between the caller's read and the write fails the
  CAS instead of being silently overwritten, and the old epoch's grants
  stay dead.

Shape mirrors the control tables (migration 022): one row per work,
conditional single-statement transitions at the application layer,
timestamps timezone-aware. The epoch column is monotonic — re-arming a
cleared fence (pause after resume) keeps the HIGHER epoch, so a replayed
pause command can never roll the authority backwards.

NEXT-02's generation axis: the fence's epochs and the run's durable
attempt generation (``FlowRun.cancellation_generation``) share one rule —
a transition that opens a NEW attempt (retry / re-dispatch, a resume
that dispatches a new runner) moves BOTH above every retired grant. The
stale-grant refusal engages exactly when the axes are aligned (the
fence's resumed epoch has been reached by the run's own generation); a
same-attempt steering resume — no new attempt, the continuing claim
keeps its identity — reopens publication for the live attempt without
renaming it.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Integer, String, case, update
from sqlalchemy.exc import IntegrityError
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
    the native effect — whether because the pause is active or because
    the caller's candidate grant (``expected_generation``) belongs to an
    attempt the resume already retired. The remaining fields make the
    refusal actionable (which epoch fenced the work, when, what the
    stale grant was) and ride the blocked outcome's reason so an
    operator sees the persisted authority, not a guess.
    """

    work_id: str
    fenced: bool
    publication_epoch_bumped: int = 0
    fenced_at: datetime | None = None
    resumed_publication_epoch: int | None = None
    #: NEXT-02: the candidate grant the caller pinned (``None`` = the
    #: caller holds no generation-scoped grant — legacy/direct callers).
    expected_generation: int | None = None
    #: NEXT-02: set when the refusal is a STALE GRANT (the fence is
    #: cleared; the candidate's generation is simply below the resumed
    #: one) — the audit distinction between "paused" and "superseded".
    stale_generation: int | None = None

    @property
    def reason(self) -> str:
        """The actionable refusal sentence ('' when not fenced)."""
        if not self.fenced:
            return ""
        if self.stale_generation is not None:
            current = self.resumed_publication_epoch
            return (
                f"stale publication grant for work {self.work_id}: candidate generation "
                f"{self.stale_generation} is below the resumed generation {current} — "
                "the attempt that minted this grant died with the resume; only the "
                "current attempt's grants publish"
            )
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

    Idempotent and monotonic, in ONE conditional statement (NEXT-02 —
    no read-modify-write window): the epoch only ever RISES
    (``max(row, provided)``) and a cleared fence re-arms with a fresh
    ``fenced_at`` (pause after resume). Two concurrent raises, or a
    raise racing a clear, can never interleave: each is a single UPDATE
    the row's primary key serializes, and the epoch CASE keeps the
    authority monotonic whatever order they land in. Called by control
    processing the moment the pause is on record — the same transaction
    boundary ordering CTL-05 uses (the fence is durable BEFORE the
    interrupt matters, because the publisher reads the row, not the
    interrupt's fate).
    """
    epoch = int(publication_epoch_bumped)
    now = _utcnow()
    async with session_factory() as session:
        bumped = case(
            (PauseFenceRow.publication_epoch_bumped < epoch, epoch),
            else_=PauseFenceRow.publication_epoch_bumped,
        )
        result = await session.execute(
            update(PauseFenceRow)
            .where(PauseFenceRow.work_id == work_id)
            .values(
                publication_epoch_bumped=bumped,
                fenced_at=now,
                cleared_at=None,
                resumed_publication_epoch=None,
                cleared_by_command=None,
                raised_by_command=(raised_by_command or None) or PauseFenceRow.raised_by_command,
            )
            .execution_options(synchronize_session=False)
        )
        if not result.rowcount:
            try:
                session.add(
                    PauseFenceRow(
                        work_id=work_id,
                        publication_epoch_bumped=epoch,
                        fenced_at=now,
                        raised_by_command=raised_by_command or None,
                    )
                )
                await session.flush()
            except IntegrityError:
                # A concurrent first-raise won the insert — its row is on
                # record; re-run the monotonic UPDATE once on top of it.
                await session.rollback()
                await session.execute(
                    update(PauseFenceRow)
                    .where(PauseFenceRow.work_id == work_id)
                    .values(
                        publication_epoch_bumped=bumped,
                        fenced_at=now,
                        cleared_at=None,
                        resumed_publication_epoch=None,
                        cleared_by_command=None,
                        raised_by_command=(raised_by_command or None)
                        or PauseFenceRow.raised_by_command,
                    )
                    .execution_options(synchronize_session=False)
                )
        await session.commit()
        row = await session.get(PauseFenceRow, work_id)
    return PauseFenceDecision(
        work_id=work_id,
        fenced=True,
        publication_epoch_bumped=int(row.publication_epoch_bumped) if row else epoch,
        fenced_at=row.fenced_at if row is not None else now,
    )


async def pause_fence_decision(
    session_factory: SessionFactory,
    work_id: str,
    *,
    expected_generation: int | None = None,
) -> PauseFenceDecision:
    """The publisher-side read: is publication for *work_id* fenced?

    Durable by construction — the answer comes from the committed row,
    never from process memory, so a restarted API/worker sees the same
    fence the pause left. A missing row is not fenced. An ACTIVE row
    (``cleared_at IS NULL``) is fenced — the pause refuses publication
    whatever the caller holds.

    NEXT-02 — the conditional leg: a CLEARED row is fenced for a caller
    whose candidate GRANT generation (``expected_generation`` — the
    generation the candidate's dispatch/claim was minted under) is below
    the fence's current generation (``resumed_publication_epoch``, the
    epoch the resume opened): the old attempt's authority died with the
    resume, and clearing the fence reopened publication only for grants
    at or above the resumed generation. The stale-grant check engages
    only when the axes are KNOWN aligned — the run's own durable
    generation has reached the resumed epoch (a retry / resume
    re-dispatch bumped it there); a same-attempt steering resume keeps
    the live attempt's identity and does not rename its grants.
    ``expected_generation=None`` (a caller with no generation-scoped
    grant) keeps the plain fenced/cleared contract.
    """
    expected = None if expected_generation is None else int(expected_generation)
    async with session_factory() as session:
        row = await session.get(PauseFenceRow, work_id)
        run_generation: int | None = None
        if row is not None and row.cleared_at is not None and expected is not None:
            run_generation = await _run_generation(session, work_id)
    if row is None:
        return PauseFenceDecision(work_id=work_id, fenced=False, expected_generation=expected)
    if row.cleared_at is not None:
        current = (
            int(row.resumed_publication_epoch)
            if row.resumed_publication_epoch is not None
            else None
        )
        aligned = current is not None and run_generation is not None and current <= run_generation
        if expected is not None and current is not None and expected < current and aligned:
            return PauseFenceDecision(
                work_id=work_id,
                fenced=True,
                publication_epoch_bumped=int(row.publication_epoch_bumped),
                resumed_publication_epoch=current,
                expected_generation=expected,
                stale_generation=expected,
            )
        return PauseFenceDecision(
            work_id=work_id,
            fenced=False,
            publication_epoch_bumped=int(row.publication_epoch_bumped),
            resumed_publication_epoch=current,
            expected_generation=expected,
        )
    return PauseFenceDecision(
        work_id=work_id,
        fenced=True,
        publication_epoch_bumped=int(row.publication_epoch_bumped),
        fenced_at=row.fenced_at,
        expected_generation=expected,
    )


async def _run_generation(session: Any, work_id: str) -> int | None:
    """The run's durable attempt generation, or None when unreadable.

    Read inside the caller's session (one round trip, no nested
    session); an unreadable row is ``None`` — the fence then cannot
    CONFIRM axis alignment and the stale-grant check stands down rather
    than guessing.
    """
    from sqlalchemy import select

    from forge.durable.models import FlowRun

    try:
        generation = await session.scalar(
            select(FlowRun.cancellation_generation).where(FlowRun.id == work_id)
        )
    except Exception:  # noqa: BLE001 — alignment confirmation is best-effort
        return None
    return int(generation) if generation is not None else None


async def clear_pause_fence(
    session_factory: SessionFactory,
    work_id: str,
    *,
    resumed_publication_epoch: int | None = None,
    cleared_by_command: str = "",
    expected_publication_epoch: int | None = None,
) -> bool:
    """Resume clears the fence under a NEW publication epoch — atomically.

    ONE conditional UPDATE (NEXT-02): the clear lands only on an ACTIVE
    fence (``cleared_at IS NULL``) whose ``publication_epoch_bumped`` is
    exactly ``expected_publication_epoch`` when the caller pins it — the
    compare-and-swap that makes two concurrent resumes, or a resume
    racing a re-arming pause, decide ONE winner instead of overwriting
    each other. The new epoch is computed IN the statement
    (``publication_epoch_bumped + 1``) so the generation bump and the
    clear are the same atomic write.

    ``resumed_publication_epoch`` explicitly given overrides the bumped
    value (the CTL-06 fresh-epoch rule stays expressible). Returns
    whether an active fence was actually cleared (False for a missing or
    already-cleared fence, or a failed CAS — idempotent, never an error).
    """
    async with session_factory() as session:
        new_epoch = (
            int(resumed_publication_epoch)
            if resumed_publication_epoch is not None
            else PauseFenceRow.publication_epoch_bumped + 1
        )
        statement = (
            update(PauseFenceRow)
            .where(PauseFenceRow.work_id == work_id, PauseFenceRow.cleared_at.is_(None))
            .values(
                cleared_at=_utcnow(),
                resumed_publication_epoch=new_epoch,
                cleared_by_command=cleared_by_command or None,
            )
            .execution_options(synchronize_session=False)
        )
        if expected_publication_epoch is not None:
            statement = statement.where(
                PauseFenceRow.publication_epoch_bumped == int(expected_publication_epoch)
            )
        result = await session.execute(statement)
        await session.commit()
        return bool(result.rowcount)
