"""The checkpoint transaction: capture, restore, resume (NXT-15..NXT-18).

The review's finding was that pause USED to confirm a checkpoint no
storage ever saw — a fabricated ``artifact:wip:<work_id>`` and a
``checkpoint_captured`` flag with no export, no receipt, no digest.
This module is the real sequence, as one explicit transaction the pause
drain can run:

1. **Capture** (:func:`capture_wip`) — after observed quiescence (the
   cooperative drain), serialize the working tree against the tracked
   baseline: modified and new files are uploaded to the
   :class:`~forge.adaptive.artifact_store.ContentAddressedStore` as
   content-addressed blobs (untracked files carry CONTENT, never just a
   filename), deletions are recorded, the executable bit is recorded as
   a mode, and the whole state freezes into a versioned manifest
   (``forge.wip.manifest/2``) whose own content address is the
   checkpoint reference. Every uploaded blob and the manifest itself
   are then READ BACK through the store's verified read and re-hashed
   to their addresses — a receipt is minted ``verified`` only after
   that read-back, and the manifest + blobs are pinned with a retention
   reference (``checkpoint:<work_id>``) so GC cannot delete the
   checkpoint of an actively paused work on age alone.
2. **Restore** (:func:`restore_wip`) — on a SECOND runner with a fresh
   store instance: fetch the manifest through the verified read, refuse
   unsupported/legacy schemas (a filename list without blobs restores
   nothing), validate every path against escapes BEFORE writing, then
   re-fetch every blob digest-verified and apply it — files written
   (with modes), deletions applied — reporting per-file outcomes. A
   missing or corrupt required blob FAILS the restore with the path and
   digest named, instead of resurrecting half a workspace that looks
   restored.
3. **Resume** (:func:`resume_from_checkpoint`) — NXT-18's gate: the
   CURRENT authorization is re-checked through a callable (no
   constructor-default ``permissions_valid=True``), the checkpoint
   BYTES are re-verified NOW against the store, and only then a FRESH
   execution epoch is allocated (``prior + 1``, durable-artifact
   reconstruction) and returned bound to the receipt. A partial pause,
   a revoked grant, an expired or tampered checkpoint each refuse with
   the specific reason.

The transaction's edges are the drain's edges
(:func:`forge.adaptive.control.drain_turn` consumes
:func:`cooperative_capture` as its capture capability and books the
mailbox's ``checkpointed`` rung through :func:`book_checkpoint`): a
failed capture never fabricates success — the pause lands
``paused_failed``/: ``paused_partial`` naming the last REAL
recoverable checkpoint.

Wave C/D adds the LIVE cross-runner legs WITHOUT moving any of the
above: ``capture_wip(upload=channel)`` also carries the verified
checkpoint to the control plane
(:mod:`forge.adaptive.checkpoint_channel`) and the receipt carries the
durable ``remote_ref``; ``restore_wip(download=channel)`` accepts that
remote reference and fetches the checkpoint digest-verified into a
fresh local store before restoring. Both parameters are optional and
structural — the transaction above runs unchanged when they are absent.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Final, Literal, Protocol

from forge.adaptive.artifact_store import ContentAddressedStore, CorruptArtifactError
from forge.adaptive.control import CaptureResult, MailboxSurface, PauseState, new_execution_epoch

__all__ = [
    "MANIFEST_SCHEMA",
    "CaptureFailed",
    "CheckpointError",
    "CheckpointReceipt",
    "FileRestore",
    "RestoreReport",
    "ResumeOutcome",
    "UploadFailed",
    "accepts_epoch",
    "book_checkpoint",
    "capture_wip",
    "cooperative_capture",
    "restore_wip",
    "resume_from_checkpoint",
]

#: The portable manifest schema this module writes and restores. Version
#: 2 (``forge.wip.manifest/1`` in :mod:`forge.adaptive.artifact_store`
#: remains the frozen historical shape) carries a CONTENT DIGEST for
#: every file — a checkpoint whose untracked section is a bare filename
#: list restores nothing and is refused here, not guessed at.
MANIFEST_SCHEMA: Final = "forge.wip.manifest/2"

#: Normalized file modes a manifest records. The executable bit is the
#: only permission distinction a portable checkpoint promises.
_MODE_EXECUTABLE: Final = 0o755
_MODE_REGULAR: Final = 0o644

#: Roles a captured file can carry: ``modified`` (tracked, content
#: differs from the baseline) or ``new`` (not in the tracked baseline —
#: the untracked content a revive must reconstruct from a BLOB).
FileRole = Literal["modified", "new"]


class CheckpointError(Exception):
    """Base class for checkpoint-transaction refusals (actionable, not crashes)."""


class CaptureFailed(CheckpointError):
    """The WIP capture could not be completed or verified (NXT-15).

    Raised by :func:`capture_wip` when the tree cannot be honestly
    serialized (a symlink the manifest refuses to carry), the store
    rejects a blob, or the post-upload read-back disagrees with the
    digests. The pause drain books it as a FAILED pause — never as a
    captured checkpoint.
    """


class UploadFailed(CheckpointError):
    """The channel leg of a capture could not complete (wave C/D).

    Raised by :func:`capture_wip` when an ``upload=`` channel was given
    and the transport to the control plane refused — unreachable,
    unauthenticated, size-capped, or an incomplete local checkpoint.
    The LOCAL capture may have committed (its blobs stay in the store
    as the last recoverable state), but the receipt carries no durable
    remote reference it cannot support, so the pause books
    ``paused_failed`` with this reason instead of a fabricated durable
    claim.
    """


class WipUploadChannel(Protocol):
    """The structural transport :func:`capture_wip` accepts as ``upload=``.

    Satisfied by
    :class:`forge.adaptive.checkpoint_channel.CheckpointChannel`
    (``upload_checkpoint(store, work_id)`` returning the durable
    :class:`~forge.adaptive.checkpoint_channel.CheckpointRef`); kept
    structural so this module never imports the transport and the
    channel builds ON the checkpoint transaction, not into it.
    """

    def upload_checkpoint(self, store: ContentAddressedStore, work_id: str) -> object: ...


class RemoteCheckpointSource(Protocol):
    """The structural transport :func:`restore_wip` accepts as ``download=``.

    Satisfied by :class:`forge.adaptive.checkpoint_channel.CheckpointChannel`
    (``fetch_checkpoint(remote_ref, store)`` pulling the checkpoint
    digest-verified into *store* and returning its LOCAL manifest
    address).
    """

    def fetch_checkpoint(self, remote_ref: str, store: ContentAddressedStore) -> str: ...


@dataclass(frozen=True)
class CheckpointReceipt:
    """The durable proof a real capture committed (implements
    :class:`forge.adaptive.control.CaptureResult`).

    ``artifact_id``/``digest`` are the manifest's content address in
    the store (identical by construction — the address IS the digest).
    ``sequence`` is the applied-command watermark the checkpoint
    carries; ``verified`` is True only after the read-back re-hashed
    every blob AND the manifest to its address. ``remote_ref`` (wave
    C/D) is the DURABLE reference the control plane handed back when
    the capture also uploaded — ``<work_id>@<checkpoint_id>`` — empty
    for a purely local capture.
    """

    artifact_id: str
    digest: str
    sequence: int
    files: int
    deletions: int
    verified: bool
    work_id: str = ""
    source_oids: dict[str, str] = field(default_factory=dict)
    remote_ref: str = ""


@dataclass(frozen=True)
class FileRestore:
    """One manifest entry's restore outcome (NXT-17's per-file evidence)."""

    path: str
    outcome: Literal["restored", "deleted", "already-absent", "failed"]
    digest: str = ""
    reason: str = ""


@dataclass(frozen=True)
class RestoreReport:
    """The second-runner restore verdict with per-file evidence."""

    artifact_id: str
    ok: bool
    files: tuple[FileRestore, ...]
    failures: tuple[str, ...]

    @property
    def restored_paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.files if item.outcome == "restored")


@dataclass(frozen=True)
class ResumeOutcome:
    """The NXT-18 resume verdict: authorized NOW, bytes verified NOW, fresh epoch."""

    ok: bool
    reason: str
    epoch: dict | None = None
    authorization_valid: bool = False
    checkpoint_verified: bool = False


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative_posix(root: Path, path: Path) -> str:
    """The manifest key for *path*: POSIX-style, relative to the tree root."""
    return path.relative_to(root).as_posix()


def _safe_manifest_path(path: str) -> PurePosixPath:
    """Validate one manifest path BEFORE any write; refuse escapes.

    Manifest paths are portable strings, not host paths: an absolute
    path or a ``..`` component (or a drive-letter style backslash form)
    is an escape attempt and fails the whole restore loudly instead of
    writing outside the target workspace.
    """
    normalized = path.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if not path or pure.is_absolute() or not pure.parts or ".." in pure.parts or path != normalized:
        raise ValueError(f"manifest path escapes the restore target: {path!r}")
    return pure


def _mode_of(path: Path) -> int:
    """Normalize a file's permissions to the portable mode pair."""
    executable = bool(os.stat(path, follow_symlinks=False).st_mode & stat.S_IXUSR)
    return _MODE_EXECUTABLE if executable else _MODE_REGULAR


