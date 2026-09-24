"""One configured checkpoint repository across upload, resume and operations.

Q35-03 (review ``c7ae8db``): the HTTP upload route honored the
:class:`~forge.api_checkpoint_channel.DurabilityContract` — with
``FORGE_CHECKPOINT_DURABILITY=postgres`` the checkpoint INDEX lived in
``checkpoint_metadata`` (alembic 026) — while the resume producer
(:meth:`forge.adaptive.wiring.OperatorControlService._active_resume_spec`)
built the SYNCHRONOUS filesystem JSON-index reader with no contract and
no session factory. In postgres mode a checkpoint was visible to the
API and invisible to a fresh resume producer: upload and resume read
DIFFERENT authorities, and a control-process restart made ``/resume``
refuse work the API had just confirmed.

This module is the single seam both sides share. One
:class:`CheckpointRepository` protocol — active-entry lookup, verified
put, verified read, and the :meth:`~CheckpointRepository.authority`
name — implemented twice over the SAME storage discipline the channel
already runs (the implementations WRAP
:class:`forge.api_checkpoint_channel.CheckpointStore`; there is no
second copy of the store's logic):

- :class:`FilesystemCheckpointRepository` — authority ``filesystem``:
  the per-work JSON index under ``flock`` (the ``best_effort``
  contract), with the sync store calls run off the event loop;
- :class:`PostgresCheckpointRepository` — authority ``postgres``: the
  ``checkpoint_metadata`` table through the store's transactional
  ``a*`` operations, with a database outage surfaced as the TYPED
  :class:`CheckpointRepositoryUnavailable` — never as "no checkpoint"
  and never as a filesystem fallback.

:func:`resolve_repository` is the ONE composition point: it reads
``FORGE_CHECKPOINT_DURABILITY`` and ``FORGE_CHECKPOINT_STORE_DIR`` (or
an injected storage root) plus the session factory (strictly the
caller's — the app passes ``app.state.session_factory``, the control
service passes the same factory it derives for its mailbox) and refuses
half-configurations AT CONSTRUCTION with
:class:`CheckpointRepositoryMisconfigured` — a typo must never
silently downgrade the durability the operator believes she has, and a
postgres selection without the database wiring must never fall back to
the filesystem index the deployment moved away from.

The typed read states are preserved and disjoint:

- absent — ``entry``/``read`` answer ``None``: the configured authority
  holds nothing for the work;
- corrupt — :class:`forge.api_checkpoint_channel.CheckpointCorruptError`
  propagates from the verified read: stored bytes no longer hash to
  their address are never served as if they were the checkpoint;
- unavailable — :class:`CheckpointRepositoryUnavailable`: the
  configured authority could not be reached (database outage); a
  RECOVERABLE condition, distinct from absence, never answered by
  consulting a different store.

No JSON mirror writes exist here in either mode: a mirror would be a
second authority whose active pointer can drift from the configured
one — exactly the defect this module removes.

Q35-05 (review ``c7ae8db``, probe P03) adds the garbage-collection
safety layer BOTH authorities share, in this module:

- **Pins** (:class:`CheckpointPins`) — an explicit protection overlay
  for approved consumers. An authorized resume PINS its exact
  checkpoint (``pin``); the pinned row AND its blobs survive newer
  checkpoints and every retention pass until the pin is released
  EXPLICITLY (``unpin``) — never by time alone. The overlay lives at
  ``<root>/pins/<work_id>.json`` under the SAME per-work ``flock`` the
  filesystem index uses (``<root>/works/<work_id>.lock``), so a pin
  can never land inside a retention critical section: it is a
  code-level structure on the blob volume (which every contract
  already requires to be shared — the CAS blobs live there), NOT a
  second checkpoint index, and it needs no schema migration.
- **The pending-GC journal** (:class:`CheckpointGcJournal`) — the
  postgres authority's counterpart of the filesystem index's
  ``pending_gc`` record, persisted at ``<root>/gc/<work_id>.json``:
  written BEFORE the deletion commits, cleared AFTER the unlink sweep,
  and completed by the next retention pass against CURRENT
  reachability. A collector killed anywhere in its two-phase window
  converges; nothing still referenced is ever unlinked by the recovery.
- **The two-phase contract itself** (mark → recheck → sweep) lives in
  :mod:`forge.api_checkpoint_channel` — the store owns the locks and
  transactions the phases must run inside — but the protection overlay
  both phases consult is the shared logic here.

R36-04 (review ``16339c2``, probe P04) adds the synchronization
protocol the recheck above could not replace: Q35-05's SELECT only
sees references committed BEFORE it ran, so a DIFFERENT work's landing
(per-work locks never conflict) could still commit a reference to a
shared CAS digest between the collector's FINAL reference scan and its
unlink — an acknowledged checkpoint left holding an address without
bytes. READ COMMITTED is a statement-time snapshot, not a prohibition
on future references; the fix is ONE volume-wide reference/delete lock
(the store's ``cas-refs.lock`` flock, plus a postgres
``pg_advisory_lock`` twin on a constant key where the blob root is not
shared) held:

- by every LANDING for its reference-recording transaction — the
  filesystem index read-modify-write, or the metadata row transaction
  (the blob writes stay OUTSIDE the lock; under it the landing
  RE-LANDS any closure address a sweep unlinked mid-put, before the
  reference commits), and
- by every SWEEP (explicit retention, on-upload cleanup, pending-GC
  recovery) from its FINAL reference scan through its last unlink:
  lock → final scan → delete-metadata transaction → unlink → unlock.

The one lock order (volume lock before the per-work pin flock and the
metadata rows; after the per-work index lock and the first-upload
advisory anchor) is cycle-free — two concurrent collectors plus a
concurrent writer serialize rather than deadlock, every wait bounded
by ``FORGE_CHECKPOINT_GC_LOCK_WAIT_SECONDS`` with the typed
``GCLockTimeout`` refusal (a sweep aborts and retries later; a landing
fails with nothing committed). ``FORGE_CHECKPOINT_SWEEP=off`` is the
operator's rollout fence: both authorities keep marking (tombstones,
journal records, metadata deletions) but never unlink, and turning
sweeping back on collects the marked set against CURRENT reachability.
Pins are untouched — consumer identity binding stays exactly as
Q35-05 left it; this issue is strictly the writer/deleter protocol.
This volume-wide mutex is the conservative first implementation the
issue prescribes; per-digest refinement is deferred until load
measurement justifies it (the lock and sweep seams in the store module
are the only places it would land).

R36-03 (review ``16339c2``, issue #262) adds the TYPED lookup the
retry/revival chain consumes: :meth:`CheckpointRepository.lookup_outcome`
answers :class:`CheckpointLookupOutcome` — ``exact`` (with the
checkpoint's content address), ``absent``, ``unavailable``, ``corrupt``
or ``unauthorized`` — and NOTHING collapses one into another. The
store-backed half lives on :class:`_StoreBackedRepository` (the ACTIVE
entry, then the VERIFIED read so rot is never "exact"); the HTTP half
is :class:`HttpCheckpointRepository` — the authenticated
``GET /lane/checkpoints/{work_id}`` surface the lane itself dials,
wrapped for processes without blob/database access; and
:func:`resolve_checkpoint_lookup_authority` is the selection matrix
(session factory → the configured repository; control URL + lane
credential → the HTTP proxy; neither → the caller's typed
``unavailable`` — never a filesystem index, never the legacy token).

R36-05 (review ``16339c2``, issue #264) moves the cutover FENCE into
this composition point. Q35-21's migration tool could flip an authority
MARKER (``<root>/migration/authority.json``) and documented the
``enforce_authority_marker(resolve_repository(...))`` wrapper — but the
NORMAL :func:`resolve_repository` returned RAW repositories, so an
already-running old-authority process kept writing the retired index
after a cutover. Now ``resolve_repository(..., fenced=True)`` (the
DEFAULT) attaches :class:`AuthorityMarkerFence` to the resolved
repository: the marker is read at construction (the
:data:`METRIC_CONFIGURED_VS_ACTIVE` startup line) and RE-CHECKED before
every metadata mutation (``put``/``put_checkpoint``/``apply_retention``)
— an authority mismatch refuses with the typed
:class:`MutationsFencedError` naming configured-vs-active, and a cutover
holding its fence refuses BOTH sides. Reads never fence: retired history
stays explicitly read-only and distinguishable (``authority()`` keeps
naming the authority that answered). LOCK ORDER, stated once: the fence
check runs BEFORE the mutation takes the store's volume-wide GC lock
(``<root>/cas-refs.lock`` and its postgres advisory twin) and before
every per-work lock — a fence refusal therefore never WAITS on the
volume lock, and no path holds the volume lock while waiting for the
cutover fence, so the orders cannot cycle. The first supported rollout
mode is the DRAINED-OFFLINE cutover: the fence makes concurrent
processes SAFE TO REFUSE (an old process refuses mutations after the
flip, without a restart), and does not promise an online zero-downtime
migration — a mutation that passed the check instants before the flip
may still commit. :func:`enforce_authority_marker` remains the wrapper
for repositories constructed DIRECTLY (the migration documentation's
spelling), sharing this ONE fence implementation.

Crash windows, documented once for both authorities: (1) a put writes
blobs before its index row commits — a crash between them leaves
collectable CAS orphans the health report names, never an index entry
whose bytes are missing; (2) a retention pass commits metadata
deletion before unlinking — a crash there leaves the pending-GC record
(journal or index) whose next pass completes against fresh
reachability; (3) orphan recovery is exactly that completion pass —
re-derived from CURRENT references, never replayed blindly.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import socket
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from forge.api_checkpoint_channel import CheckpointStore, StoragePolicy

try:  # POSIX process-level advisory locking (Linux CI, macOS dev boxes).
    import fcntl
except ImportError:  # pragma: no cover — non-POSIX platform without flock
    fcntl = None  # type: ignore[assignment]

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "AUTHORITY_FILESYSTEM",
    "AUTHORITY_HTTP",
    "AUTHORITY_MARKER_SCHEMA",
    "AUTHORITY_POSTGRES",
    "AuthorityMarkerFence",
    "BACKUP_MANIFEST_SCHEMA",
    "BackupMismatchError",
    "CheckpointGcJournal",
    "CheckpointLookupOutcome",
    "CheckpointPins",
    "CheckpointRepository",
    "CheckpointRepositoryMisconfigured",
    "CheckpointRepositoryUnavailable",
    "FilesystemCheckpointRepository",
    "HttpCheckpointRepository",
    "LOOKUP_ABSENT",
    "LOOKUP_CORRUPT",
    "LOOKUP_EXACT",
    "LOOKUP_UNAVAILABLE",
    "LOOKUP_UNAUTHORIZED",
    "METRIC_CONFIGURED_VS_ACTIVE",
    "METRIC_MUTATION_REFUSED",
    "MutationsFencedError",
    "PostgresCheckpointRepository",
    "SingleAuthorityRepository",
    "StoreBackup",
    "authority_marker_path",
    "authority_state_report",
    "backup_store",
    "cutover_fence_path",
    "cutover_in_progress",
    "enforce_authority_marker",
    "migration_dir",
    "read_authority_marker",
    "resolve_checkpoint_lookup_authority",
    "resolve_repository",
    "restore_store",
    "verify_backup_consistency",
]

#: The authority names :meth:`CheckpointRepository.authority` answers.
#: The filesystem spelling names the ``best_effort`` durability's index;
#: the postgres spelling names the ``checkpoint_metadata`` table.
AUTHORITY_FILESYSTEM: Final = "filesystem"
AUTHORITY_POSTGRES: Final = "postgres"

#: R36-03: the authenticated checkpoint-channel PROXY's authority name —
#: a process without blob/database access reads the same authority
#: through the lane's own HTTP surface instead of a local index.
AUTHORITY_HTTP: Final = "checkpoint-channel"

#: R36-03 (issue #262): the five TYPED lookup states. A retry/revival
#: continuation decision consumes these and NOTHING may collapse one
#: into another — a 503, an expired credential or unreadable bytes are
#: answers of their own kind, never "no checkpoint".
LOOKUP_EXACT: Final = "exact"
LOOKUP_ABSENT: Final = "absent"
LOOKUP_UNAVAILABLE: Final = "unavailable"
LOOKUP_CORRUPT: Final = "corrupt"
LOOKUP_UNAUTHORIZED: Final = "unauthorized"

#: R36-05 (issue #264): the observability names the cutover fence
#: reports. The startup/health line is
#: :data:`METRIC_CONFIGURED_VS_ACTIVE` (see :func:`authority_state_report`);
#: every refusal logs :data:`METRIC_MUTATION_REFUSED` with the same
#: configured-vs-active pair the error names.
METRIC_CONFIGURED_VS_ACTIVE: Final = "migration.configured_vs_active_authority"
METRIC_MUTATION_REFUSED: Final = "migration.mutation_refused"

#: The deployment authority MARKER document's schema — written by the
#: migration tool's cutover/rollback under its exclusive fence, read
#: HERE at composition time and before every metadata mutation.
AUTHORITY_MARKER_SCHEMA: Final = "forge.checkpoint.authority/1"


class CheckpointRepositoryUnavailable(Exception):
    """The configured checkpoint authority could not be reached.

    Raised by the postgres repository when the metadata database fails
    on an index operation (a :class:`sqlalchemy.exc.SQLAlchemyError`,
    or a driver-level error that escaped SQLAlchemy's translation —
    the boundary is deliberately broad, with the store's own typed
    refusals passed through untouched) and by the filesystem repository
    when the store's root cannot even be built. A RECOVERABLE
    condition, deliberately typed: a caller must distinguish "the
    authority is down" from "the authority holds nothing" — answering
    an outage with ``None`` would make a resumable checkpoint look
    absent, and answering it by consulting a different store would be
    the silent authority switch Q35-03 removes.
    """


class CheckpointRepositoryMisconfigured(Exception):
    """The deployment names a checkpoint authority the wiring cannot build.

    Raised by :func:`resolve_repository` (and the postgres repository's
    constructor) for an unknown ``FORGE_CHECKPOINT_DURABILITY`` value or
    a ``postgres`` selection without a session factory. Construction-time
    refusal — the process that needs the authority fails fast with the
    specific diagnostic instead of degrading to a filesystem index the
    operator believes is transactional.
    """


class MutationsFencedError(CheckpointRepositoryUnavailable):
    """A metadata mutation refused: another authority owns the store.

    The SPECIFIC refusal a fenced repository answers with after a
    cutover (or while a cutover holds the fence): exactly one backend
    accepts metadata mutations, the other refuses with this error while
    its immutable READS stay available. Never retried automatically —
    the operator either cuts back (the migration tool's
    ``rollback --verify-report ...``) or fixes the deployment's
    configured authority.

    R36-05: the error subclasses
    :class:`CheckpointRepositoryUnavailable` deliberately — the HTTP
    channel's existing outage mapping then answers a fenced upload with
    503 plus the fence message (a REFUSAL naming the configured-vs-active
    pair), never an untyped 500 and never a silent write to the retired
    authority.
    """


# ---------------------------------------------------------------------------
# R36-05: the cutover fence at the composition root — the authority marker
# ---------------------------------------------------------------------------


def migration_dir(root: Path | str) -> Path:
    """The migration state directory: ``<store-root>/migration``."""
    return Path(root) / "migration"


def authority_marker_path(root: Path | str) -> Path:
    """The deployment authority marker: ``<store-root>/migration/authority.json``.

    The state file the migration tool's CUTOVER writes atomically and
    the standard composition READS (through :class:`AuthorityMarkerFence`
    at construction and before every metadata mutation): it names the
    ONE authority that accepts metadata mutations. It is a marker, not
    an index — it never decides which checkpoint is active.
    """
    return migration_dir(root) / "authority.json"


def cutover_fence_path(root: Path | str) -> Path:
    """The exclusive cutover fence: ``<store-root>/migration/cutover.lock``.

    Taken (exclusive ``flock``) only by the migration tool's flip
    commands and the reverse import; probed non-blockingly by
    :func:`cutover_in_progress` on every fenced mutation. While it is
    held, fenced repositories refuse mutations on BOTH sides.
    """
    return migration_dir(root) / "cutover.lock"


def read_authority_marker(root: Path | str) -> dict[str, Any] | None:
    """The authority marker document, or ``None`` when none exists."""
    path = authority_marker_path(root)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(document, dict) and document.get("schema") == AUTHORITY_MARKER_SCHEMA:
        return document
    return None


def cutover_in_progress(root: Path | str) -> bool:
    """Whether a cutover/rollback currently holds the fence (a probe)."""
    if fcntl is None:  # pragma: no cover — non-POSIX without flock
        return False
    path = cutover_fence_path(root)
    if not path.is_file():
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True  # held by the cutover process
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


class AuthorityMarkerFence:
    """The mutation fence every standard-composed repository carries (R36-05).

    ONE implementation of the cutover fence, shared by the composition
    root (:func:`resolve_repository` attaches it by default) and by the
    documented wrapper (:func:`enforce_authority_marker` — the spelling
    for repositories constructed directly). It reads the deployment
    authority marker at CONSTRUCTION (the snapshot
    :meth:`state` reports at startup) and RE-CHECKS the marker before
    every metadata mutation:

    - a cutover holding its fence → :class:`MutationsFencedError` (both
      sides refuse; the flip is single-flight);
    - no marker → allowed (the fence is dormant — no cutover ran);
    - the marker names a DIFFERENT authority than the repository serves
      → :class:`MutationsFencedError` naming configured-vs-active, with
      :data:`METRIC_MUTATION_REFUSED` logged.

    Reads never pass through here — retired history stays read-only and
    distinguishable (``authority()`` keeps naming the authority that
    answered). The pin overlay (``pin``/``unpin``) is deliberately NOT
    fenced: it is a protection overlay on the shared blob volume, not
    the metadata authority the cutover retires, and its removal
    endangers nothing while every deletion path is fenced.

    LOCK ORDER, stated once for every consumer: the fence check runs
    BEFORE the mutation takes the store's volume-wide GC lock
    (``<root>/cas-refs.lock`` and its postgres advisory twin) and before
    any per-work lock — a fence refusal therefore never WAITS on the
    volume lock, and no path holds the volume lock while waiting for
    the cutover fence, so the two orders cannot cycle. The honest
    boundary: a mutation that passed this check instants before the
    marker flipped may still commit — the first supported rollout mode
    is the DRAINED-OFFLINE cutover, where the fence makes concurrent
    processes safe to REFUSE without promising online zero downtime.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        #: The construction-time snapshot (observability only — every
        #: mutation re-reads the marker FRESH, so a running process
        #: switches behavior the moment the cutover lands).
        self.construction_marker: dict[str, Any] | None = read_authority_marker(self._root)

    def refuse_mutations(self, configured_authority: str) -> None:
        """Refuse unless *configured_authority* is the marker's authority."""
        if cutover_in_progress(self._root):
            raise MutationsFencedError(
                "a checkpoint authority cutover is in progress (fence at "
                f"{cutover_fence_path(self._root)}) — mutations are fenced on both "
                "sides; retry once the cutover completes"
            )
        marker = read_authority_marker(self._root)
        if marker is None:
            return  # dormant: no cutover has flipped this store
        named = str(marker.get("authority") or "")
        if named and named != configured_authority:
            _LOGGER.warning(
                "%s: configured=%s active=%s work_root=%s — the marker at %s "
                "names another authority; the mutation is refused",
                METRIC_MUTATION_REFUSED,
                configured_authority,
                named,
                self._root,
                authority_marker_path(self._root),
            )
            raise MutationsFencedError(
                f"the checkpoint authority marker at {authority_marker_path(self._root)} "
                f"names {named!r}; this {configured_authority!r} repository refuses "
                "metadata mutations (configured vs active authority mismatch) — "
                "immutable reads stay available; recovery: `python -m "
                "forge.adaptive.checkpoint_migration rollback --verify-report <path>`"
            )

    def state(self) -> dict[str, Any]:
        """The marker-side view of the fence (a fresh read, never cached)."""
        marker = read_authority_marker(self._root)
        return {
            "active_authority": str(marker.get("authority") or "") if marker else None,
            "marker_generation": marker.get("generation") if marker else None,
            "marker_path": str(authority_marker_path(self._root)),
            "cutover_in_progress": cutover_in_progress(self._root),
        }


