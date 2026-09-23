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
import os
import random
import re
import socket
import tempfile
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
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

__all__ = [
    "AUTHORITY_FILESYSTEM",
    "AUTHORITY_POSTGRES",
    "CheckpointGcJournal",
    "CheckpointPins",
    "CheckpointRepository",
    "CheckpointRepositoryMisconfigured",
    "CheckpointRepositoryUnavailable",
    "FilesystemCheckpointRepository",
    "PostgresCheckpointRepository",
    "resolve_repository",
]

#: The authority names :meth:`CheckpointRepository.authority` answers.
#: The filesystem spelling names the ``best_effort`` durability's index;
#: the postgres spelling names the ``checkpoint_metadata`` table.
AUTHORITY_FILESYSTEM: Final = "filesystem"
AUTHORITY_POSTGRES: Final = "postgres"


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
    """

    _store: CheckpointStore

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

    def __init__(self, root: Path | str, *, policy: StoragePolicy | None = None) -> None:
        from forge.api_checkpoint_channel import CheckpointStore

        try:
            self._store = CheckpointStore(Path(root), policy=policy)
        except OSError as exc:
            raise CheckpointRepositoryUnavailable(
                f"the filesystem checkpoint authority at {root} cannot be initialized: {exc}"
            ) from exc

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
        """

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

    def __init__(
        self,
        root: Path | str,
        session_factory: async_sessionmaker[AsyncSession] | None,
        *,
        policy: StoragePolicy | None = None,
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
        Q35-05 wraps that call with the two serialization layers the
        store alone cannot provide: the per-work PIN flock (a pin can
        never land inside this landing's retention recheck) and, for
        the work's FIRST checkpoint, :meth:`first_upload_lock` (an
        empty row set is not a mutex).
        """
        try:
            if await self.entry(work_id) is None:
                async with self.first_upload_lock(work_id):
                    return await self._locked_aput(work_id, manifest_bytes, blobs, sequence)
            return await self._locked_aput(work_id, manifest_bytes, blobs, sequence)
        except (CheckpointRepositoryUnavailable, CheckpointRepositoryMisconfigured):
            raise  # already typed — never re-wrapped
        except Exception as exc:
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
        """One landing under the per-work pin flock (see :meth:`put_checkpoint`)."""
        async with self._store.pins.alock(work_id):
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
        try:
            return await self._store.aapply_retention(work_id, keep_last)
        except Exception as exc:
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


def resolve_repository(
    env: Mapping[str, str] | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    root: Path | str | None = None,
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
    if mode == DURABILITY_POSTGRES:
        if session_factory is None:
            raise CheckpointRepositoryMisconfigured(
                f"{DURABILITY_ENV}=postgres needs a session factory wired to the "
                "database holding checkpoint_metadata — refusing to degrade the "
                "checkpoint authority back to the filesystem index"
            )
        return PostgresCheckpointRepository(storage_root, session_factory)
    return FilesystemCheckpointRepository(storage_root)