def _tree_path(root: Path, rel: str) -> Path:
    """The on-tree path for a manifest key (validated against escapes)."""
    return root.joinpath(*_safe_manifest_path(rel).parts)


# ---------------------------------------------------------------------------
# Capture (NXT-15 / NXT-17)
# ---------------------------------------------------------------------------


def capture_wip(
    *,
    work_id: str,
    root: Path,
    store: ContentAddressedStore,
    tracked_baseline: Mapping[str, str],
    source_oids: Mapping[str, str] | None = None,
    sequence: int = 0,
    upload: WipUploadChannel | None = None,
) -> CheckpointReceipt:
    """Capture the working tree at *root* as a digest-verified checkpoint.

    ``tracked_baseline`` maps path -> content digest for the source
    snapshot/revision the WIP applies on top of (the caller's durable
    authority — :class:`forge.adaptive.models.SnapshotSet` digests in
    production). Files whose digest still matches the baseline are NOT
    re-uploaded (the baseline reconstructs them); modified, new and
    untracked files are uploaded as content-addressed blobs; paths in
    the baseline that vanished from the tree are recorded as
    deletions. The manifest freezes all of it plus source OIDs and the
    applied-command ``sequence``, and its own content address becomes
    the checkpoint reference.

    Verification is part of the capture, not an afterthought: every
    blob and the manifest are READ BACK through
    :meth:`ContentAddressedStore.get_verified` (which re-hashes the
    bytes to the address) before the receipt is minted ``verified``.
    The manifest and every blob are pinned with the
    ``checkpoint:<work_id>`` retention reference so pruning cannot
    delete them while the pause lives.

    ``upload`` (wave C/D, optional): a transport channel — when given,
    the verified capture is ALSO uploaded to the control plane and the
    returned receipt carries the durable ``remote_ref``. A refused
    upload raises :class:`UploadFailed`: the pause then lands
    ``paused_failed`` (the local blobs remain the last recoverable
    state), never a receipt claiming durability the transport did not
    grant.
    """
    root = Path(root)
    if not root.is_dir():
        raise CaptureFailed(f"working tree {root} does not exist — nothing to capture")

    files: dict[str, dict[str, object]] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if not (Path(dirpath) / name).is_symlink())
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink():
                raise CaptureFailed(
                    f"symlink in the WIP tree cannot be checkpointed honestly: {path}"
                )
            rel = _relative_posix(root, path)
            _safe_manifest_path(rel)  # the tree cannot escape itself; keep the check total
            data = path.read_bytes()
            digest = _sha256(data)
            baseline = tracked_baseline.get(rel)
            if baseline == digest:
                continue  # unchanged: the tracked baseline reconstructs it
            store.put(data)
            files[rel] = {
                "digest": digest,
                "mode": _mode_of(path),
                "role": "modified" if baseline is not None else "new",
            }
    deletions = sorted(rel for rel in tracked_baseline if not _tree_path(root, rel).exists())

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "work_id": work_id,
        "sequence": sequence,
        "source_oids": dict(source_oids or {}),
        "files": files,
        "deletions": deletions,
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    artifact_id = store.put(manifest_bytes, content_type="application/json")

    _verify_capture(store, artifact_id, files)
    receipt = CheckpointReceipt(
        artifact_id=artifact_id,
        digest=artifact_id,
        sequence=sequence,
        files=len(files),
        deletions=len(deletions),
        verified=True,
        work_id=work_id,
        source_oids=dict(source_oids or {}),
    )
    label = f"checkpoint:{work_id}"
    store.add_reference(artifact_id, label)
    for entry in files.values():
        digest = str(entry["digest"])
        store.add_reference(digest, label)
    if upload is not None:
        receipt = _upload_to_channel(upload, store=store, work_id=work_id, receipt=receipt)
    return receipt


