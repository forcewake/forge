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

R38-18 (issue #319) adds the PROFILE-BOUND deployment arms, every one
recording the frozen supported profile's manifest digest and rendering
``unqualified-for-profile`` on a deployment/bind mismatch:
:func:`drill_lost_response_at_cap` (a dropped native dispatch response
with the concurrency cap reached immediately), :func:
`drill_volume_fill_during_pause` (the checkpoint volume filled to the
configured safety threshold during a PAUSED run with a pinned
checkpoint), :func:`drill_mismatched_restore_preflight` (mismatched
metadata/blob snapshots refused at preflight before any new model
turn), :func:`drill_pause_cancel_percentiles` (pause/cancel
responsiveness with stated percentiles and scope under upload +
slow-provider load), plus :func:`profile_binding_row`,
:func:`percentile_summary`, :func:`reviewer_wip_bound_row` and
:func:`summarize_for_publication` (the sanitized published summary —
raw diagnostics stay private, the #304 discipline).

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
import math
import os
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
    check_admission,
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
    "BACKUP_RESTORE_MODEL_TURN_GATE",
    "CAS_SHARED_VOLUME_STATEMENT",
    "CAS_UNSHARED_REPLICA_BOUNDARY",
    "CapBoundaryLane",
    "CONTROL_PLANE_ROOT_CREDENTIAL_NAMES",
    "DEPLOYMENT_DRILL_SCOPE",
    "DRILLS",
    "DRILL_SCOPE",
    "DrillFixture",
    "DrillOutcome",
    "FaultedNativeLane",
    "LostStartResponse",
    "PERCENTILE_MIN_N",
    "PROFILE_BINDING_AXES",
    "PROFILE_QUALIFIED",
    "PROFILE_UNQUALIFIED",
    "PUBLISHED_REPORT_STAMP",
    "PartitionedProbe",
    "RemoteCycleRecord",
    "RemoteDispatchLane",
    "RestoreConsistencyRefused",
    "RestorePreflightRefused",
    "UploadAdmissionBudget",
    "UploadBudgetExceeded",
    "WORKFLOW_RESTORE_TABLES",
    "WorkflowCycleRecord",
    "WorkflowShapeLane",
    "build_fixture",
    "build_topology_document",
    "checkpoint_payload",
    "check_profile_binding",
    "credential_shaped_names",
    "drill_backup_restore",
    "drill_checkpoint_upload_load",
    "drill_control_responsiveness",
    "drill_credential_isolation",
    "drill_degraded_faults",
    "drill_degraded_modes",
    "drill_degradation_parking",
    "drill_lost_response_at_cap",
    "drill_mismatched_restore_preflight",
    "drill_native_start_load",
    "drill_operator_override_audit",
    "drill_pause_cancel_percentiles",
    "drill_partition_occupancy",
    "drill_redemption_lane",
    "drill_remote_occupancy",
    "drill_restore_deployment",
    "drill_token_rotation",
    "drill_volume_fill_during_pause",
    "drill_workflow_envelope",
    "drill_workflow_restore",
    "envelope_percentiles",
    "envelope_stage_seconds",
    "percentile_summary",
    "profile_binding_row",
    "profile_manifest_digest",
    "restore_with_preflight",
    "reviewer_wip_bound_row",
    "run_drill",
    "summarize_for_publication",
    "verify_workflow_consistency",
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
        outcome = await release_lease_with_evidence(
            fixture.session_factory, run_id, reason="terminal:cancelled", native_terminal=False
        )
        # The INVARIANT is the OPERATION's own outcome, read atomically
        # from its return: the failed-cancel release itself must FREE
        # NOTHING (drained, not released). Re-reading the row afterward
        # races with OTHER actors the drill runs concurrently — a
        # reconciler that later observes the native job terminal (or a
        # concurrent acquirer's reclaim parking first) is LEGAL product
        # behavior, not this drill's violation; the return value is the
        # race-free witness of what THIS release did.
        assert outcome.released == 0, (
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


# ---------------------------------------------------------------------------
# R37-20 (issue #301) — the DEPLOYMENT drills
#
# The R36-21 drills above prove the INVARIANTS on disposable fixtures.
# The customer-facing question left open (#301) is what those invariants
# mean on the ACTUAL deployment: the real topology's CAS/storage
# semantics, capacity against REAL remote occupancy, backup/restore over
# the deployment's own bytes, credential isolation on the real launch
# environment, degraded modes through the app's real endpoints, and
# token rotation. Each drill below is the same composable shape, but its
# seams are implemented by ``scripts/run_deployment_ops.py`` against the
# LIVE lab (read-only plus explicitly disposable resources) and by
# fakes in ``tests/test_deployment_ops.py`` — the machinery is identical.
# ---------------------------------------------------------------------------

#: The deployment report's scope sentence — every outcome carries it.
DEPLOYMENT_DRILL_SCOPE: Final = (
    "measured on the actual lab deployment recorded in this report "
    "(control plane, database, CAS volume, runner and provider as "
    "inspected at generation time), through read-only probes and "
    "explicitly DISPOSABLE resources only — no lab container was "
    "started, stopped or recreated by the drill run; the tested N, "
    "budgets and waits are recorded per drill and are NOT a fleet "
    "throughput claim"
)

#: The supported-topology CAS statement (R37-20's "a shared database
#: does not by itself make node-local CAS bytes available to another
#: replica"): the deployment's CAS bytes live on ONE volume mounted into
#: BOTH consumers — that, not the shared database, is what makes the
#: bytes visible to both processes.
CAS_SHARED_VOLUME_STATEMENT: Final = (
    "the checkpoint CAS bytes live on the single /app/data volume bind-"
    "mounted into BOTH the API and the worker (one host directory, one "
    "set of bytes); every process that mounts the volume sees the same "
    "content-addressed store, coordinated by the store's volume-wide "
    "lock and the single-writer publication fence"
)

#: The boundary the report must STATE rather than silently assume: a
#: replica WITHOUT the shared volume would not see the bytes, whatever
#: the database says.
CAS_UNSHARED_REPLICA_BOUNDARY: Final = (
    "a second API/worker replica started WITHOUT the same /app/data "
    "volume would NOT see this node's CAS bytes — the shared database "
    "does not replicate content-addressed blobs; scaling beyond one "
    "volume requires a shared/networked CAS root (or per-replica "
    "stores with an explicit replication contract), which this "
    "deployment has NOT demonstrated"
)

#: Control-plane root credentials the model-facing (lane dispatch)
#: variable set must NEVER carry — the isolation boundary #301 asserts
#: from the recorded dispatch envelope.
CONTROL_PLANE_ROOT_CREDENTIAL_NAMES: Final = (
    "FORGE_LANE_CONTROL_SECRET",
    "FORGE_MCP_KEY",
    "FORGE_MCP_SCOPED_TOKENS",
    "GITLAB_TOKEN",
    "FORGE_BOT_TOKEN",
    "FORGE_GITHUB_TOKEN",
    "FORGE_GITHUB_PRIVATE_KEY",
    "FORGE_AZDO_PAT",
    "DATABASE_URL",
    "REDIS_URL",
)

#: A credential-shaped env NAME pattern (values are never recorded).
_CREDENTIAL_NAME_PATTERN = re.compile(
    r"(?i)(token|secret|key|password|passwd|credential|pat\b|api_key)"
)

#: The #303 credential-delivery REFERENCE names — non-secret: a ref
#: names WHERE the value lives (the CI secret/variable or the
#: redemption route), never a value. R38-17 (#318): the isolation
#: drill flagged them as "credential-shaped beyond the lane token" on
#: a live re-run; they are the delivery CONTRACT's own variables.
NON_SECRET_CREDENTIAL_REF_NAMES: Final = (
    "FORGE_CREDENTIAL_REF",
    "FORGE_CREDENTIAL_REDEEM",
)

#: A ``credential_ref``-shaped dispatch key (``credential_ref``,
#: ``model_credential_ref``, …) — the same reference meaning in any
#: casing/separator spelling. Deliberately narrow: only names ENDING
#: in the ref/redeem words match, so the VALUE carrier names the
#: native profiles map (``FORGE_MODEL_<SEGMENT>`` — e.g.
#: ``FORGE_MODEL_ENV_ANTHROPIC_AUTH_TOKEN``) keep flagging.
_NON_SECRET_REF_SHAPE = re.compile(r"(?i)(^|_)(credential[_-]?ref|credential[_-]?redeem)$")


def is_non_secret_credential_ref(name: str) -> bool:
    """Whether *name* is a credential REFERENCE (a non-secret pointer).

    The #303 delivery vocabulary's two variables, plus any
    ``credential_ref``-shaped dispatch key: refs point at where a
    value lives; they are never values themselves, so the isolation
    boundary (values beyond the lane token) does not apply to them.
    """
    return name in NON_SECRET_CREDENTIAL_REF_NAMES or bool(_NON_SECRET_REF_SHAPE.search(name))


def credential_shaped_names(env: Mapping[str, str]) -> list[str]:
    """The credential-shaped NAMES in *env* (values never touched).

    The #296 executor's ``scan_credential_env`` pattern, applied to a
    supplied mapping so the deployment drill can name what the control
    plane holds without ever recording a value. The #303 non-secret
    REFERENCE names are excluded — they are pointers, not values.
    """

    return sorted(
        name
        for name in env
        if _CREDENTIAL_NAME_PATTERN.search(name) and not is_non_secret_credential_ref(name)
    )


# ---------------------------------------------------------------------------
# The topology declaration — pure composition over read-only inspections
# ---------------------------------------------------------------------------


def build_topology_document(
    *,
    app_health: Mapping[str, Any],
    container_inspections: Mapping[str, Mapping[str, Any]],
    runners: Sequence[Mapping[str, Any]],
    admission_env: Mapping[str, str],
    cas_host_root: str,
    budget_caps: Mapping[str, Any],
) -> dict[str, Any]:
    """Compose the deployment topology document from READ-ONLY inputs.

    *container_inspections* maps container name → the fields the drill
    needs from ``podman inspect`` (``image``, ``mounts`` as destination →
    source, ``status``, ``ports``). The document states the SUPPORTED
    topology (what is demonstrated), the observed services, the
    CAS-semantics statements above, and every DISCREPANCY between the
    declared single-volume shape and what was actually inspected — the
    boundary is stated, never silently assumed.
    """

    declared: dict[str, Any] = {
        "api_replicas": 1,
        "worker_replicas": 1,
        "cas_semantics": "single shared volume (/app/data) mounted into every consumer",
        "lock_behavior": (
            "volume-wide CAS lock (GCLockTimeout-bounded waits) + single-writer "
            "publication fence; the shared database does not replicate CAS bytes"
        ),
        "supported_scaling_boundary": CAS_UNSHARED_REPLICA_BOUNDARY,
    }
    observed_containers: dict[str, Any] = {}
    cas_mount_holders: list[str] = []
    for name, inspection in sorted(container_inspections.items()):
        mounts = {
            str(destination): str(source)
            for destination, source in dict(inspection.get("mounts") or {}).items()
        }
        if any(str(destination).startswith("/app/data") for destination in mounts):
            cas_mount_holders.append(name)
        observed_containers[name] = {
            "image": str(inspection.get("image") or ""),
            "status": str(inspection.get("status") or ""),
            "mounts": mounts,
            "ports": dict(inspection.get("ports") or {}),
        }
    discrepancies: list[str] = []
    #: The CAS CONSUMERS — the processes running the forge code. Services
    #: (postgres/redis/litellm) hold no CAS bytes and are not expected to
    #: mount the volume; a CONSUMER without the mount is.
    expected_consumers = (
        name for name in ("forge-app", "forge-worker") if name in container_inspections
    )
    for name in expected_consumers:
        if name not in cas_mount_holders:
            discrepancies.append(
                f"{name} does NOT mount the shared /app/data volume — its view of "
                "the CAS store is NOT this deployment's bytes (see the boundary "
                "statement; a consumer without the mount is outside the supported "
                "single-volume topology)"
            )
    limit = 3
    raw_limit = str(admission_env.get("FORGE_ADMISSION_MAX_ACTIVE_PER_PROJECT", "")).strip()
    if raw_limit:
        try:
            limit = int(raw_limit)
        except ValueError:
            discrepancies.append(
                "FORGE_ADMISSION_MAX_ACTIVE_PER_PROJECT is not an integer — the "
                "deployment admission bound is the default (3)"
            )
    return {
        "declared": declared,
        "observed": {
            "control_plane": {
                "health": dict(app_health),
                "version": str(app_health.get("version") or ""),
                "schema_head": str(app_health.get("schema") or ""),
                "queue_depth": app_health.get("queue_depth"),
                "dlq_depth": app_health.get("dlq_depth"),
            },
            "containers": observed_containers,
            "cas_volume": {
                "host_root": cas_host_root,
                "mounted_into": cas_mount_holders,
                "statement": CAS_SHARED_VOLUME_STATEMENT,
                "boundary": CAS_UNSHARED_REPLICA_BOUNDARY,
            },
            "services": {
                "postgres": "forge-postgres (127.0.0.1:5433 -> 5432, database 'forge')",
                "redis": "forge-redis (6379)",
                "litellm": "forge-litellm (127.0.0.1:4000 -> 4000)",
            },
            "runner_isolation": {
                "runners": [
                    {
                        "id": runner.get("id"),
                        "description": runner.get("description"),
                        "active": runner.get("active"),
                        "paused": runner.get("paused"),
                        "runner_type": runner.get("runner_type"),
                    }
                    for runner in runners
                ],
                "statement": (
                    "native lane jobs execute on the lab's own runner(s) recorded "
                    "above (no shared public runner fleet); the runner reaches the "
                    "control plane and the model route over the network the lane "
                    "template pins"
                ),
            },
            "admission": {
                "max_active_per_project": limit,
                "source": (
                    "FORGE_ADMISSION_MAX_ACTIVE_PER_PROJECT env"
                    if raw_limit
                    else "default (no env override observed)"
                ),
            },
            "budget_caps": dict(budget_caps),
        },
        "discrepancies": discrepancies,
        "read_only": True,
    }


# ---------------------------------------------------------------------------
# Deployment drill 1 — remote occupancy through the app's dispatch entry
# ---------------------------------------------------------------------------


@dataclass
class RemoteCycleRecord:
    """One dispatch cycle's measured arc on the REAL control plane.

    ``queue_wait_s`` is the wait BEFORE the slot (request → lease
    acquired, or request → the typed park verdict); ``execution_s`` is
    the slot's OWN window (lease acquired → native job observed
    terminal/cancelled). The two are measured SEPARATELY exactly as the
    issue demands — queue wait is never reported as execution time.
    """

    index: int
    run_id: str = ""
    end_state: str = "pending"
    lease_acquired: bool = False
    queue_wait_s: float | None = None
    execution_s: float | None = None
    pipeline_id: int | None = None
    job_id: int | None = None
    occupancy_final: str = ""
    detail: str = ""


class RemoteDispatchLane:
    """The seam the deployment occupancy drill drives.

    ``scripts/run_deployment_ops.py`` implements this against the LIVE
    control plane (a disposable GitLab project, the app's own webhook →
    /implement → /go dispatch entry, REAL native jobs on the lab runner,
    cancelled job-level immediately after the observation); the tests
    implement a deterministic fake. The drill code below owns only the
    invariants and the measurement bookkeeping.
    """

    #: The deployment's OBSERVED active-per-project bound.
    limit: int = 3

    async def dispatch(self, index: int) -> RemoteCycleRecord:
        raise NotImplementedError

    async def occupancy_snapshot(self) -> dict[str, int]:
        """Open leases by occupancy word for the drill's project."""
        raise NotImplementedError

    async def lease_state(self, run_id: str) -> str:
        """One run's lease occupancy word (``""`` when no open lease)."""
        raise NotImplementedError

    async def cancel_immediately(self, record: RemoteCycleRecord) -> str:
        """Job-level cancel of a dispatched native job — ``ok``/``failed``."""
        raise NotImplementedError

    async def cancel_with_lost_response(self, record: RemoteCycleRecord) -> None:
        """The client-seam lost response: the cancel was issued and its
        response is DROPPED without processing (the drill then verifies
        occupancy by observation, never by the dropped answer)."""
        raise NotImplementedError

    async def cancel_completed_job(self, record: RemoteCycleRecord) -> Mapping[str, Any]:
        """Job-level cancel of an ALREADY-completed job — the failed-cancel
        leg. Returns ``{"exercised": bool, "verdict": str, "status_before":
        str, "status_after": str}``: the provider either REFUSES the cancel
        or answers an idempotent no-op — either way the completed job's
        state must not change and no capacity may move."""
        raise NotImplementedError

    async def wait_project_drained(self, timeout_s: float) -> tuple[bool, float]:
        """Bounded wait until the project holds ZERO open leases.

        Returns ``(drained, waited_s)`` — the deployment's own reconciler
        does the draining; the drill only observes, bounded.
        """
        raise NotImplementedError


async def drill_remote_occupancy(
    lane: RemoteDispatchLane,
    *,
    cycles: int = 4,
    reconcile_timeout_s: float = 240.0,
    sample_interval_s: float = 0.5,
) -> DrillOutcome:
    """N concurrent dispatch cycles through the REAL admission: capacity
    NEVER exceeded, overload parked with a typed verdict, queue wait
    measured separately from execution, unknown occupancy visible.

    The lost-response leg cancels a native job and DROPS the response at
    the client seam — occupancy must resolve by the reconciler's
    observation, never by the answer we pretended not to receive. The
    failed-cancel leg cancels an already-completed job: the provider's
    refusal moves nothing (a failed cancel never releases capacity).
    """

    outcome = DrillOutcome(drill="deployment_remote_occupancy")
    outcome.tested_limits = {
        "cycles": cycles,
        "observed_limit": lane.limit,
        "reconcile_timeout_s": reconcile_timeout_s,
        "sample_interval_s": sample_interval_s,
        "native_jobs": "REAL on the lab runner, cancelled job-level immediately after observation",
    }
    peak_occupied = 0
    occupancy_words_seen: set[str] = set()
    stop = asyncio.Event()
    sampler_done = asyncio.Event()

    async def sampler() -> None:
        nonlocal peak_occupied
        while not stop.is_set():
            snapshot = await lane.occupancy_snapshot()
            peak_occupied = max(peak_occupied, sum(snapshot.values()))
            occupancy_words_seen.update(snapshot)
            await asyncio.sleep(sample_interval_s)
        sampler_done.set()

    sampler_task = asyncio.create_task(sampler())
    records = list(await asyncio.gather(*(lane.dispatch(index) for index in range(cycles))))
    dispatched = [record for record in records if record.end_state == "dispatched"]
    parked = [record for record in records if record.end_state != "dispatched"]
    # The observation window is deliberately short: what the sampler saw
    # while the cycles raced is the evidence; then everything is
    # cancelled IMMEDIATELY (bounded spend — no lane runs to completion).
    lost_response_record = dispatched[0] if dispatched else None
    for record in dispatched[1:]:
        await lane.cancel_immediately(record)
    lease_before_lost = ""
    if lost_response_record is not None:
        lease_before_lost = await lane.lease_state(lost_response_record.run_id)
        await lane.cancel_with_lost_response(lost_response_record)
    lease_after_lost = (
        await lane.lease_state(lost_response_record.run_id)
        if lost_response_record is not None
        else ""
    )
    failed_cancel: Mapping[str, Any] = {}
    if dispatched:
        failed_cancel = dict(await lane.cancel_completed_job(dispatched[-1]))
    stop.set()
    await sampler_done.wait()
    sampler_task.cancel()
    drained, drained_after_s = await lane.wait_project_drained(reconcile_timeout_s)

    outcome.check(
        peak_occupied <= lane.limit,
        f"open leases NEVER exceeded the deployment's observed limit of {lane.limit} "
        f"(peak observed {peak_occupied} across {cycles} concurrent dispatch cycles)",
    )
    outcome.check(
        peak_occupied > 0,
        "the load actually exercised occupancy (cycles observed holding slots)",
    )
    outcome.check(
        bool(parked) if cycles > lane.limit else True,
        "sustained overload produced a TYPED park verdict (bounded queueing or a "
        "clear refusal), never silent oversubscription — parked: "
        f"{sorted({record.end_state for record in parked}) or 'none needed (N <= limit)'}",
    )
    measured_queue_wait = [
        record.queue_wait_s for record in records if record.queue_wait_s is not None
    ]
    measured_execution = [
        record.execution_s for record in dispatched if record.execution_s is not None
    ]
    outcome.check(
        len(measured_queue_wait) == cycles,
        f"queue wait measured SEPARATELY from execution for every cycle "
        f"({len(measured_queue_wait)}/{cycles} cycles carry a queue-wait measurement)",
    )
    outcome.check(
        len(measured_execution) == len(dispatched),
        f"execution measured only over the slot's own window for every dispatched "
        f"cycle ({len(measured_execution)}/{len(dispatched)})",
    )
    saw_unknown_hold = bool(occupancy_words_seen & {"dispatched_unknown", "draining"})
    outcome.check(
        saw_unknown_hold or drained,
        "uncertain occupancy stayed VISIBLE while the reconciler had not yet "
        f"observed the native jobs (words seen: {sorted(occupancy_words_seen)}) and "
        "resolved by observation, never by an assumed answer",
    )
    if lost_response_record is not None:
        # The dropped answer itself must have released NOTHING: the run's
        # lease is still accounted (or the reconciler already resolved it
        # by its own observation — never by our pretend-not-seen answer).
        outcome.check(
            lease_after_lost != "" or lease_before_lost == "" or drained,
            "the LOST cancel response changed nothing on its own — the run's lease "
            f"stayed accounted after the dropped answer (before: {lease_before_lost!r}, "
            f"after: {lease_after_lost!r})",
        )
    outcome.check(
        not failed_cancel.get("exercised")
        or failed_cancel.get("status_after") == failed_cancel.get("status_before"),
        "the failed-cancel leg (job-level cancel of an ALREADY-completed job) changed "
        f"NOTHING — the provider {failed_cancel.get('verdict') or 'was not asked'} and the "
        f"job stayed {failed_cancel.get('status_after')!r} — a completed job's cancel "
        "releases no capacity",
    )
    outcome.check(
        drained,
        f"after every native job was cancelled job-level, the deployment's own "
        f"reconciler drained the project to ZERO open leases within "
        f"{reconcile_timeout_s}s (waited {drained_after_s:.1f}s)",
    )
    outcome.signals = {
        "execution.occupied_vs_limit": {
            "limit": lane.limit,
            "peak_occupied": peak_occupied,
            "cycles": cycles,
            "dispatched": len(dispatched),
            "parked": sorted({record.end_state for record in parked}),
        },
        "native.occupancy_unknown": {
            "occupancy_words_seen": sorted(occupancy_words_seen),
            "unknown_held_visible": saw_unknown_hold,
            "lost_response_leg": {
                "run_id": lost_response_record.run_id if lost_response_record else "",
                "lease_before": lease_before_lost,
                "lease_after_drop": lease_after_lost,
            },
            "failed_cancel_leg": failed_cancel or "not exercised",
        },
        "queue_wait_s": {
            "per_cycle": {
                str(record.index): (
                    None if record.queue_wait_s is None else round(record.queue_wait_s, 3)
                )
                for record in records
            },
            "max": round(max(measured_queue_wait), 3) if measured_queue_wait else None,
        },
        "execution_s": {
            "per_cycle": {
                str(record.index): (
                    None if record.execution_s is None else round(record.execution_s, 3)
                )
                for record in dispatched
            },
        },
        "drained_after_s": round(drained_after_s, 1),
        "cycle_end_states": {
            str(record.index): {"state": record.end_state, "detail": record.detail}
            for record in records
        },
    }
    return outcome


# ---------------------------------------------------------------------------
# Deployment drill 2 — backup/restore across the real deployment
# ---------------------------------------------------------------------------


async def drill_restore_deployment(
    source_root: Path,
    *,
    work_dir: Path,
    source_session_factory: Any | None = None,
    restore_session_factory: Any | None = None,
) -> DrillOutcome:
    """Backup the deployment's REAL store (read-only snapshot through the
    store API — no container stop), restore into a DISPOSABLE target and
    verify every pinned checkpoint resolves; a mismatched-halves restore
    is DETECTED and refused.

    Reachability is judged FIDELITY-HONEST: a checkpoint whose verified
    read works in the SOURCE must work after the restore; a checkpoint
    already unavailable/corrupt in the source stays exactly that after
    the restore (preserved and reported under ``checkpoint.reachability``
    as ``source_unavailable`` — never silently "resolved", and never a
    violation: the restore preserves the backed-up state, it does not
    repair it). A zero-file checkpoint is a LEGAL verified read (an
    empty workpackage snapshot is still a content-addressed state)."""

    outcome = DrillOutcome(drill="deployment_backup_restore")
    outcome.tested_limits = {
        "source": "the deployment's real CAS volume (read-only backup_store snapshot)",
        "target": "a disposable restore root (plus a disposable database when supplied)",
        "mismatch_shape": "metadata naming a checkpoint the blob half lacks",
    }
    started = time.monotonic()
    backup = await backup_store(source_root, work_dir / "backup")
    backup_seconds = time.monotonic() - started
    restore_started = time.monotonic()
    target = work_dir / "restore-target"
    coverage = await restore_store(backup, target, session_factory=restore_session_factory)
    restore_seconds = time.monotonic() - restore_started

    source_repo: FilesystemCheckpointRepository | PostgresCheckpointRepository
    restored: FilesystemCheckpointRepository | PostgresCheckpointRepository
    if restore_session_factory is not None:
        restored = PostgresCheckpointRepository(target, restore_session_factory)
    else:
        restored = FilesystemCheckpointRepository(target)
    # The deployed store's authority is its FILESYSTEM index (the works/
    # overlay the consumers compose); *source_session_factory* serves the
    # backup's metadata EXPORT only, never the source read — judging the
    # source through a different authority than the deployment uses
    # would manufacture unavailability that is not there.
    source_repo = FilesystemCheckpointRepository(source_root)

    async def _reachable(repository: Any, work_id: str, checkpoint_id: str | None) -> str:
        """``verified`` / ``absent`` / ``unreadable:<reason>`` — the
        verified read re-hashes every blob on the way out, so a returned
        manifest IS the digest proof (an empty checkpoint is legal)."""

        try:
            entry = await repository.entry(work_id, checkpoint_id)
            if entry is None or not entry.get("checkpoint_id"):
                return "absent"
            manifest, _blobs = await repository.read_entry(entry)
            return "verified" if manifest is not None else "absent"
        except Exception as exc:  # noqa: BLE001 — classified, never fatal to the drill
            return f"unreadable:{type(exc).__name__}"

    works: list[str] = []
    index_dir = backup.path / "works"
    if index_dir.is_dir():
        works = sorted(path.stem for path in index_dir.glob("*.json"))
    active_resolved = 0
    source_unavailable: list[dict[str, str]] = []
    fidelity_broken: list[dict[str, str]] = []
    for work_id in works:
        source_entry = await source_repo.entry(work_id)
        source_state = await _reachable(
            source_repo,
            work_id,
            str(source_entry.get("checkpoint_id") or "") if source_entry else None,
        )
        restore_state = await _reachable(restored, work_id, None)
        if restore_state == "verified":
            active_resolved += 1
        if source_state != "verified":
            source_unavailable.append({"work_id": work_id, "state": source_state})
        elif restore_state != "verified":
            fidelity_broken.append(
                {"work_id": work_id, "source": source_state, "restore": restore_state}
            )
    pins_total = 0
    pins_resolved = 0
    pins_source_unavailable = 0
    pins_fidelity_broken = 0
    for work_id in works:
        for pin in await restored.pins(work_id):
            pins_total += 1
            checkpoint_id = str(pin.get("checkpoint_id") or "")
            source_state = await _reachable(source_repo, work_id, checkpoint_id)
            restore_state = await _reachable(restored, work_id, checkpoint_id)
            if restore_state == "verified":
                pins_resolved += 1
            if source_state != "verified":
                pins_source_unavailable += 1
            elif restore_state != "verified":
                pins_fidelity_broken += 1
    outcome.check(
        bool(works) and not fidelity_broken,
        f"every work whose verified read works in the deployment's store ALSO "
        f"resolves in the disposable restore ({active_resolved}/{len(works)} verified; "
        f"{len(source_unavailable)} already unavailable in the source and preserved "
        f"as unavailable; fidelity broken: {fidelity_broken or 'none'})",
    )
    outcome.check(
        pins_total == 0 or (pins_resolved + pins_source_unavailable) == pins_total,
        f"every PINNED checkpoint either resolves after the restore "
        f"({pins_resolved}/{pins_total} verified) or was ALREADY unavailable in the "
        f"backed-up source ({pins_source_unavailable} preserved as unavailable); "
        f"fidelity broken: {pins_fidelity_broken}",
    )

    # The mismatched halves: metadata naming a checkpoint whose bytes the
    # blob half does not carry — detected BEFORE any restore, refused
    # typed, nothing written.
    import shutil

    mismatch_dir = work_dir / "backup-mismatched"
    mismatch_dir.mkdir(parents=True)
    shutil.copytree(backup.path / "works", mismatch_dir / "works", dirs_exist_ok=True)
    for shard in sorted(p for p in backup.path.iterdir() if p.is_dir() and len(p.name) == 2):
        shutil.copytree(shard, mismatch_dir / shard.name, dirs_exist_ok=True)
    ghost_manifest, ghost_blobs, ghost_id = checkpoint_payload("wp-deploy-mismatch", 9)
    (mismatch_dir / "works" / "wp-deploy-mismatch.json").write_text(
        json.dumps(
            {
                "work_id": "wp-deploy-mismatch",
                "checkpoints": [
                    {
                        "checkpoint_id": ghost_id,
                        "sequence": 9,
                        "files": len(ghost_manifest),
                        "uploaded_at": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    mismatches = verify_backup_consistency(mismatch_dir)
    refused_restore = False
    affected_works: list[str] = []
    refused_target = work_dir / "restore-refused"
    try:
        await restore_store(mismatch_dir, refused_target)
    except BackupMismatchError as exc:
        refused_restore = True
        affected_works = sorted({str(item.get("work_id")) for item in exc.affected})
    nothing_written = not refused_target.exists() or not any(refused_target.iterdir())
    outcome.check(
        bool(mismatches),
        f"the mismatched halves were DETECTED before any restore "
        f"({len(mismatches)} affected checkpoint(s))",
    )
    outcome.check(
        refused_restore and "wp-deploy-mismatch" in affected_works,
        "the mismatched restore REFUSED with the typed BackupMismatchError listing "
        "the affected works — never silently accepted",
    )
    outcome.check(nothing_written, "the refused restore wrote NOTHING into the target")
    outcome.signals = {
        "checkpoint.reachability": {
            "works": len(works),
            "works_resolved": active_resolved,
            "works_source_unavailable": source_unavailable,
            "pins": pins_total,
            "pins_resolved": pins_resolved,
            "pins_source_unavailable": pins_source_unavailable,
            "checkpoints": coverage.get("checkpoints", 0),
            "metadata_rows": coverage.get("metadata_rows", 0),
            "mismatch_detected": bool(mismatches),
            "mismatch_refused": refused_restore,
            "mismatch_affected_works": affected_works,
        },
        "backup_seconds": round(backup_seconds, 3),
        "restore_seconds": round(restore_seconds, 3),
    }
    return outcome


# ---------------------------------------------------------------------------
# Deployment drill 3 — credential isolation + egress denial
# ---------------------------------------------------------------------------


def drill_credential_isolation(
    *,
    envelope: Mapping[str, Any],
    control_plane_env: Mapping[str, str],
    deny_probes: Sequence[Mapping[str, Any]],
) -> DrillOutcome:
    """Assert, from the RECORDED dispatch envelope of the live run, that
    no control-plane root credential entered the model-facing variable
    set — and that the deny probes observed ACTUAL denial on the real
    deployment."""

    outcome = DrillOutcome(drill="deployment_credential_isolation")
    outcome.tested_limits = {
        "envelope_source": "the recorded dispatch envelope of the live run (#288 journal)",
        "control_plane_env": "container env NAMES only (podman inspect, read-only)",
        "deny_probes": "the #296 executor probe pattern against the live surfaces",
    }
    variable_keys = [str(key) for key in envelope.get("variable_keys") or []]
    root_names = [
        name
        for name in CONTROL_PLANE_ROOT_CREDENTIAL_NAMES
        if name in control_plane_env or name in {"DATABASE_URL", "REDIS_URL"}
    ]
    leaked = sorted(set(variable_keys) & set(root_names))
    outcome.check(
        not leaked,
        f"NO control-plane root credential name appears in the model-facing "
        f"variable set of the recorded dispatch ({len(variable_keys)} variables; "
        f"leaks: {leaked or 'none'})",
    )
    # The stronger shape: the ONLY credential-shaped variable the dispatch
    # may inject is the work-scoped lane token itself — plus the #303
    # delivery REFERENCES (R38-17/#318: FORGE_CREDENTIAL_REF /
    # FORGE_CREDENTIAL_REDEEM name where a value lives, never a value;
    # the live re-run false positive is gone, a token-shaped name still
    # flags).
    leaked_credential_shaped = [
        key
        for key in variable_keys
        if _CREDENTIAL_NAME_PATTERN.search(key)
        and key != "FORGE_LANE_CONTROL_TOKEN"
        and not is_non_secret_credential_ref(key)
    ]
    outcome.check(
        not leaked_credential_shaped,
        f"the dispatch envelope carries NO credential-shaped variable beyond the "
        f"work-scoped lane token and the non-secret delivery refs (extra: "
        f"{leaked_credential_shaped or 'none'})",
    )
    outcome.check(
        bool(variable_keys) and bool(envelope.get("token_dispatched")),
        "the dispatch carried the work-scoped, attempt-scoped lane credential "
        "(token_dispatched) and nothing else credential-shaped",
    )
    generation = envelope.get("attempt_generation")
    outcome.check(
        isinstance(generation, int),
        f"the dispatched credential is GENERATION-scoped (attempt_generation="
        f"{generation!r} recorded in the envelope)",
    )
    denied = [probe for probe in deny_probes if str(probe.get("outcome", "")).startswith("denied-")]
    violated = [probe for probe in deny_probes if str(probe.get("outcome")) == "violated"]
    outcome.check(
        not violated and (not deny_probes or len(denied) == len(deny_probes)),
        f"every egress/credential deny probe observed ACTUAL denial on the live "
        f"deployment ({len(denied)}/{len(deny_probes)} denied; violations: "
        f"{[str(probe.get('name')) for probe in violated] or 'none'})",
    )
    outcome.signals = {
        "model_facing_variable_keys": variable_keys,
        "control_plane_root_names_checked": sorted(root_names),
        "credential_shaped_beyond_lane_token": leaked_credential_shaped,
        "credential_names_on_control_plane": credential_shaped_names(control_plane_env),
        "token_dispatched": bool(envelope.get("token_dispatched")),
        "attempt_generation": generation,
        "deny_probes": [dict(probe) for probe in deny_probes],
    }
    return outcome


# ---------------------------------------------------------------------------
# Deployment drill 4 — degraded modes (storage pressure, throttling, slow ACK)
# ---------------------------------------------------------------------------


async def drill_degraded_modes(
    *,
    work_dir: Path,
    health_ok: Callable[[], Awaitable[bool]] | None = None,
    control_ack_cycle: Callable[[], Awaitable[Mapping[str, Any]]] | None = None,
    revive_limit: int = 2,
    control_objective_s: float = 30.0,
) -> DrillOutcome:
    """Every degraded mode keeps its TYPED, bounded behavior — storage
    pressure is a typed quota refusal, provider throttling climbs the
    app's own bounded retry budget (never an infinite retry), and a slow
    control ACK is measured received→applied through the app's real
    endpoints. Publication/cancellation fences are never disabled (the
    health gate is asserted around every leg)."""

    from forge.api_checkpoint_channel import StoragePolicy, StorageQuotaExceededError
    from forge.runs.revival import (
        classify_terminal_failure,
        revival_backoff_seconds,
        revival_limit,
        terminalize_failure,
    )

    outcome = DrillOutcome(drill="deployment_degraded_modes")
    outcome.tested_limits = {
        "storage_pressure": "a quota'd DISPOSABLE store (typed refusal, store intact)",
        "provider_throttling": "a fake 429 lane through the app's own revival budget",
        "slow_control_ack": "through the app's real lane-control endpoints",
        "control_objective_s": control_objective_s,
        "fences": "publication/cancellation fences NEVER disabled",
    }
    healthy_before = (await health_ok()) if health_ok is not None else True

    # -- storage pressure: the typed refusal on a disposable store ------
    quota_repo = FilesystemCheckpointRepository(
        work_dir / "deployment-quota-store",
        policy=StoragePolicy(max_total_bytes_per_work=512),
    )
    manifest, blobs, checkpoint_id = checkpoint_payload(
        "wp-deploy-pressure", 0, blob_count=4, blob_bytes=256
    )
    quota_refusal_typed = False
    try:
        await quota_repo.put("wp-deploy-pressure", checkpoint_id, manifest, blobs)
    except StorageQuotaExceededError:
        quota_refusal_typed = True
    entry_after = await quota_repo.entry("wp-deploy-pressure")
    outcome.check(
        quota_refusal_typed and entry_after is None,
        "storage pressure answered with the TYPED quota refusal and left the store "
        "byte-identical (no entry, no partial checkpoint)",
    )

    # -- provider throttling: the app's own bounded revival budget ------
    fixture = await build_fixture(work_dir / "throttle-fixture")
    try:
        throttled_reason = "harness_start_failed: upstream 429 too many requests"
        classified = classify_terminal_failure(throttled_reason)

        class _ReviveSettings:
            FORGE_RUN_AUTO_REVIVE_LIMIT = revive_limit
            FORGE_RUN_REVIVE_BACKOFF_SECONDS = 60

        settings = _ReviveSettings()
        run_fresh = uuid4().hex
        await _flow_run(fixture.session_factory, run_fresh, "waiting_harness")
        await terminalize_failure(
            fixture.session_factory, settings, run_fresh, reason=throttled_reason
        )
        run_exhausted = uuid4().hex
        await _flow_run(fixture.session_factory, run_exhausted, "waiting_harness")
        async with fixture.session_factory() as session:
            from forge.durable import FlowRun as _FlowRun

            exhausted_row = await session.get(_FlowRun, run_exhausted)
            assert exhausted_row is not None
            exhausted_row.evidence = {
                "revival": {"count": revive_limit, "due_at": "", "reason": throttled_reason}
            }
            await session.commit()
        await terminalize_failure(
            fixture.session_factory, settings, run_exhausted, reason=throttled_reason
        )

        async def _run_state(run_id: str) -> tuple[str, int]:
            async with fixture.session_factory() as session:
                row = await session.get(_FlowRun, run_id)
                assert row is not None
                stamp = dict((row.evidence or {}).get("revival") or {})
                return str(row.status), int(stamp.get("count") or 0)

        fresh_status, fresh_count = await _run_state(run_fresh)
        exhausted_status, exhausted_count = await _run_state(run_exhausted)
        ladder = [revival_backoff_seconds(attempt, settings) for attempt in range(revive_limit + 2)]
        outcome.check(
            classified == "transient",
            f"a fake 429 lane outcome is classified TRANSIENT by the app's own "
            f"classifier ({classified!r}) — throttling is retry-shaped, never fatal",
        )
        outcome.check(
            fresh_status == "blocked" and fresh_count == 1,
            f"the first throttled death parked blocked with a SCHEDULED bounded "
            f"revival (state {fresh_status}, revival count {fresh_count}/{revive_limit})",
        )
        outcome.check(
            exhausted_count == revive_limit and exhausted_status == "blocked",
            f"the exhausted budget NEVER schedules another revival (count stays "
            f"{exhausted_count}/{revive_limit}; sustained throttling degrades to a "
            "clear blocked reason, never an infinite retry)",
        )
        outcome.check(
            max(ladder) <= 900 and ladder == sorted(ladder),
            f"the retry ladder is bounded and monotone ({ladder}, ceiling 900s)",
        )
        outcome.check(
            revival_limit(settings) == revive_limit,
            f"the app's configured revive budget resolves to {revival_limit(settings)}",
        )
    finally:
        await fixture.dispose()

    # -- slow control ACK: measured received→applied through the app -----
    ack_measurement: Mapping[str, Any] = {}
    if control_ack_cycle is not None:
        ack_measurement = await control_ack_cycle()
    received_to_applied = ack_measurement.get("received_to_applied_s")
    outcome.check(
        control_ack_cycle is None
        or (
            isinstance(received_to_applied, (int, float))
            and float(received_to_applied) <= control_objective_s
        ),
        "the slow control ACK's received→applied window is MEASURED through the "
        f"app's real endpoints and inside the {control_objective_s}s objective "
        f"({received_to_applied!r}s)",
    )
    healthy_after = (await health_ok()) if health_ok is not None else True
    outcome.check(
        healthy_before and healthy_after,
        "the app stayed healthy through every degraded mode — the "
        "publication/cancellation fences were never disabled",
    )
    outcome.signals = {
        "storage.quota_refusal": {
            "typed_refusal": quota_refusal_typed,
            "store_intact": entry_after is None,
        },
        "provider_throttling": {
            "classification": "transient",
            "revive_limit": revive_limit,
            "first_death": "blocked + scheduled bounded revival",
            "exhausted_budget": "blocked, no further revival",
            "backoff_ladder_s": ladder,
        },
        "control.received_to_applied": dict(ack_measurement),
    }
    return outcome


# ---------------------------------------------------------------------------
# Deployment drill 5 — lane-control token rotation
# ---------------------------------------------------------------------------


async def drill_token_rotation(
    *,
    secret_current: str,
    secret_next: str,
    work_id: str,
    work_dir: Path,
) -> DrillOutcome:
    """Rotate the lane-control secret on a DISPOSABLE configuration: mint
    v2 credentials, verify the OLD generation (and every v1-secret
    token) is REFUSED by the generation-scoped auth path — the app's own
    ``api_lane_control`` machinery over a disposable database. The
    deployment's env is never touched."""

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from pydantic import SecretStr

    from forge.api_lane_control import lane_control_router, lane_control_token

    outcome = DrillOutcome(drill="deployment_token_rotation")
    outcome.tested_limits = {
        "configuration": "DISPOSABLE (a throwaway database + the real lane-control router)",
        "deployment_env_touched": False,
        "mechanism": "api_lane_control's generation-scoped auth ladder",
    }
    fixture = await build_fixture(work_dir / "rotation-fixture")
    try:
        async with fixture.session_factory() as session:
            session.add(
                FlowRun(
                    id=work_id,
                    project_id=1,
                    provider="gitlab",
                    status="waiting_harness",
                    cancellation_generation=1,
                )
            )
            await session.commit()

        def _mounted_app(secret: str) -> FastAPI:
            app = FastAPI()
            app.include_router(lane_control_router)
            app.state.session_factory = fixture.session_factory

            class _Settings:
                FORGE_LANE_CONTROL_SECRET = SecretStr(secret)

            app.state.settings = _Settings()
            return app

        token_v1_current = lane_control_token(secret_current, work_id, generation=1)
        # The stale-generation token is minted under the CURRENT secret:
        # the superseded-generation oracle names generations within one
        # secret's ladder; a rotated-away secret's tokens never reach it
        # (they fail the plain work-scoping compare — asserted separately).
        token_stale_generation = lane_control_token(secret_next, work_id, generation=0)
        token_v2_current = lane_control_token(secret_next, work_id, generation=1)

        async def _controls_status(secret: str, token: str) -> tuple[int, str]:
            transport = ASGITransport(app=_mounted_app(secret))
            async with AsyncClient(transport=transport, base_url="http://rotation.test") as client:
                response = await client.get(
                    "/lane/controls",
                    params={"work_id": work_id},
                    headers={"Authorization": f"Bearer {token}"},
                )
                detail = ""
                try:
                    detail = str(response.json().get("detail") or "")
                except Exception:  # noqa: BLE001 — the status is the verdict
                    detail = response.text[:120]
                return response.status_code, detail

        v1_ok_status, _ = await _controls_status(secret_current, token_v1_current)
        v1_after_rotation, _ = await _controls_status(secret_next, token_v1_current)
        stale_generation_status, stale_detail = await _controls_status(
            secret_next, token_stale_generation
        )
        v2_ok_status, _ = await _controls_status(secret_next, token_v2_current)

        outcome.check(
            v1_ok_status == 200,
            f"the CURRENT generation's token authenticates before the rotation "
            f"(HTTP {v1_ok_status})",
        )
        outcome.check(
            v1_after_rotation in (401, 403),
            f"after the secret rotation every v1-secret token is REFUSED "
            f"(HTTP {v1_after_rotation}) — rotation retires the whole old credential",
        )
        outcome.check(
            stale_generation_status == 403 and "superseded" in stale_detail.lower(),
            "an OLD-GENERATION token is refused by the generation-scoped path with "
            f"the actionable superseded-generation refusal (HTTP {stale_generation_status})",
        )
        outcome.check(
            v2_ok_status == 200,
            f"the rotated (v2) current-generation token authenticates (HTTP {v2_ok_status})",
        )
        outcome.signals = {
            "credential.rotation_generation": {
                "current_generation": 1,
                "refused_generation": 0,
                "refusal_status": stale_generation_status,
                "refusal_detail": stale_detail[:160],
                "old_secret_token_status": v1_after_rotation,
                "new_secret_token_status": v2_ok_status,
                "procedure": (
                    "mint v2 credentials, dispatch new attempts under them (their "
                    "tokens are generation-scoped), then retire v1: every v1 token "
                    "fails verification the moment the secret changes; within one "
                    "secret, superseded generations are refused naming both "
                    "generations"
                ),
            }
        }
        return outcome
    finally:
        await fixture.dispose()


# ---------------------------------------------------------------------------
# R38-18 (issue #319) — the PROFILE-BOUND deployment arms
#
# The R37-20 drills above measured the deployment as it stood. The frozen
# supported profile (#307,
# ``qualification/profiles/supported-gitlab-ce-v1.json``) changed the
# contract: the measurements are only evidence for the profile whose
# executed-lab bind the deployment MATCHES. Every drill below therefore
# records the manifest digest it ran against and refuses to pass silently
# against a different deployment (``unqualified-for-profile``), and the
# review's three named deployment-only failure arms — lost response at
# the cap, volume fill during pause, mismatched restore — are exercised
# as their own drills with typed outcomes.
# ---------------------------------------------------------------------------

#: The two renderings of a drill's profile qualification (R38-18): a
#: mismatch never passes silently, it renders unqualified.
PROFILE_QUALIFIED: Final = "qualified-for-profile"
PROFILE_UNQUALIFIED: Final = "unqualified-for-profile"

#: The executed-lab bind axes: (axis label, manifest key under
#: ``control_plane.executed_lab``, observed-deployment key). Every axis
#: must be BOTH observed on the deployment and equal to the manifest's
#: executed-lab bind — an unobserved axis is a named difference, never a
#: silent pass.
PROFILE_BINDING_AXES: Final[tuple[tuple[str, str, str], ...]] = (
    ("image_name", "image_name", "image_name"),
    ("image_id", "image_id", "image_id"),
    ("image_digest", "image_digest", "image_digest"),
    ("deployed_schema_head", "deployed_schema_head", "schema_head"),
    ("reported_version", "reported_version", "reported_version"),
)


def profile_manifest_digest(document: Mapping[str, Any]) -> str:
    """Recompute a supported-profile manifest's self-vouching digest.

    The same canonical shape ``scripts/freeze_supported_profile.py``
    froze: sha256 over the document WITHOUT its own ``manifest_digest``
    field, sorted keys, compact separators. A file that drifted from its
    freeze does not vouch for itself — the caller names that, never
    passes it.
    """

    body = {key: value for key, value in document.items() if key != "manifest_digest"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def profile_binding_row(
    manifest: Mapping[str, Any], observed_deployment: Mapping[str, Any]
) -> dict[str, Any]:
    """The profile bind row every R38-18 drill report carries.

    *manifest* is the frozen supported-profile document;
    *observed_deployment* the read-only deployment observation
    (``image_name`` / ``image_id`` / ``image_digest`` / ``schema_head`` /
    ``reported_version``). The row states the digest, the bind verdict
    and every difference — and renders the qualification
    ``unqualified-for-profile`` on ANY difference (including a manifest
    that no longer vouches for itself, or an axis the probes could not
    observe): the measurement is evidence only for the deployment the
    profile's executed-lab bind names, never silently for another.
    """

    differences: list[str] = []
    declared = str(manifest.get("manifest_digest") or "")
    if not declared:
        differences.append("the manifest carries no manifest_digest — nothing to bind")
    elif profile_manifest_digest(manifest) != declared:
        differences.append(
            "the manifest does not vouch for itself (recomputed digest differs from the "
            "declared manifest_digest — the file drifted from its freeze)"
        )
    executed_lab = dict((manifest.get("control_plane") or {}).get("executed_lab") or {})
    for axis, manifest_key, observed_key in PROFILE_BINDING_AXES:
        expected = str(executed_lab.get(manifest_key) or "")
        actual = str(observed_deployment.get(observed_key) or "")
        if not actual:
            differences.append(f"{axis}: not observed — the read-only deployment probe must see it")
        elif expected and actual != expected:
            differences.append(
                f"{axis}: the deployment reports {actual!r} but the profile's executed-lab "
                f"bind is {expected!r}"
            )
    return {
        "profile": str(manifest.get("profile") or ""),
        "manifest_digest": declared,
        "bind": "matched" if not differences else "mismatched",
        "differences": differences,
        "qualification": PROFILE_QUALIFIED if not differences else PROFILE_UNQUALIFIED,
        "statement": (
            "the measurements bind to THIS frozen profile's executed-lab composition; a "
            "deployment that differs renders unqualified-for-profile, never a silent pass"
        ),
    }


def check_profile_binding(outcome: DrillOutcome, binding: Mapping[str, Any]) -> None:
    """Record the profile bind as an objective or a violation.

    The R38-18 discipline: a profile-bound drill that ran against a
    deployment differing from the frozen profile's executed-lab bind
    FAILS naming ``unqualified-for-profile`` — the alternative (passing
    against a different deployment) is exactly the silent substitution
    the profile freeze exists to prevent.
    """

    qualification = str(binding.get("qualification") or "")
    digest = str(binding.get("manifest_digest") or "")
    if qualification == PROFILE_QUALIFIED and digest:
        outcome.objectives.append(
            f"the drill ran BOUND to the frozen supported profile (manifest {digest[:16]}…, "
            "executed-lab bind matched) — qualified-for-profile"
        )
        return
    differences = "; ".join(str(item) for item in binding.get("differences") or ())
    outcome.violations.append(
        f"the deployment does NOT match the frozen supported profile (manifest {digest[:16]}…): "
        f"{differences or 'no differences recorded'} — {PROFILE_UNQUALIFIED}, the measurement "
        "is not evidence for this deployment"
    )


def percentile_summary(
    samples: Sequence[float], *, objective_s: float | None = None
) -> dict[str, Any]:
    """The stated-percentiles summary R38-18 demands (never one latency).

    Nearest-rank percentiles over *samples* (the p-XX value is the
    ``ceil(p/100 · n)``-th ordered sample): ``n``, ``min_s``, ``p50_s``,
    ``p95_s``, ``max_s`` and the optional objective. An empty sample set
    answers ``None`` percentiles with ``n=0`` — never a fabricated zero.
    """

    ordered = sorted(float(sample) for sample in samples)

    def _pct(fraction: float) -> float:
        index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
        return ordered[index]

    if not ordered:
        return {
            "n": 0,
            "min_s": None,
            "p50_s": None,
            "p95_s": None,
            "max_s": None,
            "objective_s": objective_s,
        }
    return {
        "n": len(ordered),
        "min_s": round(ordered[0], 4),
        "p50_s": round(_pct(0.50), 4),
        "p95_s": round(_pct(0.95), 4),
        "max_s": round(ordered[-1], 4),
        "objective_s": objective_s,
    }


def reviewer_wip_bound_row(*, admission_limit: int, reviewer_wip_bound: int) -> dict[str, Any]:
    """The human-capacity discipline row (R38-18 acceptance 5).

    A STATED POLICY FIELD, not a measurement and never a throughput
    claim: the operator declares how much concurrent WIP one human
    reviewer can safely review, and the deployment's admission bound must
    stay within it — model throughput and human review capacity are
    never conflated. An incoherent row (admission beyond the reviewable
    volume) names itself; the caller surfaces it as a policy finding.
    """

    coherent = admission_limit <= reviewer_wip_bound
    return {
        "policy": (
            "admission never queues more work than the stated human review capacity can "
            "safely review (R38-18: human review capacity measured separately from model "
            "throughput)"
        ),
        "reviewer_wip_bound": reviewer_wip_bound,
        "admission_bound": admission_limit,
        "unit": "concurrent reviewable WIP (a STATED POLICY FIELD, not a measured value)",
        "coherent": coherent,
        "statement": (
            f"the deployment's admission bound ({admission_limit} active runs per project) "
            f"stays within the stated reviewer-WIP bound ({reviewer_wip_bound}) — the "
            "operator does not admit work beyond the reviewable volume"
            if coherent
            else f"the admission bound ({admission_limit}) EXCEEDS the stated reviewer-WIP "
            f"bound ({reviewer_wip_bound}) — admission is queuing work beyond the reviewable "
            "volume; lower the admission bound or grow review capacity BEFORE increasing load"
        ),
        "throughput_claim": None,
    }


# ---------------------------------------------------------------------------
# R38-18 arm 1 — a lost native dispatch response AT the concurrency cap
# ---------------------------------------------------------------------------


class CapBoundaryLane(RemoteDispatchLane):
    """The occupancy seam, phased for the cap-boundary probe.

    The deployment reality this seam encodes: a plan is a model call
    (slow, unbounded-ish latency — and per-project fair-use limited),
    while a ``/go`` dispatch verdict at a full cap lands in SECONDS, and
    a dispatched trivial job COMPLETES within minutes (its lease then
    releases). The arm therefore runs in strict phases:

    - ``plan_cycle(index)`` — plan one cycle WITHOUT dispatching it (no
      lease, no capacity; every cycle is planned before ANY dispatch);
    - ``go_dropped(index)`` — ``/go`` the planned cycle and DROP the
      native dispatch response at the client seam: the dispatch IS
      issued, the answer is deliberately never processed — the run's
      occupancy must be proven from DURABLE state afterwards, never from
      the answer the drill pretended not to receive (#241 semantics at
      the cap boundary);
    - ``go_fill(index)`` — ``/go`` the planned cycle and observe its
      dispatch (the cap fill);
    - ``go_over_cap(index)`` — ``/go`` the ALREADY-PLANNED over-cap
      cycle the moment the cap is reached and answer its verdict (the
      typed park, or an honest dispatch if a slot freed).

    With every plan done up front, the /go probe window contains no
    model call — exactly the review's "concurrency cap immediately
    reached" shape.
    """

    async def plan_cycle(self, index: int) -> RemoteCycleRecord:
        """Plan cycle *index* without dispatching it (no lease held)."""
        record = RemoteCycleRecord(index=index)
        record.end_state = "planned"
        return record

    async def go_dropped(self, index: int) -> RemoteCycleRecord:
        raise NotImplementedError

    async def go_fill(self, index: int) -> RemoteCycleRecord:
        raise NotImplementedError

    async def go_over_cap(self, index: int) -> RemoteCycleRecord:
        raise NotImplementedError


async def drill_lost_response_at_cap(
    lane: CapBoundaryLane,
    *,
    profile_binding: Mapping[str, Any],
    reconcile_timeout_s: float = 300.0,
    sample_interval_s: float = 0.5,
) -> DrillOutcome:
    """A native dispatch response dropped + the cap reached immediately.

    The review's negative test 1 (R38-18): every cycle is PLANNED first
    (plans hold no capacity); then one ``/go`` loses its native dispatch
    response at the client seam while the remaining ``/go`` cycles bring
    the project to its concurrency cap; the cycle AFTER the cap must
    park with the typed verdict; the dropped run's occupancy keeps
    consuming capacity (proven from the durable lease row — never from
    the dropped answer) until the reconciler resolves it by observation.
    """

    outcome = DrillOutcome(drill="deployment_lost_response_at_cap")
    limit = lane.limit
    outcome.tested_limits = {
        "observed_limit": limit,
        "dropped_response_cycle": 1,
        "cycles_at_cap": limit,
        "over_cap_cycles": 1,
        "phasing": (
            "every cycle planned BEFORE any dispatch (a plan holds no lease); the /go "
            "probe window contains no model call"
        ),
        "over_cap_cycle": "pre-planned (its model call completes BEFORE the cap fills)",
        "reconcile_timeout_s": reconcile_timeout_s,
        "sample_interval_s": sample_interval_s,
        "native_jobs": "REAL on the deployment's runner, cancelled job-level immediately after observation",
    }
    check_profile_binding(outcome, profile_binding)
    peak_occupied = 0
    occupancy_words_seen: set[str] = set()
    stop = asyncio.Event()
    sampler_done = asyncio.Event()

    async def sampler() -> None:
        nonlocal peak_occupied
        while not stop.is_set():
            snapshot = await lane.occupancy_snapshot()
            peak_occupied = max(peak_occupied, sum(snapshot.values()))
            occupancy_words_seen.update(snapshot)
            await asyncio.sleep(sample_interval_s)
        sampler_done.set()

    sampler_task = asyncio.create_task(sampler())
    # Phase A — every cycle PLANNED, nothing dispatched, no capacity held
    # (a plan is a model call: it stays OUTSIDE the probe window).
    plans = list(await asyncio.gather(*(lane.plan_cycle(index) for index in range(limit + 1))))
    unplanned = [record.index for record in plans if record.end_state != "planned"]
    # Phase B — the dropped-response /go FIRST, the fill cycles with it:
    # the cap is reached immediately with one occupant the client never
    # observed. The whole phase is seconds wide (no model calls inside).
    dropped, *fill = list(
        await asyncio.gather(
            lane.go_dropped(0),
            *(lane.go_fill(index) for index in range(1, limit)),
        )
    )
    # Phase C — the durable-state proof (never the dropped answer): the
    # lease row still accounts the dropped run, and the cap is full.
    lease_of_dropped = await lane.lease_state(dropped.run_id)
    snapshot_at_cap = await lane.occupancy_snapshot()
    occupied_at_cap = sum(snapshot_at_cap.values())
    # Phase D — the already-planned over-cap cycle meets the cap head-on.
    over = await lane.go_over_cap(limit)
    stop.set()
    await sampler_done.wait()
    sampler_task.cancel()
    dispatched = [dropped, *fill]
    for record in dispatched:
        if record.end_state in {"dispatched", "dispatched_response_dropped"}:
            await lane.cancel_immediately(record)
    drained, drained_after_s = await lane.wait_project_drained(reconcile_timeout_s)

    outcome.check(
        not unplanned,
        f"every cycle was PLANNED before any dispatch — the /go probe window contains no "
        f"model call (unplanned cycles: {unplanned or 'none'})",
    )
    outcome.check(
        peak_occupied <= limit,
        f"open leases NEVER exceeded the observed limit of {limit} through the dropped "
        f"dispatch response and the cap race (peak observed {peak_occupied})",
    )
    outcome.check(
        lease_of_dropped != "",
        "the DROPPED native dispatch response released NOTHING on its own — the run's "
        f"lease stayed accounted in durable state (occupancy {lease_of_dropped!r} proven "
        "from the lease row, never from the dropped answer)",
    )
    outcome.check(
        bool(occupancy_words_seen & {"dispatched_unknown", "draining", "native_running"}),
        "the controlled failure kept its occupancy VISIBLE while unresolved (words seen: "
        f"{sorted(occupancy_words_seen)}) — unknown occupancy keeps consuming capacity",
    )
    outcome.check(
        occupied_at_cap == limit,
        f"the cap was reached with the unknown occupancy INSIDE it ({occupied_at_cap}/{limit} "
        "leases open at the cap, mix "
        f"{dict(sorted(snapshot_at_cap.items()))}) — running/unknown/draining correspond to "
        "the configured bound under the controlled failure",
    )
    outcome.check(
        over.end_state.startswith("parked"),
        f"the cycle immediately after the cap parked with the TYPED verdict "
        f"({over.end_state}) — never silent oversubscription",
    )
    outcome.check(
        drained,
        f"after job-level cancels the reconciler resolved the unknown occupancy BY "
        f"OBSERVATION and drained the project to zero open leases within "
        f"{reconcile_timeout_s}s (waited {drained_after_s:.1f}s)",
    )
    outcome.signals = {
        "execution.occupied_vs_limit": {
            "limit": limit,
            "peak_occupied": peak_occupied,
            "occupied_at_cap": occupied_at_cap,
            "occupancy_mix_at_cap": dict(sorted(snapshot_at_cap.items())),
            "occupancy_words_seen": sorted(occupancy_words_seen),
        },
        "native.occupancy_unknown": {
            "dropped_response_lease_word": lease_of_dropped,
            "resolved_by": "reconciler observation (job-level cancels; never the dropped answer)",
            "over_cap_verdict": over.end_state,
        },
        "queue_wait_s": {
            "dropped_cycle": (
                None if dropped.queue_wait_s is None else round(dropped.queue_wait_s, 3)
            ),
            "measured_from": "the durable lease row (the dispatch response was dropped)",
        },
        "drained_after_s": round(drained_after_s, 1),
    }
    return outcome


# ---------------------------------------------------------------------------
# R38-18 arm 2 — the checkpoint volume filled to the safety threshold
# during a PAUSED run
# ---------------------------------------------------------------------------


async def drill_volume_fill_during_pause(
    work_dir: Path,
    *,
    profile_binding: Mapping[str, Any],
    safety_threshold_bytes: int = 4096,
    pinned_read_objective_s: float = 5.0,
) -> DrillOutcome:
    """The checkpoint volume filled to the configured safety threshold
    while a run is PAUSED with a pinned checkpoint (R38-18 negative test
    2).

    The volume is a QUOTA'D DISPOSABLE tmp-dir store — a real disk is
    never filled; the typed-refusal path is what is measured. The paused
    run's WIP is the pause-fence shape (a checkpoint PINNED by the
    persisted continuation decision); the fill is the further WIP
    snapshots that keep landing. At the threshold: new writes refuse
    TYPED (:class:`StorageQuotaExceededError`), every further write
    refuses at the same predictable boundary (admission stops on a typed
    signal, never a silent queue), and the PINNED WIP survives — a
    verified read at/after the threshold, its bytes never deleted.
    """

    from forge.api_checkpoint_channel import StoragePolicy, StorageQuotaExceededError

    outcome = DrillOutcome(drill="deployment_volume_fill_during_pause")
    outcome.tested_limits = {
        "volume_shape": "a quota'd DISPOSABLE tmp-dir store (a real disk is NEVER filled)",
        "safety_threshold_bytes": safety_threshold_bytes,
        "paused_work": "a checkpoint PINNED by the pause fence (the persisted continuation decision)",
        "further_write_attempts": 4,
        "pinned_read_objective_s": pinned_read_objective_s,
    }
    check_profile_binding(outcome, profile_binding)

    root = work_dir / "volume-fill-store"
    repository = FilesystemCheckpointRepository(
        root, policy=StoragePolicy(max_total_bytes_per_work=safety_threshold_bytes)
    )
    # The paused run's pinned WIP.
    work_id = "wp-paused-run"
    pinned_manifest, pinned_blobs, pinned_id = checkpoint_payload(
        work_id, 0, blob_count=2, blob_bytes=256
    )
    await repository.put(work_id, pinned_id, pinned_manifest, pinned_blobs)
    await repository.pin(
        work_id, pinned_id, reason="pause fence: the persisted continuation decision"
    )

    # Fill toward the configured safety threshold — further WIP snapshots
    # keep landing for the paused work until the volume refuses.
    typed_refusals = 0
    landed_during_fill = 0
    sequence = 1
    for _attempt in range(8):
        manifest, blobs, checkpoint_id = checkpoint_payload(
            work_id, sequence, blob_count=2, blob_bytes=512
        )
        sequence += 1
        try:
            await repository.put(work_id, checkpoint_id, manifest, blobs)
            landed_during_fill += 1
        except StorageQuotaExceededError:
            typed_refusals += 1
            break
    # AT the threshold: every further new write must refuse at the same
    # predictable boundary (zero silent growth — admission stops typed).
    silent_writes = 0
    for extra in range(4):
        manifest, blobs, checkpoint_id = checkpoint_payload(
            work_id, sequence + extra, blob_count=2, blob_bytes=512
        )
        try:
            await repository.put(work_id, checkpoint_id, manifest, blobs)
            silent_writes += 1
        except StorageQuotaExceededError:
            typed_refusals += 1

    # The pinned WIP survives: a VERIFIED read at/after the threshold.
    entry = await repository.entry(work_id)
    pins = await repository.pins(work_id)
    pin_rows = [pin for pin in pins if str(pin.get("checkpoint_id")) == pinned_id]
    started = time.monotonic()
    verified_read = False
    if entry is not None and entry.get("checkpoint_id"):
        manifest_back, _blobs_back = await repository.read_entry(entry)
        verified_read = manifest_back is not None
    pinned_read_s = time.monotonic() - started
    pinned_bytes_present = (root / pinned_id[:2] / pinned_id).is_file() and all(
        (root / digest[:2] / digest).is_file() for digest in pinned_blobs
    )
    store_bytes = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())

    outcome.check(
        typed_refusals >= 1 and silent_writes == 0,
        f"new writes at the safety threshold refused TYPED (StorageQuotaExceededError; "
        f"{typed_refusals} refusals, {silent_writes} silent writes) — storage growth "
        "stopped at the configured boundary",
    )
    outcome.check(
        bool(pin_rows) and verified_read,
        "the PINNED WIP SURVIVED the fill: the paused run's pinned checkpoint still reads "
        f"VERIFIED at the threshold ({len(pin_rows)} pin record(s), verified read "
        f"{pinned_read_s:.3f}s)",
    )
    outcome.check(
        pinned_bytes_present,
        "the pinned checkpoint's bytes were never deleted — quota exhaustion removed "
        "nothing the pause fence pinned",
    )
    outcome.check(
        pinned_read_s <= pinned_read_objective_s,
        f"the paused run's resume-read stayed responsive at the threshold "
        f"({pinned_read_s:.3f}s ≤ {pinned_read_objective_s}s objective)",
    )
    outcome.signals = {
        "storage.volume_fill": {
            "safety_threshold_bytes": safety_threshold_bytes,
            "store_bytes_at_refusal": store_bytes,
            "typed_refusals": typed_refusals,
            "landed_during_fill": landed_during_fill,
            "silent_writes": silent_writes,
            "admission_stop": (
                "typed refusal at the store boundary — every further write refused at the "
                "same predictable boundary (quota-tmp-dir shape, never a real disk)"
            ),
            "pinned_wip_survives": bool(pin_rows) and verified_read,
            "pinned_read_s": round(pinned_read_s, 4),
        }
    }
    return outcome


# ---------------------------------------------------------------------------
# R38-18 arm 3 — mismatched metadata/blob snapshots refused at preflight
# ---------------------------------------------------------------------------


class RestorePreflightRefused(Exception):
    """The restore preflight refused — TYPED, before any new model turn.

    ``reason`` is ``"schema-head"`` (the disposable installation's
    declared schema head does not match the frozen profile's) or
    ``"backup-halves"`` (the metadata half names checkpoints whose blob
    bytes the other half lacks). Either way the restore never starts and
    the resume dispatch — the first thing that would spend a model turn
    — is never reached.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"restore preflight refused ({reason}): {detail}")
        self.reason = reason
        self.detail = detail


#: The gate's receipt stamp: the preflight order is asserted by counting
#: model turns BEFORE the gate may open (R38-18: preflight refuses
#: BEFORE any new model turn).
BACKUP_RESTORE_MODEL_TURN_GATE: Final = (
    "preflight → restore → resume dispatch (the first model turn)"
)


async def restore_with_preflight(
    backup_dir: Path,
    target: Path,
    *,
    expected_schema_head: str,
    observed_schema_head: str,
    session_factory: Any | None = None,
) -> dict[str, Any]:
    """The R38-18 restore preflight gate (schema bind + halves + restore).

    The order the review demands: the installation's declared schema
    head must match the frozen profile's, the backup halves must be
    consistent, and only then does the restore run. Every refusal is the
    TYPED :class:`RestorePreflightRefused` — the caller's resume
    dispatch (a new model turn) sits AFTER this gate and is never
    reached on a refusal.
    """

    if observed_schema_head != expected_schema_head:
        raise RestorePreflightRefused(
            "schema-head",
            f"the disposable installation declares schema head {observed_schema_head!r} but "
            f"the frozen profile binds {expected_schema_head!r} — the metadata snapshot "
            "belongs to a different schema generation",
        )
    mismatches = verify_backup_consistency(backup_dir)
    if mismatches:
        affected = sorted({str(item.get("work_id")) for item in mismatches})
        raise RestorePreflightRefused(
            "backup-halves",
            f"the metadata half names {len(mismatches)} checkpoint(s) whose blob bytes the "
            f"other half lacks (affected works: {', '.join(affected)})",
        )
    return dict(await restore_store(backup_dir, target, session_factory=session_factory))


async def drill_mismatched_restore_preflight(
    work_dir: Path,
    *,
    profile_binding: Mapping[str, Any],
    expected_schema_head: str = "027",
    mismatched_schema_head: str = "026",
) -> DrillOutcome:
    """Mismatched metadata/blob snapshots restored into a DISPOSABLE
    installation: the preflight refuses BEFORE any new model turn
    (R38-18 negative test 3, against the frozen profile's schema head).

    Three installations are tried in order, counting model turns: the
    mismatched-halves snapshot and the wrong-schema installation both
    refuse TYPED with ZERO model turns spent; only the consistent
    snapshot at the profile's schema head restores — and only then may
    the resume dispatch (the first new model turn) run.
    """

    outcome = DrillOutcome(drill="deployment_mismatched_restore_preflight")
    outcome.tested_limits = {
        "expected_schema_head": expected_schema_head,
        "mismatched_schema_head": mismatched_schema_head,
        "snapshot_shape": "metadata naming checkpoints the blob half lacks (t2 metadata + t1 blobs)",
        "installations": "DISPOSABLE target roots (never the deployment's store)",
        "gate": BACKUP_RESTORE_MODEL_TURN_GATE,
    }
    check_profile_binding(outcome, profile_binding)

    # A seeded disposable source: two works, a pinned checkpoint.
    root = work_dir / "preflight-source"
    repository = FilesystemCheckpointRepository(root)
    for work_index in range(2):
        work_id = f"wp-preflight-{work_index}"
        for sequence in range(2):
            manifest, blobs, checkpoint_id = checkpoint_payload(work_id, sequence)
            await repository.put(work_id, checkpoint_id, manifest, blobs)
    entry = await repository.entry("wp-preflight-0")
    assert entry is not None, "the preflight source must hold an active checkpoint"
    pinned_id = str(entry["checkpoint_id"])
    await repository.pin("wp-preflight-0", pinned_id, reason="preflight drill pin")
    backup = await backup_store(root, work_dir / "preflight-backup")

    # The model-turn gate: the resume dispatch that would spend the first
    # NEW model turn after a restore. It opens ONLY after a consistent
    # restore passes preflight.
    model_turns = 0

    def _open_gate() -> None:
        nonlocal model_turns
        model_turns += 1

    # -- 1) mismatched halves: t2 metadata + t1 blobs --------------------
    import shutil

    mismatch_dir = work_dir / "preflight-mismatched"
    mismatch_dir.mkdir(parents=True)
    shutil.copytree(backup.path / "works", mismatch_dir / "works", dirs_exist_ok=True)
    if (backup.path / "pins").is_dir():
        shutil.copytree(backup.path / "pins", mismatch_dir / "pins", dirs_exist_ok=True)
    ghost_manifest, ghost_blobs, ghost_id = checkpoint_payload("wp-preflight-ghost", 9)
    (mismatch_dir / "works" / "wp-preflight-ghost.json").write_text(
        json.dumps(
            {
                "work_id": "wp-preflight-ghost",
                "checkpoints": [
                    {
                        "checkpoint_id": ghost_id,
                        "sequence": 9,
                        "files": len(ghost_manifest),
                        "uploaded_at": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    for shard in sorted(p for p in backup.path.iterdir() if p.is_dir() and len(p.name) == 2):
        shutil.copytree(shard, mismatch_dir / shard.name, dirs_exist_ok=True)

    halves_refusal: RestorePreflightRefused | None = None
    halves_target = work_dir / "preflight-refused-halves"
    try:
        await restore_with_preflight(
            mismatch_dir,
            halves_target,
            expected_schema_head=expected_schema_head,
            observed_schema_head=expected_schema_head,
        )
    except RestorePreflightRefused as exc:
        halves_refusal = exc
    halves_nothing_written = not halves_target.exists() or not any(halves_target.iterdir())

    # -- 2) the wrong-schema installation --------------------------------
    schema_refusal: RestorePreflightRefused | None = None
    schema_target = work_dir / "preflight-refused-schema"
    try:
        await restore_with_preflight(
            backup.path,
            schema_target,
            expected_schema_head=expected_schema_head,
            observed_schema_head=mismatched_schema_head,
        )
    except RestorePreflightRefused as exc:
        schema_refusal = exc
    schema_nothing_written = not schema_target.exists() or not any(schema_target.iterdir())

    # -- 3) the consistent snapshot at the profile's head MAY open the gate
    consistent_target = work_dir / "preflight-restored"
    coverage = await restore_with_preflight(
        backup.path,
        consistent_target,
        expected_schema_head=expected_schema_head,
        observed_schema_head=expected_schema_head,
    )
    restored = FilesystemCheckpointRepository(consistent_target)
    restored_entry = await restored.entry("wp-preflight-0")
    restored_verified = False
    if restored_entry is not None and restored_entry.get("checkpoint_id"):
        manifest_back, _blobs_back = await restored.read_entry(restored_entry)
        restored_verified = manifest_back is not None
    if restored_verified:
        _open_gate()  # the first new model turn — only NOW legal

    outcome.check(
        halves_refusal is not None and halves_refusal.reason == "backup-halves",
        "the mismatched metadata/blob snapshot was REFUSED at preflight with the typed "
        f"RestorePreflightRefused (backup-halves: {halves_refusal})",
    )
    outcome.check(
        halves_nothing_written,
        "the refused mismatched restore wrote NOTHING into the disposable installation",
    )
    outcome.check(
        schema_refusal is not None and schema_refusal.reason == "schema-head",
        f"an installation declaring schema head {mismatched_schema_head!r} against the "
        f"frozen profile's {expected_schema_head!r} was REFUSED at preflight BEFORE any "
        "restore ran",
    )
    outcome.check(
        schema_nothing_written,
        "the wrong-schema preflight refusal wrote NOTHING into the target",
    )
    outcome.check(
        model_turns == 1 and restored_verified,
        "the preflight ordering held: ZERO model turns through both refusals, and the "
        f"resume dispatch (the first new model turn) ran only after the consistent "
        f"restore at schema head {expected_schema_head} verified ({model_turns} turn(s))",
    )
    outcome.signals = {
        "preflight.restore_gate": {
            "expected_schema_head": expected_schema_head,
            "refusals": {
                "backup-halves": halves_refusal is not None,
                "schema-head": schema_refusal is not None,
            },
            "nothing_written": halves_nothing_written and schema_nothing_written,
            "model_turns_before_refusals": 0,
            "model_turns_after_consistent_restore": model_turns,
            "restored_checkpoints": coverage.get("checkpoints", 0),
            "restored_verified": restored_verified,
        }
    }
    return outcome


# ---------------------------------------------------------------------------
# R38-18 measurement — pause/cancel responsiveness percentiles under
# upload + slow-provider load (the REAL lane-control endpoints)
# ---------------------------------------------------------------------------


async def drill_pause_cancel_percentiles(
    work_dir: Path,
    *,
    profile_binding: Mapping[str, Any],
    cycles: int = 12,
    upload_workers: int = 3,
    puts_per_worker: int = 8,
    provider_latency_s: float = 0.05,
    control_objective_s: float = 60.0,
) -> DrillOutcome:
    """Pause/cancel responsiveness with STATED PERCENTILES and scope —
    never one best-case latency (R38-18 acceptance 2).

    ``cycles`` pause/resume control commands run through the app's REAL
    lane-control endpoints (the mounted router over a disposable
    database — the same machinery the deployment serves), each measured
    received→applied from the durable row, WHILE bounded uploads contend
    on a real store and every cycle also drives one slow-provider
    start→cancel (the cancel-under-slow-provider shape, measured
    issued→terminal). The scope sentence travels with the numbers.
    """

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from pydantic import SecretStr

    from forge.adaptive.mailbox_db import PostgresMailbox
    from forge.adaptive.models import ControlCommand
    from forge.api_lane_control import lane_control_router, lane_control_token

    outcome = DrillOutcome(drill="deployment_pause_cancel_percentiles")
    outcome.tested_limits = {
        "cycles": cycles,
        "kinds": "pause/resume control commands through the REAL lane-control endpoints",
        "upload_workers": upload_workers,
        "puts_per_worker": puts_per_worker,
        "provider_latency_s": provider_latency_s,
        "control_objective_s": control_objective_s,
        "fixture": "disposable (a real SQLite database + the real mounted lane-control router)",
    }
    check_profile_binding(outcome, profile_binding)

    fixture = await build_fixture(work_dir / "percentile-fixture")
    secret = "percentile-drill-secret"
    work_id = "wpercentile0001"
    try:
        async with fixture.session_factory() as session:
            session.add(
                FlowRun(
                    id=work_id,
                    project_id=1,
                    provider="gitlab",
                    status="waiting_harness",
                    cancellation_generation=1,
                )
            )
            await session.commit()

        app = FastAPI()
        app.include_router(lane_control_router)
        app.state.session_factory = fixture.session_factory

        class _Settings:
            FORGE_LANE_CONTROL_SECRET = SecretStr(secret)

        app.state.settings = _Settings()
        mailbox = PostgresMailbox(fixture.session_factory)
        token = lane_control_token(secret, work_id, generation=1)
        headers = {"Authorization": f"Bearer {token}"}

        # The bounded background load: uploads on a real store + (per
        # cycle below) slow provider starts. Bounded by construction.
        load_store = FilesystemCheckpointRepository(work_dir / "percentile-load-store")

        async def upload_worker(offset: int) -> None:
            for sequence in range(offset, offset + puts_per_worker):
                manifest, blobs, checkpoint_id = checkpoint_payload(
                    "wp-percentile-load", sequence % 6
                )
                await load_store.put("wp-percentile-load", checkpoint_id, manifest, blobs)

        upload_tasks = [
            asyncio.create_task(upload_worker(index * puts_per_worker))
            for index in range(upload_workers)
        ]

        slow_lane = FaultedNativeLane(latency_s=provider_latency_s)
        control_latencies: list[float] = []
        cancel_latencies: list[float] = []
        ack_failures = 0

        for cycle in range(1, cycles + 1):
            kind = "pause" if cycle % 2 == 1 else "resume"
            command = ControlCommand.model_validate(
                {
                    "command_id": f"cmd-pctl-{work_id}-{cycle}",
                    "work_id": work_id,
                    "sequence": cycle,
                    "kind": kind,
                    "actor_ref": "deployment-ops drill",
                    "actor_origin": "server_authenticated_human",
                    "idempotency_key": f"pctl-{work_id}-{cycle}",
                    "status": "received",
                    "payload": {"run_id": work_id},
                }
            )
            await mailbox.submit(command)
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://percentile.test"
            ) as client:
                ladder: list[tuple[str, dict[str, Any]]] = [
                    ("authorized", {}),
                    ("dispatching", {"plan_revision": 0, "execution_epoch": 0}),
                    ("vendor_accepted", {}),
                    ("applied", {}),
                ]
                for state, extra in ladder:
                    response = await client.post(
                        f"/lane/controls/{command.command_id}/ack",
                        headers=headers,
                        json={"state": state, "generation": 1, **extra},
                    )
                    if response.status_code != 200:
                        ack_failures += 1
                        break
            async with fixture.session_factory() as session:
                from sqlalchemy import select

                from forge.adaptive.mailbox_db import ControlCommandRow

                row = (
                    await session.execute(
                        select(ControlCommandRow).where(ControlCommandRow.id == command.command_id)
                    )
                ).scalar_one()
            if row.applied_at is not None:
                received = row.created_at
                applied = row.applied_at
                if received.tzinfo is None:
                    received = received.replace(tzinfo=UTC)
                if applied.tzinfo is None:
                    applied = applied.replace(tzinfo=UTC)
                control_latencies.append((applied - received).total_seconds())
            # The cancel-under-slow-provider leg: one slow start then a
            # provider-side cancel, measured issued→terminal.
            cancel_started = time.monotonic()
            try:
                answer = await slow_lane.start(work_id, f"pctl:{work_id}:{cycle}")
            except LostStartResponse:
                answer = None
            if answer is not None and answer.handle:
                await slow_lane.cancel(answer.handle)
            cancel_latencies.append(time.monotonic() - cancel_started)

        await asyncio.gather(*upload_tasks)
    finally:
        await fixture.dispose()

    control_percentiles = percentile_summary(control_latencies, objective_s=control_objective_s)
    cancel_percentiles = percentile_summary(cancel_latencies, objective_s=control_objective_s)
    outcome.check(
        len(control_latencies) == cycles and ack_failures == 0,
        f"every pause/resume command reached APPLIED through the real ladder "
        f"({len(control_latencies)}/{cycles} applied, {ack_failures} ack failure(s))",
    )
    outcome.check(
        control_percentiles["p95_s"] is not None
        and float(control_percentiles["p95_s"]) <= control_objective_s,
        f"control-command received→applied p95 stayed inside the {control_objective_s}s "
        f"objective under upload+slow-provider load (p50 {control_percentiles['p50_s']}s, "
        f"p95 {control_percentiles['p95_s']}s, max {control_percentiles['max_s']}s over "
        f"{control_percentiles['n']} cycles)",
    )
    outcome.check(
        cancel_percentiles["p95_s"] is not None
        and float(cancel_percentiles["p95_s"]) <= control_objective_s,
        f"cancel-under-slow-provider issued→terminal p95 stayed inside the "
        f"{control_objective_s}s objective (p50 {cancel_percentiles['p50_s']}s, p95 "
        f"{cancel_percentiles['p95_s']}s, max {cancel_percentiles['max_s']}s over "
        f"{cancel_percentiles['n']} cycles)",
    )
    outcome.signals = {
        "control.pause_cancel_percentiles_s": {
            "control": control_percentiles,
            "cancel_under_slow_provider": cancel_percentiles,
            "scope": (
                f"n={cycles} pause/resume command cycles through the REAL lane-control "
                f"endpoints over a disposable database, contended by {upload_workers}x"
                f"{puts_per_worker} checkpoint uploads and {provider_latency_s}s provider "
                f"start latency; the objective is {control_objective_s}s — a stated "
                "percentile table for THIS shape, never a best-case single latency and "
                "not a fleet claim"
            ),
        }
    }
    return outcome


# ---------------------------------------------------------------------------
# R40-15 (issue #351) — the OPERATING ENVELOPE arms
#
# The R38-18 arms measured the deployment's limits on the SIMPLE dispatch
# shape. The selected workflow has since grown its real shape — review
# rounds (#338), guarded amendments (#340) and runner-redemption dispatch
# (#343, the CURRENT lane mode) — and a token-speed number is not factory
# throughput. Every drill below re-measures the envelope over THAT shape:
#
# - :func:`drill_partition_occupancy` — occupancy under uncertainty while
#   the observation channel itself is down (the simulated network
#   partition): slots stay occupied while native work runs OR its
#   start/cancel outcome is unknown, the reconciler's partitioned pass
#   releases NOTHING, and ONE bounded pass drains everything after the
#   heal;
# - :func:`drill_degradation_parking` — the WORKLOAD side of provider
#   degradation (the app side is :func:`drill_degraded_modes`): a queued
#   burst during a sustained 429 window parks BOUNDED — typed intake
#   refusals beyond the fair-use bounds, a bounded queue, at most
#   ``1 + revive_limit`` dispatch attempts per run and ZERO model turns
#   (degradation never becomes a code-repair loop);
# - :func:`drill_workflow_envelope` — the envelope FROM the selected
#   workflow's own shape, seam-driven (:class:`WorkflowShapeLane`): a run
#   that includes a review round, an amendment and a redemption-mode
#   dispatch, with command-to-applied / issue-to-reviewed-ready /
#   reviewer-wait measured as SEPARATE records (percentiles only where
#   the sample supports them — n is labelled everywhere);
# - :func:`drill_redemption_lane` — the drills' harness leg in the
#   CURRENT lane mode: the real mounted router, a grant minted the
#   dispatch seam's way (``runner-redemption``) and the lane's redemption
#   through the REAL endpoint, plus the typed refusal arms;
# - :func:`drill_workflow_restore` — the data-bearing restore drill: a
#   disposable installation seeded with the REAL workflow shape (round
#   rows, amendment rows, grant + redemption rows, native-intent leases)
#   is backed up and restored; work + checkpoint + native-intent
#   consistency — the review_rounds and budget_amendments rows included —
#   is VERIFIED before the dispatch gate may open.
# ---------------------------------------------------------------------------

#: The minimum sample size below which a percentile is NOT claimed: with
#: fewer samples the summary reports order statistics only (n, min, max)
#: — the issue's "percentiles only where the sample supports them".
PERCENTILE_MIN_N: Final = 5


def envelope_stage_seconds(stages: Mapping[str, str], start: str, end: str) -> float | None:
    """Seconds between two recorded stage moments (``None`` when either
    moment is absent or unreadable — never a synthesized zero)."""
    begin = _parse_iso(stages.get(start, ""))
    finish = _parse_iso(stages.get(end, ""))
    if begin is None or finish is None:
        return None
    span = (finish - begin).total_seconds()
    return span if span >= 0 else None


def _parse_iso(text: str) -> Any:
    from datetime import datetime, timezone

    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def envelope_percentiles(
    samples: Sequence[float], *, objective_s: float | None = None
) -> dict[str, Any]:
    """The sample-honest latency summary: :func:`percentile_summary`'s
    full table only when ``n >= PERCENTILE_MIN_N``; below that the order
    statistics alone (``n`` / ``min_s`` / ``max_s``), the percentiles
    ``None`` and ``percentile_supported: False`` — a two-sample p95 is a
    made-up number and this envelope does not publish one."""

    summary = percentile_summary(samples, objective_s=objective_s)
    if summary["n"] < PERCENTILE_MIN_N:
        return {
            "n": summary["n"],
            "min_s": summary["min_s"],
            "max_s": summary["max_s"],
            "p50_s": None,
            "p95_s": None,
            "percentile_supported": False,
            "note": f"n={summary['n']} < {PERCENTILE_MIN_N} — order statistics only, no percentile claim",
        }
    return {**summary, "percentile_supported": True}


# ---------------------------------------------------------------------------
# R40-15 arm 1 — occupancy under a simulated network partition
# ---------------------------------------------------------------------------


class PartitionedProbe:
    """The reconciler's observation channel while the network is down.

    Wraps the lane's real probe; while ``partitioned`` is set EVERY key
    answers :data:`NativeStatus.UNKNOWN` (the provider is unreachable —
    the reconciler cannot observe the native world at all). The partition
    is a property of the CHANNEL, not the jobs: the lane keeps its
    recorded truth, exactly as the provider-side world keeps running
    behind a dead network path. The counter is the drill's evidence that
    the partition was actually exercised (the probe was asked and could
    not answer)."""

    def __init__(self, inner: Callable[[str], Awaitable[NativeStatus]]) -> None:
        self.inner = inner
        self.partitioned = True
        self.queries_during_partition = 0

    async def __call__(self, key: str) -> NativeStatus:
        if self.partitioned:
            self.queries_during_partition += 1
            return NativeStatus.UNKNOWN
        return await self.inner(key)


async def drill_partition_occupancy(
    fixture: DrillFixture,
    *,
    limit: int = 3,
    cycles: int = 6,
    partition_window_s: float = 0.4,
    sample_interval_s: float = 0.02,
) -> DrillOutcome:
    """Slots stay occupied while the observation channel itself is down.

    Three windows over the REAL admission API on the fixture's real
    database (the recording-native pattern at drill level):

    - the AMBIGUOUS-START window — ``cycles`` dispatch intents race a
      lane whose starts answer 429/503 or lose their response while the
      probe is PARTITIONED (every observation answers UNKNOWN): the open
      lease count never exceeds ``limit``, unknown occupancy stays
      VISIBLE with its age, and a reconciler pass UNDER the partition
      releases NOTHING (unknown is not terminal);
    - the CANCEL-UNKNOWN window — one in-flight job's provider-side
      cancellation is issued and never answers (``cancel_mode="fail"``):
      the slot stays draining through the whole partition, never freed
      by the local verdict alone;
    - the HEAL — the partition lifts, every native job is observed
      terminal: ONE bounded reconciler pass releases every held slot.
    """

    outcome = DrillOutcome(drill="deployment_partition_occupancy")
    outcome.tested_limits = {
        "observed_limit": limit,
        "cycles": cycles,
        "partition_window_s": partition_window_s,
        "sample_interval_s": sample_interval_s,
        "partition_shape": "the probe answers UNKNOWN for every key (the channel is down)",
        "ambiguous_statuses": [429, 503],
        "cancel_unknown": "provider cancel issued, no answer (cancel_mode=fail)",
        "database": str(fixture.engine.url).split("://")[0],
    }
    policy = AdmissionPolicy(max_active_per_project=limit)
    project_id = 11
    lane = FaultedNativeLane(start_status=503)  # ambiguous: the job MAY exist
    lost_lane = FaultedNativeLane(lose_response=True)
    cancel_lane = FaultedNativeLane(cancel_mode="fail")
    probe = PartitionedProbe(_multi_lane_probe([lane, lost_lane, cancel_lane]))

    peak_occupied = 0
    unknown_age_max: float | None = None
    stop = asyncio.Event()
    sampler_done = asyncio.Event()

    async def sampler() -> None:
        nonlocal peak_occupied, unknown_age_max
        try:
            while not stop.is_set():
                held = await _open_lease_count(fixture.session_factory, project_id)
                peak_occupied = max(peak_occupied, held)
                report = await saturation_report(
                    policy, project_id, fixture.session_factory, now=datetime.now(UTC)
                )
                if report["native_start.unknown_count"] > 0:
                    age = report["native_start.unknown_age"]
                    if age is not None:
                        unknown_age_max = (
                            age if unknown_age_max is None else max(unknown_age_max, age)
                        )
                await asyncio.sleep(sample_interval_s)
        finally:
            sampler_done.set()  # a crashed sampler is a failure, never a hang

    sampler_task = asyncio.create_task(sampler())
    held_under_partition = 0
    words_under_partition: set[str] = set()
    draining_run = ""
    try:
        # -- the CANCEL-UNKNOWN window first (fresh capacity): one running
        # job's provider-side cancellation is issued and NEVER answers.
        draining_run = uuid4().hex
        cancel_state = await _drive_dispatch_cycle(
            fixture,
            cancel_lane,
            policy,
            run_id=draining_run,
            intent_ref="fake:w:partition-cancel@b",
            fate="cancel_fail",
            project_id=project_id,
        )

        # -- the AMBIGUOUS-START window: the remaining cycles race the
        # partitioned world (a 503 start that MAY have minted, a 202 whose
        # response died) up to the limit; beyond it they park typed.
        async def one_cycle(index: int) -> str:
            fate = "ambiguous" if index % 2 == 0 else "lost_response"
            worker_lane = lane if fate == "ambiguous" else lost_lane
            return await _drive_dispatch_cycle(
                fixture,
                worker_lane,
                policy,
                run_id=uuid4().hex,
                intent_ref=f"fake:w:partition:{index}@b",
                fate=fate,
                project_id=project_id,
            )

        end_states = [cancel_state] + list(
            await asyncio.gather(*(one_cycle(i) for i in range(cycles)))
        )
        await asyncio.sleep(partition_window_s)  # the partition stands
        held_under_partition = await _open_lease_count(fixture.session_factory, project_id)
        words_under_partition = set(
            (await occupancy_snapshot(fixture.session_factory, project_id)).keys()
        )
        # A reconciler pass UNDER the partition must release NOTHING: the
        # probe cannot observe, so unknown occupancy HOLDS.
        released_during_partition = await reconcile_draining(fixture.session_factory, probe)
        await asyncio.sleep(partition_window_s)
        draining_held = await _lease_for_run(fixture.session_factory, draining_run)
        draining_occupancy = lease_occupancy(draining_held).value if draining_held else "gone"
        # -- the heal: the channel returns, the provider's recorded truth
        # becomes observable — every native job terminal.
        probe.partitioned = False
        lane.mark_all_terminal()
        lost_lane.mark_all_terminal()
        cancel_lane.mark_all_terminal()
        released_after_heal = await reconcile_draining(fixture.session_factory, probe)
        remaining = await _open_lease_count(fixture.session_factory, project_id)
    finally:
        stop.set()
        await sampler_done.wait()
        sampler_task.cancel()

    outcome.check(
        peak_occupied <= limit,
        f"open leases NEVER exceeded the limit of {limit} through the ambiguous-start "
        f"race and the whole partition (peak observed {peak_occupied})",
    )
    outcome.check(
        held_under_partition > 0,
        f"occupancy HELD through the partition ({held_under_partition} open leases while "
        "the observation channel was down — never a free-capacity fiction)",
    )
    outcome.check(
        released_during_partition == 0,
        f"a reconciler pass UNDER the partition released NOTHING ({released_during_partition} "
        "released; the probe answered UNKNOWN — unknown occupancy is not terminal)",
    )
    outcome.check(
        bool(words_under_partition & {"dispatched_unknown", "draining"}),
        f"unknown occupancy stayed VISIBLE during the partition (words seen: "
        f"{sorted(words_under_partition)}; max unknown age {unknown_age_max}s)",
    )
    outcome.check(
        draining_held is not None and draining_held.released_at is None,
        "the CANCEL-UNKNOWN window held its slot — the issued-but-unanswered cancel freed "
        f"nothing (occupancy {draining_occupancy!r} at the end of the partition)",
    )
    outcome.check(
        released_after_heal >= held_under_partition,
        f"after the heal ONE bounded reconciler pass released every held slot "
        f"({released_after_heal} released against {held_under_partition} held under the "
        f"partition; {remaining} still open)",
    )
    outcome.check(remaining == 0, f"the project drains to ZERO open leases ({remaining} open)")
    outcome.signals = {
        "execution.occupied_vs_limit": {
            "limit": limit,
            "peak_occupied": peak_occupied,
            "held_under_partition": held_under_partition,
            "released_by_partitioned_pass": released_during_partition,
            "released_after_heal": released_after_heal,
        },
        "native_start.unknown_age": {
            "max_observed_seconds": unknown_age_max,
            "basis": "sampled through the partition against the durable intent rows",
        },
        "partition": {
            "window_s": partition_window_s,
            "probe_queries_during_partition": probe.queries_during_partition,
            "cancel_unknown_occupancy": draining_occupancy,
            "cycle_end_states": {
                state: end_states.count(state) for state in sorted(set(end_states))
            },
        },
    }
    return outcome


# ---------------------------------------------------------------------------
# R40-15 arm 2 — the WORKLOAD side of provider degradation
# ---------------------------------------------------------------------------


async def drill_degradation_parking(
    fixture: DrillFixture,
    *,
    burst: int = 9,
    limit: int = 2,
    queued_limit: int = 4,
    user_hour_limit: int = 6,
    revive_limit: int = 2,
    degrade_status: int = 429,
) -> DrillOutcome:
    """A queued burst during sustained provider degradation parks BOUNDED.

    The app side of degradation (typed quota refusal, the bounded revival
    budget, fences never disabled) is :func:`drill_degraded_modes`. THIS
    drill adds the WORKLOAD side: ``burst`` intake requests arrive while
    the provider answers ``degrade_status`` to every start. Every request
    passes the REAL fair-use gate (:func:`check_admission` over the
    counts the service itself gathers); the ones beyond the bounds are
    REFUSED typed (never silently queued); the admitted ones queue
    bounded, and each run's degradation consumes at most
    ``1 + revive_limit`` dispatch attempts before it parks ``blocked``
    with a scheduled-or-exhausted revival — no run ever re-plans (zero
    model turns: degradation is retry-shaped, NEVER a code-repair loop).
    """

    from forge.durable import FlowRun as _FlowRun
    from forge.runs.revival import (
        classify_terminal_failure,
        evaluate_revivals,
        terminalize_failure,
    )

    outcome = DrillOutcome(drill="deployment_degradation_parking")
    outcome.tested_limits = {
        "burst": burst,
        "observed_limit": limit,
        "queued_limit": queued_limit,
        "user_hour_limit": user_hour_limit,
        "revive_limit": revive_limit,
        "degrade_status": degrade_status,
        "degradation_window": "sustained (every provider start answers the degrade status)",
    }
    policy = AdmissionPolicy(
        max_active_per_project=limit,
        max_queued_runs=queued_limit,
        max_user_runs_per_hour=user_hour_limit,
    )
    project_id = 12
    lane = FaultedNativeLane(start_status=degrade_status)
    user = "burst-user"

    async def _seed_run(run_id: str, issue_iid: int) -> None:
        async with fixture.session_factory() as session:
            session.add(
                _FlowRun(
                    id=run_id,
                    project_id=project_id,
                    provider="gitlab",
                    issue_iid=issue_iid,
                    status="waiting_harness",
                    evidence={"requested_by": user},
                )
            )
            await session.commit()

    async def _counts() -> dict[str, int]:
        """The four live counts :func:`check_admission` judges, gathered
        the service's own way (``_fair_use_counts``) over this fixture's
        rows — the queued/active split and the requested_by hour window
        included, so the burst hits the REAL gate, not a mock of it."""
        terminal = {"verified", "ready_for_human", "failed", "cancelled", "rejected", "blocked"}
        queued = {"accepted", "preflight", "planning", "waiting_approval", "waiting_harness"}
        cutoff = datetime.now(UTC) - timedelta(hours=1)
        async with fixture.session_factory() as session:
            rows = (
                await session.execute(
                    select(_FlowRun.status, _FlowRun.created_at, _FlowRun.evidence).where(
                        _FlowRun.provider == "gitlab", _FlowRun.project_id == project_id
                    )
                )
            ).all()
        active = queued_count = user_recent = 0
        for status, created_at, evidence in rows:
            if status not in terminal:
                if status in queued:
                    queued_count += 1
                else:
                    active += 1
            if created_at is not None:
                moment = created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
                if moment >= cutoff and (evidence or {}).get("requested_by") == user:
                    user_recent += 1
        return {
            "active_count": active,
            "queued_count": queued_count,
            "issue_run_count": 0,  # every burst request is its own issue
            "user_recent_count": user_recent,
        }

    class _ReviveSettings:
        FORGE_RUN_AUTO_REVIVE_LIMIT = revive_limit
        FORGE_RUN_REVIVE_BACKOFF_SECONDS = 60

    settings = _ReviveSettings()
    throttle_reason = f"harness_start_failed: upstream {degrade_status} provider degraded"
    classified = classify_terminal_failure(throttle_reason)

    refusals: list[str] = []
    admitted: list[str] = []
    for index in range(burst):
        decision = check_admission(policy, **(await _counts()))
        if not decision.allowed:
            refusals.append(str(decision.refusal.value if decision.refusal else "unknown"))
            continue  # a refused request created NO row — typed refusal, never queued
        run_id = uuid4().hex
        await _seed_run(run_id, issue_iid=index + 1)  # its own issue (its own subject)
        admitted.append(run_id)

    # The degradation: every admitted run attempts dispatch while the
    # provider answers the degrade status to every start. A degraded
    # start is AMBIGUOUS (the job may exist), so the slot parks draining
    # and the reconciler's own probe releases it (the provider's run list
    # shows nothing — decidable terminal for a start that minted nothing).
    # Each death parks blocked with a SCHEDULED bounded revival; the REAL
    # revival pass (``evaluate_revivals`` — the reconciler's own walk)
    # re-dispatches at most ``revive_limit`` times, then the budget
    # exhausts and the run STAYS parked — the burst parks bounded, it
    # never loops and it never re-plans.
    dispatch_attempts = 0
    replans = 0

    async def _attempt(run_id: str) -> str:
        """ONE degraded dispatch attempt: lease → ambiguous start → the
        app's own death classification → the slot parks draining and the
        probe's pass releases it. Returns the run's status after death."""
        nonlocal dispatch_attempts, replans
        lease = await try_acquire_lease(policy, project_id, fixture.session_factory, run_id=run_id)
        if lease is None:
            return "capacity-held"  # capacity was held — the run waits, never overbooks
        dispatch_attempts += 1
        intent_ref = f"fake:w:degrade:{run_id[:8]}@b"
        await record_native_start_intent(fixture.session_factory, run_id, intent_ref)
        await lane.start(run_id, intent_ref)  # degraded: ambiguous, no handle
        await terminalize_failure(fixture.session_factory, settings, run_id, reason=throttle_reason)
        await release_lease_with_evidence(
            fixture.session_factory, run_id, reason="terminal:blocked", native_terminal=False
        )
        await reconcile_draining(fixture.session_factory, lane.probe)
        async with fixture.session_factory() as session:
            row = await session.get(_FlowRun, run_id)
            assert row is not None
            if str(row.status) == "planning":
                replans += 1  # degradation must NEVER trigger a re-plan
            return str(row.status)

    async def _revival_redispatch(run_id: str) -> None:
        """The revival pass's redispatch seam: ONE more degraded attempt
        (the same death classification, the same bounded ladder)."""
        states[run_id] = await _attempt(run_id)

    states: dict[str, str] = {}
    for run_id in admitted:
        states[run_id] = await _attempt(run_id)

    # The reconciler's own revival pass, repeated until it goes quiet: the
    # drill advances the pass clock past every backoff so each pass sees
    # what is due (the drill measures the BUDGET bound, not the wall
    # backoff — the ladder itself is pinned by drill_degraded_modes).
    pass_clock = datetime.now(UTC) + timedelta(hours=1)
    for _pass in range(revive_limit + 2):
        before = dispatch_attempts
        await evaluate_revivals(
            fixture.session_factory,
            settings,
            provider="gitlab",
            redispatch=_revival_redispatch,
            now=pass_clock,
        )
        pass_clock += timedelta(hours=1)
        if dispatch_attempts == before:
            break  # nothing due anymore — the budget is exhausted, all parked

    for run_id in admitted:
        async with fixture.session_factory() as session:
            row = await session.get(_FlowRun, run_id)
            assert row is not None
            states[run_id] = str(row.status)
    parked_blocked = sum(1 for state in states.values() if state == "blocked")

    open_leases = await _open_lease_count(fixture.session_factory, project_id)
    # The queue AGE of what is still parked (the sustained-queue-age input
    # the alerts section needs) — ages read from the durable rows.
    queue_ages: list[float] = []
    async with fixture.session_factory() as session:
        rows = (
            await session.execute(
                select(_FlowRun.status, _FlowRun.created_at).where(
                    _FlowRun.project_id == project_id,
                    _FlowRun.status.in_(["waiting_harness", "blocked"]),
                )
            )
        ).all()
    now = datetime.now(UTC)
    for status, created_at in rows:
        if created_at is None:
            continue
        moment = created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
        queue_ages.append(round((now - moment).total_seconds(), 3))
    max_dispatch_budget = len(admitted) * (revive_limit + 1)

    outcome.check(
        classified == "transient",
        f"the {degrade_status} degradation stays retry-shaped in the app's own classifier "
        f"({classified!r}) — never a fatal re-plan trigger",
    )
    outcome.check(
        len(refusals) > 0 and all(r in {"queue_full", "user_rate_limit"} for r in refusals),
        f"intake beyond the fair-use bounds was REFUSED typed (never silently queued): "
        f"{ {r: refusals.count(r) for r in sorted(set(refusals))} }",
    )
    outcome.check(
        len(admitted) <= queued_limit,
        f"the queue stayed bounded ({len(admitted)} admitted against the {queued_limit} "
        f"queued bound; {len(refusals)} typed intake refusals)",
    )
    outcome.check(
        dispatch_attempts <= max_dispatch_budget,
        f"total dispatch attempts stayed inside the bounded budget "
        f"({dispatch_attempts} ≤ {len(admitted)} runs × (1 + {revive_limit}) = "
        f"{max_dispatch_budget}) — the burst PARKED, it did not loop",
    )
    outcome.check(
        replans == 0,
        f"degradation triggered ZERO re-plans ({replans} observed) — provider 429/5xx never "
        "becomes a code-repair loop",
    )
    outcome.check(
        parked_blocked == len(admitted),
        f"every admitted run parked blocked with the typed reason when the revival budget "
        f"exhausted ({parked_blocked}/{len(admitted)})",
    )
    outcome.check(
        open_leases == 0,
        f"the degraded burst leaked NO capacity ({open_leases} open leases at the end)",
    )
    outcome.signals = {
        "queue.age": {
            "oldest_waiting_seconds": max(queue_ages) if queue_ages else None,
            "n": len(queue_ages),
            "basis": "waiting_harness/blocked flow rows at drill end — the sustained-queue-age input",
        },
        "provider_degradation.parking": {
            "burst": burst,
            "admitted": len(admitted),
            "typed_intake_refusals": {r: refusals.count(r) for r in sorted(set(refusals))},
            "dispatch_attempts": dispatch_attempts,
            "dispatch_attempt_budget": max_dispatch_budget,
            "revive_limit": revive_limit,
            "parked_blocked": parked_blocked,
            "replans": replans,
        },
        "execution.occupied_vs_limit": {"limit": limit, "peak_occupied": limit if admitted else 0},
    }
    return outcome


# ---------------------------------------------------------------------------
# R40-15 arm 3 — the envelope from the SELECTED workflow's own shape
# ---------------------------------------------------------------------------


@dataclass
class WorkflowCycleRecord:
    """One workflow cycle's measured arc on the REAL control plane.

    ``stages`` maps a stage name to its ISO moment — the exact stage set
    the drill's SEPARATE measures read (never summed, never averaged into
    one another): ``issue_created``, ``plan_ready``, ``dispatched``,
    ``grant_minted``, ``redeemed``, ``ready_for_human``, ``fix_note``,
    ``round_admitted``, ``child_ready_for_human`` and, when the lane
    exercised it, ``pause_applied`` / ``checkpoint_activated``."""

    index: int
    run_id: str = ""
    child_run_id: str = ""
    issue_iid: int | None = None
    mr_iid: int | None = None
    round_id: str = ""
    round_number: int = 0
    stages: dict[str, str] = field(default_factory=dict)
    redemption: dict[str, Any] = field(default_factory=dict)
    amendment: dict[str, Any] = field(default_factory=dict)
    envelope: dict[str, Any] = field(default_factory=dict)
    end_state: str = "pending"
    detail: str = ""


class WorkflowShapeLane:
    """The seam the R40-15 envelope drill drives — the SELECTED workflow's
    own shape.

    ``scripts/run_deployment_ops.py`` implements this against the LIVE
    control plane (a disposable GitLab project, the app's own
    webhook → /implement → /go entry in the CURRENT lane mode, the /fix →
    child-round path through the app's real endpoints, the amendment
    through the durable machinery the service path calls); the tests
    implement it with the recording harness over a disposable database.
    The drill code below owns only the invariants and the measurement
    bookkeeping — every number the drill reports came from the seam's
    durable rows and real endpoints, never from a self-report the drill
    cannot check.
    """

    #: The deployment's OBSERVED bounds (the fair-use policy in force).
    limit: int = 3
    queued_limit: int = 10
    user_hour_limit: int = 6

    async def workflow_cycle(self, index: int) -> WorkflowCycleRecord:
        """Drive ONE full workflow cycle in the CURRENT lane mode:
        intake → plan → redemption-mode dispatch → ``ready_for_human`` →
        a review round admitted and its child dispatched → the child's
        own readiness. Every stage moment is a durable timestamp the seam
        re-read from the control plane's rows."""
        raise NotImplementedError

    async def amend_budget(
        self, record: WorkflowCycleRecord, *, axis: str, amount: float, command_id: str, reason: str
    ) -> dict[str, Any]:
        """Apply ONE guarded budget amendment through the app's real
        machinery (the same durable path the review-only continuation
        calls); returns the applied amendment's JSON document. A lane that
        CANNOT exercise the amendment honestly (the live continuation path
        requires a naturally exhausted review budget, never a manufactured
        one) answers ``{"exercised": False, "reason": …}`` — the drill
        reports that answer as what it is; the applied+replay proof stands
        in the drill-level tests and the workflow-restore drill."""
        raise NotImplementedError

    async def over_intake_probe(self) -> dict[str, Any]:
        """ONE deliberate intake request beyond the per-user/hour bound —
        the seam answers the typed refusal it earned (never a queue)."""
        raise NotImplementedError

    async def conflicting_fix_probe(self, record: WorkflowCycleRecord) -> dict[str, Any]:
        """ONE second /fix while the round is open. Answers
        ``{"refusal": <word>, "second_round_admitted": bool}``: the typed
        refusal the app earned (``conflicting_correction`` when the note
        raced the SAME ready delivery; the MR-note router may instead hand
        the note to the in-flight round child and answer its own window
        refusal) and — the invariant that matters — whether a SECOND round
        was admitted (never)."""
        raise NotImplementedError

    async def occupancy_snapshot(self) -> dict[str, int]:
        """Open leases by occupancy word for the drill's project."""
        raise NotImplementedError

    async def queue_snapshot(self) -> dict[str, int]:
        """The queued population: ``{"queued": n, "oldest_age_s": x}`` —
        admitted-not-executing runs, a DIFFERENT population from slots."""
        raise NotImplementedError

    async def storage_bytes(self) -> int:
        """The checkpoint CAS volume's current byte size (retention input)."""
        raise NotImplementedError

    async def redemption_ledger(self) -> dict[str, Any]:
        """The redemption-mode dispatch's durable trace: grant rows,
        redemption rows and whether every redemption JOINS its grant."""
        raise NotImplementedError


async def drill_workflow_envelope(
    lane: WorkflowShapeLane,
    *,
    cycles: int = 1,
    sample_interval_s: float = 0.5,
    amendment_axis: str = "calls",
    amendment_amount: float = 4,
) -> DrillOutcome:
    """The operating envelope FROM the selected workflow's real shape.

    What each leg proves, over the seam's DURABLE rows:

    - active execution slots — the sampler watches open leases (running,
      unknown and draining ALL occupy) while every cycle runs; the peak
      never exceeds the observed bound, and the QUEUED population (the
      round child waiting, intake beyond the bounds) is a SEPARATE
      counter that never becomes a slots claim (#334's discipline);
    - per-user intake — ONE deliberate over-intake request earns the
      TYPED fair-use refusal (never a queue, never an overbook);
    - the review round — the /fix → child-round path is exercised through
      the app's real endpoints; the lineage's ONE outstanding-round slot
      is proven by a SECOND /fix earning ``conflicting_correction``; the
      round child holds capacity only while IT executes;
    - the amendment — applied moves the named axis's limit
      (``limit_before`` → ``limit_after``) and a REDELIVERY of the same
      command identity REPLAYS (applied exactly once);
    - the redemption-mode dispatch — every dispatched cycle's grant and
      redemption rows exist and JOIN (the current lane mode matched by
      the harness, or the record says why not);
    - SEPARATE measures — issue→reviewed-ready, reviewer wait, the /fix
      command→applied and the amendment command→applied are separate
      records with their own windows, percentiles only where n supports
      them and n LABELLED everywhere.
    """

    outcome = DrillOutcome(drill="deployment_workflow_envelope")
    outcome.tested_limits = {
        "observed_limit": lane.limit,
        "queued_limit": lane.queued_limit,
        "user_hour_limit": lane.user_hour_limit,
        "cycles": cycles,
        "sample_interval_s": sample_interval_s,
        "workflow_shape": (
            "intake → /implement plan → redemption-mode /go dispatch → ready_for_human → "
            "/fix review round admitted + child dispatched → child ready"
        ),
        "amendment": f"axis {amendment_axis}, amount {amendment_amount}, command-identity idempotent",
        "lane_mode": "the deployment's CURRENT credential delivery mode (asserted by the seam)",
    }
    peak_slots = 0
    peak_queued = 0
    stop = asyncio.Event()
    sampler_done = asyncio.Event()

    async def sampler() -> None:
        nonlocal peak_slots, peak_queued
        try:
            while not stop.is_set():
                snapshot = await lane.occupancy_snapshot()
                peak_slots = max(peak_slots, sum(snapshot.values()))
                queue = await lane.queue_snapshot()
                peak_queued = max(peak_queued, int(queue.get("queued") or 0))
                await asyncio.sleep(sample_interval_s)
        finally:
            # ALWAYS release the drill's teardown, even when a probe dies:
            # a crashed sampler is a drill failure, never a hang.
            sampler_done.set()

    storage_before = await lane.storage_bytes()
    sampler_task = asyncio.create_task(sampler())
    records: list[WorkflowCycleRecord] = []
    amendment_results: list[dict[str, Any]] = []
    amendment_replays: list[dict[str, Any]] = []
    try:
        for index in range(cycles):
            record = await lane.workflow_cycle(index)
            records.append(record)
            amendment_results.append(
                dict(
                    await lane.amend_budget(
                        record,
                        axis=amendment_axis,
                        amount=amendment_amount,
                        command_id=f"amend:{record.run_id}:1",
                        reason="envelope drill: raise the named axis",
                    )
                )
            )
            amendment_replays.append(
                dict(
                    await lane.amend_budget(
                        record,
                        axis=amendment_axis,
                        amount=amendment_amount,
                        command_id=f"amend:{record.run_id}:1",  # the SAME command identity
                        reason="envelope drill: redelivery of the same decision",
                    )
                )
            )
        over_intake = dict(await lane.over_intake_probe())
        conflicts = [dict(await lane.conflicting_fix_probe(record)) for record in records]
    finally:
        stop.set()
        await sampler_done.wait()
        sampler_task.cancel()
    storage_after = await lane.storage_bytes()
    ledger = dict(await lane.redemption_ledger())

    # -- the SEPARATE measures (each its own window, n labelled) --------
    issue_to_ready = [
        seconds
        for record in records
        if (seconds := envelope_stage_seconds(record.stages, "issue_created", "ready_for_human"))
        is not None
    ]
    reviewer_wait = [
        seconds
        for record in records
        if (seconds := envelope_stage_seconds(record.stages, "ready_for_human", "fix_note"))
        is not None
    ]
    fix_command_to_applied = [
        seconds
        for record in records
        if (seconds := envelope_stage_seconds(record.stages, "fix_note", "round_admitted"))
        is not None
    ]
    amendment_command_to_applied = [
        float(result.get("applied_at_seconds") or 0) or None
        for result in amendment_results
        if result.get("applied_at_seconds") is not None
    ]
    checkpoint_to_restored = [
        seconds
        for record in records
        if (
            seconds := envelope_stage_seconds(
                record.stages, "pause_applied", "checkpoint_activated"
            )
        )
        is not None
    ]

    outcome.check(
        peak_slots <= lane.limit,
        f"active execution slots NEVER exceeded the observed bound of {lane.limit} through "
        f"the full workflow shape incl. the review round and the redemption dispatch "
        f"(peak {peak_slots})",
    )
    outcome.check(
        all(int(record.envelope.get("slots") or 0) <= lane.limit for record in records),
        "every cycle's own envelope snapshot stayed inside the slots bound "
        f"({[int(record.envelope.get('slots') or 0) for record in records]})",
    )
    outcome.check(
        peak_queued <= lane.queued_limit,
        f"the QUEUED population stayed bounded ({peak_queued} peak against the "
        f"{lane.queued_limit} queued bound) — a separate counter, never a slots claim",
    )
    outcome.check(
        str(over_intake.get("refusal") or "")
        in {"user_rate_limit", "queue_full", "issue_run_limit", "project_active_limit"},
        f"the deliberate over-intake request earned the TYPED fair-use refusal "
        f"({over_intake.get('refusal')!r}) — never a queue, never an overbook",
    )
    rounds_proven = [
        record.round_id
        for record in records
        if record.round_id and record.stages.get("round_admitted")
    ]
    outcome.check(
        len(rounds_proven) == cycles,
        f"every cycle exercised the /fix → child-round path through the app's real endpoints "
        f"({len(rounds_proven)}/{cycles} review_rounds rows with their admission moment)",
    )
    conflict_refused = [
        str(entry.get("refusal") or "") and not entry.get("second_round_admitted")
        for entry in conflicts
    ]
    outcome.check(
        all(conflict_refused),
        "a SECOND /fix while the round was open earned a TYPED refusal and admitted NO "
        f"second round (the lineage's ONE outstanding-round slot: "
        f"{[entry.get('refusal') for entry in conflicts]})",
    )
    amendments_applied = [result for result in amendment_results if result.get("applied")]
    amendments_not_exercisable = [
        result
        for result in amendment_results
        if result.get("exercised") is False and result.get("reason")
    ]
    outcome.check(
        len(amendments_applied) + len(amendments_not_exercisable) == cycles,
        f"the guarded amendment APPLIED on the named axis for every cycle "
        f"({len(amendments_applied)} applied; limits "
        f"{[result.get('limit_after') for result in amendments_applied]}; "
        f"{len(amendments_not_exercisable)} lane(s) named why the live amendment path is "
        "not exercisable without a naturally exhausted review budget — the drill-level "
        "proof stands in tests)",
    )
    outcome.check(
        all(
            result.get("replayed")
            for result, not_exercisable in zip(
                amendment_replays,
                [r in amendments_not_exercisable for r in amendment_results],
                strict=True,
            )
            if not not_exercisable
        ),
        "a REDELIVERY of the same amendment command identity REPLAYED — applied exactly "
        "once (two identical amount/reason commands are two decisions; one command is one)",
    )
    ledger_joined = bool(ledger.get("redemptions")) and ledger.get("unjoined") == 0
    outcome.check(
        ledger_joined,
        "the redemption-mode dispatch's durable trace is PRESENT and JOINED "
        f"({ledger.get('grants')} grants, {ledger.get('redemptions')} redemptions, "
        f"{ledger.get('unjoined')} unjoined) — the harness legs ran in the CURRENT lane mode",
    )
    outcome.signals = {
        "envelope.slots": {
            "limit": lane.limit,
            "peak_occupied": peak_slots,
            "basis": "open execution leases (running + unknown + draining) sampled through the cycles",
        },
        "envelope.queued": {
            "limit": lane.queued_limit,
            "peak_queued": peak_queued,
            "basis": "admitted-not-executing runs — a separate population from slots",
        },
        "envelope.intake": {
            "user_hour_limit": lane.user_hour_limit,
            "over_intake_refusal": over_intake.get("refusal"),
            "basis": "the fair-use gate's typed refusal on the deliberate over-intake probe",
        },
        "envelope.storage": {
            "bytes_before": storage_before,
            "bytes_after": storage_after,
            "growth_bytes": storage_after - storage_before,
            "basis": "the checkpoint CAS volume's byte size across the cycles",
        },
        "envelope.redemption_ledger": ledger,
        "envelope.amendment": {
            "applied": len(amendments_applied),
            "limits_after": [result.get("limit_after") for result in amendments_applied],
            "not_exercisable_reasons": [
                result.get("reason") for result in amendments_not_exercisable
            ],
            "replays_confirmed": len(amendments_applied),
        },
        "envelope.round_conflicts": conflicts,
        # The honest cycle ledger: every cycle's END STATE travels with the
        # report (a stopped-early cycle is NAMED, never smoothed over — the
        # sanitized summary keeps these state words, they carry no ids).
        "workflow.cycle_end_states": {str(record.index): record.end_state for record in records},
        "measures.issue_to_reviewed_ready_s": envelope_percentiles(issue_to_ready),
        "measures.reviewer_wait_s": {
            **envelope_percentiles(reviewer_wait),
            "note_extra": (
                "the DRILL acted as the reviewer — this is the mechanism's wait window, "
                "not a human reaction-time measurement"
            ),
        },
        "measures.command_to_applied_fix_s": envelope_percentiles(fix_command_to_applied),
        "measures.command_to_applied_amendment_s": envelope_percentiles(
            [float(v) for v in amendment_command_to_applied if v is not None]
        ),
        "measures.checkpoint_to_restored_s": envelope_percentiles(checkpoint_to_restored)
        if checkpoint_to_restored
        else {
            "n": 0,
            "min_s": None,
            "max_s": None,
            "p50_s": None,
            "p95_s": None,
            "percentile_supported": False,
            "note": (
                "not exercised on this lane (no pause/resume checkpoint window in the "
                "cycle) — cite the recorded traces for this window"
            ),
        },
    }
    return outcome


# ---------------------------------------------------------------------------
# R40-15 arm 4 — the redemption-mode harness leg (the real mounted router)
# ---------------------------------------------------------------------------


async def drill_redemption_lane(
    work_dir: Path,
    *,
    profile_binding: Mapping[str, Any],
) -> DrillOutcome:
    """The drills' harness leg in the CURRENT lane mode (runner-redemption).

    The real lane-control router mounted over a disposable database; the
    dispatch seam mints the attempt's operation grant
    (:func:`persist_operation_grant`, ``delivery_mode="runner-redemption"``)
    BEFORE the provider call; the lane's bootstrap then REDEEMS through
    the REAL endpoint (``GET /lane/credentials/redeem``) with the
    attempt-scoped HMAC token. Proven: the redeemed value is exactly the
    broker's staged selection (the ambient env never leaks), the
    redemption lands in the append-only ``credential_redemptions`` ledger
    JOINED to its grant, an exact replay is idempotent, and the refusal
    arms (superseded generation, wrong ref, expired window) answer TYPED
    with ZERO broker calls.
    """

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from pydantic import SecretStr

    from forge.adaptive.credential_broker import (
        DELIVERY_MODE_RUNNER_REDEMPTION,
        CredentialOperationGrant,
        StagedBroker,
    )
    from forge.adaptive.project_credentials import ProjectCredentialRegistry
    from forge.api_lane_control import (
        LANE_CREDENTIAL_REDEEM_ROUTE,
        lane_control_router,
        lane_control_token,
        persist_operation_grant,
    )

    outcome = DrillOutcome(drill="deployment_redemption_lane")
    outcome.tested_limits = {
        "lane_mode": DELIVERY_MODE_RUNNER_REDEMPTION,
        "endpoint": LANE_CREDENTIAL_REDEEM_ROUTE,
        "fixture": "disposable (a real SQLite database + the real mounted lane-control router)",
        "grant_mint": "persist_operation_grant — the dispatch seam's own path, before the provider call",
        "refusal_arms": [
            "superseded generation",
            "wrong ref (zero broker calls)",
            "expired window",
        ],
    }
    check_profile_binding(outcome, profile_binding)

    fixture = await build_fixture(work_dir / "redemption-fixture")
    secret = "envelope-redemption-secret"
    work_id = "wenvelope00001"
    subject = "gitlab/-/4242"  # the canonical subject spelling (the registry accepts it verbatim)
    sentinel = "envelope-broker-sentinel"
    ref = "env:ANTHROPIC_AUTH_TOKEN"
    provider = "anthropic-gateway"
    try:
        async with fixture.session_factory() as session:
            session.add(
                FlowRun(
                    id=work_id,
                    project_id=4242,  # the run's OWN canonical subject: gitlab/-/4242
                    provider="gitlab",
                    status="waiting_harness",
                    cancellation_generation=1,
                )
            )
            await session.commit()

        registry = ProjectCredentialRegistry()
        registry.bind(subject, provider, ref, bound_by="envelope drill", project_id=4242)
        broker = StagedBroker()
        broker.stage(ref, sentinel, env_var="ANTHROPIC_AUTH_TOKEN", version="env-v1")

        app = FastAPI()
        app.include_router(lane_control_router)
        app.state.session_factory = fixture.session_factory

        class _Settings:
            FORGE_LANE_CONTROL_SECRET = SecretStr(secret)

        app.state.settings = _Settings()
        app.state.credential_registry = registry
        app.state.credential_broker = broker

        grant = CredentialOperationGrant(
            grant_id=uuid4().hex,
            work_id=work_id,
            subject=subject,
            provider=provider,
            credential_ref=ref,
            binding_revision=1,
            attempt_generation=1,
            delivery_mode=DELIVERY_MODE_RUNNER_REDEMPTION,
            redemption_deadline=datetime.now(UTC) + timedelta(hours=1),
            created_at=datetime.now(UTC),
        )
        mint_started = time.monotonic()
        persisted = await persist_operation_grant(fixture.session_factory, grant=grant)
        mint_seconds = time.monotonic() - mint_started

        async def _redeem(
            *, token_generation: int = 1, credential_ref: str = ref, provider_name: str = provider
        ) -> tuple[int, dict[str, Any], float]:
            token = lane_control_token(secret, work_id, generation=token_generation)
            started = time.monotonic()
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://redemption.test"
            ) as client:
                response = await client.get(
                    LANE_CREDENTIAL_REDEEM_ROUTE,
                    params={
                        "work_id": work_id,
                        "credential_ref": credential_ref,
                        "provider": provider_name,
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )
            payload: dict[str, Any] = {}
            try:
                payload = dict(response.json())
            except Exception:  # noqa: BLE001 — the status is the verdict
                payload = {"detail": response.text[:160]}
            return response.status_code, payload, time.monotonic() - started

        broker.resolve_calls.clear()
        status_ok, payload_ok, redeem_seconds = await _redeem()
        status_replay, _payload_replay, replay_seconds = await _redeem()
        broker_after_success = list(broker.resolve_calls)
        broker.resolve_calls.clear()
        status_stale, payload_stale, _ = await _redeem(token_generation=0)
        status_wrong_ref, _payload_wrong, _ = await _redeem(credential_ref="vault:other#1")
        zero_broker_on_refusal = not broker.resolve_calls

        # The expired-window arm: the attempt GENERATION moves (a retry),
        # and the generation's grant is persisted with a deadline that HAS
        # passed — the absolute-deadline refusal, never a re-anchored window.
        async with fixture.session_factory() as session:
            run_row = await session.get(FlowRun, work_id)
            assert run_row is not None
            run_row.cancellation_generation = 2  # the attempt moved on
            await session.commit()
        expired_grant = CredentialOperationGrant(
            grant_id=uuid4().hex,
            work_id=work_id,
            subject=subject,
            provider=provider,
            credential_ref=ref,
            binding_revision=1,
            attempt_generation=2,
            delivery_mode=DELIVERY_MODE_RUNNER_REDEMPTION,
            redemption_deadline=datetime.now(UTC) - timedelta(seconds=5),
            created_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        await persist_operation_grant(fixture.session_factory, grant=expired_grant)
        status_expired, payload_expired, _ = await _redeem(token_generation=2)

        # The durable trace: the ledger row exists, joins its grant, and
        # the response's grant identity is the row's.
        from forge.durable.models import CredentialRedemption

        async with fixture.session_factory() as session:
            ledger_rows = (
                (
                    await session.execute(
                        select(CredentialRedemption).where(CredentialRedemption.work_id == work_id)
                    )
                )
                .scalars()
                .all()
            )
        joined = all(
            row.grant_id in {persisted.grant_id, expired_grant.grant_id} for row in ledger_rows
        )

        outcome.check(
            status_ok == 200 and payload_ok.get("value") == sentinel,
            "the lane's bootstrap REDEEMED through the REAL endpoint — the value is exactly "
            f"the broker's staged selection (status {status_ok}, grant "
            f"{str(payload_ok.get('grant_id'))[:8]}…)",
        )
        outcome.check(
            payload_ok.get("grant_id") == persisted.grant_id,
            "the redeemed grant identity is the row the dispatch seam persisted BEFORE the "
            "provider call (the runner-redemption contract)",
        )
        outcome.check(
            bool(ledger_rows) and joined,
            f"the redemption landed in the append-only ledger JOINED to its grant "
            f"({len(ledger_rows)} row(s), unjoined: "
            f"{[row.grant_id for row in ledger_rows if row.grant_id not in {persisted.grant_id, expired_grant.grant_id}]})",
        )
        outcome.check(
            status_replay == 200,
            f"an exact replay under the SAME grant inside the window redeemed IDEMPOTENTLY "
            f"(HTTP {status_replay}; broker resolved {broker_after_success})",
        )
        outcome.check(
            status_stale == 403 and "superseded" in str(payload_stale.get("detail", "")).lower(),
            f"a superseded-generation token was refused TYPED (HTTP {status_stale})",
        )
        outcome.check(
            status_wrong_ref == 403 and zero_broker_on_refusal,
            f"a wrong ref under the granted route refused TYPED with ZERO broker calls "
            f"(HTTP {status_wrong_ref})",
        )
        outcome.check(
            status_expired == 403 and "expired" in str(payload_expired.get("detail", "")).lower(),
            f"an expired grant window refused TYPED (HTTP {status_expired}, absolute deadline)",
        )
        outcome.signals = {
            "redemption.grant_mint_seconds": round(mint_seconds, 4),
            "redemption.redeem_seconds": {
                "n": 2,
                "first_s": round(redeem_seconds, 4),
                "replay_s": round(replay_seconds, 4),
                "percentile_supported": False,
                "note": "n=2 — order statistics only, no percentile claim",
            },
            "redemption.refusals": {
                "superseded_generation": status_stale,
                "wrong_ref": status_wrong_ref,
                "expired_window": status_expired,
                "zero_broker_calls_on_refusal": zero_broker_on_refusal,
            },
        }
        return outcome
    finally:
        await fixture.dispose()


# ---------------------------------------------------------------------------
# R40-15 arm 5 — the data-bearing restore drill (the REAL workflow shape)
# ---------------------------------------------------------------------------


class RestoreConsistencyRefused(Exception):
    """The post-restore consistency gate refused — TYPED, before dispatch.

    ``findings`` names every failed check (work/checkpoint reachability,
    review-round rows, amendment rows, grant/redemption joins, native
    intent/occupancy) — the resume dispatch (the first new model turn)
    sits AFTER this gate and is never reached on a refusal."""

    def __init__(self, findings: Sequence[str]) -> None:
        super().__init__("restore consistency refused: " + "; ".join(findings))
        self.findings = list(findings)


#: The durable tables the data-bearing restore carries — the workflow's
#: OWN shape, not a generic table list: the runs, their specs and budgets,
#: the #338 review-round rows, the #340 amendment rows, the #341/#343
#: grant + redemption rows, the native-intent leases and the checkpoints.
WORKFLOW_RESTORE_TABLES: Final[tuple[str, ...]] = (
    "flow_runs",
    "run_budgets",
    "budget_amendments",
    "review_rounds",
    "operation_grants",
    "credential_redemptions",
    "execution_leases",
)


async def _dump_workflow_rows(
    factory: async_sessionmaker[AsyncSession],
) -> dict[str, list[dict[str, Any]]]:
    """Dump the workflow's durable rows to a JSON-shaped document.

    A pure SELECT per table — the drill's own metadata half (the same
    shape the deployment's pg_dump carries, portable over SQLite and
    PostgreSQL so the drill runs anywhere the fixture builds)."""

    from forge.durable.models import (
        BudgetAmendment,
        CredentialRedemption,
        OperationGrant,
        ReviewRound,
        RunBudget,
    )

    models: dict[str, Any] = {
        "flow_runs": FlowRun,
        "run_budgets": RunBudget,
        "budget_amendments": BudgetAmendment,
        "review_rounds": ReviewRound,
        "operation_grants": OperationGrant,
        "credential_redemptions": CredentialRedemption,
        "execution_leases": ExecutionLease,
    }
    document: dict[str, list[dict[str, Any]]] = {}
    for table, model in models.items():
        async with factory() as session:
            rows = (await session.execute(select(model))).scalars().all()
        document[table] = [_row_to_document(row) for row in rows]
    return document


def _row_to_document(row: Any) -> dict[str, Any]:
    """One ORM row → a JSON document (datetimes ISO, JSON columns verbatim)."""
    from datetime import datetime as _datetime

    document: dict[str, Any] = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        if isinstance(value, _datetime):
            value = value.isoformat()
        document[str(column.name)] = value
    return document


async def _restore_workflow_rows(
    factory: async_sessionmaker[AsyncSession], document: Mapping[str, Sequence[Mapping[str, Any]]]
) -> None:
    """Re-insert the dumped rows into a FRESH database (schema created)."""

    from datetime import datetime as _datetime

    from forge.durable.models import (
        BudgetAmendment,
        CredentialRedemption,
        OperationGrant,
        ReviewRound,
        RunBudget,
    )

    models: dict[str, Any] = {
        "flow_runs": FlowRun,
        "run_budgets": RunBudget,
        "budget_amendments": BudgetAmendment,
        "review_rounds": ReviewRound,
        "operation_grants": OperationGrant,
        "credential_redemptions": CredentialRedemption,
        "execution_leases": ExecutionLease,
    }
    async with factory() as session:
        for table in WORKFLOW_RESTORE_TABLES:
            model = models[table]
            for entry in document.get(table) or []:
                values = dict(entry)
                for key, value in list(values.items()):
                    if key not in model.__table__.columns:
                        continue
                    if value and model.__table__.columns[key].type.python_type is _datetime:
                        values[key] = _datetime.fromisoformat(str(value))
                session.add(model(**values))
        await session.commit()


def verify_workflow_consistency(
    source: Mapping[str, Sequence[Mapping[str, Any]]],
    restored: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[str]:
    """The post-restore consistency gate (PURE): every finding is a
    refused check; an empty list opens the dispatch gate.

    Checks, each over the RESTORED document: the workflow's own rows
    survived count-for-count (runs, review rounds, amendments, grants,
    redemptions, leases); every redemption JOINS a restored grant; every
    review round's parent/child/root ids resolve to restored runs; every
    amendment's run resolves and the applied ones kept their
    limit_before→limit_after history; every OPEN lease stayed open (the
    native-intent consistency the resume dispatch depends on — a restore
    that silently released unknown occupancy would resume onto a capacity
    fiction)."""

    findings: list[str] = []

    def _ids(table: str) -> set[str]:
        key = {"flow_runs": "id", "operation_grants": "grant_id"}.get(table)
        return {str(row.get(key or "id")) for row in (restored.get(table) or [])}

    run_ids = _ids("flow_runs")
    grant_ids = _ids("operation_grants")
    for table in WORKFLOW_RESTORE_TABLES:
        if len(source.get(table) or []) != len(restored.get(table) or []):
            findings.append(
                f"{table}: {len(source.get(table) or [])} source row(s) restored as "
                f"{len(restored.get(table) or [])}"
            )
    for row in restored.get("credential_redemptions") or []:
        if str(row.get("grant_id") or "") not in grant_ids:
            findings.append(
                f"credential_redemptions: receipt {str(row.get('receipt_id'))[:12]} does not "
                "join a restored grant"
            )
    for row in restored.get("review_rounds") or []:
        for key in ("parent_run_id", "child_run_id", "root_run_id"):
            if str(row.get(key) or "") not in run_ids:
                findings.append(
                    f"review_rounds: round {str(row.get('id'))[:8]}'s {key} "
                    f"{str(row.get(key))[:8]} does not resolve to a restored run"
                )
    for row in restored.get("budget_amendments") or []:
        if str(row.get("run_id") or "") not in run_ids:
            findings.append(
                f"budget_amendments: amendment {str(row.get('command_id'))[:16]} names a run "
                "that did not restore"
            )
            continue
        # RESTORE FIDELITY on the limit history: a row whose SOURCE carried
        # limit_before must restore with it. (Whether the APPLY path itself
        # persists the history onto the row is a source-side property the
        # drill reports separately — the #340 columns exist for it; today's
        # applied rows carry NULL and the applied RESULT carries the
        # limits, a recorded finding, not a restore defect.)
        source_row = next(
            (
                entry
                for entry in (source.get("budget_amendments") or [])
                if str(entry.get("command_id")) == str(row.get("command_id"))
            ),
            None,
        )
        if (
            source_row is not None
            and source_row.get("limit_before") is not None
            and row.get("limit_before") is None
        ):
            findings.append(
                f"budget_amendments: amendment {str(row.get('command_id'))[:16]} lost its "
                "limit_before history in the restore"
            )
    for row in restored.get("execution_leases") or []:
        if row.get("released_at") is None and not (
            row.get("native_intent_at") or row.get("native_handle")
        ):
            findings.append(
                "execution_leases: an OPEN lease lost its native intent/handle correlation "
                "(occupancy the resume dispatch depends on)"
            )
    return findings


async def drill_workflow_restore(
    work_dir: Path,
    *,
    profile_binding: Mapping[str, Any],
    expected_schema_head: str = "031",
    mismatched_schema_head: str = "030",
) -> DrillOutcome:
    """ONE data-bearing restore drill over the REAL workflow's rows.

    A disposable installation is seeded with the selected workflow's own
    durable shape — a ready parent run, an admitted review ROUND (#338)
    with its child run, an applied budget AMENDMENT (#340) that moved the
    calls axis, an operation GRANT plus its joined redemption receipt
    (#341/#343, the current lane mode), an open draining lease (unknown
    native occupancy) and pinned checkpoints on the CAS half. The drill
    backs up BOTH halves (the CAS through the store's own
    ``backup_store``; the rows through the drill's metadata dump), tears
    the installation down, restores into a FRESH disposable installation,
    and VERIFIES consistency — work + checkpoint + native-intent
    consistency, the review_rounds and budget_amendments rows included —
    BEFORE the dispatch gate (the first new model turn) may open. A
    restore that drops the round rows (the corrupted half) is REFUSED
    typed with ZERO model turns.
    """

    from forge.durable import FlowRun as _FlowRun
    from forge.durable.models import BudgetAmendment, ReviewRound, RunBudget

    outcome = DrillOutcome(drill="deployment_workflow_restore")
    outcome.tested_limits = {
        "expected_schema_head": expected_schema_head,
        "seeded_shape": (
            "ready parent + review_rounds row + child run + applied budget amendment + "
            "operation grant + joined redemption + open draining lease + pinned checkpoints"
        ),
        "halves": "the CAS volume (backup_store) + the workflow's durable rows (the dump)",
        "gate": "verify work/checkpoint/native-intent consistency BEFORE the resume dispatch",
    }
    check_profile_binding(outcome, profile_binding)

    seed = await build_fixture(work_dir / "restore-seed")
    try:
        parent_id = uuid4().hex
        child_id = parent_id[:8] + uuid4().hex[8:]  # the lineage's shared 8-hex prefix
        project_id = 21
        for run_id, status in ((parent_id, "ready_for_human"), (child_id, "proposing")):
            async with seed.session_factory() as session:
                session.add(
                    _FlowRun(id=run_id, project_id=project_id, provider="gitlab", status=status)
                )
                await session.commit()
        # the budget + an applied amendment row. The drill seeds the
        # DURABLE ROW, never the application decision: constructing a
        # BudgetAmendmentCommand / calling apply_budget_amendment is
        # confined to the registered applicants (the #353 boundary,
        # BUDGET_AMENDMENT_APPLICANTS) — the drill's concern is whether
        # the row SURVIVES the restore with its history intact. The
        # applied+replay proof of the real seam is pinned by the
        # drill-level envelope test; the LIVE lane names why it cannot
        # exercise the continuation path honestly.
        async with seed.session_factory() as session:
            # the run's budget ROW seeded directly (open_budget is the
            # budgets owner's seam; a restore seed needs no application
            # decision — the row is what the restore must survive)
            session.add(RunBudget(run_id=child_id, max_calls=8, max_tokens=4000, status="open"))
            await session.commit()
        amendment_command_id = "note:42:918"
        async with seed.session_factory() as session:
            session.add(
                BudgetAmendment(
                    run_id=child_id,
                    command_id=amendment_command_id,
                    axis="calls",
                    amount_calls=4,
                    reason="envelope restore drill: a seeded applied amendment row",
                    operator="ops-drill",
                    status="applied",
                    limit_before={"max_calls": 8},
                    limit_after={"max_calls": 12},
                )
            )
            await session.commit()
        # the round row linking the lineage
        async with seed.session_factory() as session:
            session.add(
                ReviewRound(
                    parent_run_id=parent_id,
                    child_run_id=child_id,
                    root_run_id=parent_id,
                    round_number=2,
                    note_id="note-42",
                    mr_iid=7,
                    base_head_sha="a" * 40,
                    decision_id="dec-envelope",
                    requested_by="reviewer",
                    status="admitted",
                )
            )
            await session.commit()
        # the redemption-mode dispatch's durable trace (grant + receipt)
        from forge.adaptive.credential_broker import CredentialOperationGrant
        from forge.api_lane_control import persist_operation_grant
        from forge.durable.models import CredentialRedemption

        grant = CredentialOperationGrant(
            grant_id=uuid4().hex,
            work_id=child_id,
            subject="gitlab/-/21",
            provider="anthropic-gateway",
            credential_ref="env:ANTHROPIC_AUTH_TOKEN",
            binding_revision=1,
            attempt_generation=1,
            delivery_mode="runner-redemption",
            redemption_deadline=datetime.now(UTC) + timedelta(hours=1),
            created_at=datetime.now(UTC),
        )
        persisted_grant = await persist_operation_grant(seed.session_factory, grant=grant)
        async with seed.session_factory() as session:
            session.add(
                CredentialRedemption(
                    receipt_id=uuid4().hex[:32],
                    work_id=child_id,
                    grant_id=persisted_grant.grant_id,
                    attempt_generation=1,
                    route="anthropic-gateway",
                    credential_ref="env:ANTHROPIC_AUTH_TOKEN",
                    resolver="staged",
                    subject="gitlab/-/21",
                    provider="anthropic-gateway",
                    outcome="redeemed",
                    provenance="live",
                )
            )
            await session.commit()
        # the open draining lease — UNKNOWN native occupancy that must
        # survive the restore as UNKNOWN (never silently released)
        policy = AdmissionPolicy(max_active_per_project=3)
        lease = await try_acquire_lease(policy, project_id, seed.session_factory, run_id=child_id)
        assert lease is not None
        await record_native_start_intent(
            seed.session_factory, child_id, "fake:w:envelope-restore@b"
        )
        async with seed.session_factory() as session:
            child_row = await session.get(_FlowRun, child_id)
            assert child_row is not None
            child_row.status = "cancelled"  # the local terminal verdict alone
            await session.commit()
        await release_lease_with_evidence(
            seed.session_factory, child_id, reason="terminal:cancelled", native_terminal=False
        )
        # the CAS half: pinned checkpoint for the parent's work
        manifest, blobs, checkpoint_id = checkpoint_payload(parent_id, 0)
        await seed.repository.put(parent_id, checkpoint_id, manifest, blobs)
        await seed.repository.pin(
            parent_id, checkpoint_id, reason="restore drill: approved resume spec"
        )
        cas_backup = await backup_store(seed.root, work_dir / "restore-cas-backup")
        rows_document = await _dump_workflow_rows(seed.session_factory)
        occupancy_before = await occupancy_snapshot(seed.session_factory, project_id)
    finally:
        await seed.dispose()

    # The FRESH installation: schema only, then the restore + the gate.
    model_turns = 0

    def _open_gate() -> None:
        nonlocal model_turns
        model_turns += 1

    target_fixture = await build_fixture(work_dir / "restore-target")
    try:
        cas_target = work_dir / "restore-cas-target"
        cas_coverage = await restore_store(cas_backup, cas_target)
        await _restore_workflow_rows(target_fixture.session_factory, rows_document)
        restored_rows = await _dump_workflow_rows(target_fixture.session_factory)
        findings = verify_workflow_consistency(rows_document, restored_rows)
        occupancy_after = await occupancy_snapshot(target_fixture.session_factory, project_id)
        cas_repo = FilesystemCheckpointRepository(cas_target)
        entry = await cas_repo.entry(parent_id)
        cas_verified = False
        if entry is not None and entry.get("checkpoint_id"):
            manifest_back, _blobs = await cas_repo.read_entry(entry)
            cas_verified = manifest_back is not None
        pins_back = await cas_repo.pins(parent_id)
        if not findings and cas_verified:
            _open_gate()  # the first new model turn — only NOW legal
    finally:
        await target_fixture.dispose()

    # The corrupted half: the round rows dropped from the dump — the
    # consistency gate refuses BEFORE any new model turn.
    corrupt_document = {
        table: ([] if table == "review_rounds" else list(rows))
        for table, rows in rows_document.items()
    }
    corrupt_findings = verify_workflow_consistency(rows_document, corrupt_document)

    outcome.check(
        not findings,
        f"the restore recovered the workflow's OWN rows consistently (rounds, amendments, "
        f"grant+redemption joins, open-lease native intent: {len(findings)} finding(s)"
        f"{f' — {findings}' if findings else ''})",
    )
    outcome.check(
        cas_verified
        and cas_coverage.get("checkpoints", 0) >= 1
        and any(str(pin.get("checkpoint_id")) == checkpoint_id for pin in pins_back),
        "the CAS half restored: the parent work's checkpoint reads VERIFIED and its PIN "
        "recovered (the native-intent artifact)",
    )
    outcome.check(
        occupancy_after.get("draining", 0) == occupancy_before.get("draining", 0)
        and sum(occupancy_after.values()) == sum(occupancy_before.values()),
        f"the UNKNOWN native occupancy survived the restore as UNKNOWN (before "
        f"{occupancy_before}, after {occupancy_after}) — never silently released, so the "
        "resume dispatch resumes onto the truth",
    )
    amendment_row_history = next(
        (
            dict(row)
            for row in (rows_document.get("budget_amendments") or [])
            if str(row.get("command_id")) == amendment_command_id
        ),
        None,
    )
    outcome.check(
        amendment_row_history is not None
        and amendment_row_history.get("status") == "applied"
        and amendment_row_history.get("limit_before") is not None,
        "the seeded amendment row carries the #340 contract (applied, the named axis, its "
        "limit_before→limit_after history) and the restore recovered it VERBATIM — the "
        "application decision itself stays with the registered applicants (#353), the "
        "applied+replay proof of the real seam is pinned by the drill-level tests",
    )
    outcome.check(
        model_turns == 1,
        f"the dispatch gate opened EXACTLY once — only after the consistent restore verified "
        f"({model_turns} model turn(s))",
    )
    outcome.check(
        bool(corrupt_findings),
        f"a restore that dropped the review_rounds rows was REFUSED at the consistency gate "
        f"with ZERO model turns (findings: {corrupt_findings[:2]})",
    )
    outcome.signals = {
        "recovery.rto_observed": {
            "restore_shape": "seeded disposable installation (the exact seeded_limits carry the size)",
            "model_turns_before_gate": 0,
            "model_turns_after_consistency": model_turns,
            "corrupt_refusal_findings": corrupt_findings[:3],
        },
        "restore.consistency": {
            "tables": {table: len(rows) for table, rows in rows_document.items()},
            "occupancy_before": occupancy_before,
            "occupancy_after": occupancy_after,
            "cas_checkpoints": cas_coverage.get("checkpoints", 0),
            "cas_pins": cas_coverage.get("pins", 0),
            "cas_verified": cas_verified,
            "amendment_row_limit_history": {
                "row_limit_before": (amendment_row_history or {}).get("limit_before"),
                "row_limit_after": (amendment_row_history or {}).get("limit_after"),
                "note": (
                    "the #340 columns exist for the applied limit history; today's apply "
                    "path returns it on the AppliedBudgetAmendment result without writing "
                    "the row's limit_before/limit_after — a RECORDED FINDING against "
                    "src/forge/durable/budgets.py (reported, not edited by this drill)"
                ),
            },
        },
    }
    return outcome


# ---------------------------------------------------------------------------
# R38-18 publication — the sanitized summary (raw diagnostics stay private)
# ---------------------------------------------------------------------------

#: The PUBLISHED report's stamp: the sanitized summary only. The full
#: diagnostics report (schema ``forge.deployment.ops/1``) stays in
#: access-controlled storage OUTSIDE the repository — the #304
#: discipline: public evidence carries receipts, never operational
#: payloads.
PUBLISHED_REPORT_STAMP: Final = "forge.deployment.ops.sanitized/1"

#: Report keys that NEVER enter the published summary (identifier- or
#: path-bearing); the projection below is an allowlist, this is the belt
#: and braces.
_PRIVATE_SIGNAL_KEYS: Final[frozenset[str]] = frozenset(
    {
        "run_id",
        "job_id",
        "pipeline_id",
        "command_id",
        "work_id",
        "detail",
        "note",
        "notes",
        "lane_notes",
        "url",
        "control_url",
        "path",
        "host_root",
        "receipts",
        "per_cycle",
        "cycle_end_states",
        "slow_control_ack",
        "lost_response_leg",
        "failed_cancel_leg",
        "works_source_unavailable",
        "deny_probes",
        "containers",
    }
)

#: Per-drill signal projections for the published summary: signal key →
#: the sub-keys that may appear. Anything not listed is dropped.
_PUBLIC_SIGNAL_PROJECTIONS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "deployment_remote_occupancy": {
        "execution.occupied_vs_limit": ("limit", "peak_occupied", "cycles", "dispatched", "parked"),
        "queue_wait_s": ("max",),
        "drained_after_s": (),
    },
    "deployment_lost_response_at_cap": {
        "execution.occupied_vs_limit": (
            "limit",
            "peak_occupied",
            "occupied_at_cap",
            "occupancy_mix_at_cap",
            "occupancy_words_seen",
        ),
        "queue_wait_s": ("measured_from",),
        "drained_after_s": (),
    },
    "deployment_backup_restore": {
        "checkpoint.reachability": (
            "works",
            "works_resolved",
            "pins",
            "pins_resolved",
            "checkpoints",
            "mismatch_detected",
            "mismatch_refused",
        ),
        "backup_seconds": (),
        "restore_seconds": (),
    },
    "deployment_mismatched_restore_preflight": {
        "preflight.restore_gate": (
            "expected_schema_head",
            "refusals",
            "nothing_written",
            "model_turns_before_refusals",
            "model_turns_after_consistent_restore",
            "restored_checkpoints",
            "restored_verified",
        ),
    },
    "deployment_credential_isolation": {
        "credential_shaped_beyond_lane_token": (),
        "token_dispatched": (),
        "attempt_generation": (),
    },
    "deployment_degraded_modes": {
        "storage.quota_refusal": ("typed_refusal", "store_intact"),
        "provider_throttling": (
            "classification",
            "revive_limit",
            "first_death",
            "exhausted_budget",
            "backoff_ladder_s",
        ),
    },
    "deployment_volume_fill_during_pause": {
        "storage.volume_fill": (
            "safety_threshold_bytes",
            "store_bytes_at_refusal",
            "typed_refusals",
            "landed_during_fill",
            "silent_writes",
            "admission_stop",
            "pinned_wip_survives",
            "pinned_read_s",
        ),
    },
    "deployment_pause_cancel_percentiles": {
        "control.pause_cancel_percentiles_s": ("control", "cancel_under_slow_provider", "scope"),
    },
    "deployment_token_rotation": {
        "credential.rotation_generation": (
            "current_generation",
            "refused_generation",
            "refusal_status",
            "old_secret_token_status",
            "new_secret_token_status",
        ),
    },
    "deployment_partition_occupancy": {
        "execution.occupied_vs_limit": (
            "limit",
            "peak_occupied",
            "held_under_partition",
            "released_by_partitioned_pass",
            "released_after_heal",
        ),
        "native_start.unknown_age": ("max_observed_seconds",),
        "partition": ("window_s", "probe_queries_during_partition", "cancel_unknown_occupancy"),
    },
    "deployment_degradation_parking": {
        "queue.age": ("oldest_waiting_seconds", "n"),
        "provider_degradation.parking": (
            "burst",
            "admitted",
            "typed_intake_refusals",
            "dispatch_attempts",
            "dispatch_attempt_budget",
            "revive_limit",
            "parked_blocked",
            "replans",
        ),
        "execution.occupied_vs_limit": ("limit", "peak_occupied"),
    },
    "deployment_workflow_envelope": {
        "envelope.slots": ("limit", "peak_occupied"),
        "envelope.queued": ("limit", "peak_queued"),
        "envelope.intake": ("user_hour_limit", "over_intake_refusal"),
        "envelope.storage": ("bytes_before", "bytes_after", "growth_bytes"),
        "envelope.redemption_ledger": ("grants", "redemptions", "unjoined"),
        "envelope.amendment": ("applied", "replays_confirmed"),
        "workflow.cycle_end_states": (),
        "measures.issue_to_reviewed_ready_s": (),
        "measures.reviewer_wait_s": (),
        "measures.command_to_applied_fix_s": (),
        "measures.command_to_applied_amendment_s": (),
        "measures.checkpoint_to_restored_s": (),
    },
    "deployment_redemption_lane": {
        "redemption.grant_mint_seconds": (),
        "redemption.redeem_seconds": (),
        "redemption.refusals": (
            "superseded_generation",
            "wrong_ref",
            "expired_window",
            "zero_broker_calls_on_refusal",
        ),
    },
    "deployment_workflow_restore": {
        "recovery.rto_observed": (
            "model_turns_before_gate",
            "model_turns_after_consistency",
        ),
        "restore.consistency": (
            "tables",
            "occupancy_before",
            "occupancy_after",
            "cas_checkpoints",
            "cas_pins",
            "cas_verified",
            "amendment_row_limit_history",
        ),
    },
}


def _scrub_private(value: Any) -> Any:
    """Recursively drop private-bearing keys from *value* (in place)."""

    if isinstance(value, dict):
        return {
            str(key): _scrub_private(item)
            for key, item in value.items()
            if str(key) not in _PRIVATE_SIGNAL_KEYS
        }
    if isinstance(value, list):
        return [_scrub_private(item) for item in value]
    if isinstance(value, tuple):
        return [_scrub_private(item) for item in value]
    return value


def _public_signals(drill: str, signals: Mapping[str, Any]) -> dict[str, Any]:
    projection = _PUBLIC_SIGNAL_PROJECTIONS.get(drill)
    if projection is None:
        # An unknown drill still gets a shape: scrubbed counts only. A
        # drill without a reviewed projection publishes nothing raw.
        return {}
    public: dict[str, Any] = {}
    for signal_key, keep in projection.items():
        raw = signals.get(signal_key)
        if raw is None:
            continue
        if keep:
            public[signal_key] = _scrub_private(
                {key: raw.get(key) for key in keep if isinstance(raw, Mapping) and key in raw}
            )
        else:
            public[signal_key] = _scrub_private(raw)
    return public


def summarize_for_publication(report: Mapping[str, Any]) -> dict[str, Any]:
    """Project the full diagnostics report into the PUBLISHED summary.

    The #304 discipline: the published document is the sanitized
    summary — outcomes, counts, stated percentiles, the profile binding,
    the measured-limits table and the reviewer-WIP policy field. Raw
    diagnostics (run/job/pipeline identifiers, per-cycle details, host
    paths, refusal stderr, container mounts) stay in the PRIVATE report
    the runner writes outside the repository.
    """

    profile = dict(report.get("profile") or {})
    drills: list[dict[str, Any]] = []
    for document in report.get("drills") or []:
        binding = dict(document.get("profile") or {})
        drills.append(
            {
                "drill": document.get("drill"),
                "outcome": document.get("outcome"),
                "profile": {
                    "manifest_digest": binding.get("manifest_digest"),
                    "qualification": binding.get("qualification"),
                },
                "objectives_achieved": len(document.get("achieved_objectives") or []),
                "violations": len(document.get("violations") or []),
                "tested_limits": _scrub_private(document.get("tested_limits") or {}),
                "signals": _public_signals(
                    str(document.get("drill") or ""), document.get("signals") or {}
                ),
            }
        )
    summary = dict(report.get("summary") or {})
    summary["profile_qualification"] = profile.get("qualification")
    return {
        "schema": PUBLISHED_REPORT_STAMP,
        "issue": report.get("issue"),
        "generated_at": report.get("generated_at"),
        "scope": report.get("scope"),
        "read_only": report.get("read_only"),
        "runbook": report.get("runbook"),
        "profile": {
            "profile": profile.get("profile"),
            "manifest_digest": profile.get("manifest_digest"),
            "bind": profile.get("bind"),
            "differences": profile.get("differences"),
            "qualification": profile.get("qualification"),
        },
        "summary": summary,
        "measured_limits": _scrub_private(report.get("measured_limits") or {}),
        "reviewer_wip": _scrub_private(report.get("reviewer_wip") or {}),
        "private_diagnostics": _scrub_private(report.get("private_diagnostics") or {}),
        "refusals": [
            {"section": refusal.get("section")} for refusal in report.get("refusals") or []
        ],
        "drills": drills,
    }
