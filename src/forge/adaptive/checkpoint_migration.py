"""Q35-21: the operational migration of checkpoint metadata to one authority.

The in-process split between upload and resume authorities is CLOSED
(:mod:`forge.adaptive.checkpoint_repository` — one configured repository
behind :func:`~forge.adaptive.checkpoint_repository.resolve_repository`).
What remained was the OPERATIONAL migration: existing installations hold
their checkpoint index in per-work filesystem JSON (``works/<id>.json``
under the ``best_effort`` contract), and adopting the postgres authority
(``checkpoint_metadata``, alembic 026) needs an explicit, restartable,
idempotent procedure — a bare schema migration migrates no index
CONTENTS, and a silent flip would strand every paused work whose
checkpoint the new authority has never seen.

This module is that procedure, as explicit commands::

    uv run python -m forge.adaptive.checkpoint_migration inventory
    uv run python -m forge.adaptive.checkpoint_migration import \
        --database-url "$DATABASE_URL"
    uv run python -m forge.adaptive.checkpoint_migration verify \
        --database-url "$DATABASE_URL"
    uv run python -m forge.adaptive.checkpoint_migration cutover
    uv run python -m forge.adaptive.checkpoint_migration rollback \
        --verify-report <path>          # the controlled reverse

The command set and its gates:

- **inventory** — a structured report (JSON) of the filesystem store:
  works, index entries, manifests, content digests, references, active
  checkpoints and the pin overlay. Missing and corrupt entries are
  LISTED, never invented and never silently skipped.
- **import** — idempotent copy of the old metadata into the postgres
  authority through the repository protocol (the same verified landing
  the upload route uses). Every blob is digest-verified BEFORE the
  import; a missing blob makes that ENTRY unimportable — reported, the
  run continues with the rest, the exit code is partial. Re-running
  changes nothing: the natural keys ``(work_id, checkpoint_id)`` make a
  repeated import a no-op, and selection is derived from the rows, so
  no re-import can alter which checkpoint is active. Import runs with
  on-upload retention DISABLED — a migration must not silently drop the
  history the old index kept.
- **verify** — per-work comparison of the old index against the new
  authority: active-entry identity, entry sets, every PINNED exact
  reference resolved by the new backend, and blob reachability through
  the configured root. A disagreement is a DISAGREEMENT — both actives
  are reported and the operator resolves it; arrival time is never
  authority and no timestamp ever picks a winner (R28-06 discipline).
- **cutover** — flips the deployment's authority MARKER (a state file
  the deployment reads: ``<store-root>/migration/authority.json``) —
  only after verify passes, only under an exclusive fence on the shared
  blob volume (``<store-root>/migration/cutover.lock``), never deleting
  the old inventory. After cutover exactly ONE backend accepts
  mutations: wrap the resolved repository with
  :func:`enforce_authority_marker` (the documented composition for
  deployments) and the other authority refuses every mutation with the
  typed :class:`MutationsFencedError` while its immutable READS stay
  available.
- **rollback** — forward-only by default: the reverse is REFUSED unless
  a clean verify report for the CURRENT data state is supplied
  (``--verify-report <path>``). Checkpoints uploaded after cutover
  exist only in the database; the documented recovery path is
  ``import --reverse`` (database -> filesystem index, same discipline),
  then verify, then rollback.

The observability names the issue asks for ride in the reports:
``migration.checkpoint_coverage``, ``migration.conflicts``,
``storage.blob_reachability`` (see :func:`run_verify` and
:func:`preflight`).

Blob volumes: the CAS blobs are content-addressed filesystem bytes
under BOTH contracts, so a shared database does NOT make node-local
content shared. :func:`preflight` (wired into ``forge doctor``) warns —
never blocks — when the configured root looks node-local, and the
runbook documents the heuristic and the two-replica requirement.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

try:  # POSIX process-level advisory locking (Linux CI, macOS dev boxes).
    import fcntl
except ImportError:  # pragma: no cover — non-POSIX platform without flock
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "AUTHORITY_MARKER_SCHEMA",
    "EXIT_OK",
    "EXIT_PARTIAL",
    "EXIT_REFUSED",
    "IMPORT_SCHEMA",
    "INVENTORY_SCHEMA",
    "MigrationCommandError",
    "MutationsFencedError",
    "SingleAuthorityRepository",
    "VERIFY_SCHEMA",
    "authority_marker_path",
    "cutover_fence",
    "cutover_in_progress",
    "enforce_authority_marker",
    "main",
    "preflight",
    "read_authority_marker",
    "run_cutover",
    "run_import",
    "run_inventory",
    "run_rollback",
    "run_verify",
    "scan_inventory",
]

#: Exit codes: 0 clean, 1 refused/failed, 3 import completed PARTIALLY
#: (some entries unimportable — reported, the rest imported).
EXIT_OK: Final = 0
EXIT_REFUSED: Final = 1
EXIT_PARTIAL: Final = 3

#: The report/marker document schemas (versioned once, checked on load).
INVENTORY_SCHEMA: Final = "forge.checkpoint.migration.inventory/1"
IMPORT_SCHEMA: Final = "forge.checkpoint.migration.import/1"
VERIFY_SCHEMA: Final = "forge.checkpoint.migration.verify/1"
AUTHORITY_MARKER_SCHEMA: Final = "forge.checkpoint.authority/1"

#: The observability metric names (Q35-21's observability section).
METRIC_COVERAGE: Final = "migration.checkpoint_coverage"
METRIC_CONFLICTS: Final = "migration.conflicts"
METRIC_REACHABILITY: Final = "storage.blob_reachability"

#: How long a fence taker retries (jittered) before refusing — the same
#: budget class as the index/pin locks (NEXT-06); cutover writes are rare.
_FENCE_WAIT_SECONDS: Final = 5.0

#: A content address is the only string that becomes a CAS path here —
#: the store's own rule (R28-01), mirrored so the migration can never be
#: talked into reading outside the CAS by a malformed index entry.
_HEX64_LEN: Final = 64


class MigrationCommandError(Exception):
    """A migration command refused: bad state, bad arguments, failed gate.

    The message is the operator's instruction — which gate failed and
    the documented recovery path. Raised by the command functions; the
    CLI prints it and exits :data:`EXIT_REFUSED`.
    """


class MutationsFencedError(RuntimeError):
    """A metadata mutation refused: another authority owns the store.

    The SPECIFIC refusal the fenced backend answers with after a
    cutover (or while a cutover holds the fence): exactly one backend
    accepts mutations, the other refuses with this error while its
    immutable READS stay available. Never retried automatically — the
    operator either cut back (``rollback --verify-report ...``) or fixes
    the deployment's configured authority.
    """


class _InterruptedMigration(RuntimeError):
    """The test seam for crash-at-a-boundary proofs (never CLI-visible)."""


def _now_iso() -> str:
    # Microsecond precision: the rollback gate compares a verify report's
    # generated_at against the marker's flipped_at, and whole seconds
    # would make "taken after the flip" undecidable within one second.
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_address(digest: object) -> bool:
    return (
        isinstance(digest, str)
        and len(digest) == _HEX64_LEN
        and all(character in "0123456789abcdef" for character in digest)
    )


# ---------------------------------------------------------------------------
# Paths and atomic documents
# ---------------------------------------------------------------------------


def migration_dir(root: Path | str) -> Path:
    """The migration's state directory: ``<store-root>/migration``."""
    return Path(root) / "migration"