def authority_state_report(
    env: Mapping[str, str] | None = None,
    *,
    root: Path | str | None = None,
) -> dict[str, Any]:
    """``migration.configured_vs_active_authority`` — the startup/health line.

    R36-05: wherever the application composes the repository (the app's
    startup, the control service's composition, ``forge doctor``), this
    is the ONE line that reports the deployment's CONFIGURED checkpoint
    authority (``FORGE_CHECKPOINT_DURABILITY``, with ``best_effort``
    spelled as the ``filesystem`` authority it names) against the
    authority the deployment's MARKER declares ACTIVE, plus the marker's
    generation (its flip counter — content identity, never a timestamp
    authorization). ``state`` is one of:

    - ``unmarked`` — no marker exists; the fence is dormant (no cutover
      has run against this store root);
    - ``aligned`` — the configured repository IS the authority the
      marker names; mutations pass the fence;
    - ``mismatch`` — the configured repository is NOT the marker's
      authority: this process's metadata mutations will be refused with
      :class:`MutationsFencedError` until it restarts on the configured
      authority or the operator rolls the marker back.

    A misconfigured mode (junk ``FORGE_CHECKPOINT_DURABILITY``) is
    reported as ``configured_problem`` — the composition itself refuses
    construction separately, this report never raises.
    """
    from forge.api_checkpoint_channel import (
        CHECKPOINT_STORE_DIR_ENV,
        DEFAULT_CHECKPOINT_ROOT,
        DURABILITY_BEST_EFFORT,
        DurabilityContract,
    )

    source: Mapping[str, str] = os.environ if env is None else env
    storage_root = (
        Path(root)
        if root is not None
        else Path(str(source.get(CHECKPOINT_STORE_DIR_ENV, "")).strip() or DEFAULT_CHECKPOINT_ROOT)
    )
    configured_problem = ""
    try:
        mode = DurabilityContract.mode_from_env(dict(source))
    except ValueError as exc:
        mode = ""
        configured_problem = str(exc)
    configured_authority = AUTHORITY_FILESYSTEM if mode == DURABILITY_BEST_EFFORT else mode
    marker = read_authority_marker(storage_root)
    active = str(marker.get("authority") or "") if marker else ""
    if marker is None:
        state = "unmarked"
    elif configured_authority and configured_authority != active:
        state = "mismatch"
    else:
        state = "aligned"
    return {
        "metric": METRIC_CONFIGURED_VS_ACTIVE,
        "configured_repository": mode or None,
        "configured_authority": configured_authority or None,
        "active_authority": active or None,
        "marker_generation": marker.get("generation") if marker else None,
        "marker_path": str(authority_marker_path(storage_root)),
        "cutover_in_progress": cutover_in_progress(storage_root),
        "state": state,
        "configured_problem": configured_problem,
    }