def _upload_to_channel(
    channel: WipUploadChannel,
    *,
    store: ContentAddressedStore,
    work_id: str,
    receipt: CheckpointReceipt,
) -> CheckpointReceipt:
    """The wave C/D upload leg: attach the durable reference or refuse.

    The channel returns the durable reference (an object exposing
    ``remote_ref``, or its ``checkpoint_id``, or a plain string). Any
    transport refusal — unreachable control plane, size cap, incomplete
    local checkpoint — becomes :class:`UploadFailed` carrying the
    reason: the local capture stays as the last recoverable state, and
    no receipt leaves here claiming a remote copy that does not exist.
    """
    try:
        outcome = channel.upload_checkpoint(store, work_id)
    except Exception as exc:  # noqa: BLE001 — every transport refusal is booked, none swallowed
        raise UploadFailed(f"checkpoint channel upload failed for {work_id}: {exc}") from exc
    remote_ref = getattr(outcome, "remote_ref", None)
    if not isinstance(remote_ref, str) or not remote_ref:
        remote_ref = str(getattr(outcome, "checkpoint_id", outcome))
    return replace(receipt, remote_ref=remote_ref)


def _verify_capture(
    store: ContentAddressedStore,
    artifact_id: str,
    files: Mapping[str, Mapping[str, object]],
) -> None:
    """Read every captured blob and the manifest back, digest-verified.

    An unverified upload is not a checkpoint: any read-back miss,
    CorruptArtifactError or digest disagreement raises
    :class:`CaptureFailed` — the pause then lands ``paused_failed``,
    never a captured claim the store cannot support. The caller mints
    the receipt ``verified=True`` only after this returns.
    """
    try:
        manifest_read = store.get_verified(artifact_id, principal=store.tenant)
        if manifest_read is None or _sha256(manifest_read) != artifact_id:
            raise CaptureFailed(
                f"manifest read-back failed for {artifact_id}: the committed "
                "reference does not resolve digest-identical"
            )
        for rel, entry in sorted(files.items()):
            digest = str(entry["digest"])
            blob = store.get_verified(digest, principal=store.tenant)
            if blob is None:
                raise CaptureFailed(
                    f"blob read-back failed for {rel} ({digest}): the uploaded bytes "
                    "did not resolve digest-identical"
                )
    except CorruptArtifactError as exc:
        raise CaptureFailed(f"verification quarantined corrupt content: {exc}") from exc