def authority_marker_path(root: Path | str) -> Path:
    """The deployment authority marker: ``<store-root>/migration/authority.json``.

    The state file CUTOVER writes atomically and the deployment READS
    (through :func:`enforce_authority_marker` at composition time): it
    names the ONE authority that accepts metadata mutations. It is a
    marker, not an index — it never decides which checkpoint is active.
    """
    return migration_dir(root) / "authority.json"


def _fence_path(root: Path | str) -> Path:
    return migration_dir(root) / "cutover.lock"


def _write_json_atomic(path: Path, document: Mapping[str, Any]) -> None:
    """Land *document* atomically (temp + rename + fsync) — never torn."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-migration-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(document), handle, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        os.unlink(tmp_name)
        raise


def _load_document(path: Path, expected_schema: str) -> dict[str, Any]:
    """Load one of this module's documents, refusing foreign/junk bytes."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MigrationCommandError(f"cannot read {path}: {exc}") from exc
    except ValueError as exc:
        raise MigrationCommandError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema") != expected_schema:
        found = document.get("schema") if isinstance(document, dict) else None
        raise MigrationCommandError(
            f"{path} is not a {expected_schema} document (found {found!r}) — "
            "refusing to act on a report this tool did not write"
        )
    return document


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


# ---------------------------------------------------------------------------
# The cutover fence — an explicit advisory lock on the shared blob volume
# ---------------------------------------------------------------------------


@contextmanager
def cutover_fence(root: Path | str, *, wait_seconds: float = _FENCE_WAIT_SECONDS) -> Iterator[None]:
    """The exclusive fence a cutover/rollback/reverse-import holds.

    An advisory ``flock`` at ``<store-root>/migration/cutover.lock`` —
    the equivalent-explicit-fence spelling of the repository's
    ``first_upload_lock`` mechanics (a session-scoped advisory anchor
    exists only under the postgres dialect; the fence must serialize
    BOTH authorities, and every durability contract already requires
    the blob volume to be shared, so the lock lives there). While it is
    held, every repository wrapped by
    :func:`enforce_authority_marker` refuses metadata mutations on both
    sides; the marker write itself happens inside it, so no reader can
    observe a half-flipped authority.
    """
    if fcntl is None:  # pragma: no cover — non-POSIX without flock
        yield
        return
    path = _fence_path(root)
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
                    raise MigrationCommandError(
                        f"another cutover holds the fence at {path} — a checkpoint "
                        "authority cutover is single-flight; retry once it completes"
                    ) from None
                time.sleep(min(random.uniform(0.0005, 0.003), remaining))
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def cutover_in_progress(root: Path | str) -> bool:
    """Whether a cutover/rollback currently holds the fence (a probe)."""
    if fcntl is None:  # pragma: no cover — non-POSIX without flock
        return False
    path = _fence_path(root)
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


# ---------------------------------------------------------------------------
# The filesystem-store inventory
# ---------------------------------------------------------------------------


def _cas_path(root: Path, digest: str) -> Path:
    return root / digest[:2] / digest


def _read_digest(root: Path, digest: str, *, verify: bool) -> tuple[str, bytes | None]:
    """One CAS artifact's (status, bytes): ``ok`` | ``missing`` | ``corrupt``.

    *verify* re-hashes the bytes to the address (the import/verify
    spelling); without it the check is reachability (presence) only —
    the cheap spelling the doctor preflight uses.
    """
    path = _cas_path(root, digest)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return "missing", None
    except OSError as exc:
        raise MigrationCommandError(f"cannot read {path}: {exc}") from exc
    if verify and _sha256(data) != digest:
        return "corrupt", None
    return "ok", data


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


def _entry_order(entry: Mapping[str, Any]) -> tuple[int, str]:
    """The deterministic selection key — ``(sequence, checkpoint_id)``.

    The same rule the store and the metadata table pin (R28-06): arrival
    order is never authority, and ties break by content address, so the
    migration's active derivation can never disagree with either backend.
    """
    sequence = entry.get("sequence")
    return (sequence if isinstance(sequence, int) else 0, str(entry.get("checkpoint_id") or ""))


def _inspect_checkpoint(
    root: Path, checkpoint_id: str, *, verify_digests: bool
) -> tuple[dict[str, Any], bytes | None, dict[str, bytes]]:
    """One checkpoint's on-disk verdict: manifest + every referenced blob.

    Returns the report fragment, the manifest bytes (when readable and —
    under *verify_digests* — hashing to their address) and the blob
    bytes (the import closure). Problems are NAMED per digest; nothing
    is invented and nothing is silently skipped.
    """
    problems: list[str] = []
    if not _is_address(checkpoint_id):
        problems.append(f"checkpoint id {checkpoint_id!r} is not a content address")
        return {"status": "invalid", "problems": problems}, None, {}
    manifest_status, manifest_bytes = _read_digest(root, checkpoint_id, verify=verify_digests)
    if manifest_status != "ok":
        problems.append(f"manifest {manifest_status}: {checkpoint_id}")
        return {"status": manifest_status, "problems": problems}, None, {}
    assert manifest_bytes is not None
    blobs: dict[str, bytes] = {}
    blob_states: dict[str, str] = {}
    for digest in sorted(_entry_files(manifest_bytes)):
        status, data = _read_digest(root, digest, verify=verify_digests)
        blob_states[digest] = status
        if status != "ok":
            problems.append(f"blob {status}: {digest}")
            continue
        assert data is not None
        blobs[digest] = data
    return (
        {"status": "ok", "blobs": blob_states, "problems": problems},
        manifest_bytes,
        blobs,
    )