@dataclass(frozen=True)
class CheckpointLookupOutcome:
    """The TYPED answer of one checkpoint-presence lookup (R36-03).

    The retry/revival continuation chain used to collapse every lookup
    failure into ``False`` ("no checkpoint") — a PostgreSQL-only
    checkpoint read as nothing in ``/retry`` exactly when the control
    plane was slow or the legacy token window closed. This is the typed
    replacement: :attr:`state` is one of :data:`LOOKUP_EXACT`,
    :data:`LOOKUP_ABSENT`, :data:`LOOKUP_UNAVAILABLE`,
    :data:`LOOKUP_CORRUPT` or :data:`LOOKUP_UNAUTHORIZED`, and NOTHING
    may collapse one into another — the consumer decides, the lookup
    never decides for it by lossy encoding.

    ``exact`` additionally carries the checkpoint's :attr:`checkpoint_id`
    (its content address — the manifest's SHA-256) and :attr:`digest`
    (the same address, the ``continuation.checkpoint_digest``
    observability spelling), so the decision can PIN the exact bytes it
    approved; a later upload changes nothing about a decision that
    already holds one. The remaining states carry an operator-facing
    :attr:`detail` instead, plus the :attr:`authority` that answered and
    the measured :attr:`latency_s` (``checkpoint.lookup.latency``).
    """

    #: One of the ``LOOKUP_*`` state constants.
    state: str
    #: The exact checkpoint's content address (``exact`` only).
    checkpoint_id: str | None = None
    #: The exact checkpoint's digest — the same address (``exact`` only).
    digest: str | None = None
    #: Which authority answered (``filesystem`` / ``postgres`` /
    #: ``checkpoint-channel`` / the legacy adapter's label).
    authority: str = ""
    #: The operator-facing explanation of a non-exact answer.
    detail: str = ""
    #: How long the lookup took, when it was measured.
    latency_s: float | None = None

    @classmethod
    def exact(
        cls,
        checkpoint_id: str,
        *,
        authority: str = "",
        digest: str | None = None,
        latency_s: float | None = None,
    ) -> CheckpointLookupOutcome:
        """The one state that carries the checkpoint's identity."""
        return cls(
            state=LOOKUP_EXACT,
            checkpoint_id=checkpoint_id,
            digest=digest or checkpoint_id,
            authority=authority,
            latency_s=latency_s,
        )

    @classmethod
    def missing(
        cls,
        state: str,
        *,
        authority: str = "",
        detail: str = "",
        latency_s: float | None = None,
    ) -> CheckpointLookupOutcome:
        """A non-exact answer (absent/unavailable/corrupt/unauthorized)."""
        return cls(state=state, authority=authority, detail=detail, latency_s=latency_s)

    @property
    def is_exact(self) -> bool:
        return self.state == LOOKUP_EXACT

    @property
    def is_absent(self) -> bool:
        return self.state == LOOKUP_ABSENT

    @property
    def is_unavailable(self) -> bool:
        return self.state == LOOKUP_UNAVAILABLE

    @property
    def is_corrupt(self) -> bool:
        return self.state == LOOKUP_CORRUPT

    @property
    def is_unauthorized(self) -> bool:
        return self.state == LOOKUP_UNAUTHORIZED


@runtime_checkable
class CheckpointRepository(Protocol):
    """The one checkpoint authority upload, resume and operations share.

    Every method is async: the control-plane surfaces that consume this
    seam (the HTTP channel's routes, the resume producer) run on the
    event loop, and the underlying stores range from tiny JSON reads to
    transactional database round-trips. Implementations must keep the
    typed read states disjoint — ``None`` is absence,
    :class:`forge.api_checkpoint_channel.CheckpointCorruptError` is
    rotted bytes, :class:`CheckpointRepositoryUnavailable` is an
    unreachable authority — and must never answer one with another.
    """

    async def entry(self, work_id: str) -> dict[str, Any] | None:
        """The work's ACTIVE entry (highest ``(sequence, checkpoint_id)``).

        ``None`` when the configured authority holds nothing for the
        work — arrival order is never authority (R28-06), so a delayed
        lower-sequence upload never demotes the entry this returns.
        """
        ...

    async def put(
        self,
        work_id: str,
        checkpoint_id: str,
        manifest_bytes: bytes,
        blobs: dict[str, bytes],
    ) -> None:
        """Land one verified checkpoint under its declared address.

        *checkpoint_id* must equal the manifest's own content address
        (SHA-256) — a mismatch is refused before any write, so a caller
        can never poison an address with bytes it does not back. The
        checkpoint's sequence comes from the manifest itself (the
        durable document, not a transport header), and the landing obeys
        the store's own verification prelude: the blob set must be
        exactly the manifest's referenced digests, all keys are content
        addresses, and the storage policy's caps refuse before the
        first write.
        """
        ...

    async def read(self, work_id: str) -> tuple[bytes, list[bytes]] | None:
        """The ACTIVE checkpoint's manifest and blobs, verified on read.

        ``None`` when absent; the blobs are ordered by digest so two
        readers of the same checkpoint see the same payload. Every blob
        is re-hashed to its address — rotted bytes raise
        :class:`forge.api_checkpoint_channel.CheckpointCorruptError`
        rather than being served as if they were the checkpoint.
        """
        ...

    async def lookup_outcome(self, work_id: str) -> CheckpointLookupOutcome:
        """The TYPED presence lookup the retry/revival chain runs (R36-03).

        One call, five disjoint answers: ``exact`` (the work's ACTIVE
        checkpoint exists AND its bytes re-hash to their addresses — the
        outcome carries the checkpoint's content address), ``absent``
        (the configured authority provably holds nothing for the work),
        ``corrupt`` (the index names a checkpoint whose stored bytes no
        longer match), ``unavailable`` (the authority could not be
        reached) and ``unauthorized`` (the HTTP proxy's credential was
        refused). Nothing collapses: the CALLER distinguishes an outage
        from an absence, exactly the distinction the untyped boolean
        this replaces destroyed.
        """
        ...

    async def authority(self) -> str:
        """``"filesystem"`` or ``"postgres"`` — the configured authority."""
        ...

    async def pin(self, work_id: str, checkpoint_id: str, reason: str = "") -> bool:
        """Protect one exact checkpoint from garbage collection (Q35-05).

        Called when a consumer's claim becomes DURABLE — the resume
        authorization point pins the exact checkpoint its approved
        ResumeSpec binds to. The pinned row AND every blob its manifest
        references survive newer checkpoints and every retention pass
        until the pin is released EXPLICITLY by :meth:`unpin` — never by
        time alone. Pinning a checkpoint the authority does not hold is
        an honest ``ValueError`` refusal (a typo must not become a pin
        that lives forever). Returns whether a NEW pin was created.
        """
        ...

    async def unpin(self, work_id: str, checkpoint_id: str, reason: str | None = None) -> int:
        """Release pin(s): every pin of the checkpoint, or only *reason*'s.

        The explicit release that eventually allows GC. Returns how many
        pin records were removed (0 when none matched — idempotent).
        """
        ...

    async def pins(self, work_id: str | None = None) -> list[dict[str, Any]]:
        """The recorded pins (one work's, or every work's) — the operator view."""
        ...


#: A work id names a pin FILE path segment — the same safe-segment rule
#: the channel's index path uses, mirrored here so the overlay can never
#: be talked into writing outside ``pins/`` by an unvalidated id.
_PIN_WORK_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: How long a pin (or journal) read-modify-write retries for the
#: per-work flock before refusing — the same budget class as the index
#: lock's (NEXT-06); pin writes are rare and short.
_PIN_LOCK_WAIT_SECONDS: Final = 5.0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _safe_work_id(work_id: str) -> str:
    """The validated id a pin/journal path segment may be built from."""
    if not _PIN_WORK_ID.fullmatch(work_id):
        raise ValueError(
            f"work id {work_id!r} is not a safe path segment (letters, digits, "
            "'.', '_', '-'; at most 128 chars) — the pin overlay refuses it"
        )
    return work_id


def _work_anchor_key(work_id: str) -> int:
    """The STABLE per-work key of the first-upload advisory anchor.

    The first 8 bytes of the work id's SHA-256 as a signed 64-bit
    integer — the same value in every process, so concurrent first
    uploads of one work land on the same ``pg_advisory_lock`` key
    without any table row to agree through. A collision between two
    DIFFERENT works (2^-64 per pair) would only over-serialize them —
    never under-serialize — so it is safe by construction.
    """
    return int.from_bytes(hashlib.sha256(work_id.encode("utf-8")).digest()[:8], "big", signed=True)


def _advisory_sql(statement: str) -> Any:
    """The advisory-lock statement as SQLAlchemy text (postgres-only)."""
    from sqlalchemy import text

    return text(statement)


#: Session-scoped advisory lock/unlock — held on ONE dedicated
#: connection for the duration of a work's first landing (see
#: :meth:`PostgresCheckpointRepository.first_upload_lock`).
_ADVISORY_LOCK_SQL: Final = _advisory_sql("SELECT pg_advisory_lock(:key)")
_ADVISORY_UNLOCK_SQL: Final = _advisory_sql("SELECT pg_advisory_unlock(:key)")


