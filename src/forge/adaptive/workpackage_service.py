"""WorkPackage coordination over the EXISTING repository runs (R28-19).

The durable :class:`~forge.adaptive.workpackage.WorkPackageCoordinator`
(NXT-23) drives children through an injected child-run FACTORY — an
abstract seam, deliberately provider-free. This module is the
operational half the 1ae5290 review asked for next: a
:class:`WorkPackageService` whose children are REAL
:class:`~forge.durable.FlowRun` rows, the same runs the gateway and the
runs services own, not in-memory stand-ins.

The composition, top to bottom:

- :func:`package_from_plan` turns an issue's MULTI-PHASE plan into a
  :class:`~forge.adaptive.workpackage.WorkPackage`: each phase's items
  become :class:`~forge.adaptive.workpackage.WorkItemRef` children whose
  ``depends_on`` is DERIVED from the phase order (a phase-n item waits
  for every phase-(n-1) item), so
  :func:`~forge.adaptive.workpackage.compile_dependencies` reproduces
  the plan's phases exactly. A single-phase plan is refused — one phase
  needs no coordination, it needs a run.
- The service hands the coordinator a child-run factory that dispatches
  through a :data:`RunStarter` seam. The DEFAULT starter creates a REAL
  FlowRun row (the durable core of ``RunService.start_run`` /
  ``GitHubRunService.start_run`` — everything those methods do before
  their provider I/O legs) with a run id DETERMINISTICALLY derived from
  the committed intent key, so the crash window between intent and
  dispatch replays to exactly ONE child row. Callers with a provider
  service inject it instead via :func:`run_service_starter`; the
  one-active-run-per-subject invariant those services enforce provides
  the same adopt-on-replay behavior.
- :meth:`WorkPackageService.advance_from_children` is the run-side
  connective tissue the review asked for: it reads each launched child's
  CURRENT status from the FlowRun row (never from a cached object),
  maps TERMINAL statuses to the coordinator's outcome vocabulary
  (``ready_for_human`` — the designed end of a completed
  implement-verify cycle — reads as success; ``blocked``/``failed``/
  ``cancelled`` read as failure), records the outcome together with the
  evidence the run accumulated (the tested world digest, the candidate
  sha, the lane's exit classification), and only then calls
  ``advance``. A phase moves when ALL of its children have terminal
  rows; anything less is the coordinator's typed
  :class:`~forge.adaptive.workpackage.PhaseAdvanceRefused` naming what
  is awaited and what failed.
- :meth:`WorkPackageService.package_status` snapshots the parent's
  state with a LIVE run-status link for every child — links and reasons
  for every child, including partial outcomes (the R28-19 acceptance
  criterion), read from the durable record plus the FlowRun rows.

The child evidence contract (what :func:`outcome_evidence_of` reads off
a child run): the tested world digest at
``evidence["tested_world_digest"]`` (the same spelling the coordinator
persists), the candidate sha from the ``candidate_shas`` column the
runs services append to (falling back to the journaled
``evidence["published_candidate"]["sha"]``), and the lane's exit
classification at ``evidence["harness"]["driver_exit"]`` (the key a
harness cycle journals when it adopts the artifact meta's ``exit``).
Anything absent stays absent — unknown is never guessed here.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from forge.adaptive.workpackage import (
    ChildRunFactory,
    WorkItemRef,
    WorkPackage,
    WorkPackageCoordinator,
    WorkPackageStateError,
    _WORKPACKAGE_RECORD_KEY,
    compile_dependencies,
    read_workpackage_state,
)
from forge.durable import FlowRun, FlowStatus, TERMINAL_STATUSES

__all__ = [
    "ChildOutcomeEvidence",
    "ChildSubject",
    "RunStarter",
    "WorkPackageService",
    "child_run_id_for_intent",
    "outcome_evidence_of",
    "package_from_plan",
    "run_service_starter",
    "start_child_run_row",
]

#: Anything that yields sessions — ``async_sessionmaker`` duck-types here
#: (the same alias shape the coordinator's SessionFactory uses).
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

#: The REAL-run dispatch seam: ``(item_id, repository_id, *, writable,
#: intent_key, brief) -> run_id``. Same shape as the coordinator's
#: :data:`~forge.adaptive.workpackage.ChildRunFactory` on purpose — the
#: service IS the factory it hands the coordinator, and the starter is
#: the one seam a provider service plugs into. The contract: the starter
#: MUST be idempotent on *intent_key* (a replayed intent adopts the run
#: it already started, never starts a second one).
RunStarter = ChildRunFactory

#: The terminal statuses that read as a child SUCCESS. ``ready_for_human``
#: is the designed terminal of a completed implement-verify cycle
#: (ADR-0004); the other terminals (``blocked``, ``failed``,
#: ``cancelled``) read as failure — a blocked child has no result to
#: vouch for, and a cancelled one withdrew.
_SUCCESS_TERMINAL: frozenset[str] = frozenset({FlowStatus.READY_FOR_HUMAN.value})

#: Every terminal status, spelled once (the comparison against FlowRun
#: rows is string-based — the rows carry the value, not the enum).
_ALL_TERMINAL: frozenset[str] = frozenset(status.value for status in TERMINAL_STATUSES)


class ChildSubject(NamedTuple):
    """Where a child repository lives for the provider that owns it.

    The WorkPackage speaks in ``repository_id`` strings (authorization
    shape); FlowRun rows key on the provider's own numeric subject ids.
    This tuple is the bridge the DEFAULT run starter needs — the same
    ``(provider, project_id, issue_iid)`` triple a real ``start_run``
    call would carry.
    """

    project_id: int
    issue_iid: int | None = None
    provider: str = "github"


@dataclass(frozen=True)
class ChildOutcomeEvidence:
    """What one child's run row can PROVE about its outcome (R28-19).

    Read off the FlowRun row by :func:`outcome_evidence_of`; every field
    is ``""`` when the run never journaled it — unknown stays unknown,
    never guessed.
    """

    tested_world_digest: str
    candidate_sha: str
    exit: str


def outcome_evidence_of(run: FlowRun) -> ChildOutcomeEvidence:
    """The per-child outcome evidence a terminal run carries.

    The contract this reads is documented at the module head: the tested
    world digest is a top-level evidence key (the coordinator's own
    spelling), the candidate sha prefers the ``candidate_shas`` column
    the runs services append to and falls back to the journaled
    ``published_candidate`` fragment, and the exit classification rides
    the harness evidence fragment. Absent keys stay ``""`` — the caller
    (and the durable record) sees unknown, not a fabricated value.
    """
    evidence: Mapping[str, Any] = run.evidence or {}
    world = str(evidence.get("tested_world_digest") or "").strip() or None
    shas = [str(sha) for sha in (getattr(run, "candidate_shas", None) or []) if str(sha)]
    candidate_sha = ""
    if shas:
        candidate_sha = shas[-1]
    else:
        published = evidence.get("published_candidate")
        if isinstance(published, dict):
            candidate_sha = str(published.get("sha") or "")
    harness = evidence.get("harness")
    exit_status = ""
    if isinstance(harness, dict):
        exit_status = str(harness.get("driver_exit") or "")
    if not exit_status:
        exit_status = str(evidence.get("driver_exit") or "")
    return ChildOutcomeEvidence(
        tested_world_digest=world or "",
        candidate_sha=candidate_sha,
        exit=exit_status,
    )


def child_run_id_for_intent(intent_key: str) -> str:
    """The DETERMINISTIC FlowRun id for one start intent.

    ``"wp"`` + the first 30 hex of sha256(intent_key) — exactly the
    32-char budget FlowRun ids have. Determinism IS the idempotency: a
    process that dies after creating the child row but before the
    coordinator saved the link replays the same intent, derives the same
    id, and ADOPTS the row it finds there — one child ever exists per
    intent, by construction rather than by catch.
    """
    return "wp" + hashlib.sha256(intent_key.encode("utf-8")).hexdigest()[:30]


async def start_child_run_row(
    session_factory: SessionFactory,
    subject_of: Mapping[str, ChildSubject],
    *,
    item_id: str,
    repository_id: str,
    intent_key: str,
    brief: str,
) -> str:
    """Start one child as a REAL FlowRun row (the default dispatch).

    The durable core of ``RunService.start_run`` /
    ``GitHubRunService.start_run`` minus their provider I/O legs: one
    FlowRun row parked at ``waiting_harness`` (a dispatched lane child
    waits for its lane exactly like a real one), its evidence carrying
    the intent key and the task brief so the row explains itself. The
    run id is derived from the intent (:func:`child_run_id_for_intent`),
    and an id that already exists is ADOPTED — the crash-window replay
    converges to one row. A repository without a :class:`ChildSubject`
    mapping fails LOUD: the service refuses to guess where a child
    repository lives.
    """
    run_id = child_run_id_for_intent(intent_key)
    async with session_factory() as session:
        existing = await session.get(FlowRun, run_id)
        if existing is not None:
            return str(existing.id)
        subject = subject_of.get(repository_id)
        if subject is None:
            raise WorkPackageStateError(
                f"no child subject mapping for repository {repository_id!r}"
                f" (item {item_id!r}) — refusing to guess where the child runs"
            )
        session.add(
            FlowRun(
                id=run_id,
                project_id=subject.project_id,
                issue_iid=subject.issue_iid,
                provider=subject.provider,
                status=FlowStatus.WAITING_HARNESS.value,
                status_reason=f"workpackage child {item_id} ({repository_id})",
                evidence={
                    "workpackage_intent": intent_key,
                    "workpackage_item": item_id,
                    "task_brief": brief,
                },
            )
        )
        await session.commit()
    return run_id


def run_service_starter(
    start_run: Callable[..., Awaitable[str]],
    subject_of: Mapping[str, ChildSubject],
) -> RunStarter:
    """Adapt a REAL ``start_run`` into the :data:`RunStarter` seam (R28-19).

    *start_run* is the GitHub-shaped service entry
    (``GitHubRunService.start_run(project_id=..., issue_number=..., ...)``;
    the GitLab twin takes ``issue_iid`` — a one-lambda translation away).
    The adapter maps the item's repository to its provider subject and
    dispatches with the item's brief as the issue description, forge as
    the author. Idempotency is INHERITED, not added: those services
    enforce one active run per subject, so an intent replayed after a
    crash finds the active child and returns ITS id — the same
    adopt-on-replay contract the default starter gets from
    determinism.
    """

    async def starter(
        item_id: str, repository_id: str, *, writable: bool, intent_key: str, brief: str
    ) -> str:
        if not writable:
            raise WorkPackageStateError(
                f"item {item_id!r} is read-only — a reference snapshot never"
                " reaches the run starter"
            )
        subject = subject_of.get(repository_id)
        if subject is None:
            raise WorkPackageStateError(
                f"no child subject mapping for repository {repository_id!r}"
                f" (item {item_id!r}) — refusing to guess where the child runs"
            )
        return await start_run(
            project_id=subject.project_id,
            issue_number=subject.issue_iid,
            issue_title=f"[workpackage] {item_id}: {brief}"[:120],
            issue_description=brief,
            author_username="forge-workpackage",
        )

    return starter


def package_from_plan(
    package_id: str,
    objective: str,
    phases: Sequence[Sequence[WorkItemRef]],
    *,
    read_only_repositories: Sequence[str] = (),
) -> WorkPackage:
    """Turn a MULTI-PHASE plan into the parent :class:`WorkPackage`.

    Each phase's items are the plan's children for that phase; every
    phase-*n* item's ``depends_on`` is DERIVED as the whole of phase
    *n-1* (the plan's phase order IS the dependency structure the
    package must reproduce), so ``compile_dependencies`` re-derives
    exactly the phases given. Hand-set ``depends_on`` on the items is
    REPLACED, not merged — the phases are the authority.

    Refused loudly: a plan with fewer than two phases (one phase needs
    no coordination — run the child), an empty phase list, and any
    EMPTY phase (a phase with no children is not a phase, it is a
    hole in the plan).
    """
    if len(phases) < 2:
        raise ValueError(
            f"plan for {package_id!r} has {len(phases)} phase(s): a work package"
            " coordinates a MULTI-phase plan — a single phase is one run,"
            " not a package"
        )
    if any(not phase for phase in phases):
        raise ValueError(f"plan for {package_id!r} has an empty phase — nothing to dispatch")

    items: list[WorkItemRef] = []
    for index, phase in enumerate(phases):
        predecessors = tuple(item.item_id for item in phases[index - 1]) if index else ()
        for item in phase:
            items.append(
                WorkItemRef(
                    item_id=item.item_id,
                    repository_id=item.repository_id,
                    writable=item.writable,
                    depends_on=predecessors,
                )
            )
    package = WorkPackage(
        package_id=package_id,
        objective=objective,
        items=tuple(items),
        read_only_repositories=tuple(read_only_repositories),
    )
    violations = package.validate()
    if violations:
        raise ValueError(f"plan for {package_id!r} builds an invalid package: {violations}")
    derived = compile_dependencies(items)
    given = [[item.item_id for item in phase] for phase in phases]
    if derived != given:
        raise ValueError(
            f"plan for {package_id!r} does not reproduce its own phases"
            f" (derived {derived} != given {given})"
        )
    return package


class WorkPackageService:
    """One coordinated package whose children are REAL repository runs.

    Owns no coordination state of its own: the durable record is the
    :class:`~forge.adaptive.workpackage.WorkPackageCoordinator`'s, and
    every public entry re-reads it. What the service ADDS over the
    coordinator is the run-side wiring — dispatch through a real
    :data:`RunStarter`, outcomes read from FlowRun rows, evidence
    attached from what the child run actually journaled, and a status
    snapshot with live links for every child.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        run_starter: RunStarter | None = None,
        subject_of: Mapping[str, ChildSubject] | None = None,
    ) -> None:
        self._session_factory = session_factory
        subjects: Mapping[str, ChildSubject] = subject_of or {}
        if run_starter is None:
            self._run_starter: RunStarter = _default_starter(session_factory, subjects)
        else:
            self._run_starter = run_starter
        self._coordinator = WorkPackageCoordinator(session_factory, self._dispatch_child)

    # -- the dispatch path -------------------------------------------------

    async def _dispatch_child(
        self, item_id: str, repository_id: str, *, writable: bool, intent_key: str, brief: str
    ) -> str:
        """The coordinator's factory seam, backed by the REAL starter."""
        return await self._run_starter(
            item_id, repository_id, writable=writable, intent_key=intent_key, brief=brief
        )

    async def start_package(
        self,
        package: WorkPackage,
        *,
        parent_run_id: str,
        task_brief: str,
        tested_world_digest: str | None = None,
    ) -> dict[str, Any]:
        """Persist the package for *parent_run_id* and dispatch phase 0.

        Thin, deliberate: every durability rule (one package per parent,
        intents before factory calls, snapshots for read-only items) is
        the coordinator's; this entry only forwards. *tested_world_digest*
        pins the package's active world — child outcomes recorded from
        any OTHER world are rejected as unproven (the NXT-23 rule the
        run-side reading below enforces for free, because the digest it
        forwards comes from the child run's own evidence).
        """
        return await self._coordinator.start(
            package,
            parent_run_id=parent_run_id,
            task_brief=task_brief,
            tested_world_digest=tested_world_digest,
        )

    # -- the run-side reading ---------------------------------------------

    async def advance_from_children(
        self, parent_run_id: str, package: WorkPackage
    ) -> dict[str, Any]:
        """Record proven outcomes from the FlowRun rows, then advance.

        For every launched writer child without a recorded outcome: read
        the CURRENT FlowRun row; a TERMINAL status maps to the outcome
        vocabulary and is recorded with the run's own evidence (world
        digest, candidate sha, exit — attached to the durable outcome,
        see :func:`outcome_evidence_of`); a non-terminal row leaves the
        child awaiting. Children whose outcome was recorded but whose
        evidence annotation never landed (a crash between the two
        writes) get the annotation replayed — the scan is idempotent.

        Then — and only then — ``advance``: a phase moves when ALL its
        children have terminal rows mapped to proven outcomes, else the
        coordinator's :class:`~forge.adaptive.workpackage.PhaseAdvanceRefused`
        propagates with what is awaited and what failed, and NOTHING
        dispatches.
        """
        state = await self._require_state(parent_run_id)
        for item_id, child in sorted((state.get("children") or {}).items()):
            if str(child.get("kind") or "") != "child_run":
                continue
            if str(child.get("intent_status") or "") != "launched":
                continue
            run = await self._read_child_run(parent_run_id, item_id, child)
            outcome = (child.get("outcome") or {}).get("status")
            if outcome is None:
                terminal = self._terminal_outcome(run)
                if terminal is None:
                    continue  # a silent child is NOT proof — advance will name it
                application = await self._coordinator.record_outcome(
                    parent_run_id,
                    item_id,
                    terminal.status,
                    detail=terminal.detail,
                    tested_world_digest=terminal.evidence.tested_world_digest or None,
                )
                if not application.applied:
                    continue  # the coordinator's typed refusal is already durable
                state = application.record
            await self._attach_evidence(parent_run_id, item_id, run)
        return await self._coordinator.advance(parent_run_id, package)

    async def package_status(self, parent_run_id: str) -> dict[str, Any]:
        """The parent's snapshot: links and reasons for EVERY child.

        The durable record's children joined with the LIVE FlowRun row
        status (``run_status``; ``"missing"`` when a launched link names
        a row that is not there — visible corruption, never hidden).
        Partial outcomes appear exactly as partial: a child may carry a
        recorded outcome while its sibling is still running.
        """
        state = await self._require_state(parent_run_id)
        children: list[dict[str, Any]] = []
        for item_id, child in sorted((state.get("children") or {}).items()):
            entry: dict[str, Any] = {
                "item_id": item_id,
                "repository_id": child.get("repository_id"),
                "writable": bool(child.get("writable")),
                "kind": child.get("kind"),
                "phase": child.get("phase"),
                "intent_status": child.get("intent_status"),
                "child_run_id": child.get("child_run_id"),
                "outcome": child.get("outcome"),
            }
            run_id = str(child.get("child_run_id") or "")
            if run_id:
                async with self._session_factory() as session:
                    run = await session.get(FlowRun, run_id)
                entry["run_status"] = str(run.status) if run is not None else "missing"
                if run is not None:
                    entry["status_reason"] = str(run.status_reason or "")
            children.append(entry)
        return {
            "package_id": state.get("package_id"),
            "parent_run_id": parent_run_id,
            "state": state.get("state"),
            "current_phase": state.get("current_phase"),
            "failed_item": state.get("failed_item"),
            "children": children,
        }

    # -- internals ----------------------------------------------------------

    async def _require_state(self, parent_run_id: str) -> dict[str, Any]:
        state = await read_workpackage_state(self._session_factory, parent_run_id)
        if state is None:
            raise WorkPackageStateError(
                f"parent run {parent_run_id!r} coordinates no work package; start one first"
            )
        return state

    async def _read_child_run(
        self, parent_run_id: str, item_id: str, child: Mapping[str, Any]
    ) -> FlowRun:
        """The child's CURRENT row — the runs table, not a cached object."""
        run_id = str(child.get("child_run_id") or "")
        async with self._session_factory() as session:
            run = await session.get(FlowRun, run_id)
        if run is None:
            raise WorkPackageStateError(
                f"parent run {parent_run_id!r} links child {item_id!r} to flow run"
                f" {run_id!r}, which does not exist — a launched link without"
                " its run is corruption, not an outcome"
            )
        return run

    def _terminal_outcome(self, run: FlowRun) -> _TerminalOutcome | None:
        """Map a child's terminal FlowRun status to the outcome vocabulary.

        ``None`` while the run is still alive — the honest non-answer.
        The detail string carries the run id, the terminal status and
        its reason so the durable outcome explains ITSELF.
        """
        status = str(run.status or "")
        if status not in _ALL_TERMINAL:
            return None
        evidence = outcome_evidence_of(run)
        outcome_status = "succeeded" if status in _SUCCESS_TERMINAL else "failed"
        reason = str(run.status_reason or "").strip()
        detail = f"run {run.id} terminal status={status}"
        if reason:
            detail += f"; reason={reason}"
        if evidence.candidate_sha:
            detail += f"; candidate_sha={evidence.candidate_sha}"
        if evidence.exit:
            detail += f"; exit={evidence.exit}"
        return _TerminalOutcome(outcome_status, detail, evidence)

    async def _attach_evidence(self, parent_run_id: str, item_id: str, run: FlowRun) -> None:
        """Attach the run's outcome evidence to the durable outcome (additive).

        One annotation per child: the evidence dict lands under
        ``children[<item>]["outcome"]["evidence"]`` in the same
        single-transaction shape the coordinator persists with. A
        replay finds the annotation already there and writes nothing.
        """
        extracted = outcome_evidence_of(run)
        evidence = {
            "candidate_sha": extracted.candidate_sha,
            "exit": extracted.exit,
            "tested_world_digest": extracted.tested_world_digest,
            "run_status": str(run.status or ""),
        }
        async with self._session_factory() as session:
            row = await session.get(FlowRun, parent_run_id)
            if row is None:
                raise WorkPackageStateError(f"flow run {parent_run_id!r} not found")
            record = dict(row.evidence or {})
            state = dict(record.get(_WORKPACKAGE_RECORD_KEY) or {})
            children = dict(state.get("children") or {})
            child = dict(children.get(item_id) or {})
            outcome = dict(child.get("outcome") or {})
            if outcome.get("evidence") == evidence:
                return  # idempotent — the annotation already landed
            outcome["evidence"] = evidence
            child["outcome"] = outcome
            children[item_id] = child
            state["children"] = children
            record[_WORKPACKAGE_RECORD_KEY] = state
            row.evidence = record
            await session.commit()


@dataclass(frozen=True)
class _TerminalOutcome:
    """One terminal child mapped onto the coordinator's vocabulary."""

    status: str
    detail: str
    evidence: ChildOutcomeEvidence


def _default_starter(
    session_factory: SessionFactory, subjects: Mapping[str, ChildSubject]
) -> RunStarter:
    """The default REAL-run dispatch: durable FlowRun rows (see module head)."""

    async def starter(
        item_id: str, repository_id: str, *, writable: bool, intent_key: str, brief: str
    ) -> str:
        return await start_child_run_row(
            session_factory,
            subjects,
            item_id=item_id,
            repository_id=repository_id,
            intent_key=intent_key,
            brief=brief,
        )

    return starter