def cooperative_capture(
    *,
    work_id: str,
    root: Path,
    store: ContentAddressedStore,
    tracked_baseline: Mapping[str, str],
    source_oids: Mapping[str, str] | None = None,
    sequence: int = 0,
    upload: WipUploadChannel | None = None,
) -> Callable[[], CaptureResult]:
    """Bind :func:`capture_wip` into the zero-argument capability
    :func:`forge.adaptive.control.drain_turn` consumes as ``capture=``
    (the returned :class:`CheckpointReceipt` structurally satisfies the
    :class:`~forge.adaptive.control.CaptureResult` contract).

    The sequence is the pause state's applied-command watermark at drain
    time — pass ``state.last_applied_command_sequence`` so the committed
    checkpoint carries what the mailbox actually applied. ``upload``
    forwards the wave C/D channel: the drain's capture then also lands
    the durable remote copy, and a refused upload books the same honest
    ``paused_failed`` as any failed capture.
    """
    return lambda: capture_wip(
        work_id=work_id,
        root=root,
        store=store,
        tracked_baseline=tracked_baseline,
        source_oids=source_oids,
        sequence=sequence,
        upload=upload,
    )


def book_checkpoint(mailbox: MailboxSurface, command_id: str) -> str:
    """Carry the pause command onto the mailbox's ``checkpointed`` rung.

    The transaction's last bookkeeping step (NXT-15 / the MailboxSurface
    integration): once the durable receipt is committed in the pause
    state, the command that requested the pause climbs the ladder's
    final rung — the checkpoint CARRIES it. Works over the in-memory
    :class:`~forge.adaptive.control.Mailbox` synchronously; the durable
    :class:`~forge.adaptive.mailbox_db.PostgresMailbox` exposes the same
    ``checkpoint`` as an awaitable (its caller awaits and applies the
    same rule: only an ``applied`` command may be booked).
    """
    return mailbox.checkpoint(command_id).status


