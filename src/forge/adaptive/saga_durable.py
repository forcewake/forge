"""Durable two-writer publication over provider-shaped native semantics (#277, R36-18).

The 2026-09-23 two-writer qualification (``two_writer_qualification.py``)
proved the models against fakes: a scripted remote whose ``commit`` was
idempotent per ``(branch, marker)``, and the in-memory saga store. This
module is the step the review asks for next — ONE two-writer change run
through durable storage and a remote whose DUPLICATE BEHAVIOR IS
PROVIDER-REALISTIC:

- **:class:`PostgresSagaStore`** — the :class:`SagaStore` seam over a REAL
  database (PostgreSQL via asyncpg, aiosqlite in tests). The saga's whole
  persisted state — per-repo step/compensation records and the step
  journal — lives as a versioned JSON document on the PARENT run's
  ``FlowRun.evidence`` column (the same shape the durable
  ``WorkPackageCoordinator`` uses), with one :class:`Outbox` row per
  observability transition (``saga.unknown_effects``,
  ``recovery.native_adoption``, ``workpackage.partial_publication``,
  ``human_edit.conflicts``) committed in the SAME transaction as the
  state it announces. NO new table, NO migration: the closed status
  vocabulary of ``publication_intents`` has no slot for
  ``parked_human``/``preparing`` and forcing one would be dishonest —
  the JSON column on the existing row is the established pattern.
- **The native-shaped reference remote moved OUT** (R37-19/#300):
  ``NativeShapedRemote`` — the in-process GitLab/GitHub-shaped remote
  with provider-realistic duplicate behavior — now lives in
  :mod:`forge.adaptive.reference.native_shaped_remote` (the labelled
  evaluation package). The old ``saga_durable.NativeShapedRemote``
  import path keeps working through a LAZY compatibility re-export, so
  importing this runtime module (and everything downstream of it,
  including the native adapters) stays reference-free — no
  reference-native implementation is ever selected implicitly by an
  import.
- **:class:`DurablePublicationEntry`** — the customer-shaped entry: the
  REAL :class:`WorkPackageCoordinator` starts the package (durable
  child-start intents, REAL child ``FlowRun`` rows through
  :func:`workpackage_service.start_child_run_row`), the REAL
  :class:`SagaCoordinator` drives each writer's publication over the
  durable store and whatever effect surface the caller hands it (the
  reference remote in tests/qualification; the REAL native adapters of
  ``forge.adaptive.saga_native`` against live providers), and — the
  R36-18 gate — a dependent child's publication CANNOT EVEN PERSIST ITS
  INTENT while the required producer outcome is unproven: the store
  refuses saves that would move a repository whose phase has not been
  admitted (:class:`PhaseAdmissionRefused`), and outcomes are recorded
  only from the PERSISTED saga state, never from invocation completion.

The kill discipline (the crash matrix): death is injected at the store's
COMMIT BOUNDARY — an :class:`observer <CommitObserver>` fires after every
durable save (:func:`kill_at_boundary`) — so the pre-crash process is the
REAL coordinator interrupted between its own saves, never a mirrored
walker. A lost provider response is reconciled through NATIVE
correlation only: recovery lists the branch's commits and adopts the
effect when the marker-carrying commit is established; when the surface
cannot prove anything the effect STAYS ``outcome_unknown`` (fail closed —
``saga.unknown_effects`` visible), and a head the saga cannot account for
is a HUMAN edit: parked, preserved, never force-overwritten.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.adaptive.publication_saga import (
    PublicationSaga,
    ProviderRejectedError,  # noqa: F401 — compat: the reference remote's refusal surface
    ProviderUnavailableError,  # noqa: F401 — compat: the reference remote's refusal surface
    RepoPublication,
    SagaCoordinator,
    begin_saga,
    observe_human_merge,
)
from forge.adaptive.workpackage import (
    OutcomeApplication,
    PhaseAdvanceRefused,
    WorkPackageCoordinator,
    read_workpackage_state,
)
from forge.adaptive.workpackage_service import ChildSubject, start_child_run_row
from forge.adaptive.two_writer_qualification import TwoWriterScenario
from forge.durable import FlowRun, Outbox

if TYPE_CHECKING:
    # The effect-interface seam (#295): the remote the entry drives is any
    # SagaEffectSurface — the reference NativeShapedRemote (now in the
    # evaluation package, re-exported lazily below) or the native adapters
    # in ``forge.adaptive.saga_native`` over the REAL provider clients.
    # TYPE_CHECKING-only: saga_native imports this module's NativeCommit at
    # runtime (no cycle), and this module's runtime import stays
    # reference-free.
    from forge.adaptive.reference.native_shaped_remote import NativeShapedRemote
    from forge.adaptive.saga_native import SagaEffectSurface

__all__ = [
    "SAGA_RECORD_KEY",
    "SAGA_STATE_SCHEMA",
    "OUTCOME_OF_PUBLICATION_STATUS",
    "AdmissionGuard",
    "CommitObserver",
    "SessionFactory",
    "DurablePublicationEntry",
    "DriveReport",
    "NativeCommit",
    "NativeShapedRemote",
    "OutstandingEffect",
    "PartialPublication",
    "PhaseAdmissionRefused",
    "PostgresSagaStore",
    "ProcessDied",
    "SagaDurabilityError",
    "StandingEffect",
    "journal_tail",
    "kill_at_boundary",
    "saga_from_document",
    "saga_to_document",
]


def __getattr__(name: str) -> Any:
    """The R37-19 (#300) compatibility re-export — LAZY on purpose.

    ``NativeShapedRemote`` moved to
    ``forge.adaptive.reference.native_shaped_remote`` (the labelled
    evaluation package). Re-exporting it lazily keeps this runtime
    module's import REFERENCE-FREE: production imports (saga_native and
    everything downstream) never load the reference package, while the
    old ``saga_durable.NativeShapedRemote`` path keeps working for
    tests and qualification runners. Removing the re-export is gated on
    the callers draining to the reference path — pinned by
    ``tests/test_reference_separation.py``.
    """
    if name == "NativeShapedRemote":
        from forge.adaptive.reference.native_shaped_remote import NativeShapedRemote

        return NativeShapedRemote
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


#: Where the saga document lives inside the parent run's evidence blob
#: (the same JSON-column pattern ``WorkPackageCoordinator`` persists under;
#: in-place mutation of a JSON column is not change-tracked, the whole
#: evidence dict is reassigned on every save).
SAGA_RECORD_KEY = "publication_saga"

#: The persisted saga document's schema discriminator (versioned like every
#: domain tag: a breaking change to what the document covers bumps it).
SAGA_STATE_SCHEMA = "forge.saga.durable/1"

#: Publication statuses that prove a child's outcome for phase gating.
#: ``ready_for_review``/``human_merged`` mean the reviewable effect stands
#: (the human merge decision stays with the human — the bot never merges);
#: ``failed`` is a definitive provider refusal. ``outcome_unknown``
#: (unproven surface), ``parked_human`` (a human decision is owed) and the
#: mid-ladder rungs prove NOTHING — the consumer stays gated.
OUTCOME_OF_PUBLICATION_STATUS: Mapping[str, str] = {
    "ready_for_review": "succeeded",
    "human_merged": "succeeded",
    "failed": "failed",
}

#: Repos whose reviewable outcome is established — the complement is the
#: OUTSTANDING half of a ``partially_published`` report.
_REVIEW_ESTABLISHED = frozenset({"ready_for_review", "human_merged"})

#: Anything that yields sessions — ``async_sessionmaker`` duck-types here
#: (the same alias shape the coordinator and the runs services use).
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

#: The store's PRE-commit gate: raised (by the entry) when a pass tries to
#: persist a publication transition for a repository whose phase has not
#: been admitted — the consumer cannot even record its intent while the
#: required producer outcome is unproven.
AdmissionGuard = Callable[[PublicationSaga], Awaitable[None]]

#: The store's POST-commit boundary observer — fires after every durable
#: save with the committed saga. This is the crash-matrix seam: an observer
#: that raises :class:`ProcessDied` kills the driving process exactly
#: between the coordinator's own saves.
CommitObserver = Callable[[PublicationSaga], Awaitable[None]]


class SagaDurabilityError(RuntimeError):
    """A durable-saga storage failure — LOUD on purpose.

    The missing parent run, a saga id that is not the one this parent
    coordinates: refusing beats coercing when the durability record is
    the thing recovery trusts.
    """


class PhaseAdmissionRefused(RuntimeError):
    """A publication pass tried to move an UNADMITTED repository.

    The R36-18 gate at the storage boundary: every repository of a LATER
    phase must stay ``preparing`` (plan only — no intent, no effect, no
    note) until the phases before it have PROVEN outcomes. The refusal
    fires BEFORE the save commits, so the unadmitted publication leaves
    nothing durable behind — not even its write-ahead intent.
    """


class ProcessDied(RuntimeError):
    """The crash matrix's death signal: the driving process 'died' at a
    store commit boundary. Everything durable is exactly what the last
    committed save wrote — what a real crash leaves."""


# ---------------------------------------------------------------------------
# Saga serialization — the versioned JSON document on the parent run.
# ---------------------------------------------------------------------------


def saga_to_document(saga: PublicationSaga) -> dict[str, Any]:
    """The saga as its persisted JSON document (``forge.saga.durable/1``)."""
    return {
        "schema": SAGA_STATE_SCHEMA,
        "saga_id": saga.saga_id,
        "work_id": saga.work_id,
        "publication_epoch": saga.publication_epoch,
        "candidate_digest": saga.candidate_digest,
        "repos": [
            {
                "repository_id": repo.repository_id,
                "branch": repo.branch,
                "expected_base_oid": repo.expected_base_oid,
                "status": repo.status,
                "head_oid": repo.head_oid,
                "review_url": repo.review_url,
                "adopted": repo.adopted,
                "note": repo.note,
            }
            for repo in saga.repos
        ],
        "steps": [dict(step) for step in saga.steps],
    }


def saga_from_document(document: Mapping[str, Any]) -> PublicationSaga:
    """Rebuild the saga from its persisted document (the store's read side)."""
    repos = tuple(
        RepoPublication(
            repository_id=str(repo["repository_id"]),
            branch=str(repo["branch"]),
            expected_base_oid=str(repo["expected_base_oid"]),
            status=str(repo["status"]),  # type: ignore[arg-type]
            head_oid=repo.get("head_oid"),
            review_url=repo.get("review_url"),
            adopted=bool(repo.get("adopted") or False),
            note=str(repo.get("note") or ""),
        )
        for repo in document.get("repos") or ()
    )
    steps = tuple(
        {str(key): str(value) for key, value in dict(step).items()}
        for step in document.get("steps") or ()
    )
    return PublicationSaga(
        saga_id=str(document["saga_id"]),
        work_id=str(document["work_id"]),
        publication_epoch=int(document["publication_epoch"]),
        candidate_digest=str(document["candidate_digest"]),
        repos=repos,
        steps=steps,
    )


def journal_tail(saga: PublicationSaga) -> tuple[str, str] | None:
    """The journal's last ``(repository_id, step)`` — the boundary label.

    The commit-boundary seam names saves by what the coordinator just
    journaled; an empty journal has no boundary yet.
    """
    if not saga.steps:
        return None
    last = saga.steps[-1]
    return (str(last.get("repo") or ""), str(last.get("step") or ""))


def kill_at_boundary(repository_id: str, step: str) -> CommitObserver:
    """Arm the store's commit boundary to die at ``(repository_id, step)``.

    The step names are the journaled ones. Two mappings are the REAL
    coordinator's own batching, not approximations: ``fence_check`` and
    ``commit_intent`` share ONE durable save (the intent write-ahead), so
    a ``fence_check`` death targets that save; ``prepare`` is journaled by
    ``begin_saga`` and persisted by the pass's FIRST save (the begun
    intents), so a ``prepare`` death fires there. Every other step dies
    exactly when its save commits.
    """

    match_step = "commit_intent" if step == "fence_check" else step
    saves = 0

    async def observe(saga: PublicationSaga) -> None:
        nonlocal saves
        saves += 1
        if step == "prepare":
            if saves == 1:
                raise ProcessDied(f"died after the begun intents of {saga.saga_id} (prepare)")
            return
        if journal_tail(saga) == (repository_id, match_step):
            raise ProcessDied(f"died after {match_step} of {repository_id}")

    return observe


# ---------------------------------------------------------------------------
# The durable store — the saga document on the parent run + outbox events.
# ---------------------------------------------------------------------------


def _observability_events(
    previous_document: Mapping[str, Any] | None, saga: PublicationSaga
) -> list[tuple[str, dict[str, Any]]]:
    """The outbox rows a transition earns, from the DIFF of the documents.

    Per-repo transitions: into ``outcome_unknown``
    (``saga.unknown_effects`` — the honest unproven surface), a first
    ``adopted`` (``recovery.native_adoption`` — the crash window closed
    by NATIVE correlation, not by a duplicate create), into
    ``parked_human`` (``human_edit.conflicts`` — a decision is owed).
    Saga-level: ``workpackage.partial_publication`` whenever effects
    STAND while others remain OUTSTANDING and that partial shape is new
    (each outstanding effect identified with status and note).
    """
    previous_repos: dict[str, Mapping[str, Any]] = {
        str(repo.get("repository_id")): repo
        for repo in (previous_document or {}).get("repos") or ()
    }
    previous_status = str(saga_from_document(previous_document).status) if previous_document else ""
    events: list[tuple[str, dict[str, Any]]] = []
    for repo in saga.repos:
        before = previous_repos.get(repo.repository_id)
        before_status = str((before or {}).get("status") or "")
        if repo.status == "outcome_unknown" and before_status != "outcome_unknown":
            events.append(
                (
                    "saga.unknown_effects",
                    {
                        "saga_id": saga.saga_id,
                        "repository_id": repo.repository_id,
                        "reason": repo.note,
                    },
                )
            )
        if repo.adopted and not bool((before or {}).get("adopted") or False):
            events.append(
                (
                    "recovery.native_adoption",
                    {
                        "saga_id": saga.saga_id,
                        "repository_id": repo.repository_id,
                        "head_oid": repo.head_oid,
                        "correlation": "commit-message marker over the listed branch history",
                    },
                )
            )
        if repo.status == "parked_human" and before_status != "parked_human":
            events.append(
                (
                    "human_edit.conflicts",
                    {
                        "saga_id": saga.saga_id,
                        "repository_id": repo.repository_id,
                        "conflict": repo.note,
                    },
                )
            )

    def _outstanding(document_repos: Mapping[str, Mapping[str, Any]] | None) -> list[str]:
        if document_repos is None:
            return []
        return sorted(
            str(repo.get("repository_id"))
            for repo in document_repos.values()
            if str(repo.get("status") or "") not in _REVIEW_ESTABLISHED
        )

    outstanding_now = sorted(
        repo.repository_id for repo in saga.repos if repo.status not in _REVIEW_ESTABLISHED
    )
    outstanding_before = _outstanding(previous_repos if previous_document else None)
    standing = [repo for repo in saga.repos if repo.status in _REVIEW_ESTABLISHED]
    newly_partial = previous_status not in {"failed", "parked"} and saga.status in {
        "failed",
        "parked",
    }
    if standing and outstanding_now and (outstanding_now != outstanding_before or newly_partial):
        events.append(
            (
                "workpackage.partial_publication",
                {
                    "saga_id": saga.saga_id,
                    "saga_status": str(saga.status),
                    "standing": [
                        {
                            "repository_id": repo.repository_id,
                            "status": repo.status,
                            "review_url": repo.review_url,
                        }
                        for repo in standing
                    ],
                    "outstanding": [
                        {
                            "repository_id": repo.repository_id,
                            "status": repo.status,
                            "note": repo.note,
                            "review_url": repo.review_url,
                        }
                        for repo in saga.repos
                        if repo.status not in _REVIEW_ESTABLISHED
                    ],
                },
            )
        )
    return events


class PostgresSagaStore:
    """The :class:`SagaStore` seam over a REAL database (NXT-24 → R36-18).

    ``save`` is the coordinator's write-ahead leg made durable: the saga
    document replaces the ``publication_saga`` key on the parent run's
    ``FlowRun.evidence`` JSON column in ONE transaction with the outbox
    rows the transition earns. The optional :data:`AdmissionGuard` fires
    BEFORE the transaction (the phase gate — an unadmitted repository
    cannot persist a publication step); the optional
    :data:`CommitObserver` fires AFTER the commit (the crash-matrix
    seam). One publication saga per parent run, enforced by refusing a
    save whose saga id is not the one the parent already coordinates.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        parent_run_id: str,
        on_commit: CommitObserver | None = None,
        admit_save: AdmissionGuard | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._parent_run_id = parent_run_id
        self._on_commit = on_commit
        self._admit_save = admit_save

    async def save(self, saga: PublicationSaga) -> None:
        async with self._session_factory() as session:
            run = await session.get(FlowRun, self._parent_run_id)
            if run is None:
                raise SagaDurabilityError(
                    f"parent run {self._parent_run_id!r} not found — a saga needs its parent"
                )
            stored = (run.evidence or {}).get(SAGA_RECORD_KEY)
            previous_document = stored if isinstance(stored, dict) else None
            if previous_document is not None:
                previous_id = str(previous_document.get("saga_id") or "")
                if previous_id != saga.saga_id:
                    raise SagaDurabilityError(
                        f"parent run {self._parent_run_id!r} coordinates saga {previous_id!r},"
                        f" not {saga.saga_id!r} — one publication saga per parent run"
                    )
            if self._admit_save is not None:
                await self._admit_save(saga)
            merged = dict(run.evidence or {})
            merged[SAGA_RECORD_KEY] = saga_to_document(saga)
            run.evidence = merged
            for event_type, payload in _observability_events(previous_document, saga):
                session.add(
                    Outbox(flow_run_id=self._parent_run_id, event_type=event_type, payload=payload)
                )
            await session.commit()
        if self._on_commit is not None:
            await self._on_commit(saga)

    async def load(self, saga_id: str) -> PublicationSaga | None:
        async with self._session_factory() as session:
            run = await session.get(FlowRun, self._parent_run_id)
            if run is None:
                raise SagaDurabilityError(
                    f"parent run {self._parent_run_id!r} not found — a saga needs its parent"
                )
            stored = (run.evidence or {}).get(SAGA_RECORD_KEY)
        if not isinstance(stored, dict):
            return None
        saga = saga_from_document(stored)
        if saga.saga_id != saga_id:
            raise SagaDurabilityError(
                f"parent run {self._parent_run_id!r} coordinates saga {saga.saga_id!r},"
                f" not {saga_id!r}"
            )
        return saga


# ---------------------------------------------------------------------------
# The provider-shaped remote — native duplicate behavior, native identities.
# ---------------------------------------------------------------------------


class NativeCommit(NamedTuple):
    """One commit as the provider spells it: sha, parent, message, author.

    The marker correlation recovery depends on is IN THE MESSAGE (the
    ``forge-saga:<id>:<digest>`` trailer — the same shape forge's real
    writer puts in commit messages); providers offer no marker-keyed
    dedup, and this remote does not invent one.
    """

    sha: str
    parent: str
    message: str
    author: str


# ---------------------------------------------------------------------------
# The durable publication entry — children gated on PERSISTED outcomes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StandingEffect:
    """One reviewable effect that STANDS (its review is open or merged)."""

    repository_id: str
    status: str
    review_url: str
    head_oid: str


@dataclass(frozen=True)
class OutstandingEffect:
    """One intended effect whose reviewable outcome is not established —
    each one identified, with its status and note (the honest
    ``partially_published`` report)."""

    repository_id: str
    status: str
    note: str
    review_url: str


@dataclass(frozen=True)
class PartialPublication:
    """The package-level partial view over the PERSISTED saga."""

    saga_status: str
    standing: tuple[StandingEffect, ...]
    outstanding: tuple[OutstandingEffect, ...]

    def to_document(self) -> dict[str, Any]:
        return {
            "saga_status": self.saga_status,
            "standing": [vars(effect) for effect in self.standing],
            "outstanding": [vars(effect) for effect in self.outstanding],
        }


@dataclass(frozen=True)
class DriveReport:
    """What one ``drive()`` converged to, read from durable state."""

    package_state: str
    saga_status: str
    passes: int
    refusal: str
    partial: PartialPublication
    child_launches: tuple[Mapping[str, Any], ...]

    def to_document(self) -> dict[str, Any]:
        return {
            "package_state": self.package_state,
            "saga_status": self.saga_status,
            "passes": self.passes,
            "refusal": self.refusal,
            "partial": self.partial.to_document(),
            "child_launches": [dict(launch) for launch in self.child_launches],
        }


class DurablePublicationEntry:
    """The durable two-writer entry: real coordinators, durable state, a
    native-shaped remote, and the phase gate at the storage boundary.

    One ``drive()`` pass: admit (from the DURABLE workpackage record)
    the repositories whose phases are proven, run the REAL
    :class:`SagaCoordinator` over the durable store and the native
    remote (the store refuses any save that would move an unadmitted
    repository), record outcomes FROM THE PERSISTED SAGA ONLY, then let
    the :class:`WorkPackageCoordinator` advance — which is what launches
    the next phase's child as a REAL ``FlowRun`` row. Restart-safe by
    construction: every decision re-reads the database, so a fresh entry
    over the same rows and the same remote adopts what the dead process
    left instead of duplicating it.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        scenario: TwoWriterScenario,
        *,
        remote: SagaEffectSurface | NativeShapedRemote,
        subjects: Mapping[str, ChildSubject] | None = None,
        on_boundary: CommitObserver | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._scenario = scenario
        self._remote = remote
        self._package = scenario.package()
        self._parent_run_id = f"run-{scenario.scenario_id}"
        self._saga_id = f"saga-{scenario.scenario_id}"
        self._world = scenario.freeze().tested_world_digest or ""
        self._subjects: dict[str, ChildSubject] = dict(subjects or {}) or {
            repo.repository_id: ChildSubject(project_id=101 + index, provider="github")
            for index, repo in enumerate(scenario.writer_repos())
        }
        self._child_launches: list[dict[str, Any]] = []
        self._on_boundary = on_boundary

    # -- identity ------------------------------------------------------------

    @property
    def parent_run_id(self) -> str:
        return self._parent_run_id

    @property
    def saga_id(self) -> str:
        return self._saga_id

    @property
    def child_launches(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self._child_launches)

    # -- the durable legs ------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """Seed the parent run and start the package (idempotent — a replay
        adopts launched children and re-issues only unlaunched intents)."""
        async with self._session_factory() as session:
            existing = await session.get(FlowRun, self._parent_run_id)
            if existing is None:
                session.add(
                    FlowRun(
                        id=self._parent_run_id,
                        project_id=99,
                        status="planning",
                        status_reason="two-writer durable publication parent",
                    )
                )
                await session.commit()
        coordinator = WorkPackageCoordinator(self._session_factory, self._dispatch_child)
        return await coordinator.start(
            self._package,
            parent_run_id=self._parent_run_id,
            task_brief=self._scenario.objective,
            tested_world_digest=self._world,
        )

    async def publish_pass(self) -> PublicationSaga:
        """One gated publication pass through the REAL coordinator.

        Fail-closed provider errors (:class:`ProviderUnavailableError`)
        stop the pass with the intent persisted and nothing created
        blind; a phase-gate refusal (:class:`PhaseAdmissionRefused`) is
        the normal stop at a phase boundary. Both return the PERSISTED
        saga — the caller's view never runs ahead of the store.
        """
        admitted = await self._admitted_repositories()

        async def admit(candidate: PublicationSaga) -> None:
            self._assert_admission(admitted, candidate)

        store = PostgresSagaStore(
            self._session_factory,
            parent_run_id=self._parent_run_id,
            on_commit=self._on_boundary,
            admit_save=admit,
        )
        saga = await store.load(self._saga_id)
        if saga is None:
            saga = self._begin_saga()
            await store.save(saga)
        for repo in saga.repos:
            self._remote.pin_expected_head(repo.repository_id, repo.branch, repo.expected_base_oid)
        coordinator: SagaCoordinator = SagaCoordinator(store, self._remote)
        try:
            return await coordinator.run(
                saga, current_publication_epoch=self._scenario.publication_epoch
            )
        except (PhaseAdmissionRefused, ProviderUnavailableError):
            persisted = await self._saga_state()
            return persisted if persisted is not None else saga

    async def record_decided_outcomes(self) -> tuple[OutcomeApplication, ...]:
        """Record child outcomes FROM THE PERSISTED SAGA STATE ONLY.

        ``ready_for_review``/``human_merged`` prove success (the merge
        decision stays with the human); ``failed`` records failure;
        ``outcome_unknown`` and ``parked_human`` record NOTHING — an
        unproven surface and an owed human decision are not outcomes.
        Invocation completion is never consulted.
        """
        saga = await self._saga_state()
        if saga is None:
            return ()
        coordinator = WorkPackageCoordinator(self._session_factory, self._dispatch_child)
        applications: list[OutcomeApplication] = []
        for item in self._package.items:
            if not item.writable:
                continue
            repo = saga.repo(item.repository_id)
            if repo is None:
                continue
            outcome = OUTCOME_OF_PUBLICATION_STATUS.get(repo.status)
            if outcome is None:
                continue
            detail = f"publication {repo.status} (saga {saga.saga_id})"
            if repo.review_url:
                detail += f"; review {repo.review_url}"
            if repo.note:
                detail += f"; note: {repo.note}"
            applications.append(
                await coordinator.record_outcome(
                    self._parent_run_id,
                    item.item_id,
                    outcome,
                    detail=detail,
                    tested_world_digest=self._world,
                )
            )
        return tuple(applications)

    async def drive(self, *, max_passes: int = 8) -> DriveReport:
        """Passes until the package completes or a pass makes no progress."""
        saga = await self._saga_state()
        package_state, refusal = await self._package_state_and_refusal()
        passes = 0
        seen: set[tuple[str, str, int]] = set()
        for _ in range(max_passes):
            digest = saga.steps_digest if saga is not None else ""
            fingerprint = (str(digest), package_state, len(self._child_launches))
            if package_state == "complete" or fingerprint in seen:
                break
            seen.add(fingerprint)
            saga = await self.publish_pass()
            passes += 1
            await self.record_decided_outcomes()
            package_state, refusal = await self._package_state_and_refusal()
            saga = await self._saga_state() or saga
        partial = await self.partial_publication()
        return DriveReport(
            package_state=package_state,
            saga_status=str(saga.status) if saga is not None else "absent",
            passes=passes,
            refusal=refusal,
            partial=partial,
            child_launches=tuple(dict(launch) for launch in self._child_launches),
        )

    async def observe_merge(self, repository_id: str) -> PublicationSaga:
        """Persist the OBSERVED human merge (the only route to
        ``human_merged`` — the entry records an observation, it never
        performs or triggers a merge)."""
        store = PostgresSagaStore(self._session_factory, parent_run_id=self._parent_run_id)
        saga = await store.load(self._saga_id)
        if saga is None:
            raise SagaDurabilityError(
                f"no saga {self._saga_id!r} under {self._parent_run_id!r} to observe a merge on"
            )
        merged = observe_human_merge(saga, repository_id)
        await store.save(merged)
        return merged

    # -- the reports ------------------------------------------------------------

    async def partial_publication(self) -> PartialPublication:
        """The ``partially_published`` view over the PERSISTED saga: every
        standing reviewable effect and every outstanding one, identified."""
        saga = await self._saga_state()
        if saga is None:
            return PartialPublication("absent", (), ())
        standing = tuple(
            StandingEffect(
                repository_id=repo.repository_id,
                status=repo.status,
                review_url=repo.review_url or "",
                head_oid=repo.head_oid or "",
            )
            for repo in saga.repos
            if repo.status in _REVIEW_ESTABLISHED
        )
        outstanding = tuple(
            OutstandingEffect(
                repository_id=repo.repository_id,
                status=repo.status,
                note=repo.note,
                review_url=repo.review_url or "",
            )
            for repo in saga.repos
            if repo.status not in _REVIEW_ESTABLISHED
        )
        return PartialPublication(str(saga.status), standing, outstanding)

    async def unknown_effects(self) -> tuple[OutstandingEffect, ...]:
        """The visible unknown surface: repos whose outcome is unproven."""
        partial = await self.partial_publication()
        return tuple(effect for effect in partial.outstanding if effect.status == "outcome_unknown")

    async def outbox_events(self) -> list[dict[str, Any]]:
        """The observability trail (for assertions over the named events)."""
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(Outbox)
                        .where(Outbox.flow_run_id == self._parent_run_id)
                        .order_by(Outbox.id)
                    )
                )
                .scalars()
                .all()
            )
        return [{"event_type": row.event_type, "payload": dict(row.payload)} for row in rows]

    def credential_staging(self) -> dict[str, list[str]]:
        """The credential fan-out this entry implies: one WRITER credential
        per writer repository, READ credentials everywhere else — the
        staging ``verify_credential_scope`` judges."""
        staging: dict[str, list[str]] = {}
        for repo in self._scenario.writer_repos():
            staging[repo.repository_id] = [f"cred-write-{repo.repository_id}"]
        for repository_id in sorted(self._package.reader_repos()):
            staging[repository_id] = [f"cred-read-{repository_id}"]
        return staging

    # -- internals ----------------------------------------------------------------

    def _begin_saga(self) -> PublicationSaga:
        saga = begin_saga(
            self._scenario.scenario_id,
            publication_epoch=self._scenario.publication_epoch,
            candidate_digest=self._world,
            repo_plans={
                repo.repository_id: (repo.branch, repo.base_oid)
                for repo in self._scenario.writer_repos()
            },
        )
        return replace(saga, saga_id=self._saga_id)

    def _store(self, admit_save: AdmissionGuard | None = None) -> PostgresSagaStore:
        return PostgresSagaStore(
            self._session_factory,
            parent_run_id=self._parent_run_id,
            on_commit=self._on_boundary,
            admit_save=admit_save,
        )

    async def _saga_state(self) -> PublicationSaga | None:
        return await PostgresSagaStore(
            self._session_factory, parent_run_id=self._parent_run_id
        ).load(self._saga_id)

    async def _package_state_and_refusal(self) -> tuple[str, str]:
        coordinator = WorkPackageCoordinator(self._session_factory, self._dispatch_child)
        try:
            advanced = await coordinator.advance(self._parent_run_id, self._package)
            return str(advanced.get("state") or ""), ""
        except PhaseAdvanceRefused as exc:
            persisted = await read_workpackage_state(self._session_factory, self._parent_run_id)
            return str((persisted or {}).get("state") or ""), str(exc)

    async def _admitted_repositories(self) -> frozenset[str]:
        """Writer repositories whose phase the DURABLE record has admitted
        (every phase up to and including the current one)."""
        state = await read_workpackage_state(self._session_factory, self._parent_run_id)
        if state is None:
            raise SagaDurabilityError(
                f"parent run {self._parent_run_id!r} coordinates no work package;"
                " start the package before publishing"
            )
        items = {item.item_id: item for item in self._package.items}
        admitted: set[str] = set()
        for phase in (state.get("phases") or [])[: int(state.get("current_phase") or 0) + 1]:
            for item_id in phase:
                item = items.get(item_id)
                if item is not None and item.writable:
                    admitted.add(item.repository_id)
        return frozenset(admitted)

    @staticmethod
    def _assert_admission(admitted: frozenset[str], candidate: PublicationSaga) -> None:
        """The phase gate at the storage boundary (see module head)."""
        for repo in candidate.repos:
            if repo.repository_id in admitted:
                continue
            if repo.status != "preparing" or repo.head_oid or repo.review_url or repo.note:
                raise PhaseAdmissionRefused(
                    f"the publication pass tried to persist repository"
                    f" {repo.repository_id!r} at status {repo.status!r}, but its phase is not"
                    " proven yet — an unadmitted writer cannot record publication state"
                )
            climbed = [
                step
                for step in candidate.steps
                if step.get("repo") == repo.repository_id
                and str(step.get("step") or "") not in ("prepare", "")
            ]
            if climbed:
                raise PhaseAdmissionRefused(
                    f"the publication pass tried to persist step"
                    f" {climbed[0].get('step')!r} for {repo.repository_id!r}, whose phase"
                    " is not proven — the write-ahead intent itself is refused"
                )

    async def _dispatch_child(
        self, item_id: str, repository_id: str, *, writable: bool, intent_key: str, brief: str
    ) -> str:
        """The coordinator's factory seam: journal the launch, then start the
        child as a REAL FlowRun row (deterministic id — replay adopts)."""
        self._child_launches.append(
            {
                "item_id": item_id,
                "repository_id": repository_id,
                "writable": writable,
                "intent_key": intent_key,
                "brief": brief,
            }
        )
        return await start_child_run_row(
            self._session_factory,
            self._subjects,
            item_id=item_id,
            repository_id=repository_id,
            intent_key=intent_key,
            brief=brief,
        )
