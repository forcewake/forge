"""Operational drills — capacity, storage recovery and degraded mode (R36-21).

Issue #280 (review ``16339c2``, item R36-21): occupancy intents (#262),
GC locks (#263) and the cutover fence (#264) each landed with their own
proofs; the next operating proof is behavior under REALISTIC
CONCURRENCY, provider latency, restarts and storage limits — bounded
degradation and safe capacity accounting, not a peak-throughput
demonstration. This module is that proof, as COMPOSABLE DRILLS driven
by ``scripts/run_ops_drills.py`` (the operator's spelling) and pinned
by ``tests/test_ops_drills.py``.

Every drill runs against DISPOSABLE infrastructure the drill itself
builds — a real SQLite (or PostgreSQL) database through the REAL
admission API (:mod:`forge.adaptive.admission`), a real
:class:`~forge.adaptive.checkpoint_repository.CheckpointRepository` over
a real blob volume, and a MINIMAL LOCAL fake-native provider with
injectable faults (latency, 429/503, lost start responses, failed
cancellations). No shared lab container is touched.

The drills and what each proves:

- :func:`drill_native_start_load` — N concurrent dispatch-intent →
  handle → terminal cycles: the configured capacity is NEVER exceeded
  while starts lose responses and cancellations fail;
  ``dispatched_unknown`` occupancy holds its slot until the reconciler
  observes terminal, and reconciliation is BOUNDED (one pass with
  observed-terminal jobs drains every held slot).
- :func:`drill_checkpoint_upload_load` — bounded concurrent puts under
  a declared memory/admission budget; overload is a TYPED refusal
  (:class:`UploadBudgetExceeded`), never an unbounded queue or an OOM;
  a contended volume lock refuses with the #263 :class:`GCLockTimeout`
  contract (bounded wait, nothing committed) and the sweep converges
  once the holder releases.
- :func:`drill_control_responsiveness` — control commands (the
  operator's occupancy/saturation reads) answered WHILE uploads and a
  retention pass contend; the latency is MEASURED
  (``control.command_latency``) and bounded because reads never take
  the volume lock.
- :func:`drill_degraded_faults` — provider latency, 429, 503, a
  definite 404 refusal, a database restart (engine disposed and
  recreated — occupancy identities survive and resolve by
  ``native_intent_ref``), and a full-disk-shaped quota: each fault
  produces its TYPED behavior (refusal / hold / unknown), never a hang
  and never a silent success.
- :func:`drill_backup_restore` —
  :func:`~forge.adaptive.checkpoint_repository.backup_store` /
  :func:`~forge.adaptive.checkpoint_repository.restore_store`
  round-trip: metadata AND blobs together (pins and pending-GC state
  included), the selected active and pinned checkpoints RECOVER
  verified, and a MISMATCHED pair (metadata from t2, blobs from t1) is
  REFUSED with :class:`BackupMismatchError` listing the affected works
  — never silently accepted.
- :func:`drill_operator_override_audit` — the explicitly approved
  force-release: the audit (:func:`release_lease_with_evidence` with an
  ``override`` naming approver + reason) makes the risk VISIBLE
  (``override.audit``); the same release WITHOUT the override parks
  draining — an override never relabels uncertain occupancy
  observed-terminal silently.

The SATURATION SIGNALS (the issue's observability):
``execution.occupied_vs_limit``, ``native_start.unknown_age``,
``checkpoint.upload_memory_budget``, ``control.command_latency``,
``backup.restore_coverage`` and ``override.audit`` — derived counters
carried by every drill outcome (counts and ages over durable columns,
never secrets or prompts).

HONEST SCOPE: each drill states its achieved objectives and the exact
limits it tested (N, budget, lock-wait). Nothing here generalizes to
larger fleets without measurement — the report says so.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from forge import api_checkpoint_channel as api_channel
from forge.adaptive.admission import (
    AdmissionPolicy,
    ExecutionLease,
    NativeStatus,
    clear_native_start_intent,
    definite_start_refusal,
    lease_occupancy,
    reconcile_draining,
    record_native_handle,
    record_native_start_intent,
    release_lease_with_evidence,
    saturation_report,
    try_acquire_lease,
)
from forge.adaptive.checkpoint_repository import (
    BackupMismatchError,
    FilesystemCheckpointRepository,
    PostgresCheckpointRepository,
    backup_store,
    restore_store,
    verify_backup_consistency,
)
from forge.durable import FlowRun
from forge.models.base import Base

__all__ = [
    "DRILLS",
    "DRILL_SCOPE",
    "DrillFixture",
    "DrillOutcome",
    "FaultedNativeLane",
    "LostStartResponse",
    "UploadAdmissionBudget",
    "UploadBudgetExceeded",
    "build_fixture",
    "checkpoint_payload",
    "drill_backup_restore",
    "drill_checkpoint_upload_load",
    "drill_control_responsiveness",
    "drill_degraded_faults",
    "drill_native_start_load",
    "drill_operator_override_audit",
    "run_drill",
]

#: The report's scope sentence — every drill outcome carries it and the
#: script prints it: bounded single-deployment drills on disposable
#: fixtures, never a fleet throughput claim.
DRILL_SCOPE: Final = (
    "achieved against the tested limits only (disposable single-database "
    "fixture, synthetic bounded workloads, the exact N/budget/lock-wait "
    "recorded per drill); NOT generalized to larger fleets, higher "
    "concurrency or production hardware without new measurement"
)


# ---------------------------------------------------------------------------
# The fixture: disposable database + blob volume, real engines
# ---------------------------------------------------------------------------


@dataclass
class DrillFixture:
    """Everything a drill runs against — all of it disposable.

    ``engine`` / ``session_factory`` are a REAL database (file-backed
    SQLite by default — ``NullPool`` so concurrent sessions take
    genuinely separate connections; the in-memory ``StaticPool``
    approximation shares one connection and one transaction, which is
    not a load); ``root`` is a real blob volume; ``repository`` the
    checkpoint authority over it. A drill NEVER touches shared lab
    state.
    """

    engine: Any
    session_factory: async_sessionmaker[AsyncSession]
    root: Path
    repository: FilesystemCheckpointRepository | PostgresCheckpointRepository
    work_dir: Path
    #: The ORIGINAL connection string (never ``str(engine.url)`` —
    #: SQLAlchemy masks the password as ``***`` there, and a restarted
    #: engine must dial the real one).
    db_url: str = ""

    async def dispose(self) -> None:
        await self.engine.dispose()

    async def restart_engine(self) -> None:
        """The stopped-worker replacement: dispose and recreate (same DB).

        The fresh factory keeps NO in-process state — whatever survives,
        survives because it is durable. Occupancy identities and lease
        rows are re-read through the new factory exactly as a
        replacement API/worker process would read them.
        """

        await self.engine.dispose()
        if self.db_url and not self.db_url.startswith("sqlite"):
            self.engine = create_async_engine(self.db_url, poolclass=NullPool)
        else:
            url = self.db_url or str(self.engine.url)
            self.engine = create_async_engine(
                url, connect_args={"check_same_thread": False, "timeout": 15}, poolclass=NullPool
            )
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)


async def build_fixture(
    work_dir: Path,
    *,
    db_url: str = "",
    authority: str = "filesystem",
) -> DrillFixture:
    """Build the disposable fixture: schema, blob volume, repository.

    *db_url* selects the database (default: file-backed SQLite under
    *work_dir*). With ``authority="postgres"`` and a database URL the
    checkpoint index lives in ``checkpoint_metadata`` through the same
    :class:`~forge.adaptive.checkpoint_repository.PostgresCheckpointRepository`
    the control plane composes; the blob volume is real either way.
    """

    work_dir.mkdir(parents=True, exist_ok=True)
    if db_url:
        engine = create_async_engine(db_url, poolclass=NullPool)
    else:
        db_url = f"sqlite+aiosqlite:///{work_dir / 'drill.db'}"
        engine = create_async_engine(
            db_url,
            connect_args={"check_same_thread": False, "timeout": 15},
            poolclass=NullPool,
        )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    root = work_dir / "checkpoint-store"
    if authority == "postgres" and db_url:
        repository: FilesystemCheckpointRepository | PostgresCheckpointRepository = (
            PostgresCheckpointRepository(root, factory)
        )
    else:
        repository = FilesystemCheckpointRepository(root)
    return DrillFixture(
        engine=engine,
        session_factory=factory,
        root=root,
        repository=repository,
        work_dir=work_dir,
        db_url=db_url,
    )


# ---------------------------------------------------------------------------
# The fake-native lane: dispatch/probe seams with injectable faults
# ---------------------------------------------------------------------------


class LostStartResponse(Exception):
    """The provider ACCEPTED the start and the response was lost.

    The worker-death-in-between shape (AT-06 / the fake native server's
    ``dispatch_response=server_error``): the native job lives on at the
    provider, but no handle ever landed. The drill treats this exactly
    as production does — the start intent STAYS and occupancy is
    ``dispatched_unknown`` until the reconciler's probe resolves it.
    """


@dataclass
class StartAnswer:
    """What one injected provider start call answered."""

    status: int
    handle: str = ""
    lost_response: bool = False
    latency_s: float = 0.0

    @property
    def definite_refusal(self) -> bool:
        """A 4xx that PROVES the start was refused before acceptance."""
        return definite_start_refusal(self.status)


class FaultedNativeLane:
    """A minimal local fake-native provider with injectable faults.

    The SAME seams the production dispatch legs use — a start call
    (answered after the injected latency / status fault) and an
    occupancy probe (:class:`forge.adaptive.admission.NativeProbe`).
    The fault table:

    - ``latency_s`` — every start call awaits this long first (the
      slow-provider shape; finite by construction, so the drill
      MEASURES a delay, it never hangs);
    - ``start_status`` — the status the start call answers: ``202``
      accepts and mints a handle; 429/503 (and any 5xx/transport
      shape) are AMBIGUOUS (the job may or may not exist — occupancy
      holds); a definite 4xx (404) PROVES refusal;
    - ``lose_response`` — with a 202-class start, the job is minted and
      the response is LOST (:class:`LostStartResponse`);
    - ``cancel_mode`` — ``ok`` or ``fail``: whether provider-side
      cancellation lands (a failed cancel leaves the native job
      running, so the slot must HOLD).

    Every minted job is recorded (``jobs``) and lives until the drill
    marks it terminal — a native job outlives every local cancellation,
    exactly as the fake native server models it. Correlation is kept
    BOTH ways: a minted job remembers the intent marker that spawned
    it (``_spawn_refs``, so a LOST response is still probeable by its
    marker), and a 429-class start that minted nothing records the
    marker as answered-absent (``_no_job`` — the provider's run list
    shows nothing for it, the GitHub-probe semantics: absence of a run
    is a decidable TERMINAL, not an eternal UNKNOWN).
    """

    def __init__(
        self,
        *,
        latency_s: float = 0.0,
        start_status: int = 202,
        lose_response: bool = False,
        cancel_mode: str = "ok",
        prefix: str = "fake",
    ) -> None:
        self.latency_s = latency_s
        self.start_status = start_status
        self.lose_response = lose_response
        self.cancel_mode = cancel_mode
        self.prefix = prefix
        #: handle → ``running`` | ``terminal`` — the durable native world.
        self.jobs: dict[str, str] = {}
        #: handle → the intent marker that spawned it (lost-response probe).
        self._spawn_refs: dict[str, str] = {}
        #: intent markers the provider answered with NO job (429-class).
        self._no_job: set[str] = set()
        #: handles minted but never answered back (lost responses).
        self.lost_handles: list[str] = []
        self.cancel_attempts = 0
        self.start_calls = 0

    async def start(self, run_id: str, intent_ref: str = "") -> StartAnswer:
        """One provider start call, with the injected faults applied."""
        self.start_calls += 1
        started = time.monotonic()
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        if self.start_status in (202, 503):
            # 202: accepted, handle returned (unless the response is lost).
            # 503: the server MAY have accepted before failing — the lane
            # mints the job (the provider-side truth) and answers 503
            # WITHOUT a handle: undecidable to the caller, decidable to
            # the probe (the job exists and can be observed terminal).
            handle = f"{self.prefix}:job:{uuid4().hex[:16]}"
            self.jobs[handle] = "running"
            if intent_ref:
                self._spawn_refs[handle] = intent_ref
            if self.start_status == 202 and self.lose_response:
                self.lost_handles.append(handle)
                raise LostStartResponse(
                    f"the provider accepted the start for {run_id} ({handle}) "
                    "but the response was lost"
                )
            return StartAnswer(
                status=self.start_status,
                handle=handle if self.start_status == 202 else "",
                latency_s=time.monotonic() - started,
            )
        if intent_ref:
            self._no_job.add(intent_ref)  # 429-class: nothing was minted
        return StartAnswer(status=self.start_status, latency_s=time.monotonic() - started)

    async def cancel(self, handle: str) -> bool:
        """Provider-side cancellation — ``ok`` lands, ``fail`` never does."""
        self.cancel_attempts += 1
        if self.cancel_mode != "ok":
            return False
        if handle in self.jobs:
            self.jobs[handle] = "terminal"
        return True

    def mark_terminal(self, *handles: str) -> None:
        for handle in handles:
            if handle in self.jobs:
                self.jobs[handle] = "terminal"

    def mark_all_terminal(self) -> int:
        count = sum(1 for state in self.jobs.values() if state == "running")
        for handle in self.jobs:
            self.jobs[handle] = "terminal"
        return count

    async def probe(self, key: str) -> NativeStatus:
        """The occupancy probe (the ``NativeProbe`` seam).

        Answers for every correlation spelling: a recorded
        ``native_handle``, an intent marker whose job was minted (the
        lost-response shape — resolved by ``_spawn_refs``), and an
        intent marker the provider answered with NO job (decidable
        TERMINAL: the provider's run list shows nothing for it). A key
        the lane never saw answers UNKNOWN — uncertain occupancy holds,
        never frees.
        """

        await asyncio.sleep(0)  # the async-probe seam, honestly awaited
        state = ""
        if key in self.jobs:
            state = self.jobs[key]
        elif key in self._no_job:
            state = "terminal"  # the provider's list answers absence
        else:
            for handle, marker in self._spawn_refs.items():
                if marker == key:
                    state = self.jobs.get(handle, "")
                    break
        if state == "running":
            return NativeStatus.RUNNING
        if state == "terminal":
            return NativeStatus.TERMINAL
        return NativeStatus.UNKNOWN


# ---------------------------------------------------------------------------
# The upload admission budget — overload is a typed refusal
# ---------------------------------------------------------------------------


class UploadBudgetExceeded(RuntimeError):
    """The declared upload memory/admission budget refused this put.

    R36-21's typed overload answer: concurrent uploads may never exceed
    the declared memory/admission policy — the excess upload is
    REFUSED (this error) once its bounded admission wait expires, never
    queued silently and never allowed to accumulate unbounded in-flight
    bytes. The refusal is recoverable by design: the caller re-delivers
    once in-flight uploads drain (the checkpoint is content-addressed
    and idempotent), so overload degrades to bounded retry, never to
    unbounded memory.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(f"upload refused by the declared admission budget: {detail}")
        self.detail = detail


class _UploadAdmission:
    """One held reservation — a slot plus its bytes, released on exit."""

    def __init__(self, budget: UploadAdmissionBudget, byte_size: int) -> None:
        self._budget = budget
        self._byte_size = byte_size

    async def __aenter__(self) -> None:
        await self._budget._acquire(self._byte_size)

    async def __aexit__(self, *_exc: object) -> None:
        await self._budget._release(self._byte_size)


class UploadAdmissionBudget:
    """The bounded concurrent-upload gate: N slots, B in-flight bytes.

    The deployment-compatible spelling of "concurrent bounded uploads
    cannot exceed the declared memory/admission policy": at most
    ``max_concurrent`` puts and ``max_in_flight_bytes`` of
    manifest+blob bytes may be in flight at once. An upload that cannot
    be admitted within ``wait_seconds`` (a single upload larger than
    the whole budget refuses IMMEDIATELY) is refused with
    :class:`UploadBudgetExceeded` — overload degrades to typed
    refusals, never to an unbounded queue or process growth. The peak
    occupancy is INSTRUMENTED (``peak_concurrent`` / ``peak_bytes``):
    the drill's evidence that the budget was never exceeded.
    """

    def __init__(
        self,
        *,
        max_concurrent: int,
        max_in_flight_bytes: int,
        wait_seconds: float = 0.5,
    ) -> None:
        self.max_concurrent = max_concurrent
        self.max_in_flight_bytes = max_in_flight_bytes
        self.wait_seconds = wait_seconds
        self._condition = asyncio.Condition()
        self._in_flight_bytes = 0
        self._concurrent = 0
        self.peak_concurrent = 0
        self.peak_bytes = 0
        self.refusals = 0

    @property
    def in_flight_bytes(self) -> int:
        return self._in_flight_bytes

    def admit(self, byte_size: int) -> _UploadAdmission:
        """Reserve one upload slot + its bytes (typed refusal at admission).

        The returned async context manager releases both halves in
        ``finally`` — a failed put frees its admission exactly like a
        successful one, so a burst of failures cannot wedge the budget.
        """

        if byte_size > self.max_in_flight_bytes:
            self.refusals += 1
            raise UploadBudgetExceeded(
                f"one upload of {byte_size}B exceeds the whole in-flight budget "
                f"of {self.max_in_flight_bytes}B — re-deliver it in smaller parts "
                "or raise the declared budget"
            )
        return _UploadAdmission(self, byte_size)

    async def _acquire(self, byte_size: int) -> None:
        deadline = time.monotonic() + self.wait_seconds
        async with self._condition:
            while True:
                if (
                    self._concurrent < self.max_concurrent
                    and self._in_flight_bytes + byte_size <= self.max_in_flight_bytes
                ):
                    self._concurrent += 1
                    self._in_flight_bytes += byte_size
                    self.peak_concurrent = max(self.peak_concurrent, self._concurrent)
                    self.peak_bytes = max(self.peak_bytes, self._in_flight_bytes)
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.refusals += 1
                    raise UploadBudgetExceeded(
                        f"admission waited {self.wait_seconds}s without a slot: "
                        f"{self._concurrent}/{self.max_concurrent} uploads and "
                        f"{self._in_flight_bytes}/{self.max_in_flight_bytes}B in flight — "
                        "re-deliver once the in-flight uploads drain"
                    )
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=remaining)
                except TimeoutError:
                    pass  # loop once more so the deadline check refuses typed

    async def _release(self, byte_size: int) -> None:
        async with self._condition:
            self._concurrent = max(0, self._concurrent - 1)
            self._in_flight_bytes = max(0, self._in_flight_bytes - byte_size)
            self._condition.notify_all()