# ---------------------------------------------------------------------------
# Restore (NXT-17)
# ---------------------------------------------------------------------------


def restore_wip(
    *,
    artifact_id: str,
    store: ContentAddressedStore,
    target: Path,
    principal: str,
    download: RemoteCheckpointSource | None = None,
) -> RestoreReport:
    """Reconstruct the checkpointed WIP into *target* on a second runner.

    The manifest is fetched through the store's VERIFIED read under
    *principal* — an ungranted principal learns nothing (``None``, the
    same as absence) and tampered bytes raise before anything is
    written. Every file path is validated against escapes BEFORE the
    first write, every blob is re-fetched digest-verified, modes are
    reapplied, deletions are applied (an already-absent deletion is
    recorded, not an error), and every outcome is reported per file. A
    missing or corrupt required blob fails the restore with the path
    and digest named — resume must be blocked with evidence, never
    handed a half-alive workspace.

    ``download`` (wave C/D, optional): a transport channel — when
    given, ``artifact_id`` is read as the REMOTE durable reference
    (``<work_id>@<checkpoint_id>``) and the checkpoint is fetched
    digest-verified into *store* first (a fresh local store on the
    second runner); the local restore then continues unchanged. A
    refused fetch — malformed reference, tampered transfer, unreachable
    control plane — fails the whole restore with the reason, before a
    single file is written.
    """
    target = Path(target)
    if download is not None:
        try:
            artifact_id = download.fetch_checkpoint(artifact_id, store)
        except Exception as exc:  # noqa: BLE001 — the fetch refusal IS the restore verdict
            return RestoreReport(
                artifact_id=artifact_id,
                ok=False,
                files=(),
                failures=(
                    (
                        f"remote checkpoint {artifact_id!r} could not be fetched: {exc} — "
                        "restore refuses rather than reconstructing from nothing"
                    ),
                ),
            )
    try:
        manifest_bytes = store.get_verified(artifact_id, principal=principal)
    except CorruptArtifactError as exc:
        return RestoreReport(
            artifact_id=artifact_id,
            ok=False,
            files=(),
            failures=(f"manifest {artifact_id} is corrupt: {exc}",),
        )
    if manifest_bytes is None:
        return RestoreReport(
            artifact_id=artifact_id,
            ok=False,
            files=(),
            failures=(
                f"manifest {artifact_id} is absent or not granted to principal "
                f"{principal!r} — knowing the address probes nothing",
            ),
        )
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return RestoreReport(
            artifact_id=artifact_id,
            ok=False,
            files=(),
            failures=(f"manifest {artifact_id} is not valid JSON: {exc}",),
        )
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        schema = manifest.get("schema") if isinstance(manifest, dict) else None
        return RestoreReport(
            artifact_id=artifact_id,
            ok=False,
            files=(),
            failures=(
                f"unsupported manifest schema {schema!r}: restore requires "
                f"{MANIFEST_SCHEMA} — a filename list without content blobs restores nothing",
            ),
        )

    files = manifest.get("files")
    deletions = manifest.get("deletions")
    if not isinstance(files, dict) or not isinstance(deletions, list):
        return RestoreReport(
            artifact_id=artifact_id,
            ok=False,
            files=(),
            failures=("manifest is malformed: files/deletions sections missing",),
        )

    # Validate EVERY path before writing anything: an escape attempt must
    # fail the whole restore, not leave a half-written target behind.
    try:
        safe_files = [(str(rel), _safe_manifest_path(str(rel))) for rel in sorted(files)]
        safe_deletions = [
            (str(rel), _safe_manifest_path(str(rel))) for rel in sorted(map(str, deletions))
        ]
    except ValueError as exc:
        return RestoreReport(
            artifact_id=artifact_id,
            ok=False,
            files=(),
            failures=(str(exc),),
        )

    outcomes: list[FileRestore] = []
    failures: list[str] = []
    for rel, pure in safe_files:
        entry = files[rel]
        if not isinstance(entry, dict):
            outcomes.append(FileRestore(rel, "failed", reason="manifest entry is not an object"))
            failures.append(f"{rel}: manifest entry is not an object")
            continue
        digest = str(entry.get("digest", ""))
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            outcomes.append(
                FileRestore(rel, "failed", digest=digest, reason="entry carries no content digest")
            )
            failures.append(
                f"{rel}: entry carries no content digest — a bare filename restores nothing"
            )
            continue
        try:
            blob = store.get_verified(digest, principal=principal)
        except CorruptArtifactError as exc:
            outcomes.append(FileRestore(rel, "failed", digest=digest, reason=str(exc)))
            failures.append(f"{rel}: blob {digest} is corrupt: {exc}")
            continue
        if blob is None:
            outcomes.append(
                FileRestore(
                    rel, "failed", digest=digest, reason="required blob absent from the store"
                )
            )
            failures.append(f"{rel}: required blob {digest} is absent — resume is blocked")
            continue
        destination = target.joinpath(*pure.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(blob)
        mode = entry.get("mode", _MODE_REGULAR)
        os.chmod(destination, int(mode) if isinstance(mode, int) else _MODE_REGULAR)
        outcomes.append(FileRestore(rel, "restored", digest=digest))

    for rel, pure in safe_deletions:
        doomed = target.joinpath(*pure.parts)
        if doomed.exists():
            doomed.unlink()
            outcomes.append(FileRestore(rel, "deleted"))
        else:
            outcomes.append(FileRestore(rel, "already-absent"))

    return RestoreReport(
        artifact_id=artifact_id,
        ok=not failures,
        files=tuple(outcomes),
        failures=tuple(failures),
    )


# ---------------------------------------------------------------------------
# Resume (NXT-18)
# ---------------------------------------------------------------------------


def resume_from_checkpoint(
    state: PauseState,
    *,
    store: ContentAddressedStore,
    principal: str,
    authorization: Callable[[], tuple[bool, str]],
    attempt_id: str,
    prior_epoch: int,
) -> ResumeOutcome:
    """Resume under a FRESH epoch with CURRENT authorization (NXT-18).

    Nothing here trusts a constructor default: the pause state must end
    ``paused`` with a VERIFIED receipt (a partial/failed pause refuses —
    it has no durable checkpoint to stand on), the authorization
    callable decides with the authority it holds RIGHT NOW (revoked
    access blocks resume even when it was valid before the pause), and
    the checkpoint BYTES are re-read digest-verified against the store
    before any epoch is spent. Only then does
    :func:`forge.adaptive.control.new_execution_epoch` allocate
    ``prior_epoch + 1`` — late artifacts from the interrupted attempt
    cannot masquerade as current ones (:func:`accepts_epoch` is the
    gate callers apply to them).
    """
    receipt = state.checkpoint_receipt
    if state.pause_status != "paused" or receipt is None or not receipt.verified:
        return ResumeOutcome(
            ok=False,
            reason=(
                f"no verified durable checkpoint (pause_status={state.pause_status!r}): "
                "resume stands on a digest-verified capture or refuses"
            ),
        )
    authorized, why = authorization()
    if not authorized:
        return ResumeOutcome(ok=False, reason=f"authorization refused NOW: {why}")
    try:
        manifest_bytes = store.get_verified(receipt.artifact_id, principal=principal)
    except CorruptArtifactError as exc:
        return ResumeOutcome(
            ok=False,
            reason=f"checkpoint bytes are corrupt NOW: {exc}",
            authorization_valid=True,
        )
    if manifest_bytes is None:
        return ResumeOutcome(
            ok=False,
            reason=(
                f"checkpoint {receipt.artifact_id} no longer resolves for principal "
                f"{principal!r}: expired, pruned, or never granted — resume refuses"
            ),
            authorization_valid=True,
        )
    epoch = new_execution_epoch(attempt_id, prior_epoch)
    return ResumeOutcome(
        ok=True,
        reason=(
            f"resumed under execution epoch {epoch['execution_epoch']} from verified "
            f"checkpoint {receipt.artifact_id} (sequence {receipt.sequence})"
        ),
        epoch=epoch,
        authorization_valid=True,
        checkpoint_verified=True,
    )


def accepts_epoch(recorded_epoch: int | None, active_epoch: int) -> bool:
    """May a callback/candidate recorded under *recorded_epoch* act now?

    NXT-18's stale-artifact rule: only material recorded under the
    ACTIVE epoch is current. Anything from a superseded epoch (or of
    unknown provenance, ``None``) is refused — a late vendor callback
    or candidate artifact from the interrupted attempt must never
    advance the resumed work.
    """
    return recorded_epoch is not None and recorded_epoch == active_epoch
