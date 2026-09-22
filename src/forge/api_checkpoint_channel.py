"""LIVE cross-runner checkpoint transport — the control-plane side (wave C/D).

The lane runner's checkpoint (:mod:`forge.adaptive.checkpointing`) is a
versioned manifest plus content blobs in a store rooted on the LANE
JOB's filesystem; this router is the durable counterpart a SECOND
runner restores from. Three endpoints, one storage discipline:

- ``PUT /lane/checkpoints/{work_id}`` — accept a checkpoint (JSON-base64
  manifest + blobs), verify EVERY promise against the bytes (the
  manifest must hash to its own content address, belong to the path's
  work, and every blob must reproduce its digest — a tampered upload is
  refused with 400 naming the blob), enforce the per-blob size cap
  (413, an honest refusal), and land everything in a content-addressed
  directory via atomic temp+rename so a crash mid-write never exposes a
  partial artifact.
- ``GET /lane/checkpoints/{work_id}`` — serve the work's latest
  checkpoint (or a specific ``checkpoint_id``), digest-verified ON READ:
  bytes that no longer hash to their address answer 500 naming the
  address, they are never served as if they were the checkpoint.
- ``GET /lane/checkpoints`` — the operator surface: every held
  checkpoint reference with its work, sequence and latest flag.

Authentication is the lane control scheme's: while
``FORGE_LANE_CONTROL_SECRET`` is unset every endpoint answers 503 —
the channel is disabled, fail closed. With the secret set, the bearer
token must be the work-scoped HMAC of the work id under the secret
(:func:`forge.adaptive.checkpoint_channel.work_scoped_token`; the list
surface uses the ``checkpoints:list`` scope). A token minted for
ANOTHER work is refused with 401 — work-scoped means exactly that.

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
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, Header, HTTPException, Request

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
    "LANE_CONTROL_SECRET_ENV",
    "MAX_BLOB_BYTES_ENV",
    "CheckpointCorruptError",
    "CheckpointStore",
    "checkpoint_channel_router",
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

#: How many checkpoints per work to keep beyond the latest (0 = all).
#: The LATEST is exempt whatever this says — see apply_retention.
CHECKPOINT_RETENTION_ENV: Final = "FORGE_CHECKPOINT_RETENTION"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
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


class CheckpointStore:
    """The control plane's content-addressed checkpoint directory.

    Blobs and manifests land at ``root/<first2>/<digest>`` through an
    atomic temp-file + rename (a crash leaves an orphan temp, never a
    half-written artifact under an address whose digest promises
    integrity); reads re-hash the bytes to the address and raise
    :class:`CheckpointCorruptError` on disagreement. The per-work index
    orders the checkpoints; retention never touches the latest.
    """

    def __init__(self, root: Path, *, max_blob_bytes: int = DEFAULT_MAX_BLOB_BYTES) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._max_blob_bytes = max_blob_bytes

    # -- content-addressed files --------------------------------------------

    def _cas_path(self, digest: str) -> Path:
        return self._root / digest[:2] / digest

    def _write_cas(self, digest: str, data: bytes) -> None:
        """Land *data* under its address atomically (temp + rename)."""
        target = self._cas_path(digest)
        if target.exists():
            return  # content addressing: same bytes are already there, immutably
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
        """Write the index atomically — same crash discipline as the blobs."""
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

    # -- the operations ---------------------------------------------------------

    def put_checkpoint(
        self,
        *,
        work_id: str,
        manifest_bytes: bytes,
        blobs: dict[str, bytes],
        sequence: int,
    ) -> dict[str, Any]:
        """Store one verified checkpoint and make it the work's latest.

        Callers verify the digests BEFORE this (the endpoint does); here
        the landing order is blobs-then-manifest-then-index, each atomic
        — a crash between them leaves unreferenced CAS content
        (harmless, collectable), never an index entry whose bytes are
        missing. Re-putting an identical checkpoint is idempotent: the
        addresses exist and the index does not duplicate the entry.
        """
        checkpoint_id = _sha256(manifest_bytes)
        for digest, data in blobs.items():
            self._write_cas(digest, data)
        self._write_cas(checkpoint_id, manifest_bytes)

        document = self._load_index(work_id)
        entries: list[dict[str, Any]] = [
            entry for entry in document["checkpoints"] if isinstance(entry, dict)
        ]
        existing = next(
            (entry for entry in entries if entry.get("checkpoint_id") == checkpoint_id), None
        )
        if existing is None:
            entries.append(
                {
                    "checkpoint_id": checkpoint_id,
                    "sequence": sequence,
                    "files": len(blobs),
                    "uploaded_at": _now_iso(),
                }
            )
        document = {"work_id": work_id, "checkpoints": entries}
        self._save_index(work_id, document)
        latest = entries[-1]
        return {
            "work_id": work_id,
            "checkpoint_id": checkpoint_id,
            "sequence": sequence,
            "files": len(blobs),
            "uploaded_at": str(latest.get("uploaded_at") or ""),
            "latest": latest.get("checkpoint_id") == checkpoint_id,
        }

    def entry(self, work_id: str, checkpoint_id: str | None = None) -> dict[str, Any] | None:
        """The work's LATEST index entry, or the one named by *checkpoint_id*."""
        entries = [
            entry
            for entry in self._load_index(work_id)["checkpoints"]
            if isinstance(entry, dict) and isinstance(entry.get("checkpoint_id"), str)
        ]
        if not entries:
            return None
        if checkpoint_id is None:
            return entries[-1]
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
        return manifest_bytes, blobs

    def list_entries(self) -> list[dict[str, Any]]:
        """Every held checkpoint entry, newest-last per work, latest-flagged."""
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
            entries = [
                dict(entry, work_id=work_id)
                for entry in document.get("checkpoints", [])
                if isinstance(entry, dict)
            ]
            for position, entry in enumerate(entries):
                entry["latest"] = position == len(entries) - 1
            listed.extend(entries)
        return listed

    def apply_retention(self, work_id: str, keep_last: int) -> int:
        """Drop the OLDEST checkpoints beyond *keep_last* — never the latest.

        The rule that makes retention safe to run at any moment: even
        ``keep_last=0`` keeps the work's LATEST checkpoint — a live
        pause stands on it, and no age/count policy may delete the
        thing resume needs. Older entries are removed from the index
        and their blobs (manifest + files) are deleted from the CAS
        ONLY when no retained checkpoint of ANY work still references
        them — shared content survives its sharers. Returns how many
        checkpoint entries were removed.
        """
        document = self._load_index(work_id)
        entries = [
            entry
            for entry in document["checkpoints"]
            if isinstance(entry, dict) and isinstance(entry.get("checkpoint_id"), str)
        ]
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


