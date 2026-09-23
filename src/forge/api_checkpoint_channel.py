"""LIVE cross-runner checkpoint transport — the control-plane side (wave C/D).

The lane runner's checkpoint (:mod:`forge.adaptive.checkpointing`) is a
versioned manifest plus content blobs in a store rooted on the LANE
JOB's filesystem; this router is the durable counterpart a SECOND
runner restores from. Three endpoints, one storage discipline:

- ``PUT /lane/checkpoints/{work_id}`` — accept a checkpoint (JSON-base64
  manifest + blobs), verify EVERY promise against the bytes (the
  manifest must hash to its own content address, belong to the path's
  work, every blob must reproduce its digest — a tampered upload is
  refused with 400 naming the blob), enforce the per-blob size cap
  (413, an honest refusal), the aggregate decoded-size and entry-count
  caps, and require the blob set to be EXACTLY the manifest's
  referenced digests — extra path-shaped or otherwise malformed keys
  are refused BEFORE any write (R28-01) — then land everything in a
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
work's checkpoints. Retention
(:meth:`CheckpointStore.apply_retention`, driven by
``FORGE_CHECKPOINT_RETENTION`` — 0 keeps everything) drops the OLDEST
checkpoints beyond the keep count and can be asked to keep nothing —
and still NEVER deletes a work's LATEST checkpoint: the one a live
pause stands on always resolves. Deleted checkpoints release only
blobs no retained checkpoint of ANY work still references.

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
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, Header, HTTPException, Request

try:  # POSIX process-level advisory locking (Linux CI, macOS dev boxes).
    import fcntl
except ImportError:  # pragma: no cover — non-POSIX platform without flock
    fcntl = None  # type: ignore[assignment]

from forge.adaptive.checkpoint_channel import (
    CHECKPOINT_LIST_SCOPE,
    FORGE_LANE_CONTROL_SECRET_ENV,
    work_scoped_token,
)
from forge.adaptive.checkpointing import MANIFEST_SCHEMA

__all__ = [
    "CHECKPOINT_RETENTION_ENV",
    "CHECKPOINT_STORE_DIR_ENV",
    "DEFAULT_CHECKPOINT_ROOT",
    "DEFAULT_MAX_BLOB_BYTES",
    "DEFAULT_MAX_BLOB_ENTRIES",
    "DEFAULT_MAX_TOTAL_BLOB_BYTES",
    "DEFAULT_MAX_WORK_TOTAL_BYTES",
    "DEFAULT_HISTORY_KEEP",
    "HISTORY_KEEP_ENV",
    "LANE_CONTROL_SECRET_ENV",
    "MAX_BLOB_BYTES_ENV",
    "MAX_BLOB_ENTRIES_ENV",
    "MAX_TOTAL_BLOB_BYTES_ENV",
    "MAX_WORK_TOTAL_BYTES_ENV",
    "CheckpointCorruptError",
    "CheckpointStore",
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

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX2 = re.compile(r"^[0-9a-f]{2}$")
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


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    """Read an integer env knob, degrading to *default* (never below *minimum*)."""
    raw = os.environ.get(name, "").strip()
    try:
        return max(minimum, int(raw)) if raw else default
    except ValueError:
        return default


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

    @contextmanager
    def _index_lock(self, work_id: str) -> Iterator[None]:
        """Serialize the index read-modify-write across PROCESSES (R28-06).

        ``_save_index``'s atomic rename prevents torn bytes, not lost
        updates: two writers can both read the same predecessor and the
        second rename silently drops the first's append. An exclusive
        ``flock`` on ``works/<work_id>.lock`` makes load-append-save one
        critical section — the lock is released by closing the fd, so a
        crashed writer never leaves it held. Holders open the lock file
        separately (never the index itself), so separate descriptors —
        including two threads of one process — exclude each other
        exactly as two processes do. Non-POSIX platforms without
        ``flock`` degrade to the rename-only discipline (documented,
        never silent: the deployment target is POSIX).
        """
        if fcntl is None:  # pragma: no cover — guarded import above
            yield
            return
        self._lock_path(work_id).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path(work_id), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
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

    def _work_usage_bytes(self, entries: list[dict[str, Any]]) -> int:
        """The bytes the work's index entries currently reference on disk.

        Every retained checkpoint's manifest plus its referenced blobs,
        counted from the CAS (a digest missing on disk contributes zero —
        the health report names it; the quota must not guess a size).
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
        return sum(self._size_on_disk(digest) for digest in digests)

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

        Current usage is what the work's index references on disk; the
        upload adds only bytes NOT already stored (content addressing
        makes an idempotent re-put free, and a blob another checkpoint
        already landed is not new usage). Over the cap the upload is
        refused with :class:`StorageQuotaExceededError` — quota
        exhaustion leaves the previous checkpoints untouched: an
        explicit, recoverable state, never data loss.
        """
        cap = self.policy.max_total_bytes_per_work
        if cap <= 0:
            return
        manifest_id = _sha256(manifest_bytes)
        fresh = 0
        for digest in sorted({manifest_id, *blobs}):
            if self._cas_path(digest).exists():
                continue  # already stored (and immutable) — not new usage
            fresh += len(manifest_bytes) if digest == manifest_id else len(blobs[digest])
        current = self._work_usage_bytes(entries)
        if current + fresh > cap:
            raise StorageQuotaExceededError(
                f"work {work_id} already references {current} bytes; this upload adds"
                f" {fresh} more, over the per-work quota of {cap} bytes — the upload"
                " is refused and the work's existing checkpoints are untouched"
            )

    # -- the operations ---------------------------------------------------------

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

        with self._index_lock(work_id):
            document = self._load_index(work_id)
            entries: list[dict[str, Any]] = [
                entry for entry in document["checkpoints"] if isinstance(entry, dict)
            ]
            self._refuse_over_work_quota(work_id, entries, manifest_bytes, blobs)
            for digest, data in blobs.items():
                self._write_cas(digest, data)
            self._write_cas(checkpoint_id, manifest_bytes)
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
                "latest": latest is not None and latest.get("checkpoint_id") == checkpoint_id,
            }
        if self.policy.cleanup_trigger == "on_upload":
            keep = self.policy.retention_keep()
            if keep > 0:
                self.apply_retention(work_id, keep)
        return result

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
        :meth:`put_checkpoint` can never be dropped by this pass.
        Returns how many checkpoint entries were removed.
        """
        with self._index_lock(work_id):
            document = self._load_index(work_id)
            entries = sorted(
                (
                    entry
                    for entry in document["checkpoints"]
                    if isinstance(entry, dict) and isinstance(entry.get("checkpoint_id"), str)
                ),
                key=self._entry_order,
            )
            if not entries:
                return 0
            keep_count = max(1, min(keep_last, len(entries))) if keep_last > 0 else 1
            retained, removed = entries[-keep_count:], entries[:-keep_count]
            if not removed:
                return 0

            # Everything the removed checkpoints own — manifest plus blobs. A
            # manifest that can no longer be read contributes only its own
            # address: its blobs are kept, conservatively.
            removed_ids = {str(entry["checkpoint_id"]) for entry in removed}
            doomed_digests = set(removed_ids)
            for checkpoint_id in removed_ids:
                try:
                    doomed_digests.update(self._entry_files(self._read_verified(checkpoint_id)))
                except (FileNotFoundError, CheckpointCorruptError):
                    continue

            # Everything ANY retained checkpoint of ANY work still needs — the
            # set that decides whether a doomed digest may actually be deleted.
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
                    if checkpoint_id in removed_ids:
                        continue  # this exact entry is being removed in THIS work
                    referenced.add(str(checkpoint_id))
                    try:
                        manifest_bytes = self._read_verified(str(checkpoint_id))
                    except (FileNotFoundError, CheckpointCorruptError):
                        continue
                    referenced.update(self._entry_files(manifest_bytes))

            for digest in sorted(doomed_digests - referenced):
                self._cas_path(digest).unlink(missing_ok=True)
            self._save_index(work_id, {"work_id": work_id, "checkpoints": retained})
            return len(removed)

    # -- the operator health surface (R28-14) ----------------------------------

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
                referenced_anywhere.update(digests)
                usage = sum(self._size_on_disk(digest) for digest in digests)
                over_bytes = policy.max_total_bytes_per_work > 0 and usage > (
                    policy.max_total_bytes_per_work
                )
                over_count = policy.max_checkpoints_per_work > 0 and len(entries) > (
                    policy.max_checkpoints_per_work
                )
                works[work_id] = {
                    "checkpoints": len(entries),
                    "referenced_digests": len(digests),
                    "missing_digests": sorted(missing),
                    "bytes": usage,
                    "over_quota": over_bytes or over_count,
                    "over_quota_reasons": [
                        *(["bytes"] if over_bytes else []),
                        *(["checkpoints"] if over_count else []),
                    ],
                }

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
        orphans = sorted(set(cas_entries) - referenced_anywhere)
        return {
            "root": str(self._root),
            "policy": policy.as_dict(),
            "disk_usage_bytes": disk_usage,
            "cas_entry_count": len(cas_entries),
            "works": works,
            "over_quota_works": sorted(work for work, info in works.items() if info["over_quota"]),
            "orphan_cas_entries": orphans,
            "orphan_bytes": sum(cas_entries[digest] for digest in orphans),
            "temp_files": sorted(temp_files),
        }


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------