def scan_inventory(root: Path | str, *, verify_digests: bool = True) -> dict[str, Any]:
    """The structured inventory of the filesystem checkpoint store.

    Walks ``works/<id>.json`` (the best-effort authority's index), every
    entry's manifest and its referenced blobs, and the pin overlay —
    producing the machine-readable report ``inventory`` writes and
    ``import`` consumes as its EXPLICIT SOURCE MANIFEST. Missing and
    corrupt artifacts are listed per entry with their digests; an
    unparsable index file is named, never skipped silently; the summary
    counts every class of problem so a script can gate on them.
    """
    root = Path(root)
    works: list[dict[str, Any]] = []
    unparsable: list[str] = []
    pins_by_work: dict[str, list[str]] = {}
    for pin in _list_pins(root):
        work_id = str(pin.get("work_id") or "")
        checkpoint_id = str(pin.get("checkpoint_id") or "")
        if work_id and checkpoint_id:
            pins_by_work.setdefault(work_id, []).append(checkpoint_id)
    works_dir = root / "works"
    index_files = sorted(works_dir.glob("*.json")) if works_dir.is_dir() else []
    totals = {
        "checkpoints": 0,
        "importable_entries": 0,
        "unimportable_entries": 0,
        "missing_manifests": 0,
        "corrupt_manifests": 0,
        "missing_blobs": 0,
        "corrupt_blobs": 0,
        "pinned_checkpoints": 0,
    }
    for index_file in index_files:
        try:
            document = json.loads(index_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            unparsable.append(f"works/{index_file.name}: {exc}")
            continue
        if not isinstance(document, dict) or not isinstance(document.get("checkpoints"), list):
            unparsable.append(f"works/{index_file.name}: not an index document")
            continue
        work_id = str(document.get("work_id") or index_file.stem)
        entries: list[dict[str, Any]] = []
        for raw in document["checkpoints"]:
            if not isinstance(raw, dict):
                continue
            checkpoint_id = str(raw.get("checkpoint_id") or "")
            fragment, _manifest, _blobs = _inspect_checkpoint(
                root, checkpoint_id, verify_digests=verify_digests
            )
            entry = {
                "checkpoint_id": checkpoint_id,
                "sequence": raw.get("sequence"),
                "files": raw.get("files"),
                "uploaded_at": raw.get("uploaded_at"),
                "manifest": fragment["status"],
                "blobs": fragment.get("blobs", {}),
                "problems": fragment["problems"],
                "importable": not fragment["problems"],
            }
            entries.append(entry)
            totals["checkpoints"] += 1
            if entry["importable"]:
                totals["importable_entries"] += 1
            else:
                totals["unimportable_entries"] += 1
            if fragment["status"] == "missing":
                totals["missing_manifests"] += 1
            elif fragment["status"] == "corrupt":
                totals["corrupt_manifests"] += 1
            for state in entry["blobs"].values():
                if state == "missing":
                    totals["missing_blobs"] += 1
                elif state == "corrupt":
                    totals["corrupt_blobs"] += 1
        active = max(entries, key=_entry_order) if entries else None
        works.append(
            {
                "work_id": work_id,
                "index_file": f"works/{index_file.name}",
                "active": (
                    {
                        "checkpoint_id": active["checkpoint_id"],
                        "sequence": active["sequence"],
                    }
                    if active is not None
                    else None
                ),
                "entries": entries,
                "pinned": sorted(pins_by_work.pop(work_id, [])),
            }
        )
    # Pins for works with no index file at all — recorded explicitly: an
    # orphaned protection the operator must see, never dropped silently.
    orphaned_pins: list[dict[str, Any]] = []
    for work_id, checkpoint_ids in sorted(pins_by_work.items()):
        works.append(
            {
                "work_id": work_id,
                "index_file": None,
                "active": None,
                "entries": [],
                "pinned": sorted(checkpoint_ids),
                "problems": ["pinned checkpoints but no works/<id>.json index file"],
            }
        )
        totals["pinned_checkpoints"] += len(checkpoint_ids)
        orphaned_pins.append({"work_id": work_id, "checkpoint_ids": sorted(checkpoint_ids)})
    works.sort(key=lambda work: str(work["work_id"]))
    distinct_digests = {
        digest
        for work in works
        for entry in work["entries"]
        if _is_address(entry["checkpoint_id"])
        for digest in ({entry["checkpoint_id"], *entry["blobs"]})
    }
    return {
        "schema": INVENTORY_SCHEMA,
        "generated_at": _now_iso(),
        "store_root": str(root),
        "works": works,
        "unparsable_index_files": unparsable,
        "orphaned_pins": orphaned_pins,
        "summary": {"works": len(works), "distinct_digests": len(distinct_digests), **totals},
    }


def _list_pins(root: Path) -> list[dict[str, Any]]:
    """The pin overlay through the sibling module's public listing."""
    from forge.adaptive.checkpoint_repository import CheckpointPins

    try:
        return CheckpointPins(root).list()
    except OSError:  # pragma: no cover — an unreadable pins dir is an empty overlay
        return []


# ---------------------------------------------------------------------------
# Import — idempotent, digest-verified BEFORE the write
# ---------------------------------------------------------------------------


def _migration_policy() -> Any:
    """The env policy with on-upload retention DISABLED for migrations.

    A migration must not silently drop the history the old index kept:
    the operator's retention posture applies again AFTER the cutover,
    judged by the authority that now owns the index. The caps stay the
    env's (a deployment that raised them did so for its real blobs).
    """
    from forge.api_checkpoint_channel import StoragePolicy

    return replace(
        StoragePolicy.from_env(),
        max_checkpoints_per_work=0,
        history_keep=0,
        cleanup_trigger="manual",
    )


async def run_import(
    root: Path | str,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    inventory_path: Path | None = None,
    reverse: bool = False,
    out: Path | None = None,
    fail_after: int | None = None,
) -> tuple[dict[str, Any], int]:
    """Idempotent import of old metadata into the target authority.

    Forward (the default): every importable entry of the filesystem
    inventory lands in the postgres authority through the REPOSITORY
    protocol — the same verified landing (address prelude, policy caps,
    transactional index insert under the composite natural key) the
    upload route uses — with manifest and blobs digest-verified BEFORE
    the import. An entry whose blobs are missing/rotten on disk is
    reported as unimportable and the run CONTINUES with the rest; the
    exit code is :data:`EXIT_PARTIAL`.

    ``reverse`` (the documented rollback recovery path): every database
    entry the filesystem index lacks is copied BACK into the index, the
    same discipline in the other direction, under the cutover fence.

    Re-running forward is a NO-OP: entries already present in the
    target are skipped (no duplicate authority) and selection is
    DERIVED from the entry set, so no import can alter which checkpoint
    is active.

    *fail_after* is the crash-window test seam: raise
    ``_InterruptedMigration`` after that many landings (never CLI-set).
    """
    root = Path(root)
    if reverse:
        return await _run_reverse_import(root, session_factory, out=out, fail_after=fail_after)
    if inventory_path is not None:
        inventory = _load_document(Path(inventory_path), INVENTORY_SCHEMA)
    else:
        inventory = scan_inventory(root)
    from forge.adaptive.checkpoint_repository import PostgresCheckpointRepository

    repository = PostgresCheckpointRepository(root, session_factory, policy=_migration_policy())
    already = {
        (str(entry["work_id"]), str(entry["checkpoint_id"]))
        for entry in await repository.list_entries()
    }
    imported: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    unimportable: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    landed = 0
    for work in inventory["works"]:
        work_id = str(work["work_id"])
        for entry in work["entries"]:
            checkpoint_id = str(entry["checkpoint_id"])
            if not entry.get("importable", False):
                unimportable.append(
                    {
                        "work_id": work_id,
                        "checkpoint_id": checkpoint_id,
                        "problems": list(entry["problems"]),
                    }
                )
                continue
            if (work_id, checkpoint_id) in already:
                skipped.append({"work_id": work_id, "checkpoint_id": checkpoint_id})
                continue
            _fragment, manifest_bytes, blobs = _inspect_checkpoint(
                root, checkpoint_id, verify_digests=True
            )
            if manifest_bytes is None or len(blobs) != len(entry.get("blobs", {})):
                # The store changed under the scan — an honest refusal;
                # the next pass re-reports. Never import bytes that are gone.
                unimportable.append(
                    {
                        "work_id": work_id,
                        "checkpoint_id": checkpoint_id,
                        "problems": ["digests disappeared between inventory and import"],
                    }
                )
                continue
            try:
                await repository.put(work_id, checkpoint_id, manifest_bytes, blobs)
            except Exception as exc:  # noqa: BLE001 — one bad entry never sinks the rest
                failed.append(
                    {
                        "work_id": work_id,
                        "checkpoint_id": checkpoint_id,
                        "error": f"{exc.__class__.__name__}: {exc}"[:300],
                    }
                )
                continue
            imported.append({"work_id": work_id, "checkpoint_id": checkpoint_id})
            landed += 1
            if fail_after is not None and landed >= fail_after:
                raise _InterruptedMigration(
                    f"interrupted after {landed} landing(s) — the crash-window probe"
                )
    report = {
        "schema": IMPORT_SCHEMA,
        "generated_at": _now_iso(),
        "store_root": str(root),
        "direction": "filesystem->postgres",
        "source_manifest": str(inventory_path) if inventory_path is not None else "fresh-scan",
        "imported": imported,
        "skipped_already_present": skipped,
        "unimportable": unimportable,
        "failed": failed,
        "summary": {
            "entries_considered": len(imported) + len(skipped) + len(unimportable) + len(failed),
            "imported": len(imported),
            "skipped_already_present": len(skipped),
            "unimportable": len(unimportable),
            "failed": len(failed),
            "partial": bool(unimportable or failed),
        },
    }
    if out is not None:
        _write_json_atomic(Path(out), report)
    exit_code = EXIT_OK if not (unimportable or failed) else EXIT_PARTIAL
    return report, exit_code


async def _run_reverse_import(
    root: Path,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    out: Path | None,
    fail_after: int | None,
) -> tuple[dict[str, Any], int]:
    """The documented rollback recovery: database entries -> the old index.

    Checkpoints uploaded after a cutover exist only as database rows;
    their BLOBS are shared CAS bytes, so copying the entry back into the
    filesystem index is a plain verified re-put. Runs under the cutover
    fence (the runtime guard would refuse — deliberately: this IS the
    operator's maintenance path, not a deployment runtime mutation).
    """
    from forge.adaptive.checkpoint_repository import (
        FilesystemCheckpointRepository,
        PostgresCheckpointRepository,
    )

    source = PostgresCheckpointRepository(root, session_factory, policy=_migration_policy())
    target = FilesystemCheckpointRepository(root, policy=_migration_policy())
    inventory = scan_inventory(root)
    known = {
        (str(work["work_id"]), str(entry["checkpoint_id"]))
        for work in inventory["works"]
        for entry in work["entries"]
    }
    imported: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    landed = 0
    with cutover_fence(root):
        for entry in await source.list_entries():
            work_id, checkpoint_id = str(entry["work_id"]), str(entry["checkpoint_id"])
            if (work_id, checkpoint_id) in known:
                skipped.append({"work_id": work_id, "checkpoint_id": checkpoint_id})
                continue
            try:
                named = await source.entry(work_id, checkpoint_id)
                if named is None:
                    raise MigrationCommandError(
                        f"the database no longer holds {work_id}@{checkpoint_id}"
                    )
                manifest_bytes, blobs = await source.read_entry(named)
                await target.put(work_id, checkpoint_id, manifest_bytes, blobs)
            except Exception as exc:  # noqa: BLE001 — continue with the rest
                failed.append(
                    {
                        "work_id": work_id,
                        "checkpoint_id": checkpoint_id,
                        "error": f"{exc.__class__.__name__}: {exc}"[:300],
                    }
                )
                continue
            imported.append({"work_id": work_id, "checkpoint_id": checkpoint_id})
            landed += 1
            if fail_after is not None and landed >= fail_after:
                raise _InterruptedMigration(
                    f"interrupted after {landed} landing(s) — the crash-window probe"
                )
    report = {
        "schema": IMPORT_SCHEMA,
        "generated_at": _now_iso(),
        "store_root": str(root),
        "direction": "postgres->filesystem",
        "source_manifest": "checkpoint_metadata",
        "imported": imported,
        "skipped_already_present": skipped,
        "unimportable": [],
        "failed": failed,
        "summary": {
            "entries_considered": len(imported) + len(skipped) + len(failed),
            "imported": len(imported),
            "skipped_already_present": len(skipped),
            "unimportable": 0,
            "failed": len(failed),
            "partial": bool(failed),
        },
    }
    if out is not None:
        _write_json_atomic(Path(out), report)
    return report, (EXIT_OK if not failed else EXIT_PARTIAL)


# ---------------------------------------------------------------------------
# Verify — the gate before any authority flip
# ---------------------------------------------------------------------------


async def run_verify(
    root: Path | str,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    out: Path | None = None,
) -> tuple[dict[str, Any], int]:
    """Per-work old-index vs new-authority comparison + blob reachability.

    ``clean`` requires ALL of: every filesystem entry present in the
    database and vice versa (entry-set equality per work), the DERIVED
    actives agreeing, every PINNED exact reference resolvable by the new
    backend, and every referenced digest reachable (and hashing to its
    address) through the configured root. Any disagreement is REPORTED
    with both sides — the operator resolves it; no timestamp, no
    arrival order, no silent winner ever selects here.
    """
    root = Path(root)
    from forge.adaptive.checkpoint_repository import PostgresCheckpointRepository

    repository = PostgresCheckpointRepository(root, session_factory, policy=_migration_policy())
    inventory = scan_inventory(root)
    db_rows = await repository.list_entries()
    db_by_work: dict[str, dict[str, dict[str, Any]]] = {}
    for row in db_rows:
        db_by_work.setdefault(str(row["work_id"]), {})[str(row["checkpoint_id"])] = row

    works_reports: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    missing_in_db = 0
    missing_in_index = 0
    unreachable: list[dict[str, str]] = []
    fs_by_work = {str(work["work_id"]): work for work in inventory["works"]}
    for work_id in sorted(set(fs_by_work) | set(db_by_work)):
        fs_work = fs_by_work.get(work_id)
        fs_entries = {
            str(entry["checkpoint_id"]): entry for entry in (fs_work["entries"] if fs_work else [])
        }
        db_entries = db_by_work.get(work_id, {})
        disagreements: list[str] = []
        only_fs = sorted(set(fs_entries) - set(db_entries))
        only_db = sorted(set(db_entries) - set(fs_entries))
        for checkpoint_id in only_fs:
            disagreements.append(f"entry {checkpoint_id} is in the filesystem index only")
        for checkpoint_id in only_db:
            disagreements.append(f"entry {checkpoint_id} is in the database only")
        missing_in_db += len(only_fs)
        missing_in_index += len(only_db)
        fs_active = str(fs_work["active"]["checkpoint_id"]) if fs_work and fs_work["active"] else ""
        db_active = (
            str(max(db_entries.values(), key=_entry_order)["checkpoint_id"]) if db_entries else ""
        )
        if fs_entries and db_entries and fs_active != db_active:
            # Both actives are reported; a human decides. Timestamps and
            # arrival order are deliberately absent from this branch.
            conflicts.append(
                {
                    "work_id": work_id,
                    "filesystem_active": fs_active,
                    "database_active": db_active,
                    "resolution": "operator decision required — never selected by time",
                }
            )
            disagreements.append(
                f"active pointer conflict: filesystem={fs_active} database={db_active}"
            )
        # Reachability of every database entry's closure through the root.
        for checkpoint_id in sorted(db_entries):
            fragment, _manifest, _blobs = _inspect_checkpoint(
                root, checkpoint_id, verify_digests=True
            )
            if fragment["status"] != "ok":
                unreachable.append({"digest": checkpoint_id, "work_id": work_id})
                disagreements.append(
                    f"manifest {fragment['status']} for database entry {checkpoint_id}"
                )
            for digest, state in fragment.get("blobs", {}).items():
                if state != "ok":
                    unreachable.append({"digest": digest, "work_id": work_id})
                    disagreements.append(f"blob {state}: {digest} (database entry {checkpoint_id})")
        works_reports.append(
            {
                "work_id": work_id,
                "filesystem": {"active": fs_active or None, "entries": sorted(fs_entries)},
                "database": {"active": db_active or None, "entries": sorted(db_entries)},
                "disagreements": disagreements,
            }
        )
    pin_reports: list[dict[str, Any]] = []
    pins_failed = 0
    for pin in _list_pins(root):
        work_id, checkpoint_id = str(pin.get("work_id") or ""), str(pin.get("checkpoint_id") or "")
        if not work_id or not checkpoint_id:
            continue
        record: dict[str, Any] = {"work_id": work_id, "checkpoint_id": checkpoint_id}
        try:
            entry = await repository.entry(work_id, checkpoint_id)
            if entry is None:
                record.update(
                    {"resolved": False, "reason": "the database authority holds no such checkpoint"}
                )
            else:
                try:
                    await repository.read_entry(entry)  # the pinned bytes must be servable
                    record["resolved"] = True
                except Exception as exc:  # noqa: BLE001 — reported, never fatal
                    record.update(
                        {"resolved": False, "reason": f"{exc.__class__.__name__}: {exc}"[:200]}
                    )
        except Exception as exc:  # noqa: BLE001 — reported, never fatal to the report
            record.update({"resolved": False, "reason": f"{exc.__class__.__name__}: {exc}"[:200]})
        if not record.get("resolved"):
            pins_failed += 1
        pin_reports.append(record)
    clean = not any(work["disagreements"] for work in works_reports) and pins_failed == 0
    report = {
        "schema": VERIFY_SCHEMA,
        "generated_at": _now_iso(),
        "store_root": str(root),
        "clean": clean,
        "works": works_reports,
        "pins": pin_reports,
        "metrics": {
            METRIC_COVERAGE: {
                "works_filesystem": sum(1 for w in inventory["works"] if w["entries"]),
                "works_database": len(db_by_work),
                "missing_in_database": missing_in_db,
                "missing_in_index": missing_in_index,
            },
            METRIC_CONFLICTS: {"count": len(conflicts), "active_disagreements": conflicts},
            METRIC_REACHABILITY: {"checked": len(db_rows), "unreachable_count": len(unreachable)},
        },
        "unreachable": unreachable,
    }
    if out is not None:
        _write_json_atomic(Path(out), report)
    return report, (EXIT_OK if clean else EXIT_REFUSED)


# ---------------------------------------------------------------------------
# Cutover and rollback — the authority marker under the fence
# ---------------------------------------------------------------------------


def _require_clean_report(path: Path, root: Path) -> dict[str, Any]:
    """The gate both flips share: a CLEAN verify report for THIS root."""
    document = _load_document(path, VERIFY_SCHEMA)
    if document.get("clean") is not True:
        raise MigrationCommandError(
            f"the verify report at {path} is not clean — refusing to flip the authority; "
            "run `python -m forge.adaptive.checkpoint_migration verify --out <path>` against "
            "the CURRENT data state, resolve every disagreement it reports (operator decision "
            "— never timestamp selection), and retry with the clean report"
        )
    if Path(str(document.get("store_root"))).resolve() != root.resolve():
        raise MigrationCommandError(
            f"the verify report at {path} was taken for store root "
            f"{document.get('store_root')!r}, not {str(root)!r} — a report for the current "
            "data state is required"
        )
    return document


def _require_current_index(root: Path, report: dict[str, Any]) -> None:
    """The CURRENT-data-state gate's index half, shared by both flips.

    Compares the verify report's filesystem-entry view against a FRESH
    light scan of the index (identity only — no digests, no database):
    an upload that landed on the old authority AFTER the report was
    taken changes the store the flip would freeze, so the flip refuses
    until a fresh verify names the new state. Cheap by design — it is
    the same guard the rollback gate enforces through time.
    """
    current = {
        str(work["work_id"]): sorted(str(entry["checkpoint_id"]) for entry in work["entries"])
        for work in scan_inventory(root, verify_digests=False)["works"]
    }
    reported = {
        str(work["work_id"]): sorted(str(id) for id in work["filesystem"]["entries"])
        for work in report["works"]
    }
    if current != reported:
        changed = sorted(
            work_id
            for work_id in set(current) | set(reported)
            if current.get(work_id) != reported.get(work_id)
        )
        raise MigrationCommandError(
            "the filesystem index changed since the verify report was taken "
            f"(works: {', '.join(changed[:5])}) — a flip must gate on the CURRENT "
            "data state; re-run `python -m forge.adaptive.checkpoint_migration "
            "verify` and retry with the fresh report"
        )


def run_cutover(
    root: Path | str, *, verify_report: Path | None = None, to: str = "postgres"
) -> dict[str, Any]:
    """Flip the deployment authority marker — only after verify passes.

    Writes ``<store-root>/migration/authority.json`` atomically under
    the cutover fence. The marker is the state file the DEPLOYMENT
    reads: wrap the resolved repository with
    :func:`enforce_authority_marker` and the named authority becomes
    the only backend whose mutations succeed. The flip itself does not
    change which checkpoint is active anywhere — selection is derived
    per authority from unchanged data. The old inventory is NEVER
    deleted; the reverse is :func:`run_rollback`, gated the same way.
    """
    # Resolved once: the marker's path must be spelling-independent, so a
    # cutover run with a relative --root and a rollback run with the
    # absolute form agree on where the authority marker lives.
    root = Path(root).resolve()
    from forge.api_checkpoint_channel import DURABILITY_MODES

    if to not in DURABILITY_MODES:
        raise MigrationCommandError(f"unknown target authority {to!r}")
    report_path = (
        Path(verify_report) if verify_report is not None else migration_dir(root) / "verify.json"
    )
    document = _require_clean_report(report_path, root)
    _require_current_index(root, document)
    marker = read_authority_marker(root)
    if marker is not None and marker.get("authority") == to:
        raise MigrationCommandError(
            f"the authority marker at {authority_marker_path(root)} already names {to!r} — "
            "nothing to cut over (the reverse path is `rollback --verify-report ...`)"
        )
    previous = (
        str(marker.get("authority"))
        if marker is not None and marker.get("authority")
        else "filesystem"
    )
    document = {
        "schema": AUTHORITY_MARKER_SCHEMA,
        "authority": to,
        "previous_authority": previous,
        "flipped_at": _now_iso(),
        "store_root": str(root),
        "verify_report": str(report_path),
        "verify_report_sha256": _sha256(report_path.read_bytes()),
    }
    with cutover_fence(root):
        _write_json_atomic(authority_marker_path(root), document)
    return document


def run_rollback(root: Path | str, *, verify_report: Path) -> dict[str, Any]:
    """The controlled reverse: restore the previous authority marker.

    Forward-only by default — the rollback is REFUSED unless a CLEAN
    verify report for the CURRENT data state is supplied
    (``--verify-report <path>``): checkpoints uploaded after the
    cutover exist only in the database, and flipping back without them
    would strand paused work behind an index that cannot see it. The
    documented recovery path is ``import --reverse`` (database entries
    back into the filesystem index), then ``verify``, then this
    command. The old inventory was never deleted; nothing here deletes
    anything either.
    """
    root = Path(root).resolve()  # the same spelling-independent marker path
    marker = read_authority_marker(root)
    if marker is None:
        raise MigrationCommandError(
            f"no authority marker at {authority_marker_path(root)} — nothing to roll back"
        )
    if marker.get("authority") != "postgres":
        raise MigrationCommandError(
            f"the authority marker names {marker.get('authority')!r}, not 'postgres' — "
            "rollback restores the filesystem authority from a postgres cutover only"
        )
    report_path = Path(verify_report)
    report = _require_clean_report(report_path, root)
    # The CURRENT-data-state gate's time half: a report TAKEN BEFORE the
    # cutover cannot describe the state the rollback would return to
    # (uploads that landed after the flip exist only in the database).
    # The runbook's procedure re-runs verify after the cutover — one
    # command — so this gate is always satisfiable when the data agrees.
    report_dt = _parse_iso(str(report.get("generated_at") or ""))
    flip_dt = _parse_iso(str(marker.get("flipped_at") or ""))
    if report_dt is None or flip_dt is None or report_dt < flip_dt:
        raise MigrationCommandError(
            f"the verify report at {report_path} predates the current authority state "
            f"(report {report.get('generated_at')!r} vs cutover {marker.get('flipped_at')!r}) — "
            "re-run `python -m forge.adaptive.checkpoint_migration verify` AFTER the "
            "cutover; if post-cutover uploads exist, recover them first with "
            "`import --reverse` (the documented recovery path), then verify, then retry"
        )
    _require_current_index(root, report)
    document = {
        "schema": AUTHORITY_MARKER_SCHEMA,
        "authority": "filesystem",
        "previous_authority": "postgres",
        "flipped_at": _now_iso(),
        "store_root": str(root),
        "verify_report": str(report_path),
        "verify_report_sha256": _sha256(report_path.read_bytes()),
        "rolled_back_from": marker.get("flipped_at", ""),
    }
    with cutover_fence(root):
        _write_json_atomic(authority_marker_path(root), document)
    return document


# ---------------------------------------------------------------------------
# Exactly one mutation authority — the deployment-side guard
# ---------------------------------------------------------------------------


class SingleAuthorityRepository:
    """One repository, mutation-fenced by the deployment authority marker.

    The wrapper deployments compose per the cutover documentation:
    ``enforce_authority_marker(resolve_repository(...))``. Mutations
    (``put``/``put_checkpoint``/``apply_retention``) are refused with
    the typed :class:`MutationsFencedError` when the marker at
    ``<store-root>/migration/authority.json`` names a DIFFERENT
    authority, or while a cutover holds the fence; immutable reads
    (``entry``, ``read``, ``read_entry``, ``pins``, listings, health)
    pass through untouched, so the old root stays readable exactly as
    documented. The marker is re-read on every mutation — a running
    process switches behavior when the cutover lands, without a
    restart of the guard itself.
    """

    def __init__(self, repository: Any, root: Path | str | None = None) -> None:
        self._repository = repository
        self._root = Path(root) if root is not None else _default_store_root()

    async def _refuse_if_fenced(self) -> None:
        if cutover_in_progress(self._root):
            raise MutationsFencedError(
                "a checkpoint authority cutover is in progress (fence at "
                f"{_fence_path(self._root)}) — mutations are fenced on both sides; "
                "retry once the cutover completes"
            )
        marker = read_authority_marker(self._root)
        if marker is None:
            return
        named = str(marker.get("authority") or "")
        if named and named != await self._repository.authority():
            raise MutationsFencedError(
                f"the checkpoint authority marker at {authority_marker_path(self._root)} names "
                f"{named!r}; this repository refuses metadata mutations — immutable reads "
                "stay available; recovery: `python -m forge.adaptive.checkpoint_migration "
                "rollback --verify-report <path>`"
            )

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


def _default_store_root() -> Path:
    from forge.api_checkpoint_channel import CHECKPOINT_STORE_DIR_ENV, DEFAULT_CHECKPOINT_ROOT

    return Path(os.environ.get(CHECKPOINT_STORE_DIR_ENV, "").strip() or DEFAULT_CHECKPOINT_ROOT)


def enforce_authority_marker(
    repository: Any, root: Path | str | None = None
) -> SingleAuthorityRepository:
    """Wrap *repository* so exactly ONE authority accepts mutations.

    The documented deployment composition after a cutover (the marker's
    reader): ``enforce_authority_marker(resolve_repository(...))``. The
    marker lives at ``<store-root>/migration/authority.json`` and is
    written only by :func:`run_cutover`/:func:`run_rollback` under the
    cutover fence. Reads never fence.
    """
    return SingleAuthorityRepository(repository, root)


# ---------------------------------------------------------------------------
# The doctor preflight — coverage, conflicts, blob-volume topology
# ---------------------------------------------------------------------------

#: Node-local-looking path prefixes — the honest heuristic's deny-list.
#: A relative store root is node-local by definition (each process's
#: CWD); temp directories are per-machine. Everything else is ASSUMED
#: shared and named as an assumption, not a proof.
_NODE_LOCAL_PREFIXES: Final = ("/tmp/", "/private/tmp/", "/var/tmp/", "/private/var/folders/")


def _looks_node_local(root: Path) -> bool:
    """The documented heuristic — a warning, never a block (Q35-21)."""
    if not root.is_absolute():
        return True
    resolved = str(root)
    if any(resolved.startswith(prefix) for prefix in _NODE_LOCAL_PREFIXES):
        return True
    temp_root = str(Path(tempfile.gettempdir()))
    return bool(temp_root) and resolved.startswith(temp_root)


async def _async_db_entry_map(
    database_url: str, root: Path
) -> dict[str, dict[str, dict[str, Any]]]:
    """The per-work database entry map through the repository protocol.

    Read-only (``bootstrap=False``): the doctor preflight observes the
    metadata table, it never creates schema or files — an absent table
    is a not-visible note, exactly like an unreachable database.
    """
    from forge.adaptive.checkpoint_repository import PostgresCheckpointRepository

    engine = await open_metadata_engine(database_url, bootstrap=False)
    try:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        repository = PostgresCheckpointRepository(
            root, async_sessionmaker(engine, expire_on_commit=False)
        )
        entries: dict[str, dict[str, dict[str, Any]]] = {}
        for row in await repository.list_entries():
            entries.setdefault(str(row["work_id"]), {})[str(row["checkpoint_id"])] = row
        return entries
    finally:
        await engine.dispose()


async def preflight(
    root: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    database_url: str = "",
) -> dict[str, Any]:
    """The doctor's three migration preflight views (read-only).

    - **coverage** — while the postgres authority is ACTIVE: how many
      works still live only on the filesystem index (not yet migrated
      — their active checkpoint has no database row), plus the scan's
      blob problems; while best_effort is active the filesystem IS the
      authority and coverage passes.
    - **conflicts** — works both authorities hold whose DERIVED actives
      disagree: operator resolution demanded; a postgres-mode work
      whose database active outranks the old index active while the old
      active is still present is EXPECTED post-cutover drift (counted,
      not conflicted); never is a winner selected, and never by time.
    - **topology** — the two-replica-separate-volumes UNSUPPORTED
      topology, in its simplest honest form: a warning when the
      configured store root looks node-local (the heuristic above),
      and the note that a shared database does not make node-local
      blobs shared. A warning, never a block.
    """
    from forge.api_checkpoint_channel import (
        CHECKPOINT_STORE_DIR_ENV,
        DEFAULT_CHECKPOINT_ROOT,
        DurabilityContract,
    )

    source = os.environ if env is None else env
    store_root = (
        Path(root)
        if root is not None
        else Path(str(source.get(CHECKPOINT_STORE_DIR_ENV, "")).strip() or DEFAULT_CHECKPOINT_ROOT)
    )
    mode = DurabilityContract.mode_from_env(dict(source))
    inventory = scan_inventory(store_root, verify_digests=False)
    db_entries: dict[str, dict[str, dict[str, Any]]] | None = None
    db_note = "no DATABASE_URL"
    if database_url:
        try:
            db_entries = await _async_db_entry_map(database_url, store_root)
            db_note = ""
        except Exception as exc:  # noqa: BLE001 — not visible, never fatal
            db_note = f"database not visible ({exc.__class__.__name__})"

    fs_works = {str(work["work_id"]): work for work in inventory["works"] if work["entries"]}
    summary = inventory["summary"]
    blob_problems = (
        summary["missing_blobs"]
        + summary["corrupt_blobs"]
        + summary["missing_manifests"]
        + summary["corrupt_manifests"]
    )
    # -- coverage ---------------------------------------------------------------
    unmigrated: list[str] = []
    unmigrated_note = db_note if db_entries is None else ""
    if db_entries is not None and mode == "postgres":
        for work_id, work in fs_works.items():
            active = work["active"]
            rows = db_entries.get(work_id, {})
            if active is None or str(active["checkpoint_id"]) not in rows:
                unmigrated.append(work_id)
    coverage = {
        "mode": mode,
        "works_filesystem": len(fs_works),
        "works_database": len(db_entries) if db_entries is not None else None,
        "unmigrated_works": unmigrated,
        "blob_problems": int(blob_problems),
        "database_note": unmigrated_note,
    }
    # -- conflicts ---------------------------------------------------------------
    conflicts: list[dict[str, Any]] = []
    drift_count = 0
    if db_entries is not None:
        for work_id, work in fs_works.items():
            rows = db_entries.get(work_id)
            if not rows or work["active"] is None:
                continue
            fs_active = str(work["active"]["checkpoint_id"])
            db_active = str(max(rows.values(), key=_entry_order)["checkpoint_id"])
            if fs_active == db_active:
                continue
            explained = (
                mode == "postgres"
                and fs_active in rows
                and _entry_order(rows[db_active]) > _entry_order(rows[fs_active])
            )
            if explained:
                drift_count += 1
                continue
            conflicts.append(
                {
                    "work_id": work_id,
                    "filesystem_active": fs_active,
                    "database_active": db_active,
                    "resolution": "operator decision required — never timestamp selection",
                }
            )
    # -- topology -----------------------------------------------------------------
    topology = {
        "store_root": str(store_root),
        "looks_node_local": _looks_node_local(store_root),
        "mode": mode,
        "heuristic": (
            "a relative or temp-directory store root looks node-local; an absolute path "
            "elsewhere is ASSUMED shared — confirm every replica mounts it"
        ),
    }
    return {
        "coverage": coverage,
        "conflicts": {
            "count": len(conflicts),
            "works": conflicts,
            "post_cutover_drift": drift_count,
        },
        "topology": topology,
        "metrics": {
            METRIC_COVERAGE: coverage,
            METRIC_CONFLICTS: {"count": len(conflicts)},
            METRIC_REACHABILITY: {"blob_problems": int(blob_problems)},
        },
    }


# ---------------------------------------------------------------------------
# The database wiring and the CLI
# ---------------------------------------------------------------------------


async def open_metadata_engine(database_url: str, *, bootstrap: bool = True) -> Any:
    """A FRESH engine for the metadata database, with an honest schema gate.

    Never the process-wide engine cache: one CLI process runs several
    commands, each under its own ``asyncio.run`` loop, and a pooled
    connection pinned to a closed loop is exactly the cross-loop hazard
    the cache would hand us. Production owns the ``checkpoint_metadata``
    table through alembic 026: against PostgreSQL the tool REFUSES when
    the table is missing (run migrations first) instead of creating
    schema the migration chain must own. The SQLite approximation
    (tests, local labs) bootstraps the table when absent — checkfirst,
    never destructive — unless *bootstrap* is False (the read-only
    doctor preflight: it observes, it never creates). The CALLER
    disposes the engine.
    """
    from sqlalchemy import inspect as sa_inspect, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from forge.api_checkpoint_channel import CheckpointMetadataRow

    def _table_exists(sync_connection: Any) -> bool:
        return sa_inspect(sync_connection).has_table(CheckpointMetadataRow.__tablename__)

    connect_args: dict[str, Any] = (
        {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    )
    engine = create_async_engine(database_url, connect_args=connect_args)
    try:
        if engine.dialect.name == "postgresql" or not bootstrap:
            async with engine.connect() as conn:
                if engine.dialect.name == "postgresql":
                    registered = await conn.scalar(
                        text("SELECT to_regclass('public.checkpoint_metadata')")
                    )
                    present = bool(registered)
                else:
                    present = bool(await conn.run_sync(_table_exists))
            if not present:
                raise MigrationCommandError(
                    "the checkpoint_metadata table is not visible in the target database"
                    + (
                        " — run the alembic migrations first (`python -m forge.migrate`); "
                        "the migration tool never creates postgres schema itself"
                        if engine.dialect.name == "postgresql"
                        else ""
                    )
                )
        else:
            async with engine.begin() as conn:
                await conn.run_sync(
                    lambda sync: CheckpointMetadataRow.__table__.create(sync, checkfirst=True)
                )
    except BaseException:
        await engine.dispose()
        raise
    return engine


async def _run_with_engine(database_url: str, run: Any) -> Any:
    """Run one async command closure against a fresh engine, then dispose it."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    engine = await open_metadata_engine(database_url)
    try:
        return await run(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


def _cli_database_url(args: argparse.Namespace) -> str:
    url = str(getattr(args, "database_url", "") or "").strip() or os.environ.get("DATABASE_URL", "")
    if not url:
        raise MigrationCommandError(
            "no metadata database URL: pass --database-url or set DATABASE_URL — the "
            "postgres checkpoint authority has nowhere to live without it"
        )
    return url


def _cli_root(args: argparse.Namespace) -> Path:
    root = str(getattr(args, "root", "") or "").strip()
    return Path(root) if root else _default_store_root()


def run_inventory(root: Path | str, *, out: Path | None = None) -> dict[str, Any]:
    """The ``inventory`` command: scan + write the structured report."""
    report = scan_inventory(root)
    if out is not None:
        _write_json_atomic(Path(out), report)
    return report


async def _cmd_inventory(args: argparse.Namespace) -> int:
    root = _cli_root(args)
    out_path = Path(args.out) if args.out else migration_dir(root) / "inventory.json"
    report = run_inventory(root, out=out_path)
    summary = report["summary"]
    print(f"inventory: {summary['works']} works, {summary['checkpoints']} checkpoints")
    print(f"report: {out_path}")
    problems = (
        summary["missing_manifests"]
        + summary["corrupt_manifests"]
        + summary["missing_blobs"]
        + summary["corrupt_blobs"]
    )
    for name in report["unparsable_index_files"]:
        problems += 1
        print(f"  unparsable index file: {name}")
    for work in report["works"]:
        for entry in work["entries"]:
            for problem in entry["problems"]:
                print(f"  {work['work_id']}@{entry['checkpoint_id']}: {problem}")
    if problems:
        print(
            f"{problems} missing/corrupt artifact(s) listed above — import will report "
            "these entries as unimportable; nothing was invented or skipped"
        )
    return EXIT_OK


async def _cmd_import(args: argparse.Namespace) -> int:
    root = _cli_root(args)
    url = _cli_database_url(args)
    out_path = Path(args.out) if args.out else migration_dir(root) / "import.json"

    async def _go(factory: Any) -> Any:
        return await run_import(
            root,
            factory,
            inventory_path=Path(args.inventory) if args.inventory else None,
            reverse=bool(args.reverse),
            out=out_path,
        )

    report, exit_code = await _run_with_engine(url, _go)
    summary = report["summary"]
    print(
        f"import ({report['direction']}): {summary['imported']} imported, "
        f"{summary['skipped_already_present']} already present (no-op), "
        f"{summary['unimportable']} unimportable, {summary['failed']} failed"
    )
    print(f"report: {out_path}")
    for entry in report["unimportable"]:
        print(
            f"  unimportable {entry['work_id']}@{entry['checkpoint_id']}: "
            + "; ".join(entry["problems"])
        )
    for entry in report["failed"]:
        print(f"  failed {entry['work_id']}@{entry['checkpoint_id']}: {entry['error']}")
    if exit_code == EXIT_PARTIAL:
        print(
            "PARTIAL: the listed entries were reported, not imported — restore the "
            "missing blobs (or drop the entries deliberately) and re-run; the rest is imported",
            file=sys.stderr,
        )
    return exit_code


async def _cmd_verify(args: argparse.Namespace) -> int:
    root = _cli_root(args)
    url = _cli_database_url(args)
    out_path = Path(args.out) if args.out else migration_dir(root) / "verify.json"

    async def _go(factory: Any) -> Any:
        return await run_verify(root, factory, out=out_path)

    report, exit_code = await _run_with_engine(url, _go)
    metrics = report["metrics"]
    coverage = metrics[METRIC_COVERAGE]
    print(
        f"verify: coverage fs={coverage['works_filesystem']} db={coverage['works_database']} "
        f"(missing in db: {coverage['missing_in_database']}, in index only: "
        f"{coverage['missing_in_index']}), conflicts={metrics[METRIC_CONFLICTS]['count']}, "
        f"unreachable blobs={metrics[METRIC_REACHABILITY]['unreachable_count']}"
    )
    print(f"report: {out_path}")
    for conflict in metrics[METRIC_CONFLICTS]["active_disagreements"]:
        print(
            f"  CONFLICT {conflict['work_id']}: filesystem={conflict['filesystem_active']} "
            f"database={conflict['database_active']} — {conflict['resolution']}"
        )
    for work in report["works"]:
        for disagreement in work["disagreements"]:
            print(f"  {work['work_id']}: {disagreement}")
    for pin in report["pins"]:
        if not pin.get("resolved"):
            print(f"  pinned {pin['work_id']}@{pin['checkpoint_id']}: {pin.get('reason')}")
    if report["clean"]:
        print("clean — cutover may proceed: python -m forge.adaptive.checkpoint_migration cutover")
    else:
        print(
            "NOT CLEAN — resolve every disagreement above (operator decision), then "
            "re-verify; cutover is refused until a clean report exists",
            file=sys.stderr,
        )
    return exit_code


async def _cmd_cutover(args: argparse.Namespace) -> int:
    root = _cli_root(args)
    marker = run_cutover(
        root, verify_report=Path(args.verify_report) if args.verify_report else None
    )
    print(f"cutover complete: the authority marker now names {marker['authority']!r}")
    print(f"  marker: {authority_marker_path(root)}")
    print("  next: set FORGE_CHECKPOINT_DURABILITY=postgres and restart the deployment")
    print(
        "  the old filesystem inventory is preserved (rollback: python -m "
        "forge.adaptive.checkpoint_migration rollback --verify-report <path>)"
    )
    return EXIT_OK


async def _cmd_rollback(args: argparse.Namespace) -> int:
    root = _cli_root(args)
    marker = run_rollback(root, verify_report=Path(args.verify_report))
    print(f"rollback complete: the authority marker now names {marker['authority']!r}")
    print(f"  marker: {authority_marker_path(root)}")
    print("  next: set FORGE_CHECKPOINT_DURABILITY=best_effort and restart the deployment")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """The module CLI (``python -m forge.adaptive.checkpoint_migration``)."""
    parser = argparse.ArgumentParser(
        prog="forge-checkpoint-migration",
        description=(
            "Q35-21: migrate checkpoint metadata to one authority — inventory, "
            "idempotent import, verification, fenced cutover, gated rollback"
        ),
    )
    parser.add_argument(
        "--root",
        default=None,
        help="the checkpoint store root (default: FORGE_CHECKPOINT_STORE_DIR or data/checkpoints)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory_parser = subparsers.add_parser(
        "inventory", help="scan the filesystem store and write the structured inventory report"
    )
    inventory_parser.add_argument(
        "--out", default=None, help="report path (default: <root>/migration/inventory.json)"
    )
    inventory_parser.set_defaults(handler=_cmd_inventory)

    import_parser = subparsers.add_parser(
        "import", help="idempotent import of old metadata into the postgres authority"
    )
    import_parser.add_argument(
        "--database-url", default=None, help="the metadata database URL (default: DATABASE_URL)"
    )
    import_parser.add_argument(
        "--inventory", default=None, help="an explicit inventory report to import from"
    )
    import_parser.add_argument(
        "--reverse",
        action="store_true",
        help="the rollback recovery path: copy database entries back into the filesystem index",
    )
    import_parser.add_argument("--out", default=None, help="report path")
    import_parser.set_defaults(handler=_cmd_import)

    verify_parser = subparsers.add_parser(
        "verify", help="compare the old index with the new authority; gate the cutover"
    )
    verify_parser.add_argument(
        "--database-url", default=None, help="the metadata database URL (default: DATABASE_URL)"
    )
    verify_parser.add_argument(
        "--out", default=None, help="report path (default: <root>/migration/verify.json)"
    )
    verify_parser.set_defaults(handler=_cmd_verify)

    cutover_parser = subparsers.add_parser(
        "cutover", help="flip the authority marker (only after a clean verify report)"
    )
    cutover_parser.add_argument(
        "--verify-report",
        default=None,
        help="the clean verify report (default: <root>/migration/verify.json)",
    )
    cutover_parser.set_defaults(handler=_cmd_cutover)

    rollback_parser = subparsers.add_parser(
        "rollback", help="restore the filesystem authority marker (gated by a clean verify report)"
    )
    rollback_parser.add_argument(
        "--verify-report",
        required=True,
        help="a CLEAN verify report for the CURRENT data state — the rollback gate",
    )
    rollback_parser.set_defaults(handler=_cmd_rollback)

    args = parser.parse_args(argv)
    try:
        return asyncio.run(args.handler(args))
    except MigrationCommandError as exc:
        print(f"{args.command} refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":  # pragma: no cover — the CLI entry point
    sys.exit(main())