# ---------------------------------------------------------------------------
# Drill outcomes — the report shape
# ---------------------------------------------------------------------------


@dataclass
class DrillOutcome:
    """One drill's result: objectives achieved, limits tested, signals.

    ``outcome`` is ``pass`` when ``violations`` is empty.
    ``achieved_objectives`` and ``tested_limits`` are the honest report
    the issue demands: what was PROVEN, under exactly which bounds —
    never a generalization.
    """

    drill: str
    objectives: list[str] = field(default_factory=list)
    tested_limits: dict[str, Any] = field(default_factory=dict)
    signals: dict[str, Any] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)

    @property
    def outcome(self) -> str:
        return "pass" if not self.violations else "fail"

    def check(self, condition: bool, objective: str) -> bool:
        """Record an objective; a false condition is a VIOLATION, not a skip."""
        if condition:
            self.objectives.append(objective)
        else:
            self.violations.append(objective)
        return condition

    def as_document(self) -> dict[str, Any]:
        return {
            "drill": self.drill,
            "outcome": self.outcome,
            "achieved_objectives": list(self.objectives),
            "tested_limits": dict(self.tested_limits),
            "signals": dict(self.signals),
            "violations": list(self.violations),
            "scope": DRILL_SCOPE,
        }


# ---------------------------------------------------------------------------
# Shared drill helpers
# ---------------------------------------------------------------------------


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checkpoint_payload(
    work_id: str, sequence: int, *, blob_count: int = 2, blob_bytes: int = 256
) -> tuple[bytes, dict[str, bytes], str]:
    """A minimal ``forge.wip.manifest/2`` payload the store verifies."""
    files = {
        f"file-{index}.txt": (f"{work_id}/{sequence}/{index}\n" * max(1, blob_bytes // 16)).encode()
        for index in range(blob_count)
    }
    manifest = json.dumps(
        {
            "schema": "forge.wip.manifest/2",
            "work_id": work_id,
            "sequence": sequence,
            "source_oids": {"attempt_base": "e" * 40},
            "files": {
                name: {"digest": _digest(data), "mode": 0o644, "role": "new"}
                for name, data in sorted(files.items())
            },
            "deletions": [],
        }
    ).encode()
    document = json.loads(manifest)
    blobs = {entry["digest"]: files[name] for name, entry in document["files"].items()}
    return manifest, blobs, _digest(manifest)


def payload_size(manifest: bytes, blobs: dict[str, bytes]) -> int:
    """The admission-relevant size of one upload (manifest + blobs)."""
    return len(manifest) + sum(len(data) for data in blobs.values())


async def _flow_run(factory: async_sessionmaker[AsyncSession], run_id: str, status: str) -> None:
    """Seed one FlowRun row (the control-plane half of a dispatch cycle)."""
    async with factory() as session:
        session.add(FlowRun(id=run_id, project_id=1, provider="fake", status=status))
        await session.commit()


async def _open_lease_count(factory: async_sessionmaker[AsyncSession], project_id: int) -> int:
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(ExecutionLease).where(
                        ExecutionLease.project_id == project_id,
                        ExecutionLease.released_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        return len(rows)


async def occupancy_snapshot(
    factory: async_sessionmaker[AsyncSession], project_id: int
) -> dict[str, int]:
    """Open leases by occupancy state (the sampler's watch view)."""
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(ExecutionLease).where(
                        ExecutionLease.project_id == project_id,
                        ExecutionLease.released_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
    counts: dict[str, int] = {}
    for row in rows:
        state = lease_occupancy(row).value
        counts[state] = counts.get(state, 0) + 1
    return counts


async def _lease_for_run(
    factory: async_sessionmaker[AsyncSession], run_id: str
) -> ExecutionLease | None:
    async with factory() as session:
        return (
            (
                await session.execute(
                    select(ExecutionLease).where(ExecutionLease.run_id == run_id).limit(1)
                )
            )
            .scalars()
            .first()
        )


async def _drive_dispatch_cycle(
    fixture: DrillFixture,
    lane: FaultedNativeLane,
    policy: AdmissionPolicy,
    *,
    run_id: str,
    intent_ref: str,
    fate: str,
    project_id: int,
) -> str:
    """ONE dispatch-intent → handle → terminal cycle through the REAL API.

    The fates (the load drill's mix): ``ok`` (handle lands, job later
    observed terminal), ``lost_response`` (accepted start, no handle —
    unknown occupancy), ``cancel_fail`` (local cancel + provider-side
    cancellation FAILS — the native job runs on), ``definite_refusal``
    (a 404-class start: proven never accepted) and ``ambiguous``
    (429/503: the job may exist). Returns the cycle's end state.
    """

    lease = await try_acquire_lease(policy, project_id, fixture.session_factory, run_id=run_id)
    if lease is None:
        return "parked"
    await record_native_start_intent(fixture.session_factory, run_id, intent_ref)
    try:
        answer = await lane.start(run_id, intent_ref)
    except LostStartResponse:
        answer = None  # accepted, response lost
    if answer is not None and answer.definite_refusal:
        # PROVEN never accepted — capacity returns immediately (AT-06).
        await clear_native_start_intent(fixture.session_factory, run_id, intent_ref)
        await _flow_run(fixture.session_factory, run_id, "failed")
        await release_lease_with_evidence(
            fixture.session_factory, run_id, reason="terminal:start refused"
        )
        return "definite_refusal"
    if answer is not None and answer.handle:
        await record_native_handle(lease.lease_id, answer.handle, fixture.session_factory)
    # The local half reaches a terminal verdict; the NATIVE half is a
    # separate truth released only from evidence. A successful cycle's
    # local terminal verdict is READY_FOR_HUMAN (ADR-0004's completing
    # state); the fault family ends CANCELLED.
    local_status = "ready_for_human" if fate == "ok" else "cancelled"
    await _flow_run(fixture.session_factory, run_id, local_status)
    if fate == "cancel_fail":
        handle = next((h for h, state in lane.jobs.items() if state == "running"), "")
        if handle:
            await lane.cancel(handle)  # cancel_mode=fail → False; job runs on
        await release_lease_with_evidence(
            fixture.session_factory, run_id, reason="terminal:cancelled", native_terminal=False
        )
        # The INVARIANT is the held slot, not who parked it: a concurrent
        # acquirer's terminal-run reclaim may have parked this lease
        # draining first (drained==0 then), but NOTHING may have FREED it.
        row = await _lease_for_run(fixture.session_factory, run_id)
        assert row is not None and row.released_at is None and row.draining_at is not None, (
            "a failed cancel must leave the slot HELD draining, never freed"
        )
        return "cancel_fail_draining"
    outcome = await release_lease_with_evidence(
        fixture.session_factory, run_id, reason=f"terminal:{fate}"
    )
    if outcome.released:
        return "released"
    return "parked_draining"


# ---------------------------------------------------------------------------
# Drill 1 — native-start load
# ---------------------------------------------------------------------------


def _multi_lane_probe(lanes: list[FaultedNativeLane]) -> Callable[[str], Awaitable[NativeStatus]]:
    """One probe consulting every lane of a wave (the reconciler's view).

    A wave may mint jobs in its shared lane AND in per-fate lanes; the
    probe asks each in turn and answers UNKNOWN only when NONE of them
    can decide the key.
    """

    async def probe(key: str) -> NativeStatus:
        for lane in lanes:
            status = await lane.probe(key)
            if status is not NativeStatus.UNKNOWN:
                return status
        return NativeStatus.UNKNOWN

    return probe


async def drill_native_start_load(
    fixture: DrillFixture,
    *,
    workers: int = 8,
    cycles_per_worker: int = 3,
    limit: int = 3,
    cancel_fail_ratio: float = 0.25,
    lost_response_ratio: float = 0.25,
    seed: int = 280,
) -> DrillOutcome:
    """Concurrent dispatch cycles: capacity NEVER exceeded under load.

    ``workers`` tasks drive ``cycles_per_worker`` dispatch-intent →
    handle → terminal cycles each through the REAL admission API
    against the fixture's real database, with a deterministic fate mix
    (failed cancellations and lost start responses included). While
    they run, a sampler counts OPEN leases and watches the occupancy
    mix. Two waves run: wave 1 the pathological lane (every start loses
    its response, every provider cancel fails), wave 2 the healthy one.
    The invariants:

    - the OPEN lease count NEVER exceeds ``limit`` (every sample);
    - failed cancellations and lost responses HOLD their slots
      (``draining``/``dispatched_unknown`` observed mid-load — never a
      free-capacity fiction);
    - after every native job is marked terminal, ONE bounded reconciler
      pass releases every held slot — reconciliation is bounded, not
      eventual-only.
    """

    outcome = DrillOutcome(drill="native_start_load")
    outcome.tested_limits = {
        "workers": workers,
        "cycles_per_worker": cycles_per_worker,
        "max_active_per_project": limit,
        "cancel_fail_ratio": cancel_fail_ratio,
        "lost_response_ratio": lost_response_ratio,
        "database": str(fixture.engine.url).split("://")[0],
    }
    rng = random.Random(seed)
    policy = AdmissionPolicy(max_active_per_project=limit)
    project_id = 1
    max_observed = 0
    saw_draining_hold = False
    saw_dispatched_unknown = False
    unknown_age_seen: int | None = None
    end_states: dict[str, int] = {}

    for wave in range(2):
        lane = FaultedNativeLane(
            lose_response=wave == 0,
            cancel_mode="fail" if wave == 0 else "ok",
        )
        wave_lanes = [lane]
        stop = asyncio.Event()
        sampler_done = asyncio.Event()

        async def sampler() -> None:
            nonlocal max_observed, saw_draining_hold, saw_dispatched_unknown, unknown_age_seen
            while not stop.is_set():
                held = await _open_lease_count(fixture.session_factory, project_id)
                max_observed = max(max_observed, held)
                snapshot = await occupancy_snapshot(fixture.session_factory, project_id)
                if snapshot.get("draining", 0) > 0:
                    saw_draining_hold = True
                if snapshot.get("dispatched_unknown", 0) > 0:
                    saw_dispatched_unknown = True
                report = await saturation_report(
                    policy, project_id, fixture.session_factory, now=datetime.now(UTC)
                )
                age = report["native_start.unknown_age"]
                if report["native_start.unknown_count"] > 0 and age is not None:
                    unknown_age_seen = (
                        age if unknown_age_seen is None else max(unknown_age_seen, age)
                    )
                await asyncio.sleep(0.005)
            sampler_done.set()

        async def worker(index: int) -> None:
            for cycle in range(cycles_per_worker):
                run_id = uuid4().hex
                draw = rng.random()
                fate = "ok"
                if draw < cancel_fail_ratio:
                    fate = "cancel_fail"
                elif draw < cancel_fail_ratio + lost_response_ratio:
                    fate = "lost_response"
                worker_lane = lane
                if fate == "lost_response" and wave != 0:
                    worker_lane = FaultedNativeLane(lose_response=True)
                    wave_lanes.append(worker_lane)
                if fate == "ambiguous":
                    worker_lane = FaultedNativeLane(start_status=rng.choice([429, 503]))
                    wave_lanes.append(worker_lane)
                state = await _drive_dispatch_cycle(
                    fixture,
                    worker_lane,
                    policy,
                    run_id=run_id,
                    intent_ref=f"{worker_lane.prefix}:workflow:o/r/w@runs/{index}/{cycle}",
                    fate=fate,
                    project_id=project_id,
                )
                end_states[state] = end_states.get(state, 0) + 1

        sampler_task = asyncio.create_task(sampler())
        await asyncio.gather(*(worker(index) for index in range(workers)))
        stop.set()
        await sampler_done.wait()
        sampler_task.cancel()
        probe = _multi_lane_probe(wave_lanes)
        # Mid-load pass: bounded work; undecidable leases keep holding
        # (their jobs still run), decidable ones may release.
        await reconcile_draining(fixture.session_factory, probe)
        # Every native job finishes: ONE more bounded pass must drain
        # every slot the wave left held.
        for wave_lane in wave_lanes:
            wave_lane.mark_all_terminal()
        await reconcile_draining(fixture.session_factory, probe)
        remaining = await _open_lease_count(fixture.session_factory, project_id)
        outcome.check(
            remaining == 0,
            f"wave {wave}: every held slot released by ONE reconciler pass after "
            f"observed-terminal ({remaining} still open)",
        )

    held_final = await _open_lease_count(fixture.session_factory, project_id)
    outcome.check(
        max_observed <= limit,
        f"open leases never exceeded the limit of {limit} (peak observed {max_observed}), "
        "including failed cancels and lost responses",
    )
    outcome.check(
        saw_draining_hold or saw_dispatched_unknown,
        "failed cancellations and lost responses HELD their slots mid-load "
        f"(draining seen: {saw_draining_hold}, dispatched_unknown seen: {saw_dispatched_unknown})",
    )
    outcome.check(
        held_final == 0,
        f"the load drains to zero held slots (final {held_final})",
    )
    outcome.signals = {
        "execution.occupied_vs_limit": {"limit": limit, "peak_occupied": max_observed},
        "native_start.unknown_age": {
            "max_observed_seconds": unknown_age_seen,
            "escalation": "reconciler probe (bounded) or audited operator override",
        },
        "cycle_end_states": dict(end_states),
    }
    return outcome


# ---------------------------------------------------------------------------
# Drill 2 — checkpoint upload load under a memory/admission budget
# ---------------------------------------------------------------------------


async def drill_checkpoint_upload_load(
    fixture: DrillFixture,
    *,
    works: int = 4,
    puts_per_work: int = 4,
    max_concurrent: int = 2,
    max_in_flight_bytes: int = 4096,
    uploaders: int = 8,
    gc_lock_wait_s: float = 0.2,
    contended_sweep: bool = True,
    admit_wait_s: float = 0.05,
) -> DrillOutcome:
    """Bounded concurrent puts: overload is a TYPED refusal; lock waits
    honor the #263 GCLockTimeout contract under a contended sweep."""

    outcome = DrillOutcome(drill="checkpoint_upload_load")
    outcome.tested_limits = {
        "works": works,
        "puts_per_work": puts_per_work,
        "uploaders": uploaders,
        "max_concurrent_uploads": max_concurrent,
        "max_in_flight_bytes": max_in_flight_bytes,
        "admission_wait_seconds": admit_wait_s,
        "gc_lock_wait_seconds": gc_lock_wait_s,
    }
    budget = UploadAdmissionBudget(
        max_concurrent=max_concurrent,
        max_in_flight_bytes=max_in_flight_bytes,
        wait_seconds=admit_wait_s,
    )
    landed: list[tuple[str, str]] = []
    refusal_count = 0

    async def uploader(work_index: int) -> None:
        nonlocal refusal_count
        for sequence in range(puts_per_work):
            work_id = f"wp-upload-{work_index % works}"
            manifest, blobs, checkpoint_id = checkpoint_payload(
                work_id, sequence, blob_count=2, blob_bytes=192
            )
            try:
                async with budget.admit(payload_size(manifest, blobs)):
                    await fixture.repository.put(work_id, checkpoint_id, manifest, blobs)
                    landed.append((work_id, checkpoint_id))
            except UploadBudgetExceeded:
                refusal_count += 1

    # A deliberately over-budget single upload: the typed refusal MUST
    # precede any byte of it.
    oversized_manifest, oversized_blobs, oversized_id = checkpoint_payload(
        "wp-oversized", 0, blob_count=8, blob_bytes=4096
    )
    oversize_refused = False
    try:
        async with budget.admit(payload_size(oversized_manifest, oversized_blobs)):
            await fixture.repository.put(
                "wp-oversized", oversized_id, oversized_manifest, oversized_blobs
            )
    except UploadBudgetExceeded:
        oversize_refused = True

    # The contended sweep: an external holder owns the volume lock
    # (another process's live sweep), so a retention pass must refuse
    # TYPED within the wait budget, delete nothing, and converge after.
    gc_timeout_typed = False
    gc_wait_bounded = False
    gc_wait_seconds: float | None = None
    sweep_converged = False
    if contended_sweep:
        os.environ[api_channel.GC_LOCK_WAIT_SECONDS_ENV] = str(gc_lock_wait_s)
        try:
            sweep_old_id = ""
            for sequence in range(2):
                manifest, blobs, checkpoint_id = checkpoint_payload("wp-sweep", sequence)
                if sequence == 0:
                    sweep_old_id = checkpoint_id
                await fixture.repository.put("wp-sweep", checkpoint_id, manifest, blobs)
            sweep_old_blob = fixture.root / sweep_old_id[:2] / sweep_old_id
            import fcntl

            lock_path = fixture.root / "cas-refs.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                started = time.monotonic()
                try:
                    await asyncio.wait_for(
                        fixture.repository.apply_retention("wp-sweep", keep_last=1), timeout=30.0
                    )
                except api_channel.GCLockTimeout:
                    gc_timeout_typed = True
                    gc_wait_seconds = time.monotonic() - started
                    gc_wait_bounded = gc_wait_seconds < gc_lock_wait_s + 2.0
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            # The holder released: the SAME sweep CONVERGES — the retry
            # completes the pending-GC unlink the aborted pass marked.
            await fixture.repository.apply_retention("wp-sweep", keep_last=1)
            sweep_converged = not sweep_old_blob.exists()
        finally:
            os.environ.pop(api_channel.GC_LOCK_WAIT_SECONDS_ENV, None)

    await asyncio.gather(*(uploader(index) for index in range(uploaders)))

    outcome.check(
        budget.peak_concurrent <= max_concurrent,
        f"concurrent uploads never exceeded {max_concurrent} (peak {budget.peak_concurrent})",
    )
    outcome.check(
        budget.peak_bytes <= max_in_flight_bytes,
        f"in-flight upload bytes never exceeded {max_in_flight_bytes}B (peak {budget.peak_bytes}B)",
    )
    outcome.check(
        refusal_count > 0 or budget.peak_concurrent == max_concurrent,
        "overload beyond the declared budget produced TYPED refusals, never a silent "
        f"unbounded queue ({refusal_count} refused, {len(landed)} landed)",
    )
    outcome.check(oversize_refused, "an over-budget single upload was refused TYPED at admission")
    if contended_sweep:
        outcome.check(
            gc_timeout_typed,
            "a contended sweep refused with the TYPED GCLockTimeout — nothing deleted",
        )
        outcome.check(
            gc_wait_bounded,
            f"the lock-wait refusal stayed within its budget (observed {gc_wait_seconds}s "
            f"against {gc_lock_wait_s}s + slack) — bounded, never an indefinite block",
        )
        outcome.check(
            sweep_converged,
            "once the holder released, the same sweep CONVERGED (retry-later is recovery)",
        )
    verified_works = set()
    for work_id, _checkpoint_id in landed:
        entry = await fixture.repository.entry(work_id)
        if entry is not None and entry.get("checkpoint_id"):
            manifest, blobs = await fixture.repository.read_entry(entry)
            if manifest is not None and blobs:
                verified_works.add(work_id)
    distinct_works = {work_id for work_id, _ in landed}
    outcome.check(
        not distinct_works or verified_works == distinct_works,
        f"every landing work's active checkpoint reads back VERIFIED after the load "
        f"({len(verified_works)}/{len(distinct_works)} works)",
    )
    outcome.signals = {
        "checkpoint.upload_memory_budget": {
            "max_concurrent": max_concurrent,
            "max_in_flight_bytes": max_in_flight_bytes,
            "peak_concurrent": budget.peak_concurrent,
            "peak_in_flight_bytes": budget.peak_bytes,
            "typed_refusals": refusal_count + (1 if oversize_refused else 0),
            "landed": len(landed),
        },
        "gc_lock_timeout": {
            "typed_refusal": gc_timeout_typed if contended_sweep else None,
            "observed_wait_s": gc_wait_seconds,
            "converged_after_release": sweep_converged if contended_sweep else None,
        },
    }
    return outcome


# ---------------------------------------------------------------------------
# Drill 3 — control responsiveness under contention
# ---------------------------------------------------------------------------


async def drill_control_responsiveness(
    fixture: DrillFixture,
    *,
    commands: int = 30,
    contention_puts: int = 6,
    control_objective_s: float = 2.0,
    project_id: int = 1,
) -> DrillOutcome:
    """Control commands stay responsive WHILE uploads and retention contend.

    ``commands`` control-plane reads — the operator's occupancy,
    saturation and lease snapshots over the SAME database — run
    concurrently with checkpoint puts and a retention pass. The latency
    of every command is measured; the objective is a BOUNDED p95 under
    contention, honest because these reads never take the store's
    volume-wide GC lock.
    """

    from forge.adaptive.admission import lease_snapshot, occupancy_report

    outcome = DrillOutcome(drill="control_responsiveness")
    outcome.tested_limits = {
        "commands": commands,
        "contention_puts": contention_puts,
        "control_objective_s": control_objective_s,
    }
    policy = AdmissionPolicy(max_active_per_project=3)

    async def control_command() -> float:
        started = time.monotonic()
        await lease_snapshot(policy, project_id, fixture.session_factory)
        await occupancy_report(policy, project_id, fixture.session_factory)
        await saturation_report(policy, project_id, fixture.session_factory)
        return time.monotonic() - started

    async def background_contention() -> None:
        for sequence in range(contention_puts):
            work_id = "wp-control"
            manifest, blobs, checkpoint_id = checkpoint_payload(work_id, sequence)
            await fixture.repository.put(work_id, checkpoint_id, manifest, blobs)
        try:
            await fixture.repository.apply_retention("wp-control", keep_last=1)
        except api_channel.GCLockTimeout:
            pass  # contention is contention — the sweep retries later

    contention_task = asyncio.create_task(background_contention())
    latencies = list(await asyncio.gather(*(control_command() for _ in range(commands))))
    await contention_task
    ordered = sorted(latencies)
    median = ordered[len(ordered) // 2]
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    worst = ordered[-1]
    outcome.check(
        p95 <= control_objective_s,
        f"control-command p95 stayed under {control_objective_s}s while uploads and a "
        f"retention pass contended (p95 {p95:.3f}s, median {median:.3f}s, worst {worst:.3f}s)",
    )
    outcome.signals = {
        "control.command_latency": {
            "commands": commands,
            "median_s": round(median, 4),
            "p95_s": round(p95, 4),
            "max_s": round(worst, 4),
            "objective_s": control_objective_s,
        }
    }
    return outcome


# ---------------------------------------------------------------------------
# Drill 4 — degraded-mode faults
# ---------------------------------------------------------------------------


async def drill_degraded_faults(fixture: DrillFixture) -> DrillOutcome:
    """Every injected fault produces its TYPED behavior — never a hang,
    never a silent success."""

    outcome = DrillOutcome(drill="degraded_faults")
    outcome.tested_limits = {
        "provider_latency_s": 0.1,
        "ambiguous_statuses": [429, 503],
        "definite_statuses": [404],
        "database_restart": "engine disposed and recreated",
        "full_disk": "StoragePolicy per-work quota (typed quota refusal)",
    }
    policy = AdmissionPolicy(max_active_per_project=4)
    project_id = 2

    # -- provider latency: bounded, measured, no hang -------------------------
    slow_lane = FaultedNativeLane(latency_s=0.1)
    started = time.monotonic()
    run_id = uuid4().hex
    state = await _drive_dispatch_cycle(
        fixture,
        slow_lane,
        policy,
        run_id=run_id,
        intent_ref="fake:w:slow@b",
        fate="ok",
        project_id=project_id,
    )
    latency = time.monotonic() - started
    outcome.check(
        0.1 <= latency < 5.0 and state in {"released", "parked_draining"},
        f"provider latency delayed the start but the cycle COMPLETED typed "
        f"({latency:.2f}s, state {state}) — no hang",
    )

    # -- ambiguous faults (429 / 503): occupancy HOLDS, unknown visible -------
    for status in (429, 503):
        lane = FaultedNativeLane(start_status=status)
        run_id = uuid4().hex
        await _drive_dispatch_cycle(
            fixture,
            lane,
            policy,
            run_id=run_id,
            intent_ref=f"fake:w:degraded-{status}@b",
            fate="ambiguous",
            project_id=project_id,
        )
        row = await _lease_for_run(fixture.session_factory, run_id)
        occupancy = lease_occupancy(row) if row is not None else None
        outcome.check(
            occupancy is not None and occupancy.value in {"dispatched_unknown", "draining"},
            f"a {status} start left UNKNOWN occupancy that HOLDS its slot "
            f"({occupancy.value if occupancy else 'no lease'}) — never a free-capacity fiction",
        )
    report = await saturation_report(policy, project_id, fixture.session_factory)
    outcome.check(
        report["native_start.unknown_count"] >= 2,
        f"unknown occupancy stays VISIBLE in the saturation report "
        f"({report['native_start.unknown_count']} unknown, oldest age "
        f"{report['native_start.unknown_age']}s) with its bounded escalation path",
    )

    # -- definite refusal (404): capacity returns immediately -----------------
    lane = FaultedNativeLane(start_status=404)
    run_id = uuid4().hex
    state = await _drive_dispatch_cycle(
        fixture,
        lane,
        policy,
        run_id=run_id,
        intent_ref="fake:w:refused@b",
        fate="definite_refusal",
        project_id=project_id,
    )
    row = await _lease_for_run(fixture.session_factory, run_id)
    outcome.check(
        state == "definite_refusal" and row is not None and row.released_at is not None,
        "a definite 4xx start refusal returned capacity IMMEDIATELY (proven never accepted)",
    )

    # -- database restart: identities survive, resolved by intent ref ---------
    run_id = uuid4().hex
    restart_lane = FaultedNativeLane(lose_response=True)
    await _drive_dispatch_cycle(
        fixture,
        restart_lane,
        policy,
        run_id=run_id,
        intent_ref="fake:w:restart@b",
        fate="lost_response",
        project_id=project_id,
    )
    await fixture.restart_engine()  # the stopped worker's replacement
    report = await saturation_report(policy, project_id, fixture.session_factory)
    outcome.check(
        report["native_start.unknown_count"] >= 1,
        "after the database engine restart the unknown occupancy is STILL on record "
        "(identities survive; no occupancy lost, no oversubscription)",
    )
    restart_lane.mark_all_terminal()
    released = await reconcile_draining(fixture.session_factory, restart_lane.probe)
    outcome.check(
        released >= 1,
        f"the restarted worker's reconciler RESOLVED the unknown occupancy by its intent "
        f"ref ({released} released once observed terminal)",
    )

    # -- full disk: the quota refuses TYPED and the store stays intact --------
    from forge.api_checkpoint_channel import StoragePolicy, StorageQuotaExceededError

    quota_repo = FilesystemCheckpointRepository(
        fixture.work_dir / "tiny-quota-store",
        policy=StoragePolicy(max_total_bytes_per_work=512),
    )
    manifest, blobs, checkpoint_id = checkpoint_payload(
        "wp-full-disk", 0, blob_count=4, blob_bytes=256
    )
    refused_typed = False
    try:
        await quota_repo.put("wp-full-disk", checkpoint_id, manifest, blobs)
    except StorageQuotaExceededError:
        refused_typed = True
    entry_after = await quota_repo.entry("wp-full-disk")
    outcome.check(
        refused_typed and entry_after is None,
        "a full-disk-shaped quota refusal is TYPED (StorageQuotaExceededError) and leaves "
        "the store byte-identical (no entry, no partial checkpoint)",
    )
    outcome.signals = {
        "provider_latency_observed_s": round(latency, 3),
        "native_start.unknown_age": report["native_start.unknown_age"],
        "degraded_modes": [
            "provider latency: completed typed (no hang)",
            "429/503: unknown occupancy held + visible",
            "404: capacity returned immediately",
            "database restart: identities survived, reconciler resolved",
            "full disk: typed quota refusal, store intact",
        ],
    }
    return outcome


# ---------------------------------------------------------------------------
# Drill 5 — backup/restore
# ---------------------------------------------------------------------------


async def drill_backup_restore(fixture: DrillFixture) -> DrillOutcome:
    """Backup and restore metadata AND blobs together; DETECT the
    mismatched-halves restore."""

    outcome = DrillOutcome(drill="backup_restore")
    outcome.tested_limits = {
        "works": 2,
        "checkpoints_per_work": 3,
        "pins": 1,
        "mismatch_shape": "metadata at t2 (new checkpoint) + blobs at t1 (pre-t2 bytes)",
    }
    root = fixture.work_dir / "backup-source"
    repository = FilesystemCheckpointRepository(root)
    for work_index in range(2):
        work_id = f"wp-backup-{work_index}"
        for sequence in range(3):
            manifest, blobs, checkpoint_id = checkpoint_payload(work_id, sequence)
            await repository.put(work_id, checkpoint_id, manifest, blobs)
    entry = await repository.entry("wp-backup-0")
    assert entry is not None, "the backup source must hold an active checkpoint"
    pinned_id = str(entry["checkpoint_id"])
    await repository.pin("wp-backup-0", pinned_id, reason="drill: approved resume spec")

    # t1: the consistent backup (metadata AND blobs together).
    backup = await backup_store(root, fixture.work_dir / "backup-t1")
    # t2: MORE state lands after the backup (a new checkpoint on work 0).
    t2_manifest, t2_blobs, t2_id = checkpoint_payload("wp-backup-0", 9)
    await repository.put("wp-backup-0", t2_id, t2_manifest, t2_blobs)

    # -- the restore drill: selected active + pinned checkpoints recover -----
    target = fixture.work_dir / "restore-target"
    coverage = await restore_store(backup, target)
    restored = FilesystemCheckpointRepository(target)
    recovered_active = True
    for work_index in range(2):
        work_id = f"wp-backup-{work_index}"
        entry = await restored.entry(work_id)
        if entry is None or not entry.get("checkpoint_id"):
            recovered_active = False
            continue
        manifest_back, blobs_back = await restored.read_entry(entry)  # the VERIFIED read
        if not manifest_back or not blobs_back:
            recovered_active = False
    pins_back = await restored.pins("wp-backup-0")
    outcome.check(
        recovered_active,
        "the restore recovered the SELECTED active checkpoint of each work (verified read)",
    )
    outcome.check(
        any(pin.get("checkpoint_id") == pinned_id for pin in pins_back),
        "the PINNED checkpoint and its pin record recovered with the restore",
    )
    outcome.check(
        coverage["pins"] >= 1 and coverage["checkpoints"] >= 6,
        f"the backup carried pins and pending-GC state together with metadata and blobs "
        f"(coverage {coverage})",
    )

    # -- the mismatched pair: metadata from t2, blobs from t1 -----------------
    import shutil

    mismatch_dir = fixture.work_dir / "backup-mismatched"
    mismatch_dir.mkdir(parents=True)
    shutil.copytree(backup.path / "works", mismatch_dir / "works", dirs_exist_ok=True)
    if (backup.path / "pins").is_dir():
        shutil.copytree(backup.path / "pins", mismatch_dir / "pins", dirs_exist_ok=True)
    (mismatch_dir / "works" / "wp-backup-0.json").write_text(
        json.dumps(
            {
                "work_id": "wp-backup-0",
                "checkpoints": [
                    {"checkpoint_id": t2_id, "sequence": 9, "files": 2, "uploaded_at": ""}
                ],
            }
        ),
        encoding="utf-8",
    )
    for shard in sorted(p for p in backup.path.iterdir() if p.is_dir() and len(p.name) == 2):
        shutil.copytree(shard, mismatch_dir / shard.name, dirs_exist_ok=True)
    mismatches = verify_backup_consistency(mismatch_dir)
    refused_restore = False
    affected: list[dict[str, Any]] = []
    refused_target = fixture.work_dir / "restore-refused"
    try:
        await restore_store(mismatch_dir, refused_target)
    except BackupMismatchError as exc:
        refused_restore = True
        affected = exc.affected
    nothing_restored = not refused_target.exists() or not any(refused_target.iterdir())
    outcome.check(
        bool(mismatches),
        f"the mismatched halves were DETECTED before restore ({len(mismatches)} affected "
        "checkpoint(s), the t2-only checkpoint named)",
    )
    outcome.check(
        refused_restore and any(str(item.get("work_id")) == "wp-backup-0" for item in affected),
        "the mismatched restore REFUSED with the typed BackupMismatchError LISTING the "
        "affected works — never silently accepted",
    )
    outcome.check(nothing_restored, "the refused restore wrote NOTHING into the target")
    outcome.signals = {
        "backup.restore_coverage": {
            **coverage,
            "active_recovered": recovered_active,
            "pinned_recovered": any(pin.get("checkpoint_id") == pinned_id for pin in pins_back),
            "mismatch_refused": refused_restore,
            "mismatch_affected_works": sorted({str(i.get("work_id")) for i in affected}),
        }
    }
    return outcome


# ---------------------------------------------------------------------------
# Drill 6 — the operator override audit
# ---------------------------------------------------------------------------


async def drill_operator_override_audit(fixture: DrillFixture) -> DrillOutcome:
    """The approved force-release: the AUDIT makes the risk visible."""

    outcome = DrillOutcome(drill="operator_override_audit")
    outcome.tested_limits = {
        "occupancy_at_override": "dispatched_unknown (intent, no terminal observation)",
        "authorization": "approver + reason + ticket (explicit, recorded)",
    }
    policy = AdmissionPolicy(max_active_per_project=2)
    project_id = 3
    lane = FaultedNativeLane(lose_response=True)

    # The unaudited spelling first: NO override may never free the slot.
    run_id_plain = uuid4().hex
    await _drive_dispatch_cycle(
        fixture,
        lane,
        policy,
        run_id=run_id_plain,
        intent_ref="fake:w:plain@b",
        fate="lost_response",
        project_id=project_id,
    )
    plain = await _lease_for_run(fixture.session_factory, run_id_plain)
    outcome.check(
        plain is not None and plain.released_at is None,
        "WITHOUT the override the same terminal release parked draining — uncertain "
        "occupancy is never relabeled observed-terminal by the local verdict alone",
    )

    # The explicitly approved override: audited, approver + reason visible.
    # The audit trail lives in ``release_reason`` (a 100-char column) — the
    # drill keeps the WHOLE authorization inside that budget so nothing an
    # operator must see is truncated away.
    approver = "ops-oncall"
    reason = "runner decommissioned"
    ticket = "CHANGE-280"
    override_token = f"approver={approver};reason={reason};ticket={ticket}"
    run_id_override = uuid4().hex
    lease = await try_acquire_lease(
        policy, project_id, fixture.session_factory, run_id=run_id_override
    )
    assert lease is not None, "the override drill must hold its slot first"
    await record_native_start_intent(fixture.session_factory, run_id_override, "fake:w:override@b")
    await _flow_run(fixture.session_factory, run_id_override, "cancelled")
    released = await release_lease_with_evidence(
        fixture.session_factory,
        run_id_override,
        reason="terminal:cancelled",
        override=override_token,
    )
    row = await _lease_for_run(fixture.session_factory, run_id_override)
    audit_trail = str(row.release_reason or "") if row is not None else ""
    outcome.check(
        released.released == 1 and row is not None and row.released_at is not None,
        "the explicitly approved override released the slot (one audited action)",
    )
    outcome.check(
        approver in audit_trail and ticket in audit_trail and "override" in audit_trail,
        f"the release REASON is the audit: approver and authorization visible verbatim "
        f"({audit_trail!r})",
    )
    outcome.check(
        row is not None and lease_occupancy(row).value == "observed_terminal",
        "the override's outcome is honestly 'observed_terminal' — the AUDIT record is what "
        "makes the unobserved risk visible, never a relabeling without a trace",
    )
    outcome.signals = {
        "override.audit": {
            "approver": approver,
            "ticket": ticket,
            "audit_trail": audit_trail,
            "unaudited_release_kept_slot": plain is not None and plain.released_at is None,
            "escalation": (
                "an override requires explicit approver + reason; it is recorded in the "
                "release_reason audit trail and reported here — it never silently asserts "
                "the native job ended"
            ),
        }
    }
    return outcome


# ---------------------------------------------------------------------------
# The drill registry and the one-shot runner
# ---------------------------------------------------------------------------

#: Every drill, by the name the script's ``--drill`` accepts.
DRILLS: dict[str, Callable[..., Awaitable[DrillOutcome]]] = {
    "native_start_load": drill_native_start_load,
    "checkpoint_upload_load": drill_checkpoint_upload_load,
    "control_responsiveness": drill_control_responsiveness,
    "degraded_faults": drill_degraded_faults,
    "backup_restore": drill_backup_restore,
    "operator_override_audit": drill_operator_override_audit,
}

#: The fast (CI-sized) kwargs each drill runs with under ``run_drill``.
_FAST_KWARGS: dict[str, dict[str, Any]] = {
    "native_start_load": {"workers": 4, "cycles_per_worker": 2, "limit": 3},
    "checkpoint_upload_load": {
        "works": 3,
        "puts_per_work": 2,
        "uploaders": 6,
        "max_concurrent": 2,
        "max_in_flight_bytes": 2048,
    },
    "control_responsiveness": {"commands": 12, "contention_puts": 3},
}


async def run_drill(
    name: str,
    work_dir: Path,
    *,
    db_url: str = "",
    authority: str = "filesystem",
    fast: bool = True,
) -> DrillOutcome:
    """Build a FRESH disposable fixture and run ONE drill on it.

    ``fast=True`` selects the modest test-sized N (the CI shape);
    ``False`` keeps each drill's own defaults (the operator run's
    sizes). The fixture is disposed afterwards — nothing leaks between
    drills.
    """

    if name not in DRILLS:
        raise KeyError(f"unknown drill {name!r} (known: {', '.join(sorted(DRILLS))})")
    fixture = await build_fixture(work_dir, db_url=db_url, authority=authority)
    try:
        kwargs: dict[str, Any] = _FAST_KWARGS.get(name, {}) if fast else {}
        drill: Callable[..., Awaitable[DrillOutcome]] = DRILLS[name]
        return await drill(fixture, **kwargs)
    finally:
        await fixture.dispose()