def _retention_keep() -> int:
    raw = os.environ.get(CHECKPOINT_RETENTION_ENV, "").strip()
    try:
        return max(0, int(raw)) if raw else 0
    except ValueError:
        return 0


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
    stored: strict base64, the manifest's schema and work ownership
    (a manifest belonging to ANOTHER work is refused — the upload
    address and the checkpoint must agree), a blob for every manifest
    entry, every blob digest reproduced, and the per-blob size cap.
    Refusals name the exact blob — honest, not generic.
    """
    manifest_field = document.get("manifest")
    blobs_field = document.get("blobs")
    if not isinstance(manifest_field, str) or not isinstance(blobs_field, dict):
        raise HTTPException(status_code=400, detail="payload needs manifest and blobs sections")
    try:
        manifest_bytes = base64.b64decode(manifest_field, validate=True)
        blobs = {
            str(digest): base64.b64decode(str(encoded), validate=True)
            for digest, encoded in blobs_field.items()
        }
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid base64 content: {exc}") from exc
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
    for rel, entry in sorted(files.items()):
        digest = entry.get("digest") if isinstance(entry, dict) else None
        if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
            raise HTTPException(
                status_code=400, detail=f"manifest entry {rel!r} carries no valid content digest"
            )
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
    if not _authorized(secret, work_id, authorization):
        raise HTTPException(status_code=401, detail="invalid work-scoped token")
    try:
        document = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(document, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")

    manifest_bytes, blobs, sequence = _decode_payload(document, work_id)

    store = CheckpointStore(_store_dir(), max_blob_bytes=_max_blob_bytes())
    result = store.put_checkpoint(
        work_id=work_id,
        manifest_bytes=manifest_bytes,
        blobs=blobs,
        sequence=sequence,
    )
    keep = _retention_keep()
    if keep:
        store.apply_retention(work_id, keep)
    return result


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
    if not _authorized(secret, work_id, authorization):
        raise HTTPException(status_code=401, detail="invalid work-scoped token")
    if checkpoint_id is not None and not _HEX64.fullmatch(checkpoint_id):
        raise HTTPException(status_code=400, detail="checkpoint_id must be a 64-hex digest")

    store = CheckpointStore(_store_dir(), max_blob_bytes=_max_blob_bytes())
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
    store = CheckpointStore(_store_dir(), max_blob_bytes=_max_blob_bytes())
    return {"checkpoints": store.list_entries()}