checkpoint_channel_router = APIRouter()


def _secret() -> str:
    return os.environ.get(LANE_CONTROL_SECRET_ENV, "").strip()


def _store_dir() -> Path:
    return Path(os.environ.get(CHECKPOINT_STORE_DIR_ENV, "") or DEFAULT_CHECKPOINT_ROOT)


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


def storage_health_report(
    root: Path | str | None = None, policy: StoragePolicy | None = None
) -> dict[str, Any]:
    """The store's health report — the doctor-callable spelling (R28-14).

    Reads the root from ``FORGE_CHECKPOINT_STORE_DIR`` when not given
    and the policy from the environment, so ``forge doctor`` (or any
    operator tool) can call this single function without wiring. The
    report shape is :meth:`CheckpointStore.storage_health_report`'s.
    """
    store = CheckpointStore(Path(root) if root is not None else _store_dir(), policy=policy)
    return store.storage_health_report()


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
    try:
        document = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(document, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")

    manifest_bytes, blobs, sequence = _decode_payload(document, work_id)

    store = CheckpointStore(_store_dir(), policy=StoragePolicy.from_env())
    try:
        result = store.put_checkpoint(
            work_id=work_id,
            manifest_bytes=manifest_bytes,
            blobs=blobs,
            sequence=sequence,
        )
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
    guarded by the same operator token as the listing. Read-only.
    """
    secret = _require_enabled(request)
    if not _authorized(secret, CHECKPOINT_LIST_SCOPE, authorization):
        raise HTTPException(status_code=401, detail="invalid operator token")
    store = CheckpointStore(_store_dir(), policy=StoragePolicy.from_env())
    return store.storage_health_report()


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

    store = CheckpointStore(_store_dir(), policy=StoragePolicy.from_env())
    entry = store.entry(work_id, checkpoint_id)
    if entry is None:
        scope = f" with id {checkpoint_id}" if checkpoint_id else ""
        raise HTTPException(
            status_code=404,
            detail=f"no checkpoint held for this work{scope}",
        )
    latest_entry = store.entry(work_id)
    try:
        manifest_bytes, blobs = store.read_checkpoint(entry)
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
    store = CheckpointStore(_store_dir(), policy=StoragePolicy.from_env())
    return {"checkpoints": store.list_entries()}