class CheckpointPins:
    """The consumer-pin overlay both GC authorities consult (Q35-05).

    An explicit, durable record of WHO still needs one EXACT checkpoint:
    an approved ResumeSpec, a retention/legal hold. Retention (both the
    filesystem pass and the postgres ``a*`` pass) treats a pinned
    checkpoint id — and every digest its manifest references — as
    unreachable-by-GC until :meth:`remove` releases the pin explicitly;
    nothing expires by time.

    Storage is ONE small JSON document per work at
    ``<root>/pins/<work_id>.json``, written atomically (temp + rename)
    under an exclusive per-work ``flock`` at
    ``<root>/pins/<work_id>.lock`` — the pin RMW, the postgres
    retention transaction and the filesystem retention pass (which
    takes the pin lock nested inside its index lock) all serialize on
    it, so a pin can never land inside a retention critical section
    and a retention pass can never read a half-written pin set. The
    file is a PROTECTION OVERLAY, not a checkpoint index: it never
    decides which checkpoint is active, and it lives on the blob
    volume every durability contract already requires to be shared
    (the CAS blobs are filesystem bytes under both).
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    # -- paths ---------------------------------------------------------------

    def _pins_dir(self) -> Path:
        return self._root / "pins"

    def _pins_path(self, work_id: str) -> Path:
        return self._pins_dir() / f"{_safe_work_id(work_id)}.json"

    def lock_path(self, work_id: str) -> Path:
        """The per-work pin lock file — ``<root>/pins/<work_id>.lock``.

        The pin overlay's OWN lock: every pin read-modify-write, the
        postgres retention transaction and the postgres first landing
        serialize here. The FILESYSTEM retention pass takes this lock
        NESTED inside its index lock (index lock first, pin lock
        second — the one nesting order in the system, so no cycle), so
        a pin can never land inside a filesystem retention critical
        section either: whichever side of the two contracts runs, the
        pin set a retention pass reads is stable for the whole pass.
        """
        return self._pins_dir() / f"{_safe_work_id(work_id)}.lock"

    # -- the lock (one discipline, two spellings: sync for threads, async
    #    for the event loop — both target the same lock FILE, so takers
    #    of either spelling exclude each other) ----------------------------

    @contextmanager
    def lock(self, work_id: str, *, wait_seconds: float = _PIN_LOCK_WAIT_SECONDS) -> Iterator[None]:
        """The per-work flock, blocking-with-budget (for worker threads)."""
        if fcntl is None:  # pragma: no cover — non-POSIX without flock
            yield
            return
        path = self.lock_path(work_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(0.0, wait_seconds)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _lock_refusal(work_id) from None
                    time.sleep(min(random.uniform(0.0005, 0.003), remaining))
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @asynccontextmanager
    async def alock(
        self, work_id: str, *, wait_seconds: float = _PIN_LOCK_WAIT_SECONDS
    ) -> AsyncIterator[None]:
        """The same per-work flock, awaited off the blocking path.

        For the event-loop callers (the postgres authority's retention
        transaction): the retry sleeps are ``asyncio.sleep``, so a
        contended lock never blocks the loop; the lock itself is the
        SAME file, so it excludes the sync spellings exactly as another
        process would.
        """
        if fcntl is None:  # pragma: no cover — non-POSIX without flock
            yield
            return
        path = self.lock_path(work_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(0.0, wait_seconds)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _lock_refusal(work_id) from None
                    await asyncio.sleep(min(random.uniform(0.0005, 0.003), remaining))
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # -- the document ----------------------------------------------------------

    def _load(self, work_id: str) -> dict[str, Any]:
        path = self._pins_path(work_id)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"schema": "forge.checkpoint.pins/1", "work_id": work_id, "pins": []}
        if not isinstance(document, dict) or not isinstance(document.get("pins"), list):
            return {"schema": "forge.checkpoint.pins/1", "work_id": work_id, "pins": []}
        return document

    def _save(self, work_id: str, document: dict[str, Any]) -> None:
        path = self._pins_path(work_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-pins-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            os.unlink(tmp_name)
            raise

    # -- the operations (callers hold :meth:`lock` for read-modify-writes;
    #    readers may read without it ONLY while holding the per-work lock
    #    some other way — that is exactly what retention does) --------------

    def add(self, work_id: str, checkpoint_id: str, reason: str = "") -> bool:
        """Record one pin (idempotent per ``(checkpoint_id, reason)``)."""
        with self.lock(work_id):
            return self.add_locked(work_id, checkpoint_id, reason)

    def add_locked(self, work_id: str, checkpoint_id: str, reason: str = "") -> bool:
        """Record one pin — the caller HOLDS the per-work lock.

        The spelling :meth:`_StoreBackedRepository.pin` uses: the
        authority lookup that validates the checkpoint exists runs
        INSIDE the same lock section, so a retention pass can never
        delete the row between the pin's validation and its write (the
        pin would dangle over a deleted checkpoint).
        """
        document = self._load(work_id)
        pins = [
            entry
            for entry in document["pins"]
            if isinstance(entry, dict) and isinstance(entry.get("checkpoint_id"), str)
        ]
        if any(
            entry["checkpoint_id"] == checkpoint_id and entry.get("reason") == reason
            for entry in pins
        ):
            return False
        pins.append(
            {
                "checkpoint_id": checkpoint_id,
                "reason": reason,
                "pinned_at": _now_iso(),
                "pinned_by": f"{socket.gethostname()}|{os.getpid()}",
            }
        )
        document["pins"] = pins
        self._save(work_id, document)
        return True

    def remove(self, work_id: str, checkpoint_id: str, reason: str | None = None) -> int:
        """Release the checkpoint's pins — all of them, or only *reason*'s."""
        with self.lock(work_id):
            document = self._load(work_id)
            kept, released = [], 0
            for entry in document["pins"]:
                if not isinstance(entry, dict):
                    continue
                matches = entry.get("checkpoint_id") == checkpoint_id and (
                    reason is None or entry.get("reason") == reason
                )
                if matches:
                    released += 1
                else:
                    kept.append(entry)
            if released:
                if kept:
                    document["pins"] = kept
                    self._save(work_id, document)
                else:
                    # The work's last pin going away takes the file with it
                    # — no empty residue for the health walk to explain.
                    try:
                        self._pins_path(work_id).unlink()
                    except OSError:
                        pass
            return released

    def list(self, work_id: str | None = None) -> list[dict[str, Any]]:
        """The recorded pins as ``{work_id, checkpoint_id, reason, ...}``
        dicts, stable-ordered — the operator (and test) view."""
        if work_id is not None:
            return [
                {**entry, "work_id": work_id}
                for entry in self._load(work_id)["pins"]
                if isinstance(entry, dict)
            ]
        listed: list[dict[str, Any]] = []
        pins_dir = self._pins_dir()
        for path in sorted(pins_dir.glob("*.json")) if pins_dir.is_dir() else []:
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(document, dict):
                continue
            work = str(document.get("work_id") or path.stem)
            for entry in document.get("pins", []):
                if isinstance(entry, dict):
                    listed.append({**entry, "work_id": work})
        return sorted(
            listed, key=lambda entry: (str(entry.get("work_id")), str(entry.get("checkpoint_id")))
        )

    def ids_for(self, work_id: str) -> set[str]:
        """The pinned checkpoint ids of ONE work (no lock — a fresh read)."""
        return {
            str(entry["checkpoint_id"])
            for entry in self._load(work_id)["pins"]
            if isinstance(entry, dict) and isinstance(entry.get("checkpoint_id"), str)
        }

    def protected_ids(self) -> set[str]:
        """Every pinned checkpoint id across ALL works (no lock — fresh).

        The set both GC authorities subtract at mark time AND re-read at
        recheck time: a pin that landed between the two reads protects
        its checkpoint from that very pass.
        """
        protected: set[str] = set()
        pins_dir = self._pins_dir()
        for path in sorted(pins_dir.glob("*.json")) if pins_dir.is_dir() else []:
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(document, dict):
                continue
            for entry in document.get("pins", []):
                if isinstance(entry, dict) and isinstance(entry.get("checkpoint_id"), str):
                    protected.add(str(entry["checkpoint_id"]))
        return protected


def _lock_refusal(work_id: str) -> Exception:
    """The typed refusal when the per-work pin lock cannot be taken."""
    from forge.api_checkpoint_channel import IndexLockHeldError

    return IndexLockHeldError(work_id, "")


class CheckpointGcJournal:
    """The postgres authority's pending-GC record (Q35-05).

    The filesystem authority records its retention decision INSIDE the
    per-work index (``pending_gc``); the postgres authority's index is
    the ``checkpoint_metadata`` table, whose schema carries no decision
    column — so the tombstone persists HERE, as one small JSON overlay
    per work at ``<root>/gc/<work_id>.json`` on the shared blob volume:

    - **record** BEFORE the deletion commits — a crash after the commit
      finds the journal naming exactly the digests whose rows went;
    - **clear** AFTER the unlink sweep — the window between them is the
      recoverable crash window;
    - the next retention pass COMPLETES the journal first: it re-derives
      reachability from the CURRENT table (plus pins) and unlinks only
      what is still unreferenced — a journal left by a ROLLED-BACK
      transaction names digests whose rows still exist, so completion
      spares them and clears the record. Recovery never replays blindly.

    Operations are plain atomic file writes (no lock of their own): the
    retention pass holds the per-work flock while it records/clears, and
    a torn write is impossible (temp + rename). A journal lost to a
    concurrent direct-store call can only leak collectable orphans —
    the health report names them — never delete live bytes.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    def _journal_path(self, work_id: str) -> Path:
        return self._root / "gc" / f"{_safe_work_id(work_id)}.json"

    def record(self, work_id: str, digests: list[str] | set[str]) -> None:
        """Persist the deletion set as pending (idempotent overwrite)."""
        digests = sorted(digests)
        if not digests:
            return  # nothing to sweep — no tombstone to recover
        path = self._journal_path(work_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {"work_id": work_id, "pending": digests, "recorded_at": _now_iso()}
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-gc-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            os.unlink(tmp_name)
            raise

    def pending(self, work_id: str) -> list[str]:
        """The recorded pending digests (empty when no journal exists)."""
        try:
            document = json.loads(self._journal_path(work_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(document, dict):
            return []
        pending = document.get("pending")
        if not isinstance(pending, list):
            return []
        return [str(digest) for digest in pending if isinstance(digest, str)]

    def clear(self, work_id: str, digests: list[str] | set[str] | None = None) -> None:
        """Remove *digests* from the pending record (all of them by default).

        Digests that must STAY pending (a reference arrived and spared
        them) are simply not passed: the record keeps them for a later
        pass. An empty record takes its file with it.
        """
        path = self._journal_path(work_id)
        current = self.pending(work_id)
        if not current:
            return
        remaining = set(current) - (set(digests) if digests is not None else set(current))
        if not remaining:
            try:
                path.unlink()
            except OSError:
                pass
            return
        document = {"work_id": work_id, "pending": sorted(remaining), "recorded_at": _now_iso()}
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-gc-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            os.unlink(tmp_name)
            raise


def _manifest_sequence(manifest_bytes: bytes) -> int:
    """The sequence the manifest itself declares (0 when it does not).

    The manifest is the durable document — the transport payload's
    ``sequence`` header is a convenience copy of this field — so the
    repository's protocol-level :meth:`CheckpointRepository.put` takes
    its ordering key from the bytes it stores.
    """
    try:
        document = json.loads(manifest_bytes)
    except ValueError:
        return 0
    if isinstance(document, dict):
        sequence = document.get("sequence")
        if isinstance(sequence, int):
            return sequence
    return 0


def _refusal(exc: Exception) -> bool:
    """Whether *exc* is the store's own typed refusal, not an outage.

    The upload verification prelude refuses with ``ValueError``
    (non-address keys, a blob set that is not the manifest's exact
    closure, :class:`StorageQuotaExceededError` — a ``ValueError``
    subclass) and the verified reads refuse with
    :class:`CheckpointCorruptError`. Those are ANSWERS, not failures:
    the repository must hand them to the caller untouched instead of
    mistaking them for an unreachable authority.
    """
    from forge.api_checkpoint_channel import CheckpointCorruptError

    return isinstance(exc, (CheckpointCorruptError, ValueError))


def _unavailable(operation: str, exc: Exception) -> CheckpointRepositoryUnavailable:
    """The typed outage for *operation*, chained to its cause.

    Deliberately broad at the boundary: database failures surface as
    :class:`sqlalchemy.exc.SQLAlchemyError` MOST of the time, but
    driver-level errors can escape SQLAlchemy's translation entirely
    (an asyncpg authentication failure against a wrong database raises
    the RAW ``InvalidPasswordError`` on some paths) — and an outage
    answered as anything other than ``CheckpointRepositoryUnavailable``
    (a raw 500, a ``None`` that reads as "no checkpoint", or a
    filesystem consult) is exactly what Q35-03 forbids.
    """
    return CheckpointRepositoryUnavailable(
        f"the postgres checkpoint authority is unreachable ({operation}): {exc}"
    )


class _StoreBackedRepository:
    """The shared half of both authorities: the CAS blobs and reads.

    Both implementations wrap ONE :class:`~forge.api_checkpoint_channel.
    CheckpointStore` — the module the storage discipline already lives
    in. There is no second implementation of the content-addressed
    writes, the verified reads or the refusal prelude anywhere in this
    module; the subclasses only choose WHICH index the store consults
    (the filesystem JSON index synchronously, the ``checkpoint_metadata``
    table transactionally) and how the failures of that index are typed.

    R36-05: instances composed through :func:`resolve_repository` carry
    an :class:`AuthorityMarkerFence`; every metadata mutation passes
    :meth:`_refuse_metadata_mutation` FIRST (see the fence's lock-order
    contract). Directly constructed instances (the migration tool's own
    import spelling, and every pre-R36-05 caller) stay unfenced.
    """

    _store: CheckpointStore

    #: The authority name the subclass serves (a class constant).
    _AUTHORITY_NAME: str = ""

    #: The composition-root mutation fence — None when constructed
    #: directly (the migration tool's spelling).
    _authority_fence: AuthorityMarkerFence | None = None

    def _refuse_metadata_mutation(self) -> None:
        """The R36-05 fence check every mutation passes FIRST.

        Runs BEFORE the store takes the volume-wide GC lock or any
        per-work lock (the one documented lock order — see
        :class:`AuthorityMarkerFence`), so a fence refusal never waits
        on a contended volume and can never deadlock against a sweep.
        Reads (``entry``/``read``/``lookup_outcome``/listings/health)
        and the pin overlay deliberately do NOT pass through here.
        """
        if self._authority_fence is not None:
            self._authority_fence.refuse_mutations(self._AUTHORITY_NAME)

    async def entry(self, work_id: str, checkpoint_id: str | None = None) -> dict[str, Any] | None:
        """The authority-specific lookup every seam here composes with."""
        raise NotImplementedError  # the subclass names the authority

    async def put_checkpoint(
        self,
        *,
        work_id: str,
        manifest_bytes: bytes,
        blobs: dict[str, bytes],
        sequence: int,
    ) -> dict[str, Any]:
        """The authority-specific landing (the transport-level verdict)."""
        raise NotImplementedError  # the subclass names the authority

    async def pin(self, work_id: str, checkpoint_id: str, reason: str = "") -> bool:
        """Protect one exact checkpoint from GC (Q35-05 — see the protocol).

        The whole pin — the authority lookup that validates the
        checkpoint EXISTS and the overlay write that records the
        protection — runs inside ONE per-work lock section: a retention
        pass for this work can never delete the row between the
        validation and the write (a pin dangling over a deleted
        checkpoint protects nothing). Pinning a checkpoint the
        configured authority does not hold is an honest ``ValueError``
        (a typo must not become protection that lives forever), and an
        unreachable authority answers with its TYPED error — never a
        pin recorded against a guessed state.
        """
        async with self._store.pins.alock(work_id):
            entry = await self.entry(work_id, checkpoint_id)
            if entry is None:
                raise ValueError(
                    f"cannot pin {checkpoint_id} for {work_id}: the configured "
                    "authority holds no such checkpoint — refusing the pin rather "
                    "than recording protection for bytes nobody can read"
                )
            return await asyncio.to_thread(
                self._store.pins.add_locked, work_id, checkpoint_id, reason
            )

    async def unpin(self, work_id: str, checkpoint_id: str, reason: str | None = None) -> int:
        """Release the pin(s) — the explicit release that allows GC again."""
        return await asyncio.to_thread(self._store.pins.remove, work_id, checkpoint_id, reason)

    async def pins(self, work_id: str | None = None) -> list[dict[str, Any]]:
        """The recorded pins (one work's, or every work's) — no authority
        round-trip: the overlay IS the record."""
        return await asyncio.to_thread(self._store.pins.list, work_id)

    async def read_entry(self, entry: dict[str, Any]) -> tuple[bytes, dict[str, bytes]]:
        """Serve one entry's manifest + blobs (digest-keyed), verified.

        The mode-agnostic CAS half of the read: blobs are
        content-addressed filesystem bytes under BOTH contracts, so the
        verified read is the same call either way. Runs off the event
        loop — it re-hashes every blob on the way out.
        """
        return await asyncio.to_thread(self._store.read_checkpoint, entry)

    async def read(
        self, work_id: str, checkpoint_id: str | None = None
    ) -> tuple[bytes, list[bytes]] | None:
        """The (named or ACTIVE) checkpoint's manifest and blobs.

        The protocol spelling: absent answers ``None``, corrupt bytes
        raise the store's typed :class:`CheckpointCorruptError`, and an
        unreachable index raises :class:`CheckpointRepositoryUnavailable`
        — the three states never stand in for one another.
        """
        entry = await self.entry(work_id, checkpoint_id)
        if entry is None:
            return None
        manifest_bytes, blobs = await self.read_entry(entry)
        return manifest_bytes, [blobs[digest] for digest in sorted(blobs)]

    async def lookup_outcome(self, work_id: str) -> CheckpointLookupOutcome:
        """The typed presence lookup, shared by both store-backed
        authorities (R36-03 — see the protocol method).

        The verified read is deliberate: an ``exact`` answer must mean
        the checkpoint is not merely INDEXED but READABLE — the decision
        that pins it would otherwise dispatch a lane at bytes that rot.
        The re-hash runs off the event loop (a worker thread), the same
        as every other store read here.
        """
        from forge.api_checkpoint_channel import CheckpointCorruptError

        started = time.monotonic()
        authority = await self.authority()
        try:
            entry = await self.entry(work_id)
        except CheckpointRepositoryUnavailable as exc:
            return CheckpointLookupOutcome.missing(
                LOOKUP_UNAVAILABLE,
                authority=authority,
                detail=str(exc),
                latency_s=time.monotonic() - started,
            )
        if entry is None:
            return CheckpointLookupOutcome.missing(
                LOOKUP_ABSENT,
                authority=authority,
                detail=(
                    f"the {authority} checkpoint authority holds no checkpoint for work {work_id!r}"
                ),
                latency_s=time.monotonic() - started,
            )
        checkpoint_id = str(entry.get("checkpoint_id") or "")
        try:
            await self.read_entry(entry)  # verified — rot is never "exact"
        except CheckpointRepositoryUnavailable as exc:
            return CheckpointLookupOutcome.missing(
                LOOKUP_UNAVAILABLE,
                authority=authority,
                detail=str(exc),
                latency_s=time.monotonic() - started,
            )
        except OSError as exc:  # the blob volume itself is unreachable
            return CheckpointLookupOutcome.missing(
                LOOKUP_UNAVAILABLE,
                authority=authority,
                detail=(
                    f"the {authority} checkpoint authority cannot read the blobs "
                    f"of checkpoint {checkpoint_id[:12]} for work {work_id!r}: {exc}"
                ),
                latency_s=time.monotonic() - started,
            )
        except (CheckpointCorruptError, ValueError) as exc:
            # Rotted bytes — or an index row naming non-addresses — are
            # data the authority can no longer honor, never "absent".
            return CheckpointLookupOutcome.missing(
                LOOKUP_CORRUPT,
                authority=authority,
                detail=(
                    f"checkpoint {checkpoint_id[:12]} for work {work_id!r} no "
                    f"longer hashes to its addresses: {exc}"
                ),
                latency_s=time.monotonic() - started,
            )
        return CheckpointLookupOutcome.exact(
            checkpoint_id,
            authority=authority,
            latency_s=time.monotonic() - started,
        )

    async def put(
        self,
        work_id: str,
        checkpoint_id: str,
        manifest_bytes: bytes,
        blobs: dict[str, bytes],
    ) -> None:
        """Land the checkpoint under its declared address (see the protocol).

        The declared *checkpoint_id* is checked against the manifest's
        own content address FIRST — a mismatch refuses before any write
        — then the landing delegates to :meth:`put_checkpoint` with the
        sequence the manifest itself declares.
        """
        actual = hashlib.sha256(manifest_bytes).hexdigest()
        if checkpoint_id != actual:
            raise ValueError(
                f"checkpoint for {work_id} addresses to {actual}, not "
                f"{checkpoint_id!r} — the repository refuses to land bytes "
                "under an address they do not back"
            )
        await self.put_checkpoint(
            work_id=work_id,
            manifest_bytes=manifest_bytes,
            blobs=blobs,
            sequence=_manifest_sequence(manifest_bytes),
        )


class FilesystemCheckpointRepository(_StoreBackedRepository):
    """The ``filesystem`` authority: the per-work JSON index under ``flock``.

    Wraps the existing SYNCHRONOUS :class:`CheckpointStore` (the
    ``best_effort`` durability's default contract) with its blocking
    calls run in a worker thread, so an async caller never blocks the
    event loop on index IO. Authority is
    :data:`AUTHORITY_FILESYSTEM` — the single-process, shared-volume
    contract; a deployment that needs the transactional index selects
    :class:`PostgresCheckpointRepository` instead (never both: there is
    one configured authority).
    """

    _AUTHORITY_NAME = AUTHORITY_FILESYSTEM

    def __init__(
        self,
        root: Path | str,
        *,
        policy: StoragePolicy | None = None,
        fence: AuthorityMarkerFence | None = None,
    ) -> None:
        from forge.api_checkpoint_channel import CheckpointStore

        try:
            self._store = CheckpointStore(Path(root), policy=policy)
        except OSError as exc:
            raise CheckpointRepositoryUnavailable(
                f"the filesystem checkpoint authority at {root} cannot be initialized: {exc}"
            ) from exc
        self._authority_fence = fence

    async def authority(self) -> str:
        return AUTHORITY_FILESYSTEM

    async def entry(self, work_id: str, checkpoint_id: str | None = None) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._store.entry, work_id, checkpoint_id)

    async def put_checkpoint(
        self,
        *,
        work_id: str,
        manifest_bytes: bytes,
        blobs: dict[str, bytes],
        sequence: int,
    ) -> dict[str, Any]:
        """The transport-level landing: the store's own result dict back.

        Same verdict shape the upload route has always answered with
        (``checkpoint_id``, ``sequence``, ``latest``, and the NEXT-06
        ``superseded`` fields when another writer owns the index lock).
        The R36-05 fence check runs FIRST — before the store's per-work
        index lock and the volume-wide reference/delete lock.
        """
        self._refuse_metadata_mutation()

        def _land() -> dict[str, Any]:
            return self._store.put_checkpoint(
                work_id=work_id,
                manifest_bytes=manifest_bytes,
                blobs=blobs,
                sequence=sequence,
            )

        try:
            return await asyncio.to_thread(_land)
        except OSError as exc:
            raise CheckpointRepositoryUnavailable(
                f"the filesystem checkpoint authority is unreachable: {exc}"
            ) from exc

    async def list_entries(self) -> list[dict[str, Any]]:
        """Every held checkpoint entry, sequence-ordered, latest-flagged."""
        return await asyncio.to_thread(self._store.list_entries)

    async def apply_retention(self, work_id: str, keep_last: int) -> int:
        """The operator's retention pass (never the ACTIVE checkpoint)."""
        self._refuse_metadata_mutation()  # R36-05: the delete family fences too
        return await asyncio.to_thread(self._store.apply_retention, work_id, keep_last)

    async def storage_health_report(self, policy: StoragePolicy | None = None) -> dict[str, Any]:
        """The store's health report — names the ACTIVE durability mode."""
        return await asyncio.to_thread(self._store.storage_health_report, policy)


class PostgresCheckpointRepository(_StoreBackedRepository):
    """The ``postgres`` authority: the ``checkpoint_metadata`` table.

    Wraps the SAME async database operations the channel module
    implements (``aput_checkpoint`` / ``aentry`` / ``alist_entries`` /
    ``aapply_retention`` / ``astorage_health_report``) over one store
    built with the postgres :class:`DurabilityContract` — the
    implementation exists ONCE, in the store; this class is the seam the
    routes and the resume producer share. The BLOBS stay
    content-addressed filesystem bytes (their writes need no lock —
    same address, same bytes), so :meth:`read_entry` is the inherited
    CAS read. A database outage on any index operation surfaces as the
    TYPED :class:`CheckpointRepositoryUnavailable` — the one honest
    answer, never ``None``, never a filesystem index consult.
    """

    _AUTHORITY_NAME = AUTHORITY_POSTGRES

    def __init__(
        self,
        root: Path | str,
        session_factory: async_sessionmaker[AsyncSession] | None,
        *,
        policy: StoragePolicy | None = None,
        fence: AuthorityMarkerFence | None = None,
    ) -> None:
        from forge.api_checkpoint_channel import CheckpointStore, DurabilityContract

        if session_factory is None:
            raise CheckpointRepositoryMisconfigured(
                "the postgres checkpoint authority needs a session factory "
                "wired to the database holding checkpoint_metadata — the "
                "repository refuses to degrade the index back to the filesystem"
            )
        try:
            self._store = CheckpointStore(
                Path(root),
                policy=policy,
                durability=DurabilityContract(mode="postgres", session_factory=session_factory),
            )
        except OSError as exc:
            raise CheckpointRepositoryUnavailable(
                f"the postgres checkpoint authority's blob root at {root} "
                f"cannot be initialized: {exc}"
            ) from exc
        #: Q35-05: whether the factory really reaches PostgreSQL (probed
        #: once) — the advisory first-upload anchor exists only there;
        #: SQLite (tests) serializes writers natively.
        self._postgres_dialect: bool | None = None
        #: R36-05: the composition-root mutation fence (None when the
        #: migration tool constructs this repository directly — its
        #: imports ARE the operator's authority-moving path).
        self._authority_fence = fence

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """The DB wiring this authority answers through (the contract's own)."""
        factory = self._store.durability.session_factory
        assert factory is not None  # the constructor refused None already
        return factory

    async def _is_postgres(self) -> bool:
        """The probed dialect of the factory's engine (cached)."""
        if self._postgres_dialect is None:
            async with self.session_factory() as session:
                bind = session.bind
                self._postgres_dialect = bind is not None and bind.dialect.name == "postgresql"
        return self._postgres_dialect

    @asynccontextmanager
    async def first_upload_lock(self, work_id: str) -> AsyncIterator[None]:
        """The per-work anchor for a work's FIRST checkpoint (Q35-05).

        ``SELECT ... FOR UPDATE`` over the work's rows is the put path's
        mutex — but a work with NO rows locks an EMPTY set, which
        PostgreSQL explicit locking does not treat as a mutex: two
        concurrent first uploads would each judge the per-work quota
        over an index neither has written yet. The anchor closes that
        gap WITHOUT a schema change: a session-scoped
        ``pg_advisory_lock`` keyed STABLY on the work id (a digest of
        the id — the same key in every process), held on one dedicated
        connection for the duration of the first landing and released
        in ``finally``. Combined with the row insert's ``ON CONFLICT DO
        NOTHING`` and the subsequent ``SELECT ... FOR UPDATE`` (inside
        the store's put), the first row becomes the durable anchor
        later puts serialize on.

        Ordering discipline: the advisory anchor is ALWAYS taken before
        the per-work pin flock (only first-puts take it, and always
        first) — no cycle exists with the flock- or row-level locks.
        """
        if not await self._is_postgres():
            # SQLite (the test approximation) serializes writers at the
            # database itself; an advisory anchor would be dead weight.
            yield
            return
        key = _work_anchor_key(work_id)
        async with self.session_factory() as session:
            async with session.begin():  # holds ONE connection for the lock
                await session.execute(_ADVISORY_LOCK_SQL, {"key": key})
                try:
                    yield
                finally:
                    await session.execute(_ADVISORY_UNLOCK_SQL, {"key": key})

    async def authority(self) -> str:
        return AUTHORITY_POSTGRES

    async def entry(self, work_id: str, checkpoint_id: str | None = None) -> dict[str, Any] | None:
        try:
            return await self._store.aentry(work_id, checkpoint_id)
        except Exception as exc:
            if _refusal(exc):
                raise
            raise _unavailable(f"entry for work {work_id!r}", exc) from exc

    async def put_checkpoint(
        self,
        *,
        work_id: str,
        manifest_bytes: bytes,
        blobs: dict[str, bytes],
        sequence: int,
    ) -> dict[str, Any]:
        """The transport-level landing: one transaction for the index.

        Delegates to the store's ``aput_checkpoint`` — the work's rows
        locked ``FOR UPDATE``, the row inserted idempotently (``ON
        CONFLICT DO NOTHING`` under the composite PK) so a racing twin's
        commit wins, on-upload retention inside the same transaction,
        blobs before the commit and doomed-blob unlinks after it.
        Q35-05 added the per-work PIN flock around that call; R36-04
        moved it INSIDE the store, nested in the VOLUME-wide
        reference/delete lock the landing now takes (a different work's
        sweep must not interleave its final scan and unlink with this
        landing's reference commit). The work's FIRST checkpoint still
        serializes through :meth:`first_upload_lock` (an empty row set
        is not a mutex) — taken BEFORE the volume lock, which no sweep
        ever waits on, so no cycle exists. R36-05: the composition-root
        FENCE check runs FIRST of all — before the first-upload anchor,
        the volume lock and the pin flock (the one documented lock
        order; see :class:`AuthorityMarkerFence`).
        """
        self._refuse_metadata_mutation()
        try:
            if await self.entry(work_id) is None:
                async with self.first_upload_lock(work_id):
                    return await self._locked_aput(work_id, manifest_bytes, blobs, sequence)
            return await self._locked_aput(work_id, manifest_bytes, blobs, sequence)
        except (CheckpointRepositoryUnavailable, CheckpointRepositoryMisconfigured):
            raise  # already typed — never re-wrapped
        except Exception as exc:
            from forge.api_checkpoint_channel import GCLockTimeout

            if isinstance(exc, GCLockTimeout):
                raise  # R36-04: recoverable, typed — the landing is re-delivered
            if _refusal(exc):
                raise
            raise _unavailable(f"put for work {work_id!r}", exc) from exc

    async def _locked_aput(
        self,
        work_id: str,
        manifest_bytes: bytes,
        blobs: dict[str, bytes],
        sequence: int,
    ) -> dict[str, Any]:
        """One landing — the store holds the volume lock and the pin flock.

        R36-04 moved BOTH synchronization layers INSIDE
        :meth:`CheckpointStore.aput_checkpoint`: the landing takes the
        VOLUME-wide reference/delete lock first, then the per-work pin
        flock nested inside it — the same single order every sweep
        takes — so a retention pass for ANY work can never interleave
        its final reference scan and unlink with this landing's
        reference commit. The repository-level pin wrapper Q35-05 put
        here would INVERT that order (pin flock outside the volume
        lock, while ``aapply_retention`` holds volume-then-pins) and
        deadlock; the guarantee it provided — a pin can never land
        inside this landing's retention recheck — is unchanged, one
        level down.
        """
        return await self._store.aput_checkpoint(
            work_id=work_id,
            manifest_bytes=manifest_bytes,
            blobs=blobs,
            sequence=sequence,
        )

    async def list_entries(self) -> list[dict[str, Any]]:
        try:
            return await self._store.alist_entries()
        except Exception as exc:
            if _refusal(exc):
                raise
            raise _unavailable("list", exc) from exc

    async def apply_retention(self, work_id: str, keep_last: int) -> int:
        self._refuse_metadata_mutation()  # R36-05: the delete family fences too
        try:
            return await self._store.aapply_retention(work_id, keep_last)
        except Exception as exc:
            from forge.api_checkpoint_channel import GCLockTimeout

            if isinstance(exc, GCLockTimeout):
                raise  # R36-04: the sweep aborts and retries later — typed, not "unreachable"
            if _refusal(exc):
                raise
            raise _unavailable(f"retention for work {work_id!r}", exc) from exc

    async def storage_health_report(self, policy: StoragePolicy | None = None) -> dict[str, Any]:
        """The same report shape, works half walked from the metadata table."""
        try:
            return await self._store.astorage_health_report(policy)
        except Exception as exc:
            if _refusal(exc):
                raise
            raise _unavailable("health report", exc) from exc


def _default_store_root() -> Path:
    """The store root the environment names (the channel's default)."""
    from forge.api_checkpoint_channel import CHECKPOINT_STORE_DIR_ENV, DEFAULT_CHECKPOINT_ROOT

    return Path(os.environ.get(CHECKPOINT_STORE_DIR_ENV, "").strip() or DEFAULT_CHECKPOINT_ROOT)


def resolve_repository(
    env: Mapping[str, str] | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    root: Path | str | None = None,
    fenced: bool = True,
) -> CheckpointRepository:
    """The ONE composition point: the configured checkpoint authority.

    Reads the deployment's immutable trio from one place — the metadata
    mode (``FORGE_CHECKPOINT_DURABILITY``), the storage root
    (``FORGE_CHECKPOINT_STORE_DIR``, default ``data/checkpoints``) and
    the session factory (strictly the CALLER's: passed in, never
    guessed from the environment here — the app passes
    ``app.state.session_factory``, the control service passes the same
    factory it derives for its mailbox) — and returns the repository
    every surface should share: the HTTP upload route, the resume
    producer, the operator reads and the retention jobs.

    R36-05: *fenced* (the DEFAULT) attaches
    :class:`AuthorityMarkerFence` to the resolved repository — the
    marker at ``<root>/migration/authority.json`` is read at
    construction (report it with :func:`authority_state_report`) and
    re-checked before every metadata mutation, so an ordinary process
    composed BEFORE a cutover refuses its next mutation AFTER the flip
    with the typed :class:`MutationsFencedError` instead of writing the
    retired authority. ``fenced=False`` is the migration tool's own
    spelling (its imports ARE the operator's authority-moving path);
    the deployed wrapper for directly constructed repositories is
    :func:`enforce_authority_marker` — ONE fence implementation shared
    by both spellings.

    Fail closed, at construction: an unknown mode value and a
    ``postgres`` selection without a session factory raise
    :class:`CheckpointRepositoryMisconfigured` with the specific
    diagnostic — the caller (app startup, the control service's
    composition) refuses to run rather than letting one surface guess
    ``best_effort`` while another honors ``postgres``, which is how
    upload and resume came to read different authorities (Q35-03).
    """
    from forge.api_checkpoint_channel import (
        CHECKPOINT_STORE_DIR_ENV,
        DEFAULT_CHECKPOINT_ROOT,
        DURABILITY_ENV,
        DURABILITY_POSTGRES,
        DurabilityContract,
    )

    source: Mapping[str, str] = os.environ if env is None else env
    try:
        mode = DurabilityContract.mode_from_env(dict(source))
    except ValueError as exc:
        raise CheckpointRepositoryMisconfigured(
            f"the checkpoint authority cannot be resolved: {exc}"
        ) from exc
    storage_root = (
        Path(root)
        if root is not None
        else Path(str(source.get(CHECKPOINT_STORE_DIR_ENV, "")).strip() or DEFAULT_CHECKPOINT_ROOT)
    )
    fence = AuthorityMarkerFence(storage_root) if fenced else None
    if mode == DURABILITY_POSTGRES:
        if session_factory is None:
            raise CheckpointRepositoryMisconfigured(
                f"{DURABILITY_ENV}=postgres needs a session factory wired to the "
                "database holding checkpoint_metadata — refusing to degrade the "
                "checkpoint authority back to the filesystem index"
            )
        return PostgresCheckpointRepository(storage_root, session_factory, fence=fence)
    return FilesystemCheckpointRepository(storage_root, fence=fence)


class SingleAuthorityRepository:
    """One repository, mutation-fenced by the deployment authority marker.

    The wrapper the migration documentation composes for repositories
    constructed DIRECTLY:
    ``enforce_authority_marker(FilesystemCheckpointRepository(...))`` —
    the standard composition (:func:`resolve_repository`) now attaches
    the SAME fence (:class:`AuthorityMarkerFence`, ONE implementation)
    at resolution time, so this wrapper is the explicit spelling for
    hand-composed instances and for wrapping a raw resolution
    (``resolve_repository(..., fenced=False)``). Mutations
    (``put``/``put_checkpoint``/``apply_retention``) are refused with
    the typed :class:`MutationsFencedError` when the marker at
    ``<store-root>/migration/authority.json`` names a DIFFERENT
    authority, or while a cutover holds the fence; immutable reads
    (``entry``, ``read``, ``read_entry``, ``pins``, listings, health)
    pass through untouched, so the old root stays readable exactly as
    documented — distinguishable through :meth:`authority`, which keeps
    naming the authority that answered. The marker is re-read on every
    mutation — a running process switches behavior when the cutover
    lands, without a restart of the guard itself. Lock order: the fence
    check precedes every store lock (see :class:`AuthorityMarkerFence`).
    """

    def __init__(self, repository: Any, root: Path | str | None = None) -> None:
        self._repository = repository
        self._root = Path(root) if root is not None else _default_store_root()
        self._fence = AuthorityMarkerFence(self._root)

    async def _refuse_if_fenced(self) -> None:
        self._fence.refuse_mutations(await self._repository.authority())

    # -- fenced mutations -----------------------------------------------------

    async def put(
        self,
        work_id: str,
        checkpoint_id: str,
        manifest_bytes: bytes,
        blobs: dict[str, bytes],
    ) -> None:
        await self._refuse_if_fenced()
        return await self._repository.put(work_id, checkpoint_id, manifest_bytes, blobs)

    async def put_checkpoint(
        self,
        *,
        work_id: str,
        manifest_bytes: bytes,
        blobs: dict[str, bytes],
        sequence: int,
    ) -> dict[str, Any]:
        await self._refuse_if_fenced()
        return await self._repository.put_checkpoint(
            work_id=work_id, manifest_bytes=manifest_bytes, blobs=blobs, sequence=sequence
        )

    async def apply_retention(self, work_id: str, keep_last: int) -> int:
        await self._refuse_if_fenced()
        return await self._repository.apply_retention(work_id, keep_last)

    # -- passthrough (reads, pins, listings) ------------------------------------

    async def entry(self, work_id: str, checkpoint_id: str | None = None) -> dict[str, Any] | None:
        return await self._repository.entry(work_id, checkpoint_id)

    async def read(self, work_id: str) -> tuple[bytes, list[bytes]] | None:
        return await self._repository.read(work_id)

    async def read_entry(self, entry: dict[str, Any]) -> tuple[bytes, dict[str, bytes]]:
        return await self._repository.read_entry(entry)

    async def authority(self) -> str:
        return await self._repository.authority()

    async def pin(self, work_id: str, checkpoint_id: str, reason: str = "") -> bool:
        return await self._repository.pin(work_id, checkpoint_id, reason)

    async def unpin(self, work_id: str, checkpoint_id: str, reason: str | None = None) -> int:
        return await self._repository.unpin(work_id, checkpoint_id, reason)

    async def pins(self, work_id: str | None = None) -> list[dict[str, Any]]:
        return await self._repository.pins(work_id)

    async def list_entries(self) -> list[dict[str, Any]]:
        return await self._repository.list_entries()

    async def storage_health_report(self, policy: Any = None) -> dict[str, Any]:
        return await self._repository.storage_health_report(policy)

    def __getattr__(self, name: str) -> Any:
        # Forward-compat passthrough (first_upload_lock and whatever the
        # protocol grows next): the guard fences mutations, never reads.
        return getattr(self._repository, name)


def enforce_authority_marker(
    repository: Any, root: Path | str | None = None
) -> SingleAuthorityRepository:
    """Wrap *repository* so exactly ONE authority accepts mutations.

    The documented deployment composition for repositories constructed
    directly: ``enforce_authority_marker(FilesystemCheckpointRepository(
    root), root)``. The marker lives at
    ``<store-root>/migration/authority.json`` and is written only by the
    migration tool's cutover/rollback under the cutover fence. The
    STANDARD composition (:func:`resolve_repository`) attaches the same
    fence by default since R36-05 — wrapping an already-fenced
    repository merely checks the marker twice, never differently. Reads
    never fence.
    """
    return SingleAuthorityRepository(repository, root)


#: R36-03: how long the HTTP lookup proxy waits for one checkpoint read
#: — bounded, so a slow control plane delays the retry decision instead
#: of freezing the event loop (the old path did a synchronous 10s GET
#: INSIDE the loop).
HTTP_LOOKUP_TIMEOUT_S: Final = 5.0

#: A bare checkpoint id must be a 64-hex content address (the same shape
#: the channel's own ``checkpoint_id`` validation uses).
_HEX64: Final = re.compile(r"^[0-9a-f]{64}$")


class HttpCheckpointRepository:
    """The authenticated checkpoint-channel proxy for processes without
    blob/database access (R36-03, issue #262).

    A lane-side or worker process that must evaluate a retry's
    continuation evidence has neither the shared blob volume nor the
    metadata database — its ONE honest window on the configured
    authority is the SAME authenticated HTTP surface the lane itself
    dials: ``GET /lane/checkpoints/{work_id}`` (the checkpoint channel
    route, guarded by :func:`forge.api_lane_control.
    authorize_work_credential`). This class is that window wrapped as
    the :class:`CheckpointLookupOutcome` contract — nothing more:

    - 200 → ``exact`` (the served ``checkpoint_id`` is the identity the
      decision pins; the channel verified every blob on the way out);
    - 404 → ``absent`` (the configured authority holds nothing);
    - 401/403 → ``unauthorized`` (the credential was refused — an
      expired or superseded attempt token is an ANSWER, not an outage);
    - 503 → ``unavailable`` (the authority itself could not answer);
    - 500 → ``corrupt`` (the channel re-hash refused the stored bytes);
    - any other status or a transport error/timeout → ``unavailable``.

    The credential is the lane's OWN derivation — never a new scheme:
    either a pre-computed work-scoped token (the dispatch-provisioned
    ``FORGE_LANE_CONTROL_TOKEN`` shape, used as-is), or the shared
    secret plus the work's CURRENT attempt generation, minted exactly
    the way the dispatch mints it
    (:func:`forge.api_lane_control.lane_control_token` with the
    generation from :func:`forge.api_lane_control.durable_run_generation`).
    MINTING THE GENERATION-LESS LEGACY TOKEN IS REFUSED: the legacy
    window's closing must not break the modern path because the modern
    path never depended on it.

    The lookup runs on :class:`httpx.AsyncClient` with a bounded
    timeout — the synchronous ``httpx.get`` inside the control event
    loop is gone. An injected *client* (tests) replaces the owned one
    and is never closed here.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str | Callable[[str], Awaitable[str]] | None = None,
        secret: str | None = None,
        generation_lookup: Callable[[str], Awaitable[int | None]] | None = None,
        timeout: float = HTTP_LOOKUP_TIMEOUT_S,
        client: Any | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._secret = (secret or "").strip() or None
        self._generation_lookup = generation_lookup
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client

    async def authority(self) -> str:
        return AUTHORITY_HTTP

    async def _work_token(self, work_id: str) -> str | None:
        """The attempt-scoped credential for ONE work, or ``None``.

        ``None`` is the honest "cannot mint" — the caller answers
        ``unavailable`` with the reason, never a legacy token and never
        a guess.
        """
        if isinstance(self._token, str):
            return self._token
        if callable(self._token):
            token = await self._token(work_id)
            return str(token) if token else None
        if self._secret:
            if self._generation_lookup is None:
                return None  # no generation authority → no attempt-scoped mint
            from forge.api_lane_control import lane_control_token

            generation = await self._generation_lookup(work_id)
            if generation is None:
                return None
            return lane_control_token(self._secret, work_id, generation=generation)
        return None

    async def lookup_outcome(self, work_id: str) -> CheckpointLookupOutcome:
        """One authenticated presence lookup against the channel route."""
        import httpx

        started = time.monotonic()
        token = await self._work_token(work_id)
        if token is None:
            return CheckpointLookupOutcome.missing(
                LOOKUP_UNAVAILABLE,
                authority=AUTHORITY_HTTP,
                detail=(
                    "no attempt-scoped lane credential is configured for the "
                    "checkpoint-channel lookup (FORGE_LANE_CONTROL_TOKEN, or "
                    "FORGE_LANE_CONTROL_SECRET plus the durable generation "
                    "authority) — the modern path refuses the legacy "
                    "generation-less token rather than guessing"
                ),
                latency_s=time.monotonic() - started,
            )
        url = f"{self._base_url}/lane/checkpoints/{work_id}"
        headers = {"Authorization": f"Bearer {token}"}
        try:
            if self._client is not None:
                response = await self._client.get(url, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            return CheckpointLookupOutcome.missing(
                LOOKUP_UNAVAILABLE,
                authority=AUTHORITY_HTTP,
                detail=f"the checkpoint channel at {self._base_url} is unreachable: {exc}",
                latency_s=time.monotonic() - started,
            )
        latency = time.monotonic() - started
        status = response.status_code
        if status == 200:
            try:
                document = response.json()
            except ValueError as exc:
                return CheckpointLookupOutcome.missing(
                    LOOKUP_CORRUPT,
                    authority=AUTHORITY_HTTP,
                    detail=f"the checkpoint channel answered with non-JSON content: {exc}",
                    latency_s=latency,
                )
            checkpoint_id = str(document.get("checkpoint_id") if isinstance(document, dict) else "")
            if not _HEX64.fullmatch(checkpoint_id):
                return CheckpointLookupOutcome.missing(
                    LOOKUP_CORRUPT,
                    authority=AUTHORITY_HTTP,
                    detail=(
                        "the checkpoint channel answered 200 without a valid "
                        f"checkpoint_id ({checkpoint_id!r})"
                    ),
                    latency_s=latency,
                )
            return CheckpointLookupOutcome.exact(
                checkpoint_id, authority=AUTHORITY_HTTP, latency_s=latency
            )
        if status == 404:
            return CheckpointLookupOutcome.missing(
                LOOKUP_ABSENT,
                authority=AUTHORITY_HTTP,
                detail="the checkpoint channel holds no checkpoint for this work",
                latency_s=latency,
            )
        if status in (401, 403):
            return CheckpointLookupOutcome.missing(
                LOOKUP_UNAUTHORIZED,
                authority=AUTHORITY_HTTP,
                detail=(
                    f"the checkpoint channel refused the lane credential ({status}) — "
                    "an expired or superseded attempt token is an answer, not an outage"
                ),
                latency_s=latency,
            )
        if status == 503:
            return CheckpointLookupOutcome.missing(
                LOOKUP_UNAVAILABLE,
                authority=AUTHORITY_HTTP,
                detail="the checkpoint channel reports the checkpoint authority unavailable",
                latency_s=latency,
            )
        if status == 500:
            return CheckpointLookupOutcome.missing(
                LOOKUP_CORRUPT,
                authority=AUTHORITY_HTTP,
                detail=(
                    "the checkpoint channel failed the verified read of the "
                    "stored checkpoint (rotted bytes or a broken index row)"
                ),
                latency_s=latency,
            )
        return CheckpointLookupOutcome.missing(
            LOOKUP_UNAVAILABLE,
            authority=AUTHORITY_HTTP,
            detail=f"the checkpoint channel answered an unexpected {status}",
            latency_s=latency,
        )


def resolve_checkpoint_lookup_authority(
    env: Mapping[str, str] | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    root: Path | str | None = None,
) -> CheckpointRepository | HttpCheckpointRepository | None:
    """The R36-03 selection matrix for the RETRY/REVIVAL lookup chain.

    One honest authority per process, selected in order:

    1. a session factory is wired → :func:`resolve_repository` — the
       SAME single composition point upload, resume and operations use
       (``FORGE_CHECKPOINT_DURABILITY`` honored; a misconfiguration
       raises here exactly as it does on every other surface);
    2. no factory, but a control URL and a lane credential are
       configured (``FORGE_LANE_CONTROL_URL`` plus
       ``FORGE_LANE_CONTROL_TOKEN``, or ``FORGE_LANE_CONTROL_SECRET``
       when the factory-less process still has a generation authority —
       which it does not, so the token shape is the realistic one) →
       the :class:`HttpCheckpointRepository` proxy;
    3. neither → ``None``: the caller answers the TYPED ``unavailable``
       — never the filesystem index, never "absent".

    Deliberately NOT selected: a bare filesystem index read for a
    factory-less process (the authority a worker has no contract with —
    the exact defect this resolves) and the legacy work-only HTTP token
    (see :func:`forge.runs.revival.durable_checkpoint_outcome` for the
    explicitly opt-in legacy adapter).
    """
    source: Mapping[str, str] = os.environ if env is None else env
    if session_factory is not None:
        return resolve_repository(env=dict(source), session_factory=session_factory, root=root)
    base_url = str(source.get("FORGE_LANE_CONTROL_URL", "")).strip()
    if not base_url:
        return None
    token = str(source.get("FORGE_LANE_CONTROL_TOKEN", "")).strip()
    if token:
        return HttpCheckpointRepository(base_url=base_url, token=token)
    secret = str(source.get("FORGE_LANE_CONTROL_SECRET", "")).strip()
    if secret:
        # A generation lookup needs the durable authority — a factory-less
        # process has none, so the secret alone cannot mint the modern
        # attempt-scoped credential; the HTTP proxy is still returned and
        # answers the typed "unavailable" with the exact reason.
        return HttpCheckpointRepository(base_url=base_url, secret=secret)
    return None


# ---------------------------------------------------------------------------
# R36-21 (issue #280): backup/restore of metadata AND blobs together
# ---------------------------------------------------------------------------

#: The backup manifest document's schema — one consistent store state,
#: metadata half and blob half captured together, verifiable on restore.
BACKUP_MANIFEST_SCHEMA: Final = "forge.checkpoint.backup/1"

#: The subdirectories of a store root that ARE durable state (the lock
#: files, ``*.lock`` / ``cas-refs.lock``, and the migration cutover fence
#: are transient coordination artifacts and are deliberately NOT backed
#: up; the authority MARKER under ``migration/`` IS durable — it names
#: which authority owns the metadata).
_BACKUP_DIRS: Final = ("pins", "gc", "migration")

#: Where the exported ``checkpoint_metadata`` rows live inside a backup
#: (the postgres authority's metadata half; absent in filesystem backups).
_BACKUP_METADATA_ROWS: Final = "checkpoint_metadata.jsonl"


class BackupMismatchError(Exception):
    """A backup's metadata and blob halves do not describe ONE store state.

    Raised by :func:`restore_store` (and pre-checkable through
    :func:`verify_backup_consistency`) when a checkpoint the metadata
    half names references bytes the blob half does not carry — the
    shape an operator reassembling a backup from different moments
    (metadata taken at t2, blobs taken at t1) produces. The restore
    REFUSES before writing anything into the target: a restored index
    entry whose bytes are missing is a work that looks resumable and
    is not, exactly the silent corruption this typing exists to
    prevent. :attr:`affected` lists every offending
    ``(work_id, checkpoint_id, missing digests)`` so the operator sees
    WHICH works the mismatch touches.
    """

    def __init__(self, affected: list[dict[str, Any]]) -> None:
        works = ", ".join(sorted({str(item.get("work_id")) for item in affected}))
        super().__init__(
            f"the backup's metadata and blob halves disagree for work(s) {works}: "
            f"{len(affected)} checkpoint(s) name bytes the blob half does not carry "
            f"({'; '.join(str(item.get('work_id')) + '/' + str(item.get('checkpoint_id'))[:12] for item in affected[:5])}"
            f"{'…' if len(affected) > 5 else ''}) — restore refused; re-take the "
            "backup so both halves describe one consistent store state"
        )
        self.affected = affected


@dataclass(frozen=True)
class StoreBackup:
    """One captured store state: metadata AND blobs together (R36-21).

    :attr:`path` is the backup directory (self-contained — safe to move
    off-host); :attr:`created_at` its capture time. The counts are the
    coverage facts the restore drill reports
    (``backup.restore_coverage``): how many works/checkpoints/blobs/pins
    and pending-GC records were captured, plus the exported
    ``checkpoint_metadata`` row count (0 for filesystem-authority
    backups, whose metadata lives in ``works/``).
    """

    path: Path
    created_at: str
    works: int
    checkpoints: int
    blobs: int
    pins: int
    pending_gc: int
    metadata_rows: int

    def manifest(self) -> dict[str, Any]:
        """The backup's own manifest document (a fresh read from disk)."""
        try:
            document = json.loads((self.path / "backup.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return document if isinstance(document, dict) else {}


def _blob_half_has(path: Path, digest: str) -> bool:
    """Whether the blob half at *path* carries *digest* (64-hex only)."""
    return bool(_HEX64.fullmatch(digest)) and (path / digest[:2] / digest).is_file()


def _metadata_entries(root: Path) -> dict[str, list[dict[str, Any]]]:
    """The filesystem half's index entries: work id → entry dicts."""
    works: dict[str, list[dict[str, Any]]] = {}
    index_dir = root / "works"
    for path in sorted(index_dir.glob("*.json")) if index_dir.is_dir() else []:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(document, dict) or not isinstance(document.get("checkpoints"), list):
            continue
        entries = [e for e in document["checkpoints"] if isinstance(e, dict)]
        if entries:
            works[path.stem] = entries
    return works


def _exported_rows(root: Path) -> list[dict[str, Any]]:
    """The postgres half's exported ``checkpoint_metadata`` rows."""
    path = root / _BACKUP_METADATA_ROWS
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            document = json.loads(line)
        except ValueError:
            continue
        if isinstance(document, dict) and isinstance(document.get("work_id"), str):
            rows.append(document)
    return rows


def verify_backup_consistency(backup: StoreBackup | Path) -> list[dict[str, Any]]:
    """Check a backup's halves describe ONE state; list every mismatch.

    For EVERY checkpoint the metadata half names — filesystem index
    entries AND exported ``checkpoint_metadata`` rows — the blob half
    must carry the checkpoint's manifest bytes AND every digest that
    manifest references. Anything missing is returned as
    ``{"work_id", "checkpoint_id", "missing": [digests]}``; an empty
    list is a consistent backup. :func:`restore_store` runs exactly
    this check FIRST and refuses with :class:`BackupMismatchError`
    before writing anything.
    """

    root = Path(backup.path) if isinstance(backup, StoreBackup) else Path(backup)
    affected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    named: list[tuple[str, dict[str, Any]]] = [
        (work, entry) for work, entries in _metadata_entries(root).items() for entry in entries
    ]
    named.extend(
        (str(row["work_id"]), row) for row in _exported_rows(root)
    )  # exported row: checkpoint_id at top level
    for work_id, entry in named:
        checkpoint_id = str(entry.get("checkpoint_id") or "")
        if not checkpoint_id or (work_id, checkpoint_id) in seen:
            continue
        seen.add((work_id, checkpoint_id))
        missing: list[str] = []
        if not _blob_half_has(root, checkpoint_id):
            missing.append(checkpoint_id)
            # The manifest bytes are gone with it — the closure cannot be
            # checked; report the manifest address alone.
            affected.append(
                {"work_id": work_id, "checkpoint_id": checkpoint_id, "missing": missing}
            )
            continue
        try:
            manifest = json.loads((root / checkpoint_id[:2] / checkpoint_id).read_text("utf-8"))
        except (OSError, ValueError):
            affected.append(
                {"work_id": work_id, "checkpoint_id": checkpoint_id, "missing": [checkpoint_id]}
            )
            continue
        files = manifest.get("files") if isinstance(manifest, dict) else None
        if isinstance(files, dict):
            for name, spec in sorted(files.items()):
                digest = str(spec.get("digest") or "") if isinstance(spec, dict) else ""
                if digest and not _blob_half_has(root, digest):
                    missing.append(digest)
        if missing:
            affected.append(
                {"work_id": work_id, "checkpoint_id": checkpoint_id, "missing": missing}
            )
    return affected


def _cas_shards(root: Path) -> list[Path]:
    """The two-hex CAS shard directories of a store root (``00``..``ff``)."""
    return sorted(p for p in root.iterdir() if p.is_dir() and _HEX2_DIR(p.name))


def _HEX2_DIR(name: str) -> bool:
    """Whether *name* is a two-hex CAS shard directory name."""
    return len(name) == 2 and all(c in "0123456789abcdef" for c in name)


def _count_blobs(root: Path) -> int:
    """How many CAS files (blobs + manifests) a store half carries."""
    return sum(1 for shard in _cas_shards(root) for blob in shard.glob("*") if blob.is_file())


def _ignore_locks(_dir: str, names: list[str]) -> list[str]:
    """The copytree filter: transient ``flock`` files are never state."""
    return [name for name in names if name.endswith(".lock")]


async def backup_store(
    root: Path | str,
    target: Path | str | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> StoreBackup:
    """Capture ONE consistent store state — metadata AND blobs together.

    Copies the durable halves of the store root — the CAS blobs
    (``<root>/<xx>/<digest>``), the filesystem index (``works/``), the
    pin overlay (``pins/``), the pending-GC journal (``gc/``) and the
    authority marker (``migration/authority.json``) — into *target* (a
    sibling ``<root>-backup-<pid>-<n>`` directory under the root's
    parent when omitted). Lock files are deliberately excluded: they
    are transient coordination artifacts, never state. When
    *session_factory* is given, the postgres authority's metadata half
    (the ``checkpoint_metadata`` table) is exported to
    ``checkpoint_metadata.jsonl`` inside the backup — a postgres store
    backed up WITHOUT its factory would silently miss its entire
    index, so pass the factory the repository was composed with.

    The backup is taken WITHOUT quiescing writers: the blob writes and
    the index commits are each atomic, so the copy observes either the
    before or the after of any in-flight landing, never a torn entry
    (the same discipline the crash windows rely on). For a
    point-in-time-guaranteed copy, quiesce uploads first — the
    operations drill documents this as the runbook step.
    """

    import shutil

    source = Path(root)
    if not source.is_dir():
        raise CheckpointRepositoryUnavailable(
            f"cannot back up the checkpoint store at {source}: the root does not exist"
        )
    destination: Path | None = Path(target) if target is not None else None
    if destination is None:
        counter = 0
        while True:
            candidate = source.parent / f"{source.name}-backup-{os.getpid()}-{counter}"
            if not candidate.exists():
                destination = candidate
                break
            counter += 1
    destination.mkdir(parents=True, exist_ok=False)

    for name in _BACKUP_DIRS:
        if (source / name).is_dir():
            await asyncio.to_thread(
                shutil.copytree,
                source / name,
                destination / name,
                ignore=_ignore_locks,
                dirs_exist_ok=True,
            )
    if (source / "works").is_dir():
        await asyncio.to_thread(
            shutil.copytree,
            source / "works",
            destination / "works",
            ignore=_ignore_locks,
            dirs_exist_ok=True,
        )
    for shard in _cas_shards(source):
        await asyncio.to_thread(
            shutil.copytree, shard, destination / shard.name, dirs_exist_ok=True
        )

    exported = 0
    if session_factory is not None:
        from sqlalchemy import select

        from forge.api_checkpoint_channel import CheckpointMetadataRow

        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(CheckpointMetadataRow).order_by(
                            CheckpointMetadataRow.work_id,
                            CheckpointMetadataRow.checkpoint_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        lines = [
            json.dumps(
                {
                    "work_id": row.work_id,
                    "checkpoint_id": row.checkpoint_id,
                    "sequence": int(row.sequence),
                    "files": int(row.files),
                    "uploaded_at": str(row.uploaded_at or ""),
                },
                sort_keys=True,
            )
            for row in rows
        ]
        (destination / _BACKUP_METADATA_ROWS).write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
        )
        exported = len(lines)

    index_works = _metadata_entries(destination)
    backup = StoreBackup(
        path=destination,
        created_at=_now_iso(),
        works=len(index_works),
        checkpoints=sum(len(entries) for entries in index_works.values()) + exported,
        blobs=_count_blobs(destination),
        pins=len(list((destination / "pins").glob("*.json")))
        if (destination / "pins").is_dir()
        else 0,
        pending_gc=len(list((destination / "gc").glob("*.json")))
        if (destination / "gc").is_dir()
        else 0,
        metadata_rows=exported,
    )
    (destination / "backup.json").write_text(
        json.dumps(
            {
                "schema": BACKUP_MANIFEST_SCHEMA,
                "created_at": backup.created_at,
                "source_root": str(source),
                "works": backup.works,
                "checkpoints": backup.checkpoints,
                "blobs": backup.blobs,
                "pins": backup.pins,
                "pending_gc": backup.pending_gc,
                "metadata_rows": backup.metadata_rows,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return backup


async def restore_store(
    backup: StoreBackup | Path,
    target: Path | str,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict[str, Any]:
    """Restore a backup into *target* — verifying it FIRST (R36-21).

    The order is the safety property: :func:`verify_backup_consistency`
    runs over the backup BEFORE anything is written, and a metadata
    half naming bytes the blob half does not carry REFUSES with
    :class:`BackupMismatchError` listing the affected works — the
    mismatched-halves restore (metadata from t2, blobs from t1) is
    never silently accepted, and a refused restore leaves the target
    untouched. With the halves consistent, the CAS blobs, the
    filesystem index (``works/``), the pin overlay (``pins/``), the
    pending-GC journal (``gc/``) and the authority marker are copied
    into *target*, and — when the backup carries exported
    ``checkpoint_metadata`` rows and a *session_factory* is given —
    the rows are re-imported idempotently (an existing row with the
    same ``(work_id, checkpoint_id)`` is skipped). Returns the
    restored coverage counts (the ``backup.restore_coverage`` signal).
    """

    import shutil

    source = Path(backup.path) if isinstance(backup, StoreBackup) else Path(backup)
    destination = Path(target)
    mismatches = verify_backup_consistency(source)
    if mismatches:
        raise BackupMismatchError(mismatches)
    destination.mkdir(parents=True, exist_ok=True)
    for name in (*_BACKUP_DIRS, "works"):
        if (source / name).is_dir():
            await asyncio.to_thread(
                shutil.copytree, source / name, destination / name, dirs_exist_ok=True
            )
    for shard in _cas_shards(source):
        await asyncio.to_thread(
            shutil.copytree, shard, destination / shard.name, dirs_exist_ok=True
        )

    imported = 0
    rows = _exported_rows(source)
    if rows and session_factory is not None:
        from forge.api_checkpoint_channel import CheckpointMetadataRow

        async with session_factory() as session:
            for row in rows:
                existing = await session.get(
                    CheckpointMetadataRow, (row["work_id"], row["checkpoint_id"])
                )
                if existing is not None:
                    continue
                session.add(
                    CheckpointMetadataRow(
                        work_id=str(row["work_id"]),
                        checkpoint_id=str(row["checkpoint_id"]),
                        sequence=int(row.get("sequence") or 0),
                        files=int(row.get("files") or 0),
                        uploaded_at=str(row.get("uploaded_at") or ""),
                    )
                )
                imported += 1
            await session.commit()
    index_works = _metadata_entries(destination)
    return {
        "works": len(index_works) or (1 if rows else 0),
        "checkpoints": sum(len(entries) for entries in index_works.values()) + imported,
        "blobs": _count_blobs(destination),
        "pins": len(list((destination / "pins").glob("*.json")))
        if (destination / "pins").is_dir()
        else 0,
        "pending_gc": len(list((destination / "gc").glob("*.json")))
        if (destination / "gc").is_dir()
        else 0,
        "metadata_rows": imported,
    }
