"""LIVE cross-runner checkpoint transport — the control-plane side (wave C/D).

The lane runner's checkpoint (:mod:`forge.adaptive.checkpointing`) is a
versioned manifest plus content blobs in a store rooted on the LANE
JOB's filesystem; this router is the durable counterpart a SECOND
runner restores from. Three endpoints, one storage discipline:

- ``PUT /lane/checkpoints/{work_id}`` — accept a checkpoint (JSON-base64
  manifest + blobs), bound the ALLOCATION before any expansion
  (NEXT-05): the ``Content-Length`` header is read FIRST and refused
  with 413 over ``FORGE_CHECKPOINT_MAX_REQUEST_BYTES`` (default 512 MiB)
  before one body byte is read, then the raw body itself is streamed in
  chunks and refused mid-stream the moment the cap is exceeded — a
  chunked request with a lying or absent header still never
  materializes more than ``cap + chunk`` bytes — and only THEN is the
  JSON parsed and every promise verified against the bytes (the
  manifest must hash to its own content address, belong to the path's
  work, every blob must reproduce its digest — a tampered upload is
  refused with 400 naming the blob), the per-blob size cap
  (413, an honest refusal), the aggregate decoded-size and entry-count
  caps enforced, and the blob set required to be EXACTLY the manifest's
  referenced digests — extra path-shaped or otherwise malformed keys
  are refused BEFORE any write (R28-01) — then everything lands in a
  content-addressed directory via atomic temp+rename so a crash
  mid-write never exposes a partial artifact.
- ``GET /lane/checkpoints/{work_id}`` — serve the work's latest
  checkpoint (or a specific ``checkpoint_id``), digest-verified ON READ:
  bytes that no longer hash to their address answer 500 naming the
  address, they are never served as if they were the checkpoint.
- ``GET /lane/checkpoints`` — the operator surface: every held
  checkpoint reference with its work, sequence and latest flag.

Authentication is the lane control scheme's (NEXT-01): while
``FORGE_LANE_CONTROL_SECRET`` is unset every endpoint answers 503 —
the channel is disabled, fail closed. With the secret set, the work
endpoints (upload/download) authenticate through the SAME
attempt-credential ladder the lane-control API uses
(:func:`forge.api_lane_control.authorize_work_credential` — one token
derivation, one durable generation authority, one migration deadline):
the dispatch-issued GENERATION-SCOPED token for the run's current
attempt, or the legacy work-scoped HMAC inside the
``FORGE_LANE_LEGACY_TOKEN_DEADLINE`` window only; a superseded
generation's token is refused naming both generations. A token minted
for ANOTHER work is refused with 401 — work-scoped means exactly that.
The operator surfaces (list, health) keep the ``checkpoints:list``
scope — an operator credential, no attempt generation attached.

Storage lives under ``FORGE_CHECKPOINT_STORE_DIR`` (default
``data/checkpoints``) in the same fan-out shape the local artifact
store uses (``<first2>/<digest>``), plus a per-work index
(``works/<work_id>.json``, also written atomically) that orders the
work's checkpoints. The index's read-modify-write runs under an
exclusive per-work ``flock`` acquired NON-BLOCKING with a bounded,
jittered retry (NEXT-06): the holder's identity
(``hostname|pid|acquired_at``) is recorded INSIDE the lock file for
post-mortem, and a writer that cannot take the lock within
``FORGE_CHECKPOINT_LOCK_WAIT_SECONDS`` (default 5) loses honestly —
its upload is reported as superseded history (the other writer owns
the index) instead of blocking the API forever. Retention
(:meth:`CheckpointStore.apply_retention`, driven by
``FORGE_CHECKPOINT_RETENTION`` — 0 keeps everything) drops the OLDEST
checkpoints beyond the keep count and can be asked to keep nothing —
and still NEVER deletes a work's LATEST checkpoint: the one a live
pause stands on always resolves. Deleted checkpoints release only
blobs no retained checkpoint of ANY work still references. The
retention DECISION is recorded in the index (keep count, holder,
pending-GC digests) so a concurrent or post-crash re-run sees the last
decision, does not re-delete, and can COMPLETE a garbage collection
interrupted between the index update and the blob unlink.

R28-14 folds every cap and cleanup rule into ONE
:class:`StoragePolicy` — the per-blob cap, the per-upload manifest
entry cap, the per-work TOTAL BYTES quota, the per-work retention cap
and the disaster-recovery history floor (how many superseded
checkpoints survive cleanup as rollback targets) — built from the
environment (:meth:`StoragePolicy.from_env`) and enforced by the
STORE, before any write, as defense in depth behind the endpoint's
own checks. An over-quota upload is refused with 413 and leaves the
work's previous checkpoint byte-identical. The operator surface is
:meth:`CheckpointStore.storage_health_report` (also
``GET /lane/checkpoints/health`` and the module-level
:func:`storage_health_report` for doctor): disk usage, per-work
checkpoint counts and bytes, works over quota, and ORPHAN CAS entries
— content present on disk that no work's index references.

R32-16 (review 0fca1b7) names the deployment-specific DURABILITY
contract the pilot was missing: retention and active selection living
in filesystem JSON is fine for ONE shared-volume API process, but a
second replica — or blobs moving to object storage — makes that index
a single-writer fiction. :class:`DurabilityContract` (selected by
``FORGE_CHECKPOINT_DURABILITY``) makes the choice explicit:
``best_effort`` (the default, the filesystem index above) or
``postgres`` — the checkpoint INDEX moves to the
``checkpoint_metadata`` table (transactional puts under a
``SELECT ... FOR UPDATE`` on the work's rows, derived active
selection, retention inside the same transaction) while the BLOBS stay
content-addressed filesystem bytes whose writes need no lock (same
address, same bytes — idempotent by construction). The health report
names the active mode so an operator never mistakes one contract for
the other.

Q35-03 (review c7ae8db): every ROUTE now delegates to ONE
:class:`forge.adaptive.checkpoint_repository.CheckpointRepository`
resolved by :func:`forge.adaptive.checkpoint_repository.
resolve_repository` — the SAME configured authority the resume
producer (:mod:`forge.adaptive.wiring`) reads, so upload and resume
can never consult different stores. The async ``a*`` index operations
below remain the single implementation of the postgres contract; the
repository classes wrap them (no second copy), map a database outage
to the typed
``CheckpointRepositoryUnavailable`` (503 here — never 404, never a
filesystem fallback) and refuse half-configurations at construction.

Q35-05 (review c7ae8db, probe P03): retention is a TWO-PHASE
mark/recheck/sweep pass under BOTH durability contracts. The old pass
computed a deletion digest set from the CURRENT references, committed
the metadata deletion, and only then unlinked the CAS blobs — a work B
that added a NEW reference to a shared blob between the scan and the
unlink was left holding an index entry whose bytes were gone. Now:

- **mark** proposes candidate digests (past the horizon, protected by
  no consumer pin) — a proposal, held in memory as a tombstone
  (filesystem: the index's ``pending_gc`` record; postgres: the
  :class:`~forge.adaptive.checkpoint_repository.CheckpointGcJournal`
  overlay) with NO deletion yet;
- **recheck** re-derives the live reference set INSIDE the transaction
  (filesystem: the same lock section) that performs the deletion — a
  fresh statement that sees references committed since the mark (the
  P03 schedule), plus a fresh pin read; any digest that gained a new
  reference or a new pin leaves the deletion set. The new reference's
  commit wins the race;
- **sweep** unlinks only the survivors, idempotently — a missing blob
  is already gone, so an interrupted sweep re-runs to convergence (a
  failed transaction deletes nothing owned by committed references:
  rows commit FIRST, blobs second, journal in between).

Consumer protection is explicit (``CheckpointRepository.pin``/``unpin``
— an authorized resume PINS its exact checkpoint; pins are released
explicitly, never by time), and a work's FIRST upload serializes on a
stable per-work anchor (a ``pg_advisory_lock`` keyed on the work id
plus the row's ``INSERT ... ON CONFLICT DO NOTHING`` and ``SELECT ...
FOR UPDATE``) because ``FOR UPDATE`` over an empty row set is not a
mutex. Per-work quotas count REFERENCED bytes: a deduplicated blob
another work already stored still counts toward THIS work when the
work newly references it. Orphan recovery: content present on disk
that no index entry references is named by the health report
(``orphan_cas_entries``) and is collectable — the GC journal/index
pending record covers the commit-to-unlink crash window, and crash
recovery between blob write and index commit leaves exactly such
orphans, never a reference without bytes.

R36-04 (review ``16339c2``, probe P04) closes the window Q35-05's
recheck could not: the recheck's SELECT only ever sees references
committed BEFORE it ran, so a DIFFERENT work's landing — serialized
against the collector on a different per-work lock — could still
commit a reference to a shared digest between the collector's FINAL
reference scan and its unlink, leaving an acknowledged reference
without bytes. READ COMMITTED gives statement-time snapshots, not a
prohibition on future references; moving the SELECT closer to the
unlink is not a fix. The protocol that IS the fix — ONE lock per CAS
volume, shared by writers and deleters:

- **the lock** — ``<root>/cas-refs.lock`` (an exclusive ``flock``,
  bounded jittered retry, :class:`GCLockTimeout` on exhaustion), plus
  a postgres ``pg_advisory_lock`` twin on ONE constant key for
  deployments whose blob root is NOT shared but whose database is:
  either half alone serializes reference acquisition against
  deletion; both are taken, flock first, so mixed topologies cannot
  cycle.
- **writers** take it for the REFERENCE-RECORDING transaction only —
  the filesystem index read-modify-write, or the metadata row
  transaction — NOT for the blob writes (they are idempotent and
  content-addressed; the volume must not serialize every upload's
  fsyncs). Under the lock the landing REPAIRS first: any closure
  address a sweep unlinked while the blobs were being written outside
  the lock is re-landed BEFORE the reference commits, so a committed
  reference always finds its bytes and a crash still leaves only
  collectable orphans.
- **deleters** (explicit retention, on-upload cleanup, pending-GC
  recovery) hold it from their FINAL reference scan through the last
  unlink: lock → final scan → delete-metadata transaction → unlink →
  unlock. A reference therefore cannot commit inside that interval —
  its writer is queued on the same lock — and a writer queued behind
  a sweep re-lands what the sweep was entitled to delete.
- **ordering** (why this cannot deadlock): the volume lock is always
  taken BEFORE the per-work pin flock and the metadata rows, and
  AFTER the per-work index lock and the first-upload advisory anchor —
  one global order, no cycle. Two concurrent collectors and a
  concurrent writer serialize; every wait is bounded by
  ``FORGE_CHECKPOINT_GC_LOCK_WAIT_SECONDS`` (default 30s), a timed-out
  sweep aborts with its marks standing (a later pass completes them)
  and a timed-out landing fails with nothing committed.
- **rollout** — ``FORGE_CHECKPOINT_SWEEP=off`` fences the unlink half
  of every GC pass (both durability authorities) while the marks
  stand: metadata is deleted, tombstones and journal records persist,
  bytes stay. Turning sweeping back on collects the marked set
  against CURRENT reachability — the ordinary pending-GC completion.

This is deliberately the CONSERVATIVE first implementation the issue
asks for: one volume-wide mutex. Refinement to per-digest ownership
(or tombstoned handoff) is a measured-load question — the protocol's
seams (``_cas_refs_lock``/``_acas_refs_lock``, ``_sweep_locked``/
``_asweep_locked``) are the only places such a refinement would land.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import os
import random
import re
import socket
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import Integer, String, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

try:  # POSIX process-level advisory locking (Linux CI, macOS dev boxes).
    import fcntl
except ImportError:  # pragma: no cover — non-POSIX platform without flock
    fcntl = None  # type: ignore[assignment]

from forge.adaptive.checkpoint_channel import (
    CHECKPOINT_LIST_SCOPE,
    FORGE_LANE_CONTROL_SECRET_ENV,
    work_scoped_token,
)
from forge.adaptive.checkpoint_repository import (
    CheckpointGcJournal,
    CheckpointPins,
    CheckpointRepository,
    CheckpointRepositoryMisconfigured,
    CheckpointRepositoryUnavailable,
    resolve_repository,
)
from forge.adaptive.checkpointing import MANIFEST_SCHEMA
from forge.models.base import Base

__all__ = [
    "CHECKPOINT_RETENTION_ENV",
    "CHECKPOINT_STORE_DIR_ENV",
    "DEFAULT_CHECKPOINT_ROOT",
    "DEFAULT_MAX_BLOB_BYTES",
    "DEFAULT_MAX_BLOB_ENTRIES",
    "DEFAULT_MAX_REQUEST_BYTES",
    "DEFAULT_MAX_TOTAL_BLOB_BYTES",
    "DEFAULT_MAX_WORK_TOTAL_BYTES",
    "DEFAULT_HISTORY_KEEP",
    "DEFAULT_LOCK_WAIT_SECONDS",
    "DEFAULT_GC_LOCK_WAIT_SECONDS",
    "DURABILITY_BEST_EFFORT",
    "DURABILITY_ENV",
    "DURABILITY_MODES",
    "DURABILITY_POSTGRES",
    "GC_LOCK_WAIT_SECONDS_ENV",
    "GCLockTimeout",
    "HISTORY_KEEP_ENV",
    "LANE_CONTROL_SECRET_ENV",
    "LOCK_WAIT_SECONDS_ENV",
    "MAX_BLOB_BYTES_ENV",
    "MAX_BLOB_ENTRIES_ENV",
    "MAX_REQUEST_BYTES_ENV",
    "MAX_TOTAL_BLOB_BYTES_ENV",
    "MAX_WORK_TOTAL_BYTES_ENV",
    "SWEEP_ENV",
    "CheckpointCorruptError",
    "CheckpointMetadataRow",
    "CheckpointStore",
    "DurabilityContract",
    "IndexLockHeldError",
    "RetentionMark",
    "StoragePolicy",
    "StorageQuotaExceededError",
    "checkpoint_channel_router",
    "storage_health_report",
]

#: Alias naming the shared secret from THIS side of the wire too: the
#: lane control token scheme's env — read, never written, here.
LANE_CONTROL_SECRET_ENV = FORGE_LANE_CONTROL_SECRET_ENV

#: Where the durable checkpoint directory lives.
CHECKPOINT_STORE_DIR_ENV: Final = "FORGE_CHECKPOINT_STORE_DIR"
DEFAULT_CHECKPOINT_ROOT: Final = "data/checkpoints"

#: The server's per-blob size cap (manifest included). Over it the PUT
#: is refused with 413 naming blob, size and cap — honest refusal, not
#: a truncated store.
MAX_BLOB_BYTES_ENV: Final = "FORGE_CHECKPOINT_MAX_BLOB_BYTES"
DEFAULT_MAX_BLOB_BYTES: Final = 32 * 1024 * 1024

#: The server's aggregate cap (R28-01): the TOTAL decoded size of one
#: upload — manifest plus every blob — accepted before anything is
#: written. Over it the PUT is refused with 413; the channel is a
#: control-plane surface, not a bulk file transport.
MAX_TOTAL_BLOB_BYTES_ENV: Final = "FORGE_CHECKPOINT_MAX_TOTAL_BLOB_BYTES"
DEFAULT_MAX_TOTAL_BLOB_BYTES: Final = 256 * 1024 * 1024

#: The server's per-upload entry cap (R28-01): how many blob entries one
#: PUT may carry. A checkpoint is a bounded WIP delta, not an arbitrary
#: key-value dump; an over-count upload is refused with 413 before its
#: body is even decoded.
MAX_BLOB_ENTRIES_ENV: Final = "FORGE_CHECKPOINT_MAX_BLOB_ENTRIES"
DEFAULT_MAX_BLOB_ENTRIES: Final = 4096

#: NEXT-05: the ENCODED request-size bound — the allocation ceiling at
#: the network boundary, checked against ``Content-Length`` BEFORE the
#: body is read and against the streamed body itself while it streams.
#: A declared cap on the DECODED aggregate (below) is not an allocation
#: bound: ``request.json()`` and ``base64.b64decode`` materialize the
#: full payload first, so a hostile encoded body could exhaust API
#: memory before any validation ran. This bound runs FIRST; it
#: comfortably dominates the decoded aggregate cap (base64 decoding
#: only shrinks: 4 wire characters become 3 bytes).
MAX_REQUEST_BYTES_ENV: Final = "FORGE_CHECKPOINT_MAX_REQUEST_BYTES"
DEFAULT_MAX_REQUEST_BYTES: Final = 512 * 1024 * 1024

#: NEXT-06: how long a writer retries (with jitter) for the per-work
#: index lock before declaring the other writer the winner and landing
#: its upload as superseded history. 0 refuses after the first
#: non-blocking attempt.
LOCK_WAIT_SECONDS_ENV: Final = "FORGE_CHECKPOINT_LOCK_WAIT_SECONDS"
DEFAULT_LOCK_WAIT_SECONDS: Final = 5.0

#: How many checkpoints per work to keep beyond the latest (0 = all).
#: The LATEST is exempt whatever this says — see apply_retention.
CHECKPOINT_RETENTION_ENV: Final = "FORGE_CHECKPOINT_RETENTION"

#: R28-14: the per-work TOTAL bytes quota — the sum of every manifest and
#: blob the work's index entries reference. Over it (0 = no quota) a new
#: upload is refused with 413 BEFORE any write; the work's previous
#: checkpoints are untouched, so quota exhaustion is an explicit
#: recoverable state, never data loss.
MAX_WORK_TOTAL_BYTES_ENV: Final = "FORGE_CHECKPOINT_MAX_WORK_TOTAL_BYTES"
DEFAULT_MAX_WORK_TOTAL_BYTES: Final = 0

#: R28-14: the disaster-recovery history floor — how many SUPERSEDED
#: checkpoints survive cleanup as rollback targets beyond the active
#: one. Cleanup itself triggers after every successful upload
#: (:attr:`StoragePolicy.cleanup_trigger`); the floor bounds what it may
#: drop, so a bad resume can still be rolled back to recorded history.
HISTORY_KEEP_ENV: Final = "FORGE_CHECKPOINT_HISTORY_KEEP"
DEFAULT_HISTORY_KEEP: Final = 0

#: R32-16: the durability contract's env — which store backs the
#: checkpoint INDEX (retention decisions and active selection).
#: ``best_effort`` (the default) is the per-work filesystem JSON index
#: under ``flock``; ``postgres`` moves the index to the
#: ``checkpoint_metadata`` table (transactional, multi-replica) while
#: the blobs stay content-addressed filesystem bytes.
DURABILITY_ENV: Final = "FORGE_CHECKPOINT_DURABILITY"
DURABILITY_BEST_EFFORT: Final = "best_effort"
DURABILITY_POSTGRES: Final = "postgres"
DURABILITY_MODES: frozenset[str] = frozenset({DURABILITY_BEST_EFFORT, DURABILITY_POSTGRES})

#: R36-04: the operator's destructive-sweep switch. ``off`` disables the
#: CAS unlink half of every garbage-collection pass — explicit retention,
#: on-upload cleanup and pending-GC recovery — while the MARKS stand
#: (tombstones, journal records, deleted metadata rows are kept), so a
#: rollout can qualify the locking protocol with byte deletion fenced
#: off and turn collecting back on once satisfied. Any other value
#: (including unset) keeps sweeping enabled.
SWEEP_ENV: Final = "FORGE_CHECKPOINT_SWEEP"

#: R36-04: how long a writer's reference acquisition or a sweep waits
#: for the volume-wide reference/delete lock before refusing with
#: :class:`GCLockTimeout` — a sweep aborts and retries later; a writer
#: fails the landing honestly (nothing committed, the idempotent re-put
#: retries). Bounded, never a silent indefinite block.
GC_LOCK_WAIT_SECONDS_ENV: Final = "FORGE_CHECKPOINT_GC_LOCK_WAIT_SECONDS"
DEFAULT_GC_LOCK_WAIT_SECONDS: Final = 30.0

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX2 = re.compile(r"^[0-9a-f]{2}$")

#: R36-04: the volume-wide reference/delete lock's file, at the CAS blob
#: ROOT — one lock per volume, every work and every process.
_CAS_REFS_LOCK_NAME: Final = "cas-refs.lock"

#: R36-04: the CONSTANT postgres advisory key of the volume lock — the
#: first 8 bytes of a fixed domain string as a signed 64-bit integer,
#: the same value in every process. A collision with an unrelated
#: advisory key (2^-64 per pair) would only over-serialize, never
#: under-serialize, so it is safe by construction.
_CAS_REFS_ADVISORY_KEY: Final = int.from_bytes(
    hashlib.sha256(b"forge.checkpoint.cas-refs.volume").digest()[:8], "big", signed=True
)

#: A work id must be a safe single path segment — it names an index
#: FILE and a URL path component, so escapes (``/``, ``..``, ``@`` for
#: the remote-ref separator) are refused, not sanitized.
_WORK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: os.replace indirection so tests can simulate a crash exactly at the
#: rename (the atomic-write window) without patching the os module.
_replace = os.replace


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class CheckpointCorruptError(Exception):
    """Stored bytes no longer hash to their content address.

    Raised by :meth:`CheckpointStore._read_verified` — the read path's
    refusal to serve rotted bytes as if they were the checkpoint. The
    endpoint maps it to 500 with the address named: the operator's cue
    to re-upload, never a silently-corrupt restore.
    """

    def __init__(self, digest: str, actual: str) -> None:
        super().__init__(
            f"stored checkpoint content {digest} hashes to {actual or '<empty>'}; "
            "it will not be served — re-upload the checkpoint"
        )
        self.digest = digest
        self.actual = actual


class StorageQuotaExceededError(ValueError):
    """A checkpoint a :class:`StoragePolicy` refuses BEFORE any write.

    Raised by :meth:`CheckpointStore.put_checkpoint` for the per-blob
    cap, the manifest-entry cap and the per-work TOTAL bytes quota.
    Subclasses :class:`ValueError` so every existing ``except ValueError``
    refusal path keeps catching it; the endpoint catches it FIRST and
    answers 413 — an honest "over quota", never a truncated store, and
    never a destroyed previous checkpoint (refusal precedes the first
    write, so the work's durable state is byte-identical).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class IndexLockHeldError(RuntimeError):
    """Another writer held the per-work index lock past the wait budget.

    NEXT-06: the exclusive ``flock`` is taken NON-BLOCKING with a
    bounded, jittered retry (``FORGE_CHECKPOINT_LOCK_WAIT_SECONDS``);
    exhaustion means the OTHER writer owns the index — this upload is
    superseded history, never a corrupted append. The message names the
    recorded holder identity (``hostname|pid|acquired_at``, written
    into the lock file at acquisition) so an operator can see exactly
    which worker won.
    """

    def __init__(self, work_id: str, holder: str) -> None:
        super().__init__(
            f"another writer holds the index lock for {work_id}"
            f"{' (holder: ' + holder + ')' if holder else ''} — "
            "this upload is superseded history; the other writer's index stands"
        )
        self.work_id = work_id
        self.holder = holder


class GCLockTimeout(RuntimeError):
    """The volume-wide CAS reference/delete lock was not granted in budget.

    R36-04: reference acquisition and destructive sweeping serialize on
    ONE lock per CAS volume (``<root>/cas-refs.lock``, plus a postgres
    advisory-lock twin for blob roots that are not shared). The wait is
    bounded by :data:`GC_LOCK_WAIT_SECONDS_ENV` — a SWEEP that exhausts
    it aborts with this refusal before deleting anything (its marks
    stand; a later pass completes them against fresh reachability), and
    a WRITER that exhausts it fails the landing honestly: nothing was
    committed, the blobs it wrote are collectable orphans, and the
    content-addressed idempotent re-put retries. Never a silent
    indefinite block, and never bytes lost to an unbounded wait.
    """

    def __init__(self, wait_seconds: float) -> None:
        super().__init__(
            f"the CAS volume's reference/delete lock was not granted within "
            f"{wait_seconds:.3f}s ({GC_LOCK_WAIT_SECONDS_ENV}) — a sweep must "
            "retry later and a landing must be re-delivered; nothing was "
            "deleted or committed by the refusing side"
        )
        self.wait_seconds = wait_seconds


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    """Read an integer env knob, degrading to *default* (never below *minimum*)."""
    raw = os.environ.get(name, "").strip()
    try:
        return max(minimum, int(raw)) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """Read a float env knob, degrading to *default* (never negative)."""
    raw = os.environ.get(name, "").strip()
    try:
        return max(0.0, float(raw)) if raw else default
    except ValueError:
        return default


def _lock_wait_seconds() -> float:
    """The per-work index lock's retry budget (NEXT-06), from the env."""
    return _env_float(LOCK_WAIT_SECONDS_ENV, DEFAULT_LOCK_WAIT_SECONDS)


def _gc_lock_wait_seconds() -> float:
    """The volume-wide reference/delete lock's retry budget (R36-04)."""
    return _env_float(GC_LOCK_WAIT_SECONDS_ENV, DEFAULT_GC_LOCK_WAIT_SECONDS)


def _sweep_enabled() -> bool:
    """Whether destructive CAS unlinks are permitted (R36-04 rollout knob).

    Only the exact value ``off`` (case-insensitive) fences the unlink
    half of a GC pass; anything else — including unset — keeps sweeping.
    See :data:`SWEEP_ENV`.
    """
    return os.environ.get(SWEEP_ENV, "").strip().lower() != "off"


@dataclass(frozen=True)
class StoragePolicy:
    """Every cap and cleanup rule of the checkpoint store, in one place (R28-14).

    The endpoint's per-item caps, the ``FORGE_CHECKPOINT_RETENTION`` env
    and the R28-14 additions (per-work total quota, DR history floor) fold
    into this frozen value, built from the environment by
    :meth:`from_env`. The STORE enforces it — not just the HTTP boundary —
    so the local and network paths obey one policy object instead of two
    divergent spellings of the same rules:

    - ``max_blob_bytes`` — one blob's (or the manifest's) size cap;
    - ``max_manifest_entries`` — how many file entries one manifest may
      carry (the per-upload entry cap the endpoint already enforced);
    - ``max_total_bytes_per_work`` — per-work quota over the manifest +
      blob bytes its index entries reference (0 = no quota);
    - ``max_checkpoints_per_work`` — the retention keep count: at most
      this many checkpoints per work survive cleanup (0 = keep all);
    - ``history_keep`` — the disaster-recovery floor: cleanup always
      leaves the ACTIVE checkpoint plus at least this many superseded
      ones as rollback history;
    - ``cleanup_trigger`` — ``"on_upload"`` (retention runs after every
      successful put; the only trigger today) or ``"manual"`` (the
      operator runs :meth:`CheckpointStore.apply_retention` herself).
    """

    max_blob_bytes: int = DEFAULT_MAX_BLOB_BYTES
    max_manifest_entries: int = DEFAULT_MAX_BLOB_ENTRIES
    max_total_bytes_per_work: int = DEFAULT_MAX_WORK_TOTAL_BYTES
    max_checkpoints_per_work: int = 0
    history_keep: int = DEFAULT_HISTORY_KEEP
    cleanup_trigger: str = "on_upload"

    @classmethod
    def from_env(cls) -> StoragePolicy:
        """The operator's policy: every knob above, from its env variable."""
        return cls(
            max_blob_bytes=_env_int(MAX_BLOB_BYTES_ENV, DEFAULT_MAX_BLOB_BYTES, minimum=1),
            max_manifest_entries=_env_int(
                MAX_BLOB_ENTRIES_ENV, DEFAULT_MAX_BLOB_ENTRIES, minimum=1
            ),
            max_total_bytes_per_work=_env_int(
                MAX_WORK_TOTAL_BYTES_ENV, DEFAULT_MAX_WORK_TOTAL_BYTES
            ),
            max_checkpoints_per_work=_env_int(CHECKPOINT_RETENTION_ENV, 0),
            history_keep=_env_int(HISTORY_KEEP_ENV, DEFAULT_HISTORY_KEEP),
        )

    def retention_keep(self) -> int:
        """The keep count cleanup passes to :meth:`CheckpointStore.apply_retention`.

        ``max(0, max_checkpoints_per_work)`` bounded below by the DR floor
        (``history_keep + 1`` — the active checkpoint plus the floor's
        history). Zero means NO cleanup: retention off and no floor is
        the documented keep-everything default.
        """
        keep = max(0, self.max_checkpoints_per_work)
        if self.history_keep > 0:
            keep = max(keep, self.history_keep + 1)
        return keep

    def as_dict(self) -> dict[str, int | str]:
        """The policy as the health report prints it (operators read env names)."""
        return {
            "max_blob_bytes": self.max_blob_bytes,
            "max_manifest_entries": self.max_manifest_entries,
            "max_total_bytes_per_work": self.max_total_bytes_per_work,
            "max_checkpoints_per_work": self.max_checkpoints_per_work,
            "history_keep": self.history_keep,
            "cleanup_trigger": self.cleanup_trigger,
        }


@dataclass(frozen=True)
class DurabilityContract:
    """R32-16: which durability guarantee the checkpoint INDEX carries.

    Retention decisions and active selection are AUTHORITATIVE state —
    losing them strands blobs no resume can find. The contract names
    where that state lives:

    - ``best_effort`` (default) — the per-work filesystem JSON index
      under an exclusive ``flock``: correct for ONE API process on a
      shared volume, no guarantee across replicas or after an
      unattended index loss;
    - ``postgres`` — the index is rows in the ``checkpoint_metadata``
      table: every put/get/list runs in a transaction, concurrent puts
      serialize on ``SELECT ... FOR UPDATE`` over the work's rows, and
      a crash mid-metadata-commit rolls back atomically. The BLOBS stay
      content-addressed filesystem bytes in both modes — their writes
      need no lock (same address, same bytes, idempotent).

    :meth:`from_env` builds the contract from
    :data:`DURABILITY_ENV` and FAILS CLOSED on junk or on a ``postgres``
    selection without the session factory that reaches the metadata
    database: a typo must never silently downgrade the durability the
    operator believes she has (the same posture as
    :meth:`AdmissionPolicy.from_env`).
    """

    mode: str = DURABILITY_BEST_EFFORT
    session_factory: async_sessionmaker[AsyncSession] | None = None

    @classmethod
    def mode_from_env(cls, environ: dict[str, str] | None = None) -> str:
        """The validated mode name :data:`DURABILITY_ENV` selects.

        Empty selects the default; an unknown value raises ``ValueError``
        naming the variable — never a silent fallback.
        """
        source = os.environ if environ is None else environ
        mode = str(source.get(DURABILITY_ENV, "")).strip().lower() or DURABILITY_BEST_EFFORT
        if mode not in DURABILITY_MODES:
            raise ValueError(
                f"{DURABILITY_ENV} must be one of {sorted(DURABILITY_MODES)}, "
                f"got {mode!r} — the durability contract fails closed, never "
                "silently downgrades"
            )
        return mode

    @classmethod
    def from_env(
        cls,
        environ: dict[str, str] | None = None,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> DurabilityContract:
        """The operator's contract: the mode from the env, the DB from wiring."""
        mode = cls.mode_from_env(environ)
        if mode == DURABILITY_POSTGRES and session_factory is None:
            raise ValueError(
                f"{DURABILITY_ENV}=postgres needs a session factory wired to the "
                "database holding checkpoint_metadata — refusing to degrade the "
                "index back to the filesystem"
            )
        return cls(mode=mode, session_factory=session_factory)


class CheckpointMetadataRow(Base):
    """One checkpoint index row — the ``postgres`` durability's metadata.

    R32-16: the durable counterpart of the filesystem index's entry
    list. The composite primary key ``(work_id, checkpoint_id)`` makes a
    re-put idempotent at the database layer (the same content address
    is the same checkpoint — the INSERT simply loses to the existing
    row), and the work_id prefix keeps the per-work scans point
    lookups. ACTIVE selection stays DERIVED — the highest
    ``(sequence, checkpoint_id)``, the same deterministic rule
    :meth:`CheckpointStore._entry_order` pins for the filesystem index
    — so no pointer column can drift from the entries it summarizes.
    """

    __tablename__ = "checkpoint_metadata"

    work_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uploaded_at: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    def as_entry(self) -> dict[str, Any]:
        """The index-entry dict shape every reader of the store consumes.

        Identical to a filesystem index entry — :meth:`CheckpointStore.
        read_checkpoint` and the endpoints consume both without knowing
        which durability mode produced them.
        """
        return {
            "checkpoint_id": self.checkpoint_id,
            "sequence": int(self.sequence),
            "files": int(self.files),
            "uploaded_at": str(self.uploaded_at or ""),
        }


def _row_order(row: CheckpointMetadataRow) -> tuple[int, str]:
    """The DB spelling of the deterministic selection key."""
    return (int(row.sequence), str(row.checkpoint_id))


def _pin_list(decision: dict[str, Any] | None) -> list[str]:
    """The pin set a recorded retention decision was computed under.

    Decisions recorded before Q35-05 carry no ``pinned`` key — an empty
    list is the honest reading (no pin held anything back), and a
    decision whose pin set no longer matches the live one simply loses
    its short-circuit: the pass re-runs.
    """
    if decision is None:
        return []
    pinned = decision.get("pinned")
    return sorted(str(item) for item in pinned) if isinstance(pinned, list) else []


@dataclass(frozen=True)
class RetentionMark:
    """The MARK phase's in-memory tombstone (Q35-05) — a proposal, not a
    deletion.

    Carries the checkpoint ids a retention pass PROPOSES to drop (past
    the horizon, unpinned at mark time). The deletion transaction
    re-validates every part of it — pins re-read, rows re-locked,
    reachability re-derived from a FRESH scan — before one row goes or
    one blob is unlinked, so a reference or pin that landed since the
    mark wins the race and is removed from the deletion set.
    """

    work_id: str
    keep_last: int
    removed_ids: tuple[str, ...]
    marked_at: str


class CheckpointStore:
    """The control plane's content-addressed checkpoint directory.

    Blobs and manifests land at ``root/<first2>/<digest>`` through an
    atomic temp-file + rename (a crash leaves an orphan temp, never a
    half-written artifact under an address whose digest promises
    integrity); reads re-hash the bytes to the address and raise
    :class:`CheckpointCorruptError` on disagreement. The per-work index
    orders the checkpoints by ``(sequence, checkpoint_id)`` under an
    exclusive per-work ``flock`` (R28-06): the ACTIVE checkpoint is the
    highest sequence — never the last arrival — a lower-sequence upload
    lands as superseded history without demoting it, and two
    concurrent writers can never lose an append; retention never
    touches the active checkpoint.
    """

    def __init__(
        self,
        root: Path,
        *,
        max_blob_bytes: int | None = None,
        policy: StoragePolicy | None = None,
        durability: DurabilityContract | None = None,
        pins: CheckpointPins | None = None,
        gc_journal: CheckpointGcJournal | None = None,
    ) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        if policy is not None:
            self.policy = policy
        elif max_blob_bytes is not None:
            # The pre-R28-14 spelling: a direct per-blob override over the
            # env-derived policy (kept so every existing caller works).
            self.policy = replace(StoragePolicy.from_env(), max_blob_bytes=max_blob_bytes)
        else:
            self.policy = StoragePolicy.from_env()
        # R32-16: which index backs the store. The default contract is
        # best_effort (the filesystem index); the async ``a*`` methods are
        # the postgres surface and refuse to run without its factory.
        self.durability = durability if durability is not None else DurabilityContract()
        # Q35-05: the GC protection overlays both retention passes consult
        # — consumer pins and the pending-GC journal. They live on the
        # blob volume (code-level structures, no schema change) and are
        # shared by BOTH authorities; injectable for tests.
        self.pins = pins if pins is not None else CheckpointPins(self._root)
        self.gc_journal = gc_journal if gc_journal is not None else CheckpointGcJournal(self._root)
        # R36-04: TEST-ONLY barrier seam. When set, fired after a sweep's
        # FINAL reference scan and before its first unlink — the P04
        # schedule driver. Never set by production code; a hook may be a
        # plain callable (both authorities) or a coroutine function (the
        # async ``a*`` paths await it).
        self.gc_after_final_scan: Callable[[], Any] | None = None
        # R36-04: whether the metadata authority really reaches PostgreSQL
        # — the advisory-lock twin of the volume lock exists only there
        # (SQLite, the test approximation, serializes on the flock alone).
        # Probed once, cached.
        self._volume_lock_is_postgres: bool | None = None

    # -- content-addressed files --------------------------------------------

    def _cas_path(self, digest: str) -> Path:
        """The CAS address of *digest* — refusing anything else.

        R28-01's storage-primitive rule: the ONLY strings that may become
        filesystem paths here are 64-lowercase-hex content addresses. A
        traversal-shaped key (``../../x``), an absolute path, or any
        other non-address raises :class:`ValueError` BEFORE the path is
        constructed — defense in depth, so even a caller that forgot to
        validate its inputs cannot write outside the CAS through this
        store.
        """
        if not _HEX64.fullmatch(digest):
            raise ValueError(
                f"not a content address (expected 64 lowercase hex chars): {digest!r} "
                "— the store refuses to construct a path from it"
            )
        return self._root / digest[:2] / digest

    def _write_cas(self, digest: str, data: bytes) -> None:
        """Land *data* under its address atomically (temp + rename)."""
        if _sha256(data) != digest:
            raise ValueError(
                f"bytes hash to {_sha256(data)}, not {digest!r} — the store refuses "
                "to poison a content address with bytes it does not back"
            )
        target = self._cas_path(digest)
        if target.exists():
            # Content addressing means same address = same bytes — but a
            # rotted file under a live address is never SILENTLY adopted
            # (R28-01): re-hash what is there and refuse on disagreement,
            # so an idempotent re-put cannot hand out a "stored" verdict
            # for bytes the address does not back.
            actual = _sha256(target.read_bytes())
            if actual != digest:
                raise CheckpointCorruptError(digest, actual)
            return  # same bytes are already there, immutably
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            _replace(tmp_name, target)
        except BaseException:
            os.unlink(tmp_name)
            raise

    def _read_verified(self, digest: str) -> bytes:
        """Read an artifact, re-hashed to its address — or refuse it."""
        target = self._cas_path(digest)
        if not target.is_file():
            raise FileNotFoundError(digest)
        data = target.read_bytes()
        actual = _sha256(data)
        if actual != digest:
            raise CheckpointCorruptError(digest, actual)
        return data

    # -- the per-work index ---------------------------------------------------

    def _index_path(self, work_id: str) -> Path:
        return self._root / "works" / f"{work_id}.json"

    def _lock_path(self, work_id: str) -> Path:
        return self._root / "works" / f"{work_id}.lock"

    @staticmethod
    def _lock_holder(lock_path: Path) -> str:
        """The recorded identity of the current lock holder, if any.

        NEXT-06 writes ``hostname|pid|acquired_at`` into the lock file
        at acquisition and clears it on release, so the loser of a
        bounded retry (or an operator reading a stuck store) sees WHO
        holds the critical section — a per-holder lock PATH cannot
        exclude (two paths never conflict), so the identity rides the
        ONE shared lock file's content instead.
        """
        try:
            return lock_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    @contextmanager
    def _index_lock(self, work_id: str, *, wait_seconds: float | None = None) -> Iterator[None]:
        """Serialize the index read-modify-write across PROCESSES (R28-06/NEXT-06).

        ``_save_index``'s atomic rename prevents torn bytes, not lost
        updates: two writers can both read the same predecessor and the
        second rename silently drops the first's append. An exclusive
        ``flock`` on ``works/<work_id>.lock`` makes load-append-save one
        critical section; the lock is released by closing the fd, so a
        crashed writer never leaves it held. Holders open the lock file
        separately (never the index itself), so separate descriptors —
        including two threads of one process — exclude each other
        exactly as two processes do.

        NEXT-06: the acquire is ``LOCK_EX | LOCK_NB`` retried with
        jitter until *wait_seconds* (default
        ``FORGE_CHECKPOINT_LOCK_WAIT_SECONDS``) elapses — a live writer
        holds the critical section only for its short read-modify-write,
        so an ordinary concurrent upload waits it out and lands; a
        writer that exhausts the budget gets
        :class:`IndexLockHeldError` naming the recorded holder instead
        of blocking the API process forever. On acquisition the holder
        writes ``hostname|pid|acquired_at`` into the lock file (post-
        mortem surface) and clears it on release. Non-POSIX platforms
        without ``flock`` degrade to the rename-only discipline
        (documented, never silent: the deployment target is POSIX).
        """
        if fcntl is None:  # pragma: no cover — guarded import above
            yield
            return
        lock_path = self._lock_path(work_id)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        budget = _lock_wait_seconds() if wait_seconds is None else max(0.0, wait_seconds)
        deadline = time.monotonic() + budget
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        contended = False
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    contended = True
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise IndexLockHeldError(work_id, self._lock_holder(lock_path)) from None
                    time.sleep(min(random.uniform(0.0005, 0.003), remaining))
            # Record WHO holds the critical section — diagnostics for the
            # loser of a bounded retry and for a post-mortem on a stuck
            # store. Written ONLY when contention was observed: an
            # uncontended acquire leaves the lock file untouched (the
            # store stays byte-identical for a refused upload), and the
            # identity has diagnostic value exactly when someone else
            # was there. Best-effort: a failed write never fails the
            # upload.
            if contended:
                try:
                    os.ftruncate(fd, 0)
                    os.lseek(fd, 0, os.SEEK_SET)
                    os.write(
                        fd,
                        f"{socket.gethostname()}|{os.getpid()}|{_now_iso()}".encode(
                            "utf-8", "replace"
                        ),
                    )
                except OSError:  # pragma: no cover — holder text is diagnostics only
                    pass
            try:
                yield
            finally:
                if contended:
                    try:
                        os.ftruncate(fd, 0)  # the identity goes away with the lock
                    except OSError:  # pragma: no cover — diagnostics only
                        pass
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)  # releases the advisory lock, held or not

    def _load_index(self, work_id: str) -> dict[str, Any]:
        path = self._index_path(work_id)
        if not path.is_file():
            return {"work_id": work_id, "checkpoints": []}
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"work_id": work_id, "checkpoints": []}
        if not isinstance(document, dict) or not isinstance(document.get("checkpoints"), list):
            return {"work_id": work_id, "checkpoints": []}
        return document

    def _save_index(self, work_id: str, document: dict[str, Any]) -> None:
        """Write the index atomically — same crash discipline as the blobs.

        Callers hold :meth:`_index_lock` for the read-modify-write this
        write closes; the rename stays atomic regardless, so even a
        caller that forgot the lock can never expose torn bytes.
        """
        path = self._index_path(work_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-index-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            _replace(tmp_name, path)
        except BaseException:
            os.unlink(tmp_name)
            raise

    # -- the volume-wide reference/delete lock (R36-04) ----------------------

    def _cas_refs_lock_path(self) -> Path:
        """The VOLUME-wide reference/delete lock file: ``<root>/cas-refs.lock``.

        One lock for the whole CAS volume — every work, both durability
        authorities, every process sharing the volume. It is NOT the
        per-work index lock: the P04 defect lived precisely in the fact
        that work A's retention and work B's landing serialized on
        DIFFERENT per-work locks, so B's reference commit could slide
        between A's final reference scan and A's unlink.
        """
        return self._root / _CAS_REFS_LOCK_NAME

    @contextmanager
    def _cas_refs_lock(self, *, wait_seconds: float | None = None) -> Iterator[None]:
        """The volume lock — sync spelling (filesystem authority paths).

        ``LOCK_EX | LOCK_NB`` retried with jitter until the budget
        (:data:`GC_LOCK_WAIT_SECONDS_ENV`, or *wait_seconds*) elapses;
        exhaustion refuses with :class:`GCLockTimeout` — bounded, never
        a silent indefinite block. Ordering discipline: this lock is
        taken AFTER the per-work index/pin locks and BEFORE any
        transaction the guarded section runs (see the module docstring's
        protocol) — the ONE rule is that nothing may be acquired before
        it that someone else might take while holding it.
        """
        if fcntl is None:  # pragma: no cover — non-POSIX without flock
            yield
            return
        path = self._cas_refs_lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        budget = _gc_lock_wait_seconds() if wait_seconds is None else max(0.0, wait_seconds)
        deadline = time.monotonic() + budget
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise GCLockTimeout(budget) from None
                    time.sleep(min(random.uniform(0.0005, 0.003), remaining))
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @asynccontextmanager
    async def _acas_refs_flock(self, *, wait_seconds: float | None = None) -> AsyncIterator[None]:
        """The volume lock's flock half — async spelling (a* paths).

        Same file, same exclusion as :meth:`_cas_refs_lock`; the retry
        sleeps are ``asyncio.sleep`` so a contended lock never blocks
        the event loop.
        """
        if fcntl is None:  # pragma: no cover — non-POSIX without flock
            yield
            return
        path = self._cas_refs_lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        budget = _gc_lock_wait_seconds() if wait_seconds is None else max(0.0, wait_seconds)
        deadline = time.monotonic() + budget
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise GCLockTimeout(budget) from None
                    await asyncio.sleep(min(random.uniform(0.0005, 0.003), remaining))
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    async def _acas_refs_is_postgres(self) -> bool:
        """Whether the metadata authority really reaches PostgreSQL (cached).

        Only then does the volume lock take its advisory twin: a
        deployment whose blob root is NOT shared across processes still
        needs cross-process serialization, and ``pg_advisory_lock`` on a
        constant key provides it through the shared database. SQLite
        (the test approximation) serializes on the flock alone.
        """
        if self._volume_lock_is_postgres is None:
            factory = self.durability.session_factory
            reaches = False
            if factory is not None:
                async with factory() as session:
                    bind = session.bind
                    reaches = bind is not None and bind.dialect.name == "postgresql"
            self._volume_lock_is_postgres = reaches
        return self._volume_lock_is_postgres

    @asynccontextmanager
    async def _acas_refs_advisory(
        self, *, wait_seconds: float | None = None
    ) -> AsyncIterator[None]:
        """The volume lock's postgres twin — ``pg_advisory_lock`` on ONE key.

        A session-scoped advisory lock on the CONSTANT volume key
        (:data:`_CAS_REFS_ADVISORY_KEY` — the same value in every
        process, no table row to agree through), held on ONE dedicated
        connection for the duration and released in ``finally``. Taken
        INSIDE the flock, never outside it, so mixed deployments (some
        processes share the volume, some only the database) serialize on
        either half without a lock-order cycle.

        The wait is BOUNDED like the flock's: the acquiring transaction
        sets a transaction-scoped ``lock_timeout`` of the same budget,
        and a PostgreSQL cancellation ("canceling statement due to lock
        timeout") is translated to the same :class:`GCLockTimeout` —
        ``pg_advisory_lock`` would otherwise block forever, and an
        unbounded half would make the protocol's bounded-wait promise
        false. Any other database error propagates untouched.
        """
        from sqlalchemy import text

        factory = self._require_metadata_session()
        budget = _gc_lock_wait_seconds() if wait_seconds is None else max(0.0, wait_seconds)
        timeout_ms = max(1, int(budget * 1000))
        async with factory() as session:
            async with session.begin():  # holds ONE connection for the lock
                await session.execute(
                    text("SELECT set_config('lock_timeout', :ms, true)"), {"ms": str(timeout_ms)}
                )
                try:
                    await session.execute(
                        text("SELECT pg_advisory_lock(:key)"), {"key": _CAS_REFS_ADVISORY_KEY}
                    )
                except Exception as exc:
                    message = str(exc).lower()
                    if "lock timeout" in message or "canceling statement" in message:
                        raise GCLockTimeout(budget) from exc
                    raise
                try:
                    yield
                finally:
                    await session.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": _CAS_REFS_ADVISORY_KEY}
                    )

    @asynccontextmanager
    async def _acas_refs_lock(self, *, wait_seconds: float | None = None) -> AsyncIterator[None]:
        """The volume lock — async spelling: the flock first, then the
        postgres advisory twin when the metadata authority is PostgreSQL.

        Either half serializes reference acquisition against deletion;
        taking both, in this fixed order, covers shared-volume AND
        shared-database-only topologies with one protocol.
        """
        async with self._acas_refs_flock(wait_seconds=wait_seconds):
            if await self._acas_refs_is_postgres():
                async with self._acas_refs_advisory(wait_seconds=wait_seconds):
                    yield
            else:
                yield

    def _repair_unlinked_closure(
        self, checkpoint_id: str, manifest_bytes: bytes, blobs: dict[str, bytes]
    ) -> None:
        """Re-land closure bytes a concurrent sweep unlinked mid-put (R36-04).

        A landing's blob WRITES deliberately run OUTSIDE the volume lock
        (they are idempotent and content-addressed — holding the volume
        lock across every upload's fsyncs would serialize the whole
        deployment's writes). A sweep that held the lock between those
        writes and this reference acquisition may therefore have unlinked
        the very addresses this checkpoint is about to reference. Under
        the lock no further unlink can intervene, so re-landing whatever
        vanished HERE — cheap existence checks in the common case —
        closes the window from the writer's side: the committed
        reference always finds its bytes, and a crash before the commit
        still leaves only collectable orphans, never a reference
        without bytes.
        """
        for digest, data in sorted(blobs.items()):
            if not self._cas_path(digest).exists():
                self._write_cas(digest, data)
        if not self._cas_path(checkpoint_id).exists():
            self._write_cas(checkpoint_id, manifest_bytes)

    @staticmethod
    def _entry_order(entry: dict[str, Any]) -> tuple[int, str]:
        """The deterministic selection key: (sequence, checkpoint_id).

        R28-06: arrival order is not authority. Uploads can land out of
        order (a delayed runner's sequence 10 arriving after sequence
        20), so every "which checkpoint is active" decision compares the
        entry's own ``sequence`` — ties broken by content address, so
        two views of the same index always agree.
        """
        sequence = entry.get("sequence")
        return (sequence if isinstance(sequence, int) else 0, str(entry.get("checkpoint_id") or ""))

    @classmethod
    def _latest_entry(cls, entries: list[dict[str, Any]]) -> dict[str, Any] | None:
        """The ACTIVE checkpoint: the highest-sequence entry, not the last arrival."""
        return max(entries, key=cls._entry_order) if entries else None

    @staticmethod
    def _entry_files(manifest_bytes: bytes) -> set[str]:
        """The blob digests one manifest references (empty when unparsable)."""
        try:
            document = json.loads(manifest_bytes)
        except ValueError:
            return set()
        files = document.get("files") if isinstance(document, dict) else None
        if not isinstance(files, dict):
            return set()
        return {
            str(entry["digest"])
            for entry in files.values()
            if isinstance(entry, dict) and isinstance(entry.get("digest"), str)
        }

    # -- the storage policy (R28-14) ------------------------------------------

    def _refuse_policy_violations(
        self, work_id: str, manifest_bytes: bytes, blobs: dict[str, bytes]
    ) -> None:
        """The STORE's own cap enforcement — before any write, defense in depth.

        The endpoint checks the same caps while decoding; these lines make
        the policy a property of the STORAGE, not of one HTTP path, so the
        local and network surfaces cannot drift apart (R28-14's point).
        Per-blob and manifest-entry violations raise
        :class:`StorageQuotaExceededError` naming the exact offender.
        """
        policy = self.policy
        if len(manifest_bytes) > policy.max_blob_bytes:
            raise StorageQuotaExceededError(
                f"manifest for {work_id} is {len(manifest_bytes)} bytes; this store"
                f" accepts at most {policy.max_blob_bytes} bytes per blob — refused"
                " rather than truncated"
            )
        for digest, data in sorted(blobs.items()):
            if len(data) > policy.max_blob_bytes:
                raise StorageQuotaExceededError(
                    f"blob {digest} for {work_id} is {len(data)} bytes; this store"
                    f" accepts at most {policy.max_blob_bytes} bytes per blob — refused"
                    " rather than truncated"
                )
        entries = len(self._entry_files(manifest_bytes))
        if entries > policy.max_manifest_entries:
            raise StorageQuotaExceededError(
                f"manifest for {work_id} carries {entries} file entries; this store"
                f" accepts at most {policy.max_manifest_entries} per checkpoint —"
                " a checkpoint is a bounded WIP delta, not a key-value dump"
            )

    def _work_referenced_digests(self, entries: list[dict[str, Any]]) -> set[str]:
        """Every digest the work's index entries currently REFERENCE.

        Each retained checkpoint's manifest address plus the blob
        digests its manifest declares (an unreadable manifest keeps its
        own address — conservative). This is the reference set the
        per-work quota judges against (Q35-05: quotas count REFERENCED
        bytes, not merely bytes this process happened to write).
        """
        digests: set[str] = set()
        for entry in entries:
            checkpoint_id = entry.get("checkpoint_id")
            if not isinstance(checkpoint_id, str):
                continue
            digests.add(checkpoint_id)
            try:
                digests.update(self._entry_files(self._read_verified(checkpoint_id)))
            except (FileNotFoundError, CheckpointCorruptError, ValueError):
                continue  # unreadable manifests keep their own address only
        return digests

    def _work_usage_bytes(self, entries: list[dict[str, Any]]) -> int:
        """The bytes the work's index entries currently reference on disk.

        Every retained checkpoint's manifest plus its referenced blobs,
        counted from the CAS (a digest missing on disk contributes zero —
        the health report names it; the quota must not guess a size).
        """
        return sum(self._size_on_disk(digest) for digest in self._work_referenced_digests(entries))

    def _size_on_disk(self, digest: str) -> int:
        try:
            return self._cas_path(digest).stat().st_size
        except (FileNotFoundError, ValueError):
            return 0

    def _refuse_over_work_quota(
        self,
        work_id: str,
        entries: list[dict[str, Any]],
        manifest_bytes: bytes,
        blobs: dict[str, bytes],
    ) -> None:
        """The per-work TOTAL bytes quota — refused before the first write.

        Current usage is what the work's index references on disk, and
        the upload's NEW usage is the part of its closure the work does
        not already reference (Q35-05): a DEDUPLICATED blob another
        work already stored still counts toward THIS work when the work
        newly references it — content-addressed sharing saves disk, not
        quota. Over the cap the upload is refused with
        :class:`StorageQuotaExceededError` — quota exhaustion leaves the
        previous checkpoints untouched: an explicit, recoverable state,
        never data loss.
        """
        cap = self.policy.max_total_bytes_per_work
        if cap <= 0:
            return
        manifest_id = _sha256(manifest_bytes)
        current = self._work_referenced_digests(entries)
        fresh = {manifest_id, *blobs} - current
        fresh_bytes = sum(
            len(manifest_bytes) if digest == manifest_id else len(blobs[digest]) for digest in fresh
        )
        current_bytes = sum(self._size_on_disk(digest) for digest in current)
        if current_bytes + fresh_bytes > cap:
            raise StorageQuotaExceededError(
                f"work {work_id} already references {current_bytes} bytes; this upload "
                f"newly references {fresh_bytes} more (including shared blobs the work "
                f"did not reference before), over the per-work quota of {cap} bytes — "
                "the upload is refused and the work's existing checkpoints are untouched"
            )

    # -- the operations ---------------------------------------------------------

    def _verify_upload(self, work_id: str, manifest_bytes: bytes, blobs: dict[str, bytes]) -> str:
        """The shared refusal prelude of BOTH durability modes (R32-16).

        Everything the upload promises is checked BEFORE the first write,
        whichever index will record it: every supplied key AND every
        digest the manifest references must be a 64-hex content address,
        the blob set must be EXACTLY the manifest's referenced digests
        (extra entries refused, missing entries refused), and the
        :class:`StoragePolicy` caps hold (per-blob, manifest entries).
        Returns the checkpoint's content address (the manifest's own
        SHA-256).
        """
        checkpoint_id = _sha256(manifest_bytes)
        referenced = self._entry_files(manifest_bytes)
        supplied = set(blobs)
        malformed = sorted(
            digest for digest in referenced | supplied if not _HEX64.fullmatch(digest)
        )
        if malformed:
            raise ValueError(
                f"checkpoint for {work_id} carries non-address blob keys "
                f"(e.g. {malformed[0]!r}) — refusing before any write"
            )
        if supplied != referenced:
            raise ValueError(
                f"checkpoint for {work_id} must carry EXACTLY the manifest's referenced "
                f"blobs — missing {sorted(referenced - supplied)[:3]}, "
                f"extra {sorted(supplied - referenced)[:3]}; refusing before any write"
            )
        self._refuse_policy_violations(work_id, manifest_bytes, blobs)
        return checkpoint_id

    def put_checkpoint(
        self,
        *,
        work_id: str,
        manifest_bytes: bytes,
        blobs: dict[str, bytes],
        sequence: int,
    ) -> dict[str, Any]:
        """Store one verified checkpoint; promote it ONLY if it outranks.

        Callers verify the digests BEFORE this (the endpoint does); the
        store re-asserts the same invariants as defense in depth (R28-01)
        — every supplied key AND every digest the manifest references
        must be a 64-hex content address, and the blob set must be
        EXACTLY the manifest's referenced digests (extra entries refused,
        missing entries refused) — all BEFORE the first write, so a
        refused upload leaves the store byte-identical. With the checks
        passed, the landing order is blobs-then-manifest-then-index,
        each atomic — a crash between them leaves unreferenced CAS
        content (harmless, collectable), never an index entry whose
        bytes are missing. Re-putting an identical checkpoint is
        idempotent: the addresses exist and the index does not duplicate
        the entry.

        R28-06 promotion: the index's read-modify-write runs under the
        per-work OS lock, entries are kept sorted by ``(sequence,
        checkpoint_id)``, and a lower-sequence upload NEVER demotes the
        active checkpoint — it lands as superseded history only (the
        response says ``latest: false``), so a delayed runner arriving
        late cannot roll the work's resume point back.

        R28-14: the store's :class:`StoragePolicy` is enforced here —
        per-blob cap, manifest-entry cap and the per-work total-bytes
        quota all refuse BEFORE the first write (the blob writes moved
        inside the index lock so the quota reads one consistent index),
        and cleanup runs after a successful landing per the policy's
        trigger: retention bounded below by the DR history floor, never
        touching the active checkpoint.

        NEXT-06: the critical section is entered through the bounded
        non-blocking lock. A writer that cannot take it within
        ``FORGE_CHECKPOINT_LOCK_WAIT_SECONDS`` LOSES honestly: nothing
        is written (no blobs, no index change — the winner's index is
        byte-identical) and the returned verdict says
        ``superseded: true`` naming the holder — the other writer's
        state is the work's truth, exactly as if this upload had landed
        late with a lower sequence.

        R32-16: this is the BEST-EFFORT durability's write path (the
        filesystem index). The ``postgres`` contract's is
        :meth:`aput_checkpoint` — same refusal prelude, transactional
        index, no lock needed for the content-addressed blobs.

        R36-04: the index read-modify-write — the REFERENCE ACQUISITION
        — runs under the VOLUME-wide reference/delete lock (nested
        inside the per-work index lock), with the race repair first:
        blob writes stay outside that lock, so a sweep that held it
        meanwhile may have unlinked the freshly written addresses, and
        :meth:`_repair_unlinked_closure` re-lands whatever vanished
        before the reference commits. A sweep's final-scan-to-unlink
        interval can therefore never contain this reference's commit.
        """
        checkpoint_id = self._verify_upload(work_id, manifest_bytes, blobs)

        try:
            with self._index_lock(work_id):
                document = self._load_index(work_id)
                entries: list[dict[str, Any]] = [
                    entry for entry in document["checkpoints"] if isinstance(entry, dict)
                ]
                self._refuse_over_work_quota(work_id, entries, manifest_bytes, blobs)
                for digest, data in blobs.items():
                    self._write_cas(digest, data)
                self._write_cas(checkpoint_id, manifest_bytes)
                # R36-04: the REFERENCE ACQUISITION — the index
                # read-modify-write that makes these bytes referenced —
                # runs under the VOLUME-wide reference/delete lock, with
                # the race repair first (a sweep that held the lock while
                # this landing wrote its bytes may have unlinked them;
                # see _repair_unlinked_closure). A retention pass for ANY
                # work must hold the same lock from ITS final scan
                # through ITS unlink, so a reference can no longer commit
                # inside that window.
                with self._cas_refs_lock():
                    self._repair_unlinked_closure(checkpoint_id, manifest_bytes, blobs)
                    own = next(
                        (entry for entry in entries if entry.get("checkpoint_id") == checkpoint_id),
                        None,
                    )
                    if own is None:
                        own = {
                            "checkpoint_id": checkpoint_id,
                            "sequence": sequence,
                            "files": len(blobs),
                            "uploaded_at": _now_iso(),
                        }
                        entries.append(own)
                    entries.sort(key=self._entry_order)
                    self._save_index(work_id, {"work_id": work_id, "checkpoints": entries})
                    latest = self._latest_entry(entries)
                    result = {
                        "work_id": work_id,
                        "checkpoint_id": checkpoint_id,
                        "sequence": sequence,
                        "files": len(blobs),
                        "uploaded_at": str(own.get("uploaded_at") or ""),
                        "latest": latest is not None
                        and latest.get("checkpoint_id") == checkpoint_id,
                    }
        except IndexLockHeldError as exc:
            # NEXT-06: the other writer won. Nothing was written; the
            # caller's upload is superseded history by verdict, not by
            # index mutation — re-deliver it (the checkpoint is
            # content-addressed and idempotent) or let the winner's
            # state stand.
            return {
                "work_id": work_id,
                "checkpoint_id": checkpoint_id,
                "sequence": sequence,
                "files": len(blobs),
                "uploaded_at": "",
                "latest": False,
                "superseded": True,
                "superseded_reason": str(exc),
            }
        if self.policy.cleanup_trigger == "on_upload":
            keep = self.policy.retention_keep()
            if keep > 0:
                try:
                    self.apply_retention(work_id, keep)
                except (IndexLockHeldError, GCLockTimeout):
                    # The landing committed; retention is maintenance —
                    # the next upload or operator pass re-runs it against
                    # the recorded decision (R36-04: a contended volume
                    # lock defers the sweep, it never drops it).
                    pass
        return result

    # -- the postgres durability's index operations (R32-16) ------------------
    #
    # The async ``a*`` spellings run against the ``checkpoint_metadata``
    # table: put/get/list in real transactions, the work's rows locked
    # with ``SELECT ... FOR UPDATE`` for the read-modify-write parts
    # (quota, retention), active selection derived — never a pointer to
    # drift. The BLOBS never move: they stay content-addressed
    # filesystem bytes whose writes need no lock (an idempotent re-put
    # cannot lose to a race — same address, same bytes). The SQLite
    # dialect ignores FOR UPDATE (transactions still serialize the
    # writes); PostgreSQL enforces it — the CAS the deployment actually
    # runs on when it asked for this contract.

    def _require_metadata_session(self) -> async_sessionmaker[AsyncSession]:
        """The contract's DB wiring — postgres mode without it is a bug."""
        factory = self.durability.session_factory
        if factory is None:
            raise RuntimeError(
                "postgres durability requires a session factory on the "
                "DurabilityContract — the checkpoint index has nowhere to live"
            )
        return factory

    def _metadata_insert(self, session: AsyncSession, **values: Any) -> Any:
        """The checkpoint row's INSERT with ``ON CONFLICT DO NOTHING`` (Q35-05).

        The composite PK makes a re-put idempotent at the database layer
        — the same content address IS the same checkpoint — and the
        ON-CONFLICT spelling keeps that true when two racing puts of
        the SAME address land back to back: the loser's insert quietly
        loses to the winner's committed row instead of failing the
        transaction. The generic fallback (other dialects) keeps the
        plain INSERT semantics.
        """
        from sqlalchemy import insert as generic_insert
        from sqlalchemy.dialects import postgresql, sqlite as sqlite_dialect

        bind = session.bind
        name = bind.dialect.name if bind is not None else ""
        statement: Any
        if name == "postgresql":
            statement = postgresql.insert(CheckpointMetadataRow)
        elif name == "sqlite":
            statement = sqlite_dialect.insert(CheckpointMetadataRow)
        else:
            return generic_insert(CheckpointMetadataRow).values(**values)
        return statement.values(**values).on_conflict_do_nothing(
            index_elements=[CheckpointMetadataRow.work_id, CheckpointMetadataRow.checkpoint_id]
        )

    async def aput_checkpoint(
        self,
        *,
        work_id: str,
        manifest_bytes: bytes,
        blobs: dict[str, bytes],
        sequence: int,
    ) -> dict[str, Any]:
        """The postgres contract's landing: one transaction for the index.

        Same refusal prelude as :meth:`put_checkpoint` (verified before
        the first write — a refused upload changes nothing in EITHER
        store). Then ONE transaction: the work's rows are selected
        ``FOR UPDATE`` (the CAS lock — two concurrent puts serialize
        here, so the quota reads a consistent index and neither append
        is lost), the quota is judged, the checkpoint's row is inserted
        idempotently (``ON CONFLICT DO NOTHING`` under the composite
        PK — a racing twin's committed row wins and IS the answer), and
        the policy's on-upload retention — now the two-phase
        :meth:`_aretention_pass` — drops old rows in the SAME
        transaction. The blobs land under their addresses before the
        commit — content-addressed writes are idempotent and need no
        lock, and a crash between blob write and commit leaves
        collectable CAS content, never an index row whose bytes are
        missing. Unlinking retention-doomed blobs happens only AFTER
        the commit (rows first, blobs second — the same crash
        discipline the filesystem pass pins), and the pending-GC
        journal covers that unlink window for the next pass.

        Q35-05: a work's FIRST landing must not rely on the empty-set
        ``FOR UPDATE`` — callers that can race a first upload serialize
        through :meth:`forge.adaptive.checkpoint_repository.
        PostgresCheckpointRepository.first_upload_lock` (the advisory
        anchor) around this call.

        R36-04: the landing's REFERENCE ACQUISITION — the row
        transaction — runs under the VOLUME-wide reference/delete lock
        (flock on ``cas-refs.lock`` plus the postgres advisory twin),
        with the per-work pin flock nested inside it. The blob WRITES
        stay outside that lock (idempotent, content-addressed; holding
        the volume lock across every upload's fsyncs would serialize
        the deployment's writes); under the lock the landing REPAIRS
        first (:meth:`_repair_unlinked_closure`) — a sweep that held the
        lock while these bytes were being written may have unlinked
        them, and re-landing them under the lock, before the commit, is
        what guarantees a committed row never lacks its bytes. No
        sweep's final-scan-to-unlink interval can contain this
        transaction: sweeps hold the same lock.
        """
        checkpoint_id = self._verify_upload(work_id, manifest_bytes, blobs)
        factory = self._require_metadata_session()
        await self._acomplete_pending_gc(work_id)
        # Blob writes FIRST, outside the volume lock — a crash here
        # leaves collectable CAS content, never a row without bytes.
        for digest, data in blobs.items():
            self._write_cas(digest, data)  # idempotent, lock-free
        self._write_cas(checkpoint_id, manifest_bytes)
        doomed_digests: list[str] = []
        unlinked_digests: set[str] = set()
        async with self._acas_refs_lock():
            async with self.pins.alock(work_id):
                self._repair_unlinked_closure(checkpoint_id, manifest_bytes, blobs)
                async with factory() as session:
                    async with session.begin():
                        rows = list(
                            (
                                (
                                    await session.execute(
                                        select(CheckpointMetadataRow)
                                        .where(CheckpointMetadataRow.work_id == work_id)
                                        .order_by(
                                            CheckpointMetadataRow.sequence,
                                            CheckpointMetadataRow.checkpoint_id,
                                        )
                                        .with_for_update()
                                    )
                                )
                                .scalars()
                                .all()
                            )
                        )
                        self._refuse_over_work_quota(
                            work_id, [row.as_entry() for row in rows], manifest_bytes, blobs
                        )
                        own = next(
                            (row for row in rows if row.checkpoint_id == checkpoint_id), None
                        )
                        uploaded_at = _now_iso()
                        if own is None:
                            # rowcount is the INSERT's inserted-row count; SQLAlchemy
                            # 2.0 stubs only type it on CursorResult — access it via
                            # the runtime attr (the budgets.py precedent).
                            inserted = (
                                await session.execute(
                                    self._metadata_insert(
                                        session,
                                        work_id=work_id,
                                        checkpoint_id=checkpoint_id,
                                        sequence=sequence,
                                        files=len(blobs),
                                        uploaded_at=uploaded_at,
                                    )
                                )
                            ).rowcount  # type: ignore[attr-defined]
                            if inserted != 1:  # a racing twin's committed row won
                                twin = await session.get(
                                    CheckpointMetadataRow, (work_id, checkpoint_id)
                                )
                                if twin is not None:
                                    uploaded_at = str(twin.uploaded_at or uploaded_at)
                        else:
                            uploaded_at = str(own.uploaded_at or uploaded_at)
                        if self.policy.cleanup_trigger == "on_upload":
                            keep = self.policy.retention_keep()
                            if keep > 0:
                                _removed, doomed_digests = await self._aretention_pass(
                                    session, work_id, keep
                                )
                    # COMMIT — rows first, blobs second.
                # POST-COMMIT sweep — rows first, blobs second. R36-04: the
                # ONE unlink entry, still under this landing's volume lock.
                unlinked_digests = await self._asweep_locked(doomed_digests)
            if unlinked_digests:
                self.gc_journal.clear(work_id, sorted(unlinked_digests))
        active = self._adb_active(rows, own is None, checkpoint_id, sequence)
        return {
            "work_id": work_id,
            "checkpoint_id": checkpoint_id,
            "sequence": sequence,
            "files": len(blobs),
            "uploaded_at": uploaded_at,
            "latest": active == checkpoint_id,
        }

    @staticmethod
    def _adb_active(
        rows: list[CheckpointMetadataRow], inserted: bool, checkpoint_id: str, sequence: int
    ) -> str:
        """The DERIVED active checkpoint over the post-insert row set.

        The same ``(sequence, checkpoint_id)`` rule the filesystem index
        pins (R28-06): highest sequence wins, content address breaks
        ties, arrival order is never authority. The freshly inserted row
        participates through its own ``(sequence, checkpoint_id)``.
        """
        key = max(
            (
                *(_row_order(row) for row in rows),
                (sequence, checkpoint_id) if inserted else (-1, ""),
            )
        )
        return key[1]

    async def _amark_retention(self, work_id: str, keep_last: int) -> RetentionMark:
        """The MARK: the pass's PROPOSAL, derived with no locks held.

        Reads the work's rows in its own short session, splits them at
        the horizon and subtracts the pin set as it stands NOW — the
        result is a :class:`RetentionMark`, an in-memory tombstone that
        names candidates and nothing more. The deletion transaction
        re-validates every part of it (pins, rows, reachability) before
        anything goes, so the mark being raced is always safe.
        """
        factory = self._require_metadata_session()
        async with factory() as session:
            rows = list(
                (
                    (
                        await session.execute(
                            select(CheckpointMetadataRow.checkpoint_id)
                            .where(CheckpointMetadataRow.work_id == work_id)
                            .order_by(
                                CheckpointMetadataRow.sequence,
                                CheckpointMetadataRow.checkpoint_id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            )
        keep_count = max(1, min(keep_last, len(rows))) if keep_last > 0 else 1
        protected = self.pins.protected_ids()
        removed_ids = tuple(
            str(row_id) for row_id in rows[:-keep_count] if str(row_id) not in protected
        )
        return RetentionMark(
            work_id=work_id, keep_last=keep_last, removed_ids=removed_ids, marked_at=_now_iso()
        )

    async def _adb_referenced_digests(
        self, session: AsyncSession, exclude_ids: set[str]
    ) -> set[str]:
        """The live reference set over the CURRENT table — the RECHECK's eye.

        One FRESH statement (every checkpoint id the table holds minus
        *exclude_ids*), each manifest read for its file digests. Under
        PostgreSQL's READ COMMITTED this statement sees rows committed
        since the transaction began — exactly the property the recheck
        needs: a work B whose put committed between this pass's mark
        and its deletion transaction is VISIBLE here, and the shared
        blob B references leaves the deletion set.
        """
        ids = (
            (
                await session.execute(
                    select(CheckpointMetadataRow.checkpoint_id).where(
                        CheckpointMetadataRow.checkpoint_id.not_in(exclude_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        referenced: set[str] = set()
        for checkpoint_id in ids:
            referenced.add(str(checkpoint_id))
            try:
                referenced.update(self._entry_files(self._read_verified(str(checkpoint_id))))
            except (FileNotFoundError, CheckpointCorruptError, ValueError):
                continue  # unreadable manifests keep their own address only
        return referenced

    async def _adelete_rows(self, session: AsyncSession, rows: list[CheckpointMetadataRow]) -> None:
        """Delete the rechecked rows inside the caller's transaction.

        The seam the failed-transaction proofs crash on purpose: an
        exception here rolls the WHOLE deletion transaction back — no
        row goes, no blob is unlinked, nothing owned by a committed
        reference is lost.
        """
        for row in rows:
            await session.delete(row)

    async def _asweep_locked(self, digests: list[str]) -> set[str]:
        """The SWEEP under the volume lock — the postgres authority's ONE
        unlink entry (R36-04).

        The async twin of :meth:`_sweep_locked`: every caller holds
        :meth:`_acas_refs_lock` from its final survivor scan through this
        call; the TEST-ONLY barrier may be a coroutine function (awaited
        here); ``FORGE_CHECKPOINT_SWEEP=off`` unlinks nothing. Returns
        the digests actually unlinked — a caller's journal keeps only
        the spared set pending. The seam the interrupted-GC proofs crash
        on purpose.
        """
        hook = self.gc_after_final_scan
        if hook is not None:
            outcome = hook()
            if inspect.isawaitable(outcome):
                await outcome
        if not _sweep_enabled():
            return set()
        for digest in digests:
            self._cas_path(digest).unlink(missing_ok=True)
        return set(digests)

    async def _aretention_pass(
        self, session: AsyncSession, work_id: str, keep_last: int
    ) -> tuple[int, list[str]]:
        """Retention inside the caller's transaction; the deletion set back.

        The same rule :meth:`apply_retention` pins for the filesystem
        index — even ``keep_last=0`` keeps the ACTIVE checkpoint — as
        ONE mark/recheck/delete sequence inside the caller's
        transaction (the on-upload retention of :meth:`aput_checkpoint`):
        the pin set is read at split time, the doomed closure is
        derived, and the SURVIVOR SCAN (:meth:`_adb_referenced_digests`)
        re-derives live reachability from a fresh statement before the
        rows go. The digests are RETURNED, not unlinked: the caller
        commits first, records the pending-GC journal, then sweeps —
        a rolled-back transaction must never have deleted blobs its
        rows still reference.
        """
        rows = list(
            (
                (
                    await session.execute(
                        select(CheckpointMetadataRow)
                        .where(CheckpointMetadataRow.work_id == work_id)
                        .order_by(
                            CheckpointMetadataRow.sequence, CheckpointMetadataRow.checkpoint_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        )
        keep_count = max(1, min(keep_last, len(rows))) if keep_last > 0 else 1
        protected = self.pins.protected_ids()
        removed = [row for row in rows[:-keep_count] if str(row.checkpoint_id) not in protected]
        if not removed:
            return 0, []
        removed_ids = {str(row.checkpoint_id) for row in removed}
        doomed = self._closure_of(removed_ids)
        # RECHECK — the fresh survivor scan, plus the pin closure: a
        # reference or pin that landed since this pass began protects
        # its digests from this very deletion.
        referenced = await self._adb_referenced_digests(session, removed_ids)
        referenced |= self._pin_closure()
        deletion = sorted(doomed - referenced)
        await self._adelete_rows(session, removed)
        self.gc_journal.record(work_id, deletion)
        return len(removed), deletion

    async def _acomplete_pending_gc(self, work_id: str) -> None:
        """Complete an interrupted sweep against CURRENT reachability.

        The journal's recovery pass: whatever a previous pass recorded
        as pending but never unlinked (it crashed, or a late reference
        spared the digest, or ``FORGE_CHECKPOINT_SWEEP=off`` fenced the
        unlink half off) is re-derived from the table and the pins AS
        THEY STAND NOW — only still-unreferenced digests go. A journal
        left by a ROLLED-BACK transaction names digests whose rows
        still exist, so this pass spares them and clears the record:
        recovery never replays blindly, and never double-deletes.

        R36-04: the survivor scan AND the unlink run inside ONE volume
        lock hold (lock → final scan → unlink → unlock) — a landing
        that starts while this pass holds the lock cannot commit a
        reference into the scan-to-unlink interval.
        """
        pending = self.gc_journal.pending(work_id)
        if not pending:
            return
        factory = self._require_metadata_session()
        async with self._acas_refs_lock():
            async with factory() as session:
                referenced = await self._adb_referenced_digests(session, set())
            referenced |= self._pin_closure()
            doomed = [digest for digest in pending if digest not in referenced]
            unlinked = await self._asweep_locked(doomed)
            self.gc_journal.clear(work_id, sorted(unlinked))

    async def aentry(self, work_id: str, checkpoint_id: str | None = None) -> dict | None:
        """The work's ACTIVE entry (highest sequence), or the named one.

        The postgres contract's read: one point lookup over the work's
        rows, the same derived-active rule as every other mode — the
        entry dict's shape is identical to the filesystem index's, so
        :meth:`read_checkpoint` consumes both unchanged.
        """
        factory = self._require_metadata_session()
        async with factory() as session:
            rows = list(
                (
                    (
                        await session.execute(
                            select(CheckpointMetadataRow)
                            .where(CheckpointMetadataRow.work_id == work_id)
                            .order_by(
                                CheckpointMetadataRow.sequence,
                                CheckpointMetadataRow.checkpoint_id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            )
        if not rows:
            return None
        if checkpoint_id is None:
            return max(rows, key=_row_order).as_entry()
        own = next((row for row in rows if row.checkpoint_id == checkpoint_id), None)
        return own.as_entry() if own is not None else None

    async def alist_entries(self) -> list[dict[str, Any]]:
        """Every held checkpoint entry, sequence order per work, latest-flagged.

        The postgres contract's operator listing — same shape and same
        ordering discipline as :meth:`list_entries` (the ``latest`` flag
        names each work's derived-active entry).
        """
        factory = self._require_metadata_session()
        async with factory() as session:
            rows = list(
                (
                    (
                        await session.execute(
                            select(CheckpointMetadataRow).order_by(
                                CheckpointMetadataRow.work_id,
                                CheckpointMetadataRow.sequence,
                                CheckpointMetadataRow.checkpoint_id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            )
        listed: list[dict[str, Any]] = []
        by_work: dict[str, list[CheckpointMetadataRow]] = {}
        for row in rows:
            by_work.setdefault(row.work_id, []).append(row)
        for work_id in sorted(by_work):
            work_rows = by_work[work_id]
            active = max(work_rows, key=_row_order)
            for row in work_rows:
                entry = row.as_entry()
                entry["work_id"] = work_id
                entry["latest"] = row is active
                listed.append(entry)
        return listed

    async def aapply_retention(self, work_id: str, keep_last: int) -> int:
        """The operator's retention pass over the metadata table.

        Same contract as :meth:`apply_retention` (never the active
        checkpoint, shared content survives its sharers, pinned
        checkpoints survive until their pin is released), executed as
        the Q35-05 two-phase pass:

        1. complete any pending GC a previous pass journal'd but never
           swept (re-validated against CURRENT reachability — never a
           blind replay);
        2. **mark** — :meth:`_amark_retention` proposes the past-the-
           horizon, unpinned rows with NO locks held;
        3. **recheck + delete** — ONE transaction: the work's rows
           locked ``FOR UPDATE``, the pin set re-read, rows that were
           deleted or pinned since the mark dropped from the proposal,
           and the deletion set re-derived from a FRESH survivor scan
           (a reference committed since the mark — the P03 schedule —
           removes its digest from the deletion set; the new reference's
           commit wins the race). The pending-GC journal records the
           deletion set BEFORE the commit;
        4. **sweep** — only the rechecked survivors are unlinked after
           the commit, idempotently; the journal clears what went.

        An exception anywhere before the commit rolls the whole
        transaction back — a failed pass deletes NOTHING owned by
        committed references. The recoverable crash window is the gap
        between the commit and the unlink, and the journal's
        completion pass covers it. The per-work PIN flock is held
        across the transaction: a pin for this work can never land
        between the recheck's pin read and the commit.

        R36-04: the whole delete-and-sweep tail runs under the
        VOLUME-wide reference/delete lock, taken BEFORE the pin flock
        (the one lock order — a landing that inverts it could
        deadlock): the FRESH survivor scan, the deletion transaction,
        the commit and the unlink all sit inside one hold, so a
        DIFFERENT work's landing cannot commit a reference to a doomed
        digest between this pass's scan and its unlink. A READ
        COMMITTED statement only sees references committed before it
        ran — excluding the writer from the scan-to-unlink interval is
        what closes the P04 window, not moving the SELECT.
        """
        factory = self._require_metadata_session()
        await self._acomplete_pending_gc(work_id)
        mark = await self._amark_retention(work_id, keep_last)
        if not mark.removed_ids:
            return 0
        async with self._acas_refs_lock():
            async with self.pins.alock(work_id):
                deletion: list[str] = []
                async with factory() as session:
                    async with session.begin():
                        rows = list(
                            (
                                (
                                    await session.execute(
                                        select(CheckpointMetadataRow)
                                        .where(CheckpointMetadataRow.work_id == work_id)
                                        .order_by(
                                            CheckpointMetadataRow.sequence,
                                            CheckpointMetadataRow.checkpoint_id,
                                        )
                                        .with_for_update()
                                    )
                                )
                                .scalars()
                                .all()
                            )
                        )
                        present = {str(row.checkpoint_id): row for row in rows}
                        # RECHECK, part one — pins as they stand NOW: a pin
                        # recorded since the mark keeps its row and its bytes.
                        protected_now = self.pins.protected_ids()
                        final_ids = [
                            row_id
                            for row_id in mark.removed_ids
                            if row_id in present and row_id not in protected_now
                        ]
                        if not final_ids:
                            return 0  # converged elsewhere, or every candidate was pinned late
                        removed_ids = set(final_ids)
                        doomed = self._closure_of(removed_ids)
                        # RECHECK, part two — the FRESH survivor scan (the
                        # P03 fix): references committed since the mark are
                        # visible to this statement and leave the deletion set.
                        referenced = await self._adb_referenced_digests(session, removed_ids)
                        referenced |= self._pin_closure()
                        deletion = sorted(doomed - referenced)
                        await self._adelete_rows(session, [present[row_id] for row_id in final_ids])
                        self.gc_journal.record(work_id, deletion)
                    # COMMIT — rows first, blobs second.
                removed = len(final_ids)
                unlinked = await self._asweep_locked(deletion)
                self.gc_journal.clear(work_id, sorted(unlinked))
            return removed

    def entry(self, work_id: str, checkpoint_id: str | None = None) -> dict[str, Any] | None:
        """The work's ACTIVE entry (highest sequence), or the named one.

        R28-06: "latest" is the highest-``sequence`` entry — never
        ``entries[-1]`` — because arrival order is not authority. The
        comparison is deterministic (ties break by content address), so
        every reader of the same index agrees on which checkpoint a
        resume stands on.
        """
        entries = [
            entry
            for entry in self._load_index(work_id)["checkpoints"]
            if isinstance(entry, dict) and isinstance(entry.get("checkpoint_id"), str)
        ]
        if not entries:
            return None
        if checkpoint_id is None:
            return self._latest_entry(entries)
        return next((entry for entry in entries if entry["checkpoint_id"] == checkpoint_id), None)

    def read_checkpoint(self, entry: dict[str, Any]) -> tuple[bytes, dict[str, bytes]]:
        """Serve one checkpoint's manifest + blobs, digest-verified on read."""
        checkpoint_id = str(entry["checkpoint_id"])
        try:
            manifest_bytes = self._read_verified(checkpoint_id)
            blobs = {
                digest: self._read_verified(digest)
                for digest in sorted(self._entry_files(manifest_bytes))
            }
        except FileNotFoundError as exc:
            raise CheckpointCorruptError(str(exc), "") from exc
        except ValueError as exc:  # a rotted/tampered manifest naming non-addresses
            raise CheckpointCorruptError(checkpoint_id, "") from exc
        return manifest_bytes, blobs

    def list_entries(self) -> list[dict[str, Any]]:
        """Every held checkpoint entry, sequence order per work, latest-flagged.

        The ``latest`` flag names the highest-sequence entry of each
        work (R28-06), and the listing is sequence-ordered — never
        arrival-ordered — so the operator view agrees with what
        :meth:`entry` would resume from even when uploads landed out of
        order.
        """
        listed: list[dict[str, Any]] = []
        works_dir = self._root / "works"
        if not works_dir.is_dir():
            return listed
        for index_file in sorted(works_dir.iterdir()):
            if not index_file.is_file() or index_file.suffix != ".json":
                continue
            try:
                document = json.loads(index_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(document, dict):
                continue
            work_id = str(document.get("work_id") or index_file.stem)
            entries = sorted(
                (
                    dict(entry, work_id=work_id)
                    for entry in document.get("checkpoints", [])
                    if isinstance(entry, dict)
                ),
                key=self._entry_order,
            )
            latest = self._latest_entry(entries)
            for entry in entries:
                entry["latest"] = latest is not None and entry is latest
            listed.extend(entries)
        return listed

    def _closure_of(self, checkpoint_ids: set[str] | tuple[str, ...] | list[str]) -> set[str]:
        """Every digest the given checkpoints own — manifest plus files.

        A manifest that can no longer be read contributes only its own
        address: its blobs are kept, conservatively (the health report
        names the anomaly; GC never guesses reachability).
        """
        digests: set[str] = set(checkpoint_ids)
        for checkpoint_id in sorted(set(checkpoint_ids)):
            try:
                digests.update(self._entry_files(self._read_verified(checkpoint_id)))
            except (FileNotFoundError, CheckpointCorruptError, ValueError):
                continue
        return digests

    def _pin_closure(self) -> set[str]:
        """Every digest an active consumer PIN protects (Q35-05).

        The pinned checkpoint ids plus the blobs their manifests
        reference — the set both the MARK and the RECHECK phases of
        every retention pass subtract: a pinned checkpoint's row AND
        its bytes are unreachable by GC until the pin is released
        explicitly. Read FRESH each time (never cached): a pin that
        landed between two reads of one pass protects against that very
        pass's deletion.
        """
        return self._closure_of(self.pins.protected_ids())

    def _sweep_locked(self, digests: list[str]) -> set[str]:
        """The SWEEP under the volume lock — the filesystem authority's ONE
        unlink entry (R36-04).

        Every caller holds :meth:`_cas_refs_lock` from its FINAL reference
        scan through this call (lock → final scan → metadata deletion →
        unlink → unlock), so a reference that commits after the scan
        cannot exist: its writer needed the same lock first. Fires the
        TEST-ONLY ``gc_after_final_scan`` barrier, honors
        :func:`_sweep_enabled` (``FORGE_CHECKPOINT_SWEEP=off`` unlinks
        nothing — the marks stand for a later pass), unlinks idempotently
        (a missing blob is already gone) and RETURNS the digests actually
        unlinked so the caller keeps only the spared set pending. This is
        the seam the interrupted-GC proofs crash on purpose.
        """
        hook = self.gc_after_final_scan
        if hook is not None:
            hook()
        if not _sweep_enabled():
            return set()
        for digest in digests:
            self._cas_path(digest).unlink(missing_ok=True)
        return set(digests)

    def _referenced_by_retained_works(self, exclude_ids: set[str]) -> set[str]:
        """Every digest ANY retained checkpoint of ANY work still needs.

        The set that decides whether a doomed digest may actually be
        deleted: walks every work's index (THIS work's excluded entries
        passed via *exclude_ids*), reading each retained manifest for
        its file digests. This is the shared reachability oracle of the
        retention pass and of the pending-GC completion pass.
        """
        referenced: set[str] = set()
        works_dir = self._root / "works"
        index_files = sorted(works_dir.glob("*.json")) if works_dir.is_dir() else []
        for index_file in index_files:
            try:
                other = json.loads(index_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(other, dict):
                continue
            for entry in other.get("checkpoints", []):
                if not isinstance(entry, dict):
                    continue
                checkpoint_id = entry.get("checkpoint_id")
                if checkpoint_id in exclude_ids:
                    continue  # this exact entry is being removed in THIS work
                referenced.add(str(checkpoint_id))
                try:
                    manifest_bytes = self._read_verified(str(checkpoint_id))
                except (FileNotFoundError, CheckpointCorruptError):
                    continue
                referenced.update(self._entry_files(manifest_bytes))
        return referenced

    def apply_retention(self, work_id: str, keep_last: int) -> int:
        """Drop the OLDEST checkpoints beyond *keep_last* — never the latest.

        The rule that makes retention safe to run at any moment: even
        ``keep_last=0`` keeps the work's ACTIVE checkpoint (the highest
        SEQUENCE, R28-06 — not the last arrival) — a live pause stands
        on it, and no age/count policy may delete the thing resume
        needs. Older entries are removed from the index and their blobs
        (manifest + files) are deleted from the CAS ONLY when no
        retained checkpoint of ANY work still references them — shared
        content survives its sharers. The index read-modify-write runs
        under the per-work OS lock (R28-06), so a concurrent
        :meth:`put_checkpoint` for THIS work can never be dropped by
        this pass. Returns how many checkpoint entries were removed.

        Q35-05 makes the pass TWO-PHASE — mark, recheck, sweep — over
        the same per-work lock section:

        1. **Mark** — candidates are the entries past the horizon whose
           ids no PIN protects; their digest closure minus the CURRENT
           cross-work reachability (and minus the pin closure) is
           recorded as ``pending_gc`` — the tombstone.
        2. **Recheck** — the sweep re-derives reachability AGAIN, from
           the indexes as they stand at unlink time (a FRESH walk, plus
           a FRESH pin read): a reference some other work landed — or a
           pin some consumer recorded — between the mark and the sweep
           is REMOVED from the deletion set. The new reference's commit
           wins the race; its bytes stay readable (the P03 defect).
        3. **Sweep** — only the survivors are unlinked, idempotently.

        R36-04 places the FINAL reference scan and the unlink of BOTH
        sweeps here (the crash-recovery completion in step 1 and the
        live sweep in step 5) inside ONE volume-wide reference/delete
        lock each (``cas-refs.lock``, nested INSIDE the per-work index
        and pin locks — the one lock order the filesystem authority
        uses): a DIFFERENT work's landing can no longer commit a
        reference to a shared digest between this pass's final scan and
        its unlink, because that landing's own reference acquisition
        takes the same volume lock and must wait for the sweep to
        finish (its race repair then re-lands anything this pass was
        entitled to delete). The MARK stays outside the lock on purpose
        — a reference that commits between the mark and the deletion is
        the recheck's to catch. Moving the SELECT closer to the unlink
        would not close the window — a READ COMMITTED statement sees
        only what committed before it ran; only excluding the writer
        from the scan-to-unlink interval does.

        NEXT-06 records the DECISION in the index:
        ``{"keep", "ran_at", "removed", "holder", "kept_tail",
        "pending_gc"}``. A re-run that sees the same decision over the
        same entry set does nothing (no re-walk, no re-delete), and the
        blob unlink phase is recoverable: the index is saved FIRST with
        the doomed digests recorded as ``pending_gc``, the unlink runs
        second, the record keeps whatever the recheck spared third — a
        crash between the index update and the GC is completed by the
        next pass (re-validating reachability against the CURRENT
        indexes and pins, never blindly).
        """
        with self._index_lock(work_id), self.pins.lock(work_id):
            # The pin lock nests INSIDE the index lock (the one nesting
            # order in the system): a pin for this work can never land
            # between this pass's pin reads and its index save, so a
            # pinned checkpoint cannot be tombstoned by the very pass
            # its pin should have stopped (Q35-05).
            document = self._load_index(work_id)
            entries = sorted(
                (
                    entry
                    for entry in document["checkpoints"]
                    if isinstance(entry, dict) and isinstance(entry.get("checkpoint_id"), str)
                ),
                key=self._entry_order,
            )
            decision = document.get("retention")
            decision = decision if isinstance(decision, dict) else None

            # 1. Crash recovery first: a previous pass saved the index
            # (entries dropped, digests recorded) but died before/during
            # the unlink. Complete it against CURRENT reachability AND
            # pins — the same recheck discipline the live sweep runs.
            # R36-04: the recovery's survivor scan AND its unlink sit in
            # ONE volume-lock hold (lock → final scan → unlink → unlock).
            pending = decision.get("pending_gc") if decision is not None else None
            if isinstance(pending, list) and pending and decision is not None:
                pending_digests = {
                    str(digest) for digest in pending if _HEX64.fullmatch(str(digest))
                }
                with self._cas_refs_lock():
                    still = self._referenced_by_retained_works(set()) | self._pin_closure()
                    gone = self._sweep_locked(sorted(pending_digests - still))
                spared = sorted(pending_digests - gone)
                document["retention"] = {**decision, "pending_gc": spared}
                self._save_index(work_id, document)
                decision = document["retention"]
                # Spared digests STAY pending: a later pass re-attempts
                # them once their protection is gone.

            if not entries:
                return 0

            # 2. The recorded decision already covers this exact state —
            # a concurrent re-run (or an idempotent retry) re-deletes
            # nothing. The state includes the PIN SET the decision was
            # computed under: releasing a pin re-exposes past-the-horizon
            # entries without moving the kept tail, so a decision recorded
            # while a pin held entries back must not short-circuit the
            # pass that may now collect them (Q35-05).
            if (
                decision is not None
                and decision.get("keep") == keep_last
                and str(decision.get("kept_tail") or "") == str(entries[-1]["checkpoint_id"])
                and not decision.get("pending_gc")
                and _pin_list(decision) == sorted(self.pins.ids_for(work_id))
            ):
                return 0

            # 3. MARK — pinned entries are never candidates (Q35-05). A
            # proposal, deliberately OUTSIDE the volume lock: a reference
            # that commits after the mark but before the deletion is
            # exactly what the recheck below must still see.
            protected = self.pins.ids_for(work_id)
            keep_count = max(1, min(keep_last, len(entries))) if keep_last > 0 else 1
            removed = [
                entry
                for entry in entries[:-keep_count]
                if str(entry["checkpoint_id"]) not in protected
            ]
            if not removed:
                # Nothing to drop — still record the decision so the next
                # re-run over the same state short-circuits.
                self._save_index(
                    work_id,
                    {
                        **document,
                        "checkpoints": entries,
                        "retention": {
                            "keep": keep_last,
                            "ran_at": _now_iso(),
                            "removed": 0,
                            "holder": f"{socket.gethostname()}|{os.getpid()}",
                            "kept_tail": str(entries[-1]["checkpoint_id"]),
                            "pinned": sorted(protected),
                            "pending_gc": [],
                        },
                    },
                )
                return 0
            removed_ids = {str(entry["checkpoint_id"]) for entry in removed}
            retained = [
                entry for entry in entries if str(entry["checkpoint_id"]) not in removed_ids
            ]
            doomed_digests = self._closure_of(removed_ids)
            referenced_at_mark = (
                self._referenced_by_retained_works(removed_ids) | self._pin_closure()
            )
            pending_gc = sorted(doomed_digests - referenced_at_mark)

            # 4. TOMBSTONE — index first (entries dropped, GC recorded
            # as pending), unlink second, spared-third.
            self._save_index(
                work_id,
                {
                    **document,
                    "checkpoints": retained,
                    "retention": {
                        "keep": keep_last,
                        "ran_at": _now_iso(),
                        "removed": len(removed),
                        "holder": f"{socket.gethostname()}|{os.getpid()}",
                        "kept_tail": str(retained[-1]["checkpoint_id"]),
                        "pinned": sorted(protected),
                        "pending_gc": pending_gc,
                    },
                },
            )
            # 5. SWEEP with the RECHECK — reachability re-derived from
            # the CURRENT indexes and pins: a reference that landed
            # since the mark wins and keeps its bytes (Q35-05/P03).
            # R36-04: the FINAL reference scan and the unlink sit in ONE
            # volume-lock hold — a different work's landing takes the
            # same lock for its reference acquisition, so no reference
            # can commit inside this interval (and a landing queued
            # behind the hold re-lands its bytes afterwards).
            with self._cas_refs_lock():
                still = self._referenced_by_retained_works(set()) | self._pin_closure()
                deletable = [digest for digest in pending_gc if digest not in still]
                gone = self._sweep_locked(deletable)
            spared = [digest for digest in pending_gc if digest not in gone]
            document = self._load_index(work_id)
            document["retention"] = {**document.get("retention", {}), "pending_gc": spared}
            self._save_index(work_id, document)
            return len(removed)

    # -- the operator health surface (R28-14) ----------------------------------

    def _work_report(
        self, entries: list[dict[str, Any]], policy: StoragePolicy, work_id: str = ""
    ) -> tuple[dict[str, Any], set[str]]:
        """(per-work info, referenced digests) — the half BOTH modes report.

        The digests walk (each entry's manifest plus its referenced
        blobs, missing ones named, usage from the CAS) is index-agnostic:
        it consumes the entry-dict shape the filesystem index and the
        metadata table both produce. Q35-05 adds the work's PINNED
        checkpoint ids — the operator's view of which references GC is
        currently holding back.
        """
        digests: set[str] = set()
        for entry in entries:
            checkpoint_id = str(entry["checkpoint_id"])
            digests.add(checkpoint_id)
            try:
                files = self._entry_files(self._read_verified(checkpoint_id))
            except (FileNotFoundError, CheckpointCorruptError, ValueError):
                files = set()
            digests.update(files)
        missing = {digest for digest in digests if not self._cas_path(digest).exists()}
        usage = sum(self._size_on_disk(digest) for digest in digests)
        over_bytes = policy.max_total_bytes_per_work > 0 and usage > (
            policy.max_total_bytes_per_work
        )
        over_count = policy.max_checkpoints_per_work > 0 and len(entries) > (
            policy.max_checkpoints_per_work
        )
        info: dict[str, Any] = {
            "checkpoints": len(entries),
            "referenced_digests": len(digests),
            "missing_digests": sorted(missing),
            "bytes": usage,
            "over_quota": over_bytes or over_count,
            "over_quota_reasons": [
                *(["bytes"] if over_bytes else []),
                *(["checkpoints"] if over_count else []),
            ],
            "pinned": sorted(self.pins.ids_for(work_id)) if work_id else [],
        }
        return info, digests

    def _cas_inventory(self) -> tuple[dict[str, int], list[str], int]:
        """(CAS entries by digest, temp files, disk usage) — the disk half."""
        cas_entries: dict[str, int] = {}
        temp_files: list[str] = []
        disk_usage = 0
        for shard in sorted(self._root.iterdir()) if self._root.is_dir() else []:
            if not shard.is_dir() or len(shard.name) != 2 or not _HEX2.fullmatch(shard.name):
                continue  # the works/ directory and stray dirs are not CAS shards
            for item in sorted(shard.iterdir()):
                if not item.is_file():
                    continue
                size = item.stat().st_size
                disk_usage += size
                if _HEX64.fullmatch(item.name):
                    cas_entries[item.name] = size
                else:
                    temp_files.append(str(item.relative_to(self._root)))
        return cas_entries, temp_files, disk_usage

    def _finish_report(
        self,
        policy: StoragePolicy,
        works: dict[str, dict[str, Any]],
        referenced_anywhere: set[str],
        cas_entries: dict[str, int],
        temp_files: list[str],
        disk_usage: int,
    ) -> dict[str, Any]:
        """The assembled report — including the ACTIVE durability mode (R32-16).

        ``durability`` names which contract produced the works half, so
        an operator reading the report never mistakes a filesystem index
        for the transactional one.
        """
        orphans = sorted(set(cas_entries) - referenced_anywhere)
        return {
            "root": str(self._root),
            "durability": self.durability.mode,
            "policy": policy.as_dict(),
            "disk_usage_bytes": disk_usage,
            "cas_entry_count": len(cas_entries),
            "works": works,
            "over_quota_works": sorted(work for work, info in works.items() if info["over_quota"]),
            "orphan_cas_entries": orphans,
            "orphan_bytes": sum(cas_entries[digest] for digest in orphans),
            "temp_files": sorted(temp_files),
        }

    def storage_health_report(self, policy: StoragePolicy | None = None) -> dict[str, Any]:
        """The operator's view of the store: usage, quotas, orphans.

        Walks the per-work indexes (checkpoint count, referenced bytes,
        works over the policy's quota/retention caps, referenced digests
        missing from the CAS) and the CAS shards themselves (every
        content address on disk — an entry NO work's index references is
        an ORPHAN: harmless, collectable crash residue, but the operator
        must see it rather than wonder where the disk went). Temporary
        files (``.tmp-*``, the atomic-write crash window's leftovers)
        are listed separately. Read-only: this never mutates the store.
        """
        policy = policy or self.policy
        works: dict[str, dict[str, Any]] = {}
        referenced_anywhere: set[str] = set()
        works_dir = self._root / "works"
        if works_dir.is_dir():
            for index_file in sorted(works_dir.iterdir()):
                if not index_file.is_file() or index_file.suffix != ".json":
                    continue
                try:
                    document = json.loads(index_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(document, dict):
                    continue
                work_id = str(document.get("work_id") or index_file.stem)
                entries = [
                    entry
                    for entry in document.get("checkpoints", [])
                    if isinstance(entry, dict) and isinstance(entry.get("checkpoint_id"), str)
                ]
                works[work_id], digests = self._work_report(entries, policy, work_id)
                referenced_anywhere.update(digests)
        cas_entries, temp_files, disk_usage = self._cas_inventory()
        return self._finish_report(
            policy, works, referenced_anywhere, cas_entries, temp_files, disk_usage
        )

    async def astorage_health_report(self, policy: StoragePolicy | None = None) -> dict[str, Any]:
        """The same report under the postgres contract: works from the DB.

        The works half walks the ``checkpoint_metadata`` table (the
        authoritative index under this contract), the disk half walks
        the CAS exactly as the best-effort report does — the shape is
        identical, and ``durability`` says ``postgres``.
        """
        policy = policy or self.policy
        factory = self._require_metadata_session()
        async with factory() as session:
            rows = list(
                (
                    (
                        await session.execute(
                            select(CheckpointMetadataRow).order_by(
                                CheckpointMetadataRow.work_id,
                                CheckpointMetadataRow.sequence,
                                CheckpointMetadataRow.checkpoint_id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            )
        by_work: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_work.setdefault(row.work_id, []).append(row.as_entry())
        works: dict[str, dict[str, Any]] = {}
        referenced_anywhere: set[str] = set()
        for work_id in sorted(by_work):
            works[work_id], digests = self._work_report(by_work[work_id], policy, work_id)
            referenced_anywhere.update(digests)
        cas_entries, temp_files, disk_usage = self._cas_inventory()
        return self._finish_report(
            policy, works, referenced_anywhere, cas_entries, temp_files, disk_usage
        )


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------

checkpoint_channel_router = APIRouter()


def _secret() -> str:
    return os.environ.get(LANE_CONTROL_SECRET_ENV, "").strip()


def _store_dir() -> Path:
    return Path(os.environ.get(CHECKPOINT_STORE_DIR_ENV, "") or DEFAULT_CHECKPOINT_ROOT)


def _repository(request: Request) -> CheckpointRepository:
    """The request's ONE checkpoint repository (Q35-03) — fail closed.

    :func:`forge.adaptive.checkpoint_repository.resolve_repository` is
    the single composition point: it reads the durability mode
    (``FORGE_CHECKPOINT_DURABILITY``), the storage root and the session
    factory (the app's ``app.state.session_factory`` — the same
    authority the lane-control generation ladder uses) and returns the
    SAME repository class the resume producer resolves, so upload and
    resume can never read different authorities. A misconfiguration —
    an unknown mode, or ``postgres`` selected without any session
    factory — is a 503 refusal naming the problem, never a silent
    downgrade to the filesystem index the operator believes is
    transactional; a database outage on the index path is the typed
    ``CheckpointRepositoryUnavailable`` (also a 503) — never
    ``no-checkpoint``, never a filesystem fallback.
    """
    try:
        return resolve_repository(
            session_factory=getattr(request.app.state, "session_factory", None)
        )
    except CheckpointRepositoryMisconfigured as exc:
        raise HTTPException(
            status_code=503, detail=f"checkpoint durability is misconfigured: {exc}"
        ) from exc


def _max_blob_bytes() -> int:
    raw = os.environ.get(MAX_BLOB_BYTES_ENV, "").strip()
    try:
        return int(raw) if raw else DEFAULT_MAX_BLOB_BYTES
    except ValueError:
        return DEFAULT_MAX_BLOB_BYTES


def _max_total_blob_bytes() -> int:
    raw = os.environ.get(MAX_TOTAL_BLOB_BYTES_ENV, "").strip()
    try:
        return int(raw) if raw else DEFAULT_MAX_TOTAL_BLOB_BYTES
    except ValueError:
        return DEFAULT_MAX_TOTAL_BLOB_BYTES


def _max_blob_entries() -> int:
    raw = os.environ.get(MAX_BLOB_ENTRIES_ENV, "").strip()
    try:
        return int(raw) if raw else DEFAULT_MAX_BLOB_ENTRIES
    except ValueError:
        return DEFAULT_MAX_BLOB_ENTRIES


def _max_request_bytes() -> int:
    """The ENCODED request-size bound (NEXT-05), from the env."""
    raw = os.environ.get(MAX_REQUEST_BYTES_ENV, "").strip()
    try:
        return int(raw) if raw else DEFAULT_MAX_REQUEST_BYTES
    except ValueError:
        return DEFAULT_MAX_REQUEST_BYTES


def storage_health_report(
    root: Path | str | None = None, policy: StoragePolicy | None = None
) -> dict[str, Any]:
    """The store's health report — the doctor-callable spelling (R28-14).

    Reads the root from ``FORGE_CHECKPOINT_STORE_DIR`` when not given
    and the policy from the environment, so ``forge doctor`` (or any
    operator tool) can call this single function without wiring. The
    report shape is :meth:`CheckpointStore.storage_health_report`'s,
    plus the transport-side request cap (NEXT-05: the configured
    limits belong in the operator's evidence) and the ACTIVE durability
    mode (R32-16) — the doctor has no DB wiring, so under the postgres
    contract the works half of this CAS-only view is best-effort (the
    HTTP health endpoint walks the metadata table instead).
    """
    store = CheckpointStore(
        Path(root) if root is not None else _store_dir(),
        policy=policy,
        durability=DurabilityContract(mode=DurabilityContract.mode_from_env()),
    )
    report = store.storage_health_report()
    report["request_max_bytes"] = _max_request_bytes()
    return report


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        return ""
    scheme, _, token = authorization.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _authorized(secret: str, scope: str, authorization: str | None) -> bool:
    """Constant-time check of the work-scoped bearer HMAC."""
    token = _bearer_token(authorization)
    if not token:
        return False
    expected = work_scoped_token(secret, scope)
    try:
        return hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))
    except UnicodeEncodeError:  # pragma: no cover — header tokens are ascii in practice
        return False


async def _authorize_work(
    request: Request, secret: str, work_id: str, authorization: str | None
) -> None:
    """The WORK-surface gate: the shared attempt credential (NEXT-01).

    The very ladder :func:`forge.api_lane_control._authorize_lane` walks —
    imported, not duplicated: the current generation's attempt-scoped
    token, the legacy work-scoped one inside the migration deadline, a
    superseded generation refused with both generations named. This
    channel may be mounted standalone (no ``session_factory`` on the
    app): then there is no durable generation authority to consult, and
    the documented pre-generation posture applies (legacy inside the
    window, no staleness oracle) — with the authority CONFIGURED, an
    unreadable one is a 503 refusal, never a legacy acceptance.
    Refusals carry 401 here (this surface's existing spelling of "not
    your credential").
    """
    from forge.api_lane_control import authorize_work_credential

    await authorize_work_credential(
        request,
        secret=secret,
        authorization=authorization,
        work_id=work_id,
        refusal_status=401,
        require_authority=False,
    )


def _require_enabled(request: Request) -> str:
    """The fail-closed gate: 503 while the shared secret is unset."""
    secret = _secret()
    if not secret:
        raise HTTPException(status_code=503, detail="lane checkpoint channel disabled")
    return secret


def _validate_work_id(work_id: str) -> None:
    if not _WORK_ID.fullmatch(work_id):
        raise HTTPException(
            status_code=400,
            detail=(
                "work id must be a safe path segment (letters, digits, '.', '_', '-'; "
                f"at most 128 chars), got {work_id!r}"
            ),
        )


async def _read_bounded_body(request: Request, cap: int) -> bytes:
    """Stream the raw request body in bounded chunks (NEXT-05).

    The allocation bound for requests whose header lied or was absent
    (chunked transfer encoding has no ``Content-Length``): each chunk is
    appended and the accumulator is checked against *cap* BEFORE the
    next chunk is read, so the process never materializes more than
    ``cap + one chunk`` bytes of a hostile body. ``request.json()`` is
    deliberately NOT used — it materializes the full body first.
    """
    buffer = bytearray()
    async for chunk in request.stream():
        buffer.extend(chunk)
        if len(buffer) > cap:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"request body passed {cap} bytes while streaming (the declared "
                    f"or absent Content-Length did not bound it) — this channel "
                    f"accepts at most {cap} encoded bytes per request "
                    f"({MAX_REQUEST_BYTES_ENV}); the upload is refused before "
                    "anything is decoded or stored"
                ),
            )
    return bytes(buffer)


def _decode_payload(document: dict[str, Any], work_id: str) -> tuple[bytes, dict[str, bytes], int]:
    """Decode and VERIFY a checkpoint upload against its own bytes.

    Every promise the payload makes is checked before anything is
    stored: strict base64, the entry-count and aggregate decoded-size
    caps (413 — a checkpoint is a bounded delta, not a dump), the
    manifest's schema and work ownership (a manifest belonging to
    ANOTHER work is refused — the upload address and the checkpoint
    must agree), EVERY blob key — referenced or not — must be a
    64-hex content address (a path-shaped key is refused before any
    path could be constructed from it, R28-01), the blob set must be
    EXACTLY the manifest's referenced digests (extra entries refused,
    missing entries refused), every blob digest reproduced, and the
    per-blob size cap. Refusals name the exact key — honest, not
    generic.
    """
    manifest_field = document.get("manifest")
    blobs_field = document.get("blobs")
    if not isinstance(manifest_field, str) or not isinstance(blobs_field, dict):
        raise HTTPException(status_code=400, detail="payload needs manifest and blobs sections")
    max_entries = _max_blob_entries()
    if len(blobs_field) > max_entries:
        raise HTTPException(
            status_code=413,
            detail=(
                f"upload carries {len(blobs_field)} blob entries; this channel accepts "
                f"at most {max_entries} — the upload is refused rather than stored"
            ),
        )
    try:
        manifest_bytes = base64.b64decode(manifest_field, validate=True)
        blobs = {
            str(digest): base64.b64decode(str(encoded), validate=True)
            for digest, encoded in blobs_field.items()
        }
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid base64 content: {exc}") from exc
    # R28-01: EVERY supplied key — referenced or not — is a content
    # address or the upload dies here, before the store could ever turn
    # a key into a filesystem path.
    for digest in sorted(blobs):
        if not _HEX64.fullmatch(digest):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"blob key {digest!r} is not a content address (64 lowercase hex "
                    "chars) — the upload is refused before any write"
                ),
            )
    sequence = document.get("sequence") or 0

    try:
        manifest = json.loads(manifest_bytes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported manifest schema — this channel stores {MANIFEST_SCHEMA} only",
        )
    if manifest.get("work_id") != work_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"manifest belongs to work {manifest.get('work_id')!r}, not the "
                f"addressed work {work_id!r}"
            ),
        )
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise HTTPException(status_code=400, detail="manifest carries no files section")

    cap = _max_blob_bytes()
    if len(manifest_bytes) > cap:
        raise HTTPException(
            status_code=413,
            detail=(
                f"manifest is {len(manifest_bytes)} bytes; this channel accepts at most "
                f"{cap} bytes per blob — the upload is refused rather than truncated"
            ),
        )
    referenced: set[str] = set()
    for rel, entry in sorted(files.items()):
        digest = entry.get("digest") if isinstance(entry, dict) else None
        if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
            raise HTTPException(
                status_code=400, detail=f"manifest entry {rel!r} carries no valid content digest"
            )
        referenced.add(digest)
        data = blobs.get(digest)
        if data is None:
            raise HTTPException(
                status_code=400,
                detail=f"blob {digest} for {rel!r} is missing from the upload",
            )
        if len(data) > cap:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"blob {digest} for {rel!r} is {len(data)} bytes; this channel "
                    f"accepts at most {cap} bytes per blob — the upload is refused "
                    "rather than truncated"
                ),
            )
        if _sha256(data) != digest:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"blob {digest} for {rel!r} does not reproduce its digest "
                    f"(hashes to {_sha256(data)}) — a tampered upload is refused"
                ),
            )
    # Exact closure (R28-01): the upload may carry NOTHING the manifest
    # does not reference — an extra entry is refused before any write,
    # whatever its shape.
    extra = sorted(set(blobs) - referenced)
    if extra:
        raise HTTPException(
            status_code=400,
            detail=(
                f"upload carries {len(extra)} blob(s) the manifest does not reference "
                f"(first: {extra[0]!r}) — the blob set must be exactly the manifest's "
                "files; the upload is refused before any write"
            ),
        )
    total_cap = _max_total_blob_bytes()
    total = len(manifest_bytes) + sum(len(data) for data in blobs.values())
    if total > total_cap:
        raise HTTPException(
            status_code=413,
            detail=(
                f"upload decodes to {total} bytes total; this channel accepts at most "
                f"{total_cap} bytes per checkpoint — the upload is refused rather than "
                "stored"
            ),
        )
    return manifest_bytes, blobs, int(sequence)


@checkpoint_channel_router.put("/lane/checkpoints/{work_id}")
async def put_checkpoint(
    work_id: str,
    request: Request,
    authorization: str | None = Header(None),
) -> Any:
    """Accept one verified checkpoint upload for *work_id* (fail-closed)."""
    secret = _require_enabled(request)
    _validate_work_id(work_id)
    await _authorize_work(request, secret, work_id, authorization)
    # NEXT-05: bound the ALLOCATION before any expansion. The declared
    # encoded length is refused first (nothing is read), the streamed
    # body second (at most cap + one chunk is ever materialized), and
    # only then does JSON parsing and blob validation begin.
    cap = _max_request_bytes()
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_bytes = int(declared)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"Content-Length is not an integer: {declared!r}"
            ) from exc
        if declared_bytes > cap:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"request declares {declared_bytes} bytes; this channel accepts at "
                    f"most {cap} encoded bytes per request ({MAX_REQUEST_BYTES_ENV}) — "
                    "the body is refused before it is read"
                ),
            )
    body = await _read_bounded_body(request, cap)
    try:
        document = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(document, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")

    manifest_bytes, blobs, sequence = _decode_payload(document, work_id)

    repository = _repository(request)
    try:
        result = await repository.put_checkpoint(
            work_id=work_id,
            manifest_bytes=manifest_bytes,
            blobs=blobs,
            sequence=sequence,
        )
    except CheckpointRepositoryUnavailable as exc:  # an outage is never 404
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except GCLockTimeout as exc:
        # R36-04: a sweep held the volume lock past the landing's budget.
        # Nothing was committed (the blobs it wrote are collectable
        # orphans); the content-addressed re-put retries — 503, not 500,
        # because the caller's next attempt is the documented remedy.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except StorageQuotaExceededError as exc:  # R28-14: an honest 413, store intact
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ValueError as exc:  # the store's own defense-in-depth refusal
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except CheckpointCorruptError as exc:  # a rotted address is never adopted
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


@checkpoint_channel_router.get("/lane/checkpoints/health")
async def checkpoint_storage_health(
    request: Request,
    authorization: str | None = Header(None),
) -> Any:
    """The operator's storage health report: usage, quotas, orphans (R28-14).

    Registered BEFORE the ``{work_id}`` route so the literal path wins;
    guarded by the same operator token as the listing. Read-only. Under
    the postgres contract the works half walks the metadata table (the
    transactional index) and the report names the mode (R32-16).
    """
    secret = _require_enabled(request)
    if not _authorized(secret, CHECKPOINT_LIST_SCOPE, authorization):
        raise HTTPException(status_code=401, detail="invalid operator token")
    repository = _repository(request)
    try:
        report = await repository.storage_health_report()
    except CheckpointRepositoryUnavailable as exc:  # an outage is never a report
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    report["request_max_bytes"] = _max_request_bytes()
    report["authority"] = await repository.authority()
    return report


@checkpoint_channel_router.get("/lane/checkpoints/{work_id}")
async def get_checkpoint(
    work_id: str,
    request: Request,
    authorization: str | None = Header(None),
    checkpoint_id: str | None = None,
) -> Any:
    """Serve the work's latest (or named) checkpoint, digest-verified on read."""
    secret = _require_enabled(request)
    _validate_work_id(work_id)
    await _authorize_work(request, secret, work_id, authorization)
    if checkpoint_id is not None and not _HEX64.fullmatch(checkpoint_id):
        raise HTTPException(status_code=400, detail="checkpoint_id must be a 64-hex digest")

    repository = _repository(request)
    try:
        entry = await repository.entry(work_id, checkpoint_id)
        if entry is None:
            scope = f" with id {checkpoint_id}" if checkpoint_id else ""
            raise HTTPException(
                status_code=404,
                detail=f"no checkpoint held for this work{scope}",
            )
        latest_entry = await repository.entry(work_id)
    except CheckpointRepositoryUnavailable as exc:
        # An outage is a RECOVERABLE 503 — never 404, never a fs fallback.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        manifest_bytes, blobs = await repository.read_entry(entry)
    except CheckpointCorruptError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ValueError as exc:  # an index/manifest naming non-addresses
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "work_id": work_id,
        "checkpoint_id": entry["checkpoint_id"],
        "sequence": entry.get("sequence", 0),
        "files": entry.get("files", 0),
        "uploaded_at": entry.get("uploaded_at", ""),
        "latest": latest_entry is not None
        and latest_entry["checkpoint_id"] == entry["checkpoint_id"],
        "manifest": base64.b64encode(manifest_bytes).decode("ascii"),
        "blobs": {
            digest: base64.b64encode(data).decode("ascii") for digest, data in sorted(blobs.items())
        },
    }


@checkpoint_channel_router.get("/lane/checkpoints")
async def list_checkpoints(
    request: Request,
    authorization: str | None = Header(None),
) -> Any:
    """The operator surface: every held checkpoint reference."""
    secret = _require_enabled(request)
    if not _authorized(secret, CHECKPOINT_LIST_SCOPE, authorization):
        raise HTTPException(status_code=401, detail="invalid operator token")
    repository = _repository(request)
    try:
        entries = await repository.list_entries()
    except CheckpointRepositoryUnavailable as exc:  # an outage is never a list
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"checkpoints": entries}
