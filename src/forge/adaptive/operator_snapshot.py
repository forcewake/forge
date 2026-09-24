"""The authorized snapshot reader (R36-15) — projection inputs from live rows.

The pure projection (:mod:`forge.adaptive.operator_view`) and the export
pack (:mod:`forge.adaptive.support_bundle`) are shapes over durable rows;
this module is the ONE subject-scoped async reader that assembles those
rows from the durable authorities — the run row, the revival-attempt
records, the control commands and their deliveries, the checkpoint
repository, the pause fence, the verification verdict in the run's
evidence, the publication intents, the gate approvals and the admission
leases. Nothing here derives a state, renders a document or offers an
action — it READS rows and maps them onto the documented shapes, and it
is the only place a live surface should look for them.

Repository-scope enforcement is the module's first rule: the reader takes
an authorized SUBJECT SCOPE (repository full names) and every run query
filters by it — the run row is selected by ``id AND subject IN scope``,
and every per-run section is keyed off a run id that query already
authorized, so a token scoped to repo A never sees repo B's runs on the
list, detail or support-bundle path (pinned by tests).

Coverage honesty is the second rule: the snapshot carries a
``source_coverage`` map — ``present`` (queried, rows exist), ``missing``
(queryed, nothing there) and ``unknown`` (never queried, or the authority
was unreachable) — and a source that was not queried is NEVER reported as
an empty success. ``questions`` has no durable authority today, so it is
``unknown`` by construction; a checkpoint repository that raises
:class:`~forge.adaptive.checkpoint_repository.
CheckpointRepositoryUnavailable` makes ``checkpoints`` ``unknown``, never
"no checkpoint". ``projection_age`` says how old the newest observed row
is — the freshness number an operator reads before trusting the state.

The documented row mappings (durable column → view shape):

- ``run`` — :class:`~forge.durable.models.FlowRun`, with ``status_reason``
  as ``blocked_reason`` and ``github_repo_full_name`` as the SUBJECT
  identity (the column GitHub and Azure runs carry their repo full name
  in; a run without a full-name subject is outside every full-name scope);
- ``attempts`` — :class:`~forge.durable.models.ActionLog` rows whose
  ``retryability`` is set (the documented durable REVIVAL ATTEMPT record,
  A11): ``requested → executing`` (in flight), ``succeeded → succeeded``,
  ``failed → failed``, ``unknown_outcome → unknown``;
- ``commands`` / ``deliveries`` —
  :class:`~forge.adaptive.mailbox_db.ControlCommandRow` /
  :class:`~forge.adaptive.mailbox_db.ControlCommandDeliveryRow` for the
  run's work, in sequence order;
- ``checkpoints`` — the injected
  :class:`~forge.adaptive.checkpoint_repository.CheckpointRepository`'s
  ACTIVE entry (its content address doubles as the digest), the fence
  word from :class:`~forge.adaptive.pause_fence.PauseFenceRow`
  (``held`` while uncleared, ``cleared`` after a resume), and the
  activation proof from the latest ``resume`` command that reached
  ``applied``/``checkpointed`` — the ladder rung where the lane applied
  the restored bytes;
- ``verifications`` — the run's ``evidence["verification"]`` fragment
  (the unified R02 shape: ``status``/``tested_oid``/``observed_at``);
- ``publications`` — :class:`~forge.durable.models.PublicationIntent`
  rows for the run;
- ``approvals`` — :class:`~forge.durable.models.GateApproval` rows;
- ``occupancy`` — :class:`~forge.adaptive.admission.ExecutionLease` rows
  for the run with the DERIVED occupancy word (the admission/lease
  visibility the operator needs beside the state);
- ``saga`` — derived, never stored: ``partially_published`` exactly when
  unresolved publication intents exist (the same condition the
  projection's unresolved-effects line reports).

READ-ONLY BY CHARTER, exactly like the view it feeds: the reader issues
SELECTs and repository reads only — a test pins that rendering through a
recording repository performs no ``put``/``pin``/``unpin``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from forge.adaptive.admission import ExecutionLease, lease_occupancy
from forge.adaptive.checkpoint_repository import (
    CheckpointRepository,
    CheckpointRepositoryUnavailable,
)
from forge.adaptive.operator_view import (
    UNRESOLVED_PUBLICATION_STATUSES,
    OperatorProjection,
    _as_datetime,
    initial_projection,
)

logger = logging.getLogger(__name__)

__all__ = [
    "COVERAGE_SECTIONS",
    "COVERAGE_UNKNOWN",
    "COVERAGE_MISSING",
    "COVERAGE_PRESENT",
    "OperatorSnapshot",
    "OperatorSnapshotReader",
    "operator_scope_of_run",
]


#: The closed coverage vocabulary (mirrors the support bundle's).
COVERAGE_PRESENT: str = "present"
COVERAGE_MISSING: str = "missing"
COVERAGE_UNKNOWN: str = "unknown"

#: The sections a snapshot tracks coverage for. ``questions`` has no
#: durable authority (the control plane holds open questions in the
#: mailbox payloads, not a queryable table), so it is ``unknown`` by
#: construction — an honest "not observed", never an invented empty set.
COVERAGE_SECTIONS: tuple[str, ...] = (
    "run",
    "attempts",
    "commands",
    "deliveries",
    "checkpoints",
    "verifications",
    "publications",
    "approvals",
    "questions",
    "occupancy",
)

#: ActionLog statuses → the operator attempt vocabulary. A revival
#: attempt's ``requested`` row is an attempt IN FLIGHT (the dispatch leg
#: has not answered); ``unknown_outcome`` stays ``unknown`` — an ending
#: forge cannot prove is never guessed into failed or succeeded.
_ATTEMPT_STATUS: Mapping[str, str] = {
    "requested": "executing",
    "succeeded": "succeeded",
    "failed": "failed",
    "unknown_outcome": "unknown",
}

#: The rung at which a resume's bytes were APPLIED by the lane — the
#: activation proof a ``resumed`` state stands on.
_RESUME_APPLIED_STATUSES: frozenset[str] = frozenset({"applied", "checkpointed"})


def _iso(moment: datetime | None) -> str:
    """UTC ISO string (naive values read as UTC — sqlite stores UTC)."""
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _norm_scope(scope: Sequence[str] | Iterable[str]) -> tuple[str, ...]:
    """The canonical subject scope: stripped, non-empty, deduped, sorted."""
    return tuple(sorted({str(repo).strip() for repo in scope if str(repo).strip()}))


@dataclass(frozen=True)
class OperatorSnapshot:
    """One run's durable rows plus the honesty metadata around them.

    ``rows`` is the :mod:`forge.adaptive.operator_view` source-rows shape
    (feed it to ``initial_projection`` / ``derive_state`` /
    ``SupportBundle.build`` verbatim); ``source_coverage`` marks every
    section ``present | missing | unknown``; ``projection_age_s`` is how
    many seconds old the NEWEST observed row was at ``computed_at``
    (``None`` when no row carried a readable clock). ``occupancy`` is the
    admission/lease visibility slice (not a projection input — a separate
    fact the operator surface renders beside the state).
    """

    run_id: str
    subject: str
    rows: dict[str, Any]
    source_coverage: dict[str, str]
    occupancy: tuple[dict[str, Any], ...] = field(default=())
    computed_at: str = ""
    projection_age_s: float | None = None

    def projection(self, now: datetime | str | None = None) -> OperatorProjection:
        """The projection over this snapshot's rows (an ephemeral v1)."""
        moment = now if now is not None else (self.computed_at or None)
        return initial_projection(self.rows, moment)


def operator_scope_of_run(run: Any) -> str:
    """The run's SUBJECT identity — the repo full name its durable row
    carries (``""`` when the row has none; such a run is outside every
    full-name scope)."""
    return str(getattr(run, "github_repo_full_name", "") or "").strip()


class OperatorSnapshotReader:
    """The one subject-scoped reader assembling projection inputs.

    Every public method takes the authorized subject scope and every run
    query filters by it — ``list_snapshots`` selects runs whose subject is
    IN the scope, ``snapshot`` selects THE run by id AND subject, and the
    per-run sections key off a run id one of those queries already
    authorized. A run the scope does not name is indistinguishable from a
    run that does not exist (``None``), on every path.
    """

    def __init__(
        self,
        session_factory: Any,
        *,
        checkpoint_repository: CheckpointRepository | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._checkpoints = checkpoint_repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # -- the scoped queries -------------------------------------------------

    async def list_snapshots(self, scope: Sequence[str]) -> list[OperatorSnapshot]:
        """A snapshot for EVERY run inside *scope*, newest first."""
        authorized = _norm_scope(scope)
        if not authorized:
            return []
        from forge.durable.models import FlowRun

        async with self._session_factory() as session:
            runs = list(
                (
                    await session.execute(
                        select(FlowRun)
                        .where(FlowRun.github_repo_full_name.in_(authorized))
                        .order_by(FlowRun.updated_at.desc(), FlowRun.id.desc())
                    )
                )
                .scalars()
                .all()
            )
        snapshots = [await self._snapshot_of(run) for run in runs]
        return [snapshot for snapshot in snapshots if snapshot is not None]

    async def snapshot(self, run_id: str, scope: Sequence[str]) -> OperatorSnapshot | None:
        """The snapshot for *run_id* — ``None`` unless the scope names its
        subject (out-of-scope and unknown are the same answer)."""
        authorized = _norm_scope(scope)
        if not authorized:
            return None
        from forge.durable.models import FlowRun

        async with self._session_factory() as session:
            run = await session.scalar(
                select(FlowRun).where(
                    FlowRun.id == run_id,
                    FlowRun.github_repo_full_name.in_(authorized),
                )
            )
        if run is None:
            return None
        return await self._snapshot_of(run)

    # -- the assembly ---------------------------------------------------

    async def _snapshot_of(self, run: Any) -> OperatorSnapshot | None:
        """One run row → the full snapshot (sections scoped by run id)."""
        from forge.adaptive.mailbox_db import ControlCommandDeliveryRow, ControlCommandRow
        from forge.adaptive.pause_fence import PauseFenceRow
        from forge.durable.models import ActionLog, GateApproval, PublicationIntent

        run_id = str(run.id)
        now = self._clock()
        coverage: dict[str, str] = {section: COVERAGE_UNKNOWN for section in COVERAGE_SECTIONS}
        rows: dict[str, Any] = {"run": self._run_row(run)}
        coverage["run"] = COVERAGE_PRESENT

        # -- attempts: the durable revival records (A11) --------------------
        try:
            actions = list(
                (
                    await self._execute(
                        select(ActionLog)
                        .where(ActionLog.flow_run_id == run_id, ActionLog.retryability.is_not(None))
                        .order_by(ActionLog.created_at.asc(), ActionLog.id.asc())
                    )
                )
                .scalars()
                .all()
            )
        except SQLAlchemyError:
            logger.warning("attempt rows for run %s unreadable — coverage unknown", run_id)
            actions = None
        if actions is not None:
            rows["attempts"] = [self._attempt_row(row, run) for row in actions]
            coverage["attempts"] = COVERAGE_PRESENT if rows["attempts"] else COVERAGE_MISSING

        # -- commands + deliveries (the control history) --------------------
        try:
            commands = list(
                (
                    await self._execute(
                        select(ControlCommandRow)
                        .where(ControlCommandRow.work_id == run_id)
                        .order_by(ControlCommandRow.sequence.asc(), ControlCommandRow.id.asc())
                    )
                )
                .scalars()
                .all()
            )
        except SQLAlchemyError:
            logger.warning("command rows for run %s unreadable — coverage unknown", run_id)
            commands = None
        if commands is not None:
            rows["commands"] = [self._command_row(row) for row in commands]
            coverage["commands"] = COVERAGE_PRESENT if commands else COVERAGE_MISSING
        try:
            deliveries = list(
                (
                    await self._execute(
                        select(ControlCommandDeliveryRow)
                        .where(ControlCommandDeliveryRow.work_id == run_id)
                        .order_by(
                            ControlCommandDeliveryRow.command_id.asc(),
                            ControlCommandDeliveryRow.recipient.asc(),
                        )
                    )
                )
                .scalars()
                .all()
            )
        except SQLAlchemyError:
            logger.warning("delivery rows for run %s unreadable — coverage unknown", run_id)
            deliveries = None
        if deliveries is not None:
            rows["deliveries"] = [self._delivery_row(row) for row in deliveries]
            coverage["deliveries"] = COVERAGE_PRESENT if deliveries else COVERAGE_MISSING

        # -- the pause fence (the safely_paused evidence) --------------------
        try:
            fence_row = await self._scalar(
                select(PauseFenceRow).where(PauseFenceRow.work_id == run_id)
            )
        except SQLAlchemyError:
            logger.warning("pause fence for run %s unreadable — fence unknown", run_id)
            fence_row = ...
        if fence_row is ...:
            fence = ""
        elif fence_row is None:
            fence = ""
        else:
            fence = "cleared" if fence_row.cleared_at is not None else "held"

        # -- checkpoints: the ONE injected authority ------------------------
        checkpoints: list[dict[str, Any]] | None = None
        if self._checkpoints is None:
            coverage["checkpoints"] = COVERAGE_UNKNOWN
        else:
            try:
                entry = await self._checkpoints.entry(run_id)
            except CheckpointRepositoryUnavailable:
                logger.warning(
                    "checkpoint authority unavailable for run %s — coverage unknown", run_id
                )
                coverage["checkpoints"] = COVERAGE_UNKNOWN
            else:
                checkpoints = [self._checkpoint_row(entry, fence, commands)]
                coverage["checkpoints"] = COVERAGE_PRESENT if entry else COVERAGE_MISSING
        if checkpoints is not None:
            rows["checkpoints"] = checkpoints

        # -- verifications: the run's unified evidence fragment -------------
        evidence = dict(getattr(run, "evidence", None) or {})
        fragment = evidence.get("verification")
        if isinstance(fragment, Mapping) and fragment:
            rows["verifications"] = [self._verification_row(fragment)]
            coverage["verifications"] = COVERAGE_PRESENT
        else:
            rows["verifications"] = []
            coverage["verifications"] = COVERAGE_MISSING

        # -- publications + approvals ---------------------------------------
        try:
            intents = list(
                (
                    await self._execute(
                        select(PublicationIntent)
                        .where(PublicationIntent.run_id == run_id)
                        .order_by(PublicationIntent.created_at.asc(), PublicationIntent.id.asc())
                    )
                )
                .scalars()
                .all()
            )
        except SQLAlchemyError:
            logger.warning("publication rows for run %s unreadable — coverage unknown", run_id)
            intents = None
        if intents is not None:
            rows["publications"] = [self._publication_row(row) for row in intents]
            coverage["publications"] = COVERAGE_PRESENT if intents else COVERAGE_MISSING
        try:
            approvals = list(
                (
                    await self._execute(
                        select(GateApproval)
                        .where(GateApproval.flow_run_id == run_id)
                        .order_by(GateApproval.created_at.asc(), GateApproval.id.asc())
                    )
                )
                .scalars()
                .all()
            )
        except SQLAlchemyError:
            logger.warning("approval rows for run %s unreadable — coverage unknown", run_id)
            approvals = None
        if approvals is not None:
            rows["approvals"] = [self._approval_row(row) for row in approvals]
            coverage["approvals"] = COVERAGE_PRESENT if approvals else COVERAGE_MISSING

        # -- occupancy: the admission/lease slice ----------------------------
        try:
            leases = list(
                (
                    await self._execute(
                        select(ExecutionLease)
                        .where(ExecutionLease.run_id == run_id)
                        .order_by(ExecutionLease.acquired_at.asc(), ExecutionLease.id.asc())
                    )
                )
                .scalars()
                .all()
            )
        except SQLAlchemyError:
            logger.warning("lease rows for run %s unreadable — coverage unknown", run_id)
            leases = None
        occupancy: tuple[dict[str, Any], ...] = ()
        if leases is not None:
            occupancy = tuple(self._occupancy_row(row) for row in leases)
            coverage["occupancy"] = COVERAGE_PRESENT if leases else COVERAGE_MISSING

        # -- saga: DERIVED from unresolved effects, never stored -------------
        unresolved = [
            pub
            for pub in rows.get("publications") or []
            if str(pub.get("status") or "") in UNRESOLVED_PUBLICATION_STATUSES
        ]
        if intents is not None and unresolved:
            rows["saga"] = {"state": "partially_published", "unresolved": len(unresolved)}

        age = self._age_seconds(rows, now)
        return OperatorSnapshot(
            run_id=run_id,
            subject=operator_scope_of_run(run),
            rows=rows,
            source_coverage=coverage,
            occupancy=occupancy,
            computed_at=_iso(now),
            projection_age_s=age,
        )

    async def _execute(self, statement: Any) -> Any:
        async with self._session_factory() as session:
            return await session.execute(statement)

    async def _scalar(self, statement: Any) -> Any:
        async with self._session_factory() as session:
            return await session.scalar(statement)

    # -- the row mappings (durable column → view shape) -------------------

    @staticmethod
    def _run_row(run: Any) -> dict[str, Any]:
        return {
            "id": str(run.id),
            "status": str(run.status or ""),
            "base_sha": str(run.base_sha or ""),
            "candidate_shas": [str(sha) for sha in (run.candidate_shas or [])],
            "plan_digest": str(run.plan_digest or ""),
            "evidence": dict(run.evidence or {}),
            "blocked_reason": str(run.status_reason or ""),
            "cancel_requested": bool(run.cancel_requested),
            "created_at": _iso(run.created_at),
            "updated_at": _iso(run.updated_at),
        }

    @staticmethod
    def _attempt_row(row: Any, run: Any) -> dict[str, Any]:
        status = _ATTEMPT_STATUS.get(str(row.status or ""), "unknown")
        started = _iso(row.created_at)
        return {
            "attempt_id": f"action:{row.id}",
            "kind": str(row.action_kind or ""),
            "status": status,
            "started_at": started,
            "updated_at": started,
            "generation": getattr(run, "cancellation_generation", None),
        }

    @staticmethod
    def _command_row(row: Any) -> dict[str, Any]:
        return {
            "command_id": str(row.id),
            "work_id": str(row.work_id),
            "kind": str(row.kind or ""),
            "status": str(row.status or ""),
            "sequence": int(row.sequence),
            "actor_ref": str(row.actor_ref or ""),
            "actor_origin": str(row.actor_origin or ""),
            "created_at": _iso(row.created_at),
            "applied_at": _iso(row.applied_at),
        }

    @staticmethod
    def _delivery_row(row: Any) -> dict[str, Any]:
        return {
            "command_id": str(row.command_id),
            "recipient": str(row.recipient or ""),
            "status": str(row.status or ""),
            "created_at": _iso(row.created_at),
        }

    @staticmethod
    def _checkpoint_row(
        entry: Mapping[str, Any] | None, fence: str, commands: list[Any] | None
    ) -> dict[str, Any]:
        """The ACTIVE checkpoint as the view reads it.

        The entry's content address IS the digest (the store is
        content-addressed); ``committed_at`` is the landing the entry
        records; ``activated_at`` carries the resume proof — the latest
        resume command that reached ``applied``/``checkpointed`` (the rung
        where the lane applied the restored bytes). The fence word comes
        from the durable pause fence, never from a guess.
        """
        activated = ""
        if commands is not None:
            for command in reversed(commands):
                if (
                    str(command.kind or "") == "resume"
                    and str(command.status or "") in _RESUME_APPLIED_STATUSES
                ):
                    activated = _iso(command.applied_at)
                    break
        if not entry:
            return {
                "checkpoint_id": "",
                "digest": "",
                "committed_at": "",
                "activated_at": activated,
                "fence": fence,
                "sequence": None,
            }
        return {
            "checkpoint_id": str(entry.get("checkpoint_id") or ""),
            "digest": str(entry.get("checkpoint_id") or ""),
            "committed_at": str(entry.get("uploaded_at") or ""),
            "activated_at": activated,
            "fence": fence,
            "sequence": entry.get("sequence"),
        }

    @staticmethod
    def _verification_row(fragment: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "verification_id": "run-evidence:verification",
            "result": str(fragment.get("status") or ""),
            "candidate_sha": str(fragment.get("tested_oid") or ""),
            "producer": str(fragment.get("producer") or ""),
            "at": str(fragment.get("observed_at") or ""),
        }

    @staticmethod
    def _publication_row(row: Any) -> dict[str, Any]:
        return {
            "operation_key": str(row.operation_key or ""),
            "status": str(row.status or ""),
            "operation": str(row.operation or ""),
            "target_ref": str(row.target_ref or ""),
            "at": _iso(row.updated_at or row.created_at),
        }

    @staticmethod
    def _approval_row(row: Any) -> dict[str, Any]:
        return {
            "approved_by": f"user:{row.approver_user_id}",
            "generation": int(row.generation),
            "at": _iso(row.created_at),
            "consumed_at": _iso(row.consumed_at),
        }

    @staticmethod
    def _occupancy_row(row: ExecutionLease) -> dict[str, Any]:
        return {
            "lease_id": str(row.id),
            "project_id": int(row.project_id),
            "provider": str(row.provider or ""),
            "slot": int(row.slot),
            "occupancy": lease_occupancy(row).value,
            "acquired_at": _iso(row.acquired_at),
            "released_at": _iso(row.released_at),
            "native_intent_ref": str(row.native_intent_ref or ""),
            "native_handle": str(row.native_handle or ""),
        }

    @staticmethod
    def _age_seconds(rows: Mapping[str, Any], now: datetime) -> float | None:
        """Seconds between *now* and the NEWEST row timestamp — ``None``
        when nothing carried a readable clock (ISO strings parse; an
        unreadable value never becomes a synthesized age)."""
        stamps: list[datetime] = []
        for key, value in rows.items():
            if isinstance(value, Mapping):
                candidates: Iterable[Any] = [value]
            elif isinstance(value, (list, tuple)):
                candidates = [row for row in value if isinstance(row, Mapping)]
            else:
                continue
            for view in candidates:
                for column in ("updated_at", "created_at", "committed_at", "at", "applied_at"):
                    moment = _as_datetime(view.get(column))
                    if moment is not None:
                        stamps.append(moment)
        if not stamps:
            return None
        return max(0.0, (now - max(stamps)).total_seconds())
