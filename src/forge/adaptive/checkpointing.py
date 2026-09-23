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
   checkpoint reference. The walk's scope is the agent's work tree
   ONLY: ``.forge`` (the store never captures itself), ``.git``,
   caches and ``node_modules`` are excluded (R28-04). Every uploaded
   blob and the manifest itself are then READ BACK through the store's
   verified read and re-hashed to their addresses — a receipt is minted
   ``verified`` only after that read-back, and the manifest + blobs are
   pinned with a retention reference (``checkpoint:<work_id>``) so GC
   cannot delete the checkpoint of an actively paused work on age
   alone.
2. **Restore** (:func:`restore_wip`) — on a SECOND runner with a fresh
   store instance: fetch the manifest through the verified read, refuse
   unsupported/legacy schemas (a filename list without blobs restores
   nothing), then verify EVERYTHING before touching the workspace
   (R28-02): every path against escapes AND reserved namespaces
   (``.git``, the checkpoint store, credential-shaped names), every
   ancestor against symlink substitution (``lstat``, no following), the
   manifest's own path set against duplicates/aliases and
   file-versus-directory conflicts, every entry's kind/mode, and every
   blob digest-verified from the store. Only when the whole plan passes
   is the next generation of the workspace built complete in a STAGING
   directory and activated in ONE promotion. Two promotion modes:

   - ``promote="switch"`` (default) — the staged tree replaces the
     target by two atomic directory renames with the first rolled back
     if the second fails. Meant for a caller OUTSIDE the workspace.
   - ``promote="generation"`` (R32-01, the lane's mode) — the staged
     tree lands as a SIBLING generation
     (``<parent>/.forge-workspace-gen-<checkpoint[:12]>``) and the
     ORIGINAL TARGET IS NEVER RENAMED OR REMOVED: a process sitting in
     the target (the lane, and the CI shell that spawned it) keeps a
     valid ``os.getcwd()`` and relative writes keep working. The target
     only gains the ``.forge/workspace-generation`` POINTER naming the
     active generation for the collector step, and the report carries
     ``workspace_generation``.

   A failure ANYWHERE — preflight, staging or the switch itself —
   leaves the target at its original state, never a half-applied
   workspace; a restore killed between a promotion's two renames leaves
   the interrupted original parked under an OWNED
   ``.forge-restore-backup-<work_id>-...`` name that the next restore
   of the SAME work resolves (see :func:`_recover_abandoned_promotions`).
   R32-02: every backup/staging directory the promotion creates carries
   the owning WORK'S id in its name, and recovery processes ONLY this
   work's directories — another workspace's assets under the same
   parent are inventoried as ``unrecognized`` and never touched. Where
   the parent is not writable and the whole-tree switch is unavailable,
   the switch mode promotes per-file under a full SAVEPOINT (the
   original of every touched file is captured first and restored on any
   failure); the generation mode REFUSES there instead (rewriting the
   live target is exactly what it exists to avoid).
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
import re
import shutil
import stat
import tempfile
import uuid
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
    "PromoteMode",
    "RestorePhase",
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
_MODES: Final = frozenset({_MODE_EXECUTABLE, _MODE_REGULAR})

#: Directories the capture walk NEVER descends into (R28-04): the
#: capture scope is the agent's WORK TREE only. ``.git`` is runner
#: infrastructure (and read-only packs fail on restore), ``.forge`` is
#: the lane's own runtime state INCLUDING the checkpoint store — the
#: store must never capture itself — and the rest are caches/build
#: output no checkpoint may grow by re-capturing.
_CAPTURE_EXCLUDED_DIRS: Final = frozenset(
    {".git", ".forge", "__pycache__", ".pytest_cache", "node_modules"}
)


def _excluded_from_capture(name: str) -> bool:
    """Whether one walk directory is outside the capture scope."""
    return name in _CAPTURE_EXCLUDED_DIRS or name.endswith(".egg-info")


#: ``.forge`` entries the RESTORE owns and a manifest may never write:
#: the checkpoint store itself, and the active-generation POINTER the
#: generation promotion records for the collector step (R32-01) — a
#: restored checkpoint must not forge where the parent shell looks for
#: the active workspace.
_RESERVED_FORGE_ENTRIES: Final = frozenset({"checkpoints", "workspace-generation"})

#: The pointer document's schema (``<workspace>/.forge/workspace-generation``).
_GENERATION_POINTER_SCHEMA: Final = "forge.workspace-generation/1"


def _reserved_manifest_path(pure: PurePosixPath) -> str | None:
    """Why *pure* may not be restored, or None when it may (R28-02).

    The consumer validates the manifest INDEPENDENTLY of the producer:
    a checkpoint crossed a network boundary, so reserved namespaces are
    refused here, not trusted to have been excluded at capture. Refused:
    ``.git`` (repository/control infrastructure — config, hooks), the
    ``.forge/checkpoints`` store and the ``.forge/workspace-generation``
    pointer (restore-owned records), and credential-shaped paths (a
    restored checkpoint must not drop private keys into a runner).
    """
    parts = pure.parts
    if parts[0] == ".git":
        return f"{pure.as_posix()!r} is inside the .git namespace — repository and control state is never restored over"
    if len(parts) >= 2 and parts[0] == ".forge" and parts[1] in _RESERVED_FORGE_ENTRIES:
        return (
            f"{pure.as_posix()!r} is inside .forge/{parts[1]} — a restore-owned "
            "record the checkpoint never writes"
        )
    name = parts[-1].lower()
    if name.endswith(".pem") or name.endswith(".key"):
        return f"{pure.as_posix()!r} is credential-shaped (private key material) — never restored"
    if name == ".env" or name.startswith("credentials"):
        return f"{pure.as_posix()!r} is credential-shaped (environment/credentials file) — never restored"
    return None


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


class _PromotionFailure(Exception):
    """Internal: the promotion could not complete (NEXT-04).

    Carries the actionable reason and the workspace verdict:
    ``target_invalid=False`` means the target sits at its ORIGINAL state
    (nothing was mutated, or the savepoint/backup rollback restored it);
    ``target_invalid=True`` means the rollback ITSELF failed — the
    workspace is missing or broken and the report must say a retry
    rebuilds from the approved base, never that the target is usable.
    """

    def __init__(self, reason: str, *, target_invalid: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.target_invalid = target_invalid


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


#: The promotion verdict a :class:`RestoreReport` carries (NEXT-04):
#: ``completed`` — the whole plan landed through the generation switch;
#: ``preflight_failed`` — NOTHING was written (the verified-everything
#: gate refused before any mutation); ``promotion_failed`` — the restore
#: died while staging or switching generations and the TARGET WAS ROLLED
#: BACK to its original state (or, when ``target_invalid`` is also set,
#: the rollback itself failed and the workspace is explicitly unusable —
#: retry from the approved base).
RestorePhase = Literal["completed", "preflight_failed", "promotion_failed"]

#: How :func:`restore_wip` activates the staged generation (R32-01):
#: ``"switch"`` — the staged tree REPLACES the target by two atomic
#: renames (a caller outside the workspace); ``"generation"`` — the
#: staged tree lands as a SIBLING ``.forge-workspace-gen-*`` directory
#: and the target is never renamed or removed (the lane's mode: the
#: process and its parent CI shell may be sitting inside the target).
PromoteMode = Literal["switch", "generation"]


@dataclass(frozen=True)
class RestoreReport:
    """The second-runner restore verdict with per-file evidence.

    ``phase`` names WHERE the restore decided (NEXT-04): a preflight
    refusal never wrote a byte; a promotion failure happened with the
    workspace mutation already in flight and the target rolled back.
    ``target_invalid`` is True only when that rollback itself failed —
    the workspace is then MISSING or mixed-broken and a lane must treat
    it as unusable, rebuilding from the approved base. ``recovery``
    carries what this restore found and resolved of an EARLIER crashed
    restore's abandoned promotion (rolled-back backups, collected
    staging directories). ``workspace_generation`` (R32-01) is the
    ABSOLUTE path of the landed generation under ``promote="generation"``
    — empty in switch mode and on any failure. ``unrecognized``
    (R32-02) inventories the promotion leftovers under the shared
    parent prefixes that are NOT owned by this work — reported, never
    touched (another workspace's recovery assets).
    """

    artifact_id: str
    ok: bool
    files: tuple[FileRestore, ...]
    failures: tuple[str, ...]
    phase: RestorePhase = "completed"
    target_invalid: bool = False
    recovery: tuple[str, ...] = ()
    workspace_generation: str = ""
    unrecognized: tuple[str, ...] = ()

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
    baseline_modes: Mapping[str, int] | None = None,
    source_oids: Mapping[str, str] | None = None,
    sequence: int = 0,
    upload: WipUploadChannel | None = None,
) -> CheckpointReceipt:
    """Capture the working tree at *root* as a digest-verified checkpoint.

    ``tracked_baseline`` maps path -> content digest for the source
    snapshot/revision the WIP applies on top of (the caller's durable
    authority — :class:`forge.adaptive.models.SnapshotSet` digests in
    production). The digest scheme is CANONICAL: the baseline stores the
    sha256 of RAW file bytes, the same identity the walk computes
    (R28-04) — git blob OIDs are a different hash of a different
    wrapping and never matched here. ``baseline_modes`` (optional, the
    git index's executable bit in production) extends the comparison to
    MODES: a file whose content matches but whose executable bit
    changed is captured as a ``modified`` entry, not silently skipped.

    Files whose digest still matches the baseline are NOT re-uploaded
    (the baseline reconstructs them); modified, new and untracked files
    are uploaded as content-addressed blobs; paths in the baseline that
    vanished from the tree are recorded as deletions. The walk's scope
    is the agent's work tree only: ``.forge/`` (the store itself),
    ``.git/``, ``__pycache__/``, ``.pytest_cache/``, ``*.egg-info/``
    and ``node_modules/`` are excluded (R28-04) — repeated captures
    never include the previous checkpoint store and cannot grow from
    self-capture. The manifest freezes all of it plus source OIDs and
    the applied-command ``sequence``, and its own content address
    becomes the checkpoint reference.

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
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not (Path(dirpath) / name).is_symlink()
            and not _excluded_from_capture(name)  # R28-04: work tree only
        )
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
            mode = _mode_of(path)
            unchanged_content = baseline == digest
            unchanged_mode = baseline_modes is None or baseline_modes.get(rel) == mode
            if unchanged_content and unchanged_mode:
                continue  # unchanged: the tracked baseline reconstructs it
            store.put(data)
            files[rel] = {
                "digest": digest,
                "mode": mode,
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
    baseline_modes: Mapping[str, int] | None = None,
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
        baseline_modes=baseline_modes,
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


@dataclass(frozen=True)
class _PlannedFile:
    """One fully verified manifest file, ready to stage and move."""

    rel: str
    parts: tuple[str, ...]
    digest: str
    mode: int


def _symlink_free_target_path(
    target: Path, pure: PurePosixPath, *, final_is_file: bool
) -> str | None:
    """Why *pure* cannot be applied under *target*, or None when it can.

    R28-02's containment rule: every component of the path is
    ``lstat``-ed in the EXISTING target — a symlink anywhere along the
    way means the restore would follow it outside the workspace, and is
    refused (never resolved, never written through). Kind conflicts the
    filesystem would later reject are also named precisely here: an
    intermediate component that exists as a regular file, and (for a
    planned file) a final component that exists as a directory.
    """
    current = target
    parts = pure.parts
    for index, part in enumerate(parts):
        current = current / part
        final = index == len(parts) - 1
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if final and not final_is_file:
                return None  # already absent — nothing to delete, nothing to check
            continue  # a missing component is created by the restore itself
        except OSError as exc:
            return f"{current}: {exc}"
        if stat.S_ISLNK(info.st_mode):
            return (
                f"{current} is a symlink — the restore refuses to follow it outside the workspace"
            )
        if final:
            if final_is_file and stat.S_ISDIR(info.st_mode):
                return f"{current} exists as a directory — a file cannot replace it"
            if not final_is_file and stat.S_ISDIR(info.st_mode):
                return f"{current} exists as a directory — only file deletions are supported"
        elif not stat.S_ISDIR(info.st_mode):
            return f"{current} exists as a non-directory — it cannot carry {pure.as_posix()!r}"
    return None


#: The staging prefix beside the target (a crash leaves these behind for
#: :func:`_recover_abandoned_promotions` to collect). R32-02: the name
#: continues with the OWNING work's token, so recovery never mistakes a
#: sibling workspace's staging for this work's.
_STAGING_PREFIX: Final = ".forge-restore-"
#: The parked ORIGINAL of an interrupted promotion: present with the
#: promoted path MISSING means the promotion died between its two
#: renames — the next restore of the SAME work rolls the original back
#: before doing anything else (NEXT-04's crash semantics). R32-02: the
#: name binds it to its owner,
#: ``.forge-restore-backup-<work_id>[-<checkpoint_id[:8]>]-<pid>-<uuid>``,
#: so a shared parent can hold several workspaces' leftovers without
#: one workspace's recovery ever claiming another's.
_BACKUP_PREFIX: Final = ".forge-restore-backup-"
#: The sibling generation a ``promote="generation"`` restore lands in
#: (R32-01): ``<parent>/.forge-workspace-gen-<checkpoint_id[:12]>`` — a
#: STABLE path that is never the target and never removed while the
#: lane or its CI shell sits in the workspace.
_GENERATION_PREFIX: Final = ".forge-workspace-gen-"
#: The pointer recording the ACTIVE generation inside the workspace
#: (``<workspace>/.forge/workspace-generation``) for the collector step
#: — the parent CI shell resolves THIS, never its inherited directory
#: inode (which a promotion may have retired underneath it).
_GENERATION_POINTER: Final = ".forge/workspace-generation"

_WORK_TOKEN_RE: Final = re.compile(r"[A-Za-z0-9._-]{1,64}")
_HEX8_RE: Final = re.compile(r"[0-9a-f]{8}")


def _work_token(work_id: str) -> str:
    """The directory-name token binding promotion assets to *work_id*.

    A plain id passes through unchanged; anything that could smuggle a
    path separator or an overlong name into a directory name is folded
    to its sha256 prefix (the token only has to be STABLE and namespaced
    per work, not readable).
    """
    token = (work_id or "").strip()
    if _WORK_TOKEN_RE.fullmatch(token):
        return token
    return _sha256(token.encode("utf-8", "replace")).hex()[:16]


def _generation_dir(parent: Path, checkpoint_id: str) -> Path:
    """The stable generation path *checkpoint_id* restores into (R32-01)."""
    return parent / f"{_GENERATION_PREFIX}{checkpoint_id[:12]}"


def _backup_dir_name(work_id: str, checkpoint_id: str) -> str:
    """An OWNED park name for the original a promotion moves aside (R32-02).

    ``.forge-restore-backup-<work_id>[-<checkpoint_id[:8]>]-<pid>-<uuid>``:
    the work token binds the directory to the transaction's owner, the
    optional checkpoint fragment lets recovery distinguish a backup of
    THIS checkpoint's promotion from an older one's, and pid+uuid keep
    concurrent promotions from colliding.
    """
    token = _work_token(work_id)
    fragment = f"-{checkpoint_id[:8]}" if checkpoint_id else ""
    return f"{_BACKUP_PREFIX}{token}{fragment}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _backup_checkpoint_fragment(name: str, token: str) -> str:
    """The 8-hex checkpoint fragment an OWNED backup name carries (``""`` none).

    The name is read from the right — ``<pid>-<uuid>`` trail first — so
    dash-bearing work tokens stay intact; the fragment is the 8-hex run
    before the trail when one is present.
    """
    rest = name.removeprefix(f"{_BACKUP_PREFIX}{token}-").split("-")
    if len(rest) >= 3 and _HEX8_RE.fullmatch(rest[-3]):
        return rest[-3]
    return ""


def _landed_generation_for(parent: Path, fragment: str) -> Path | None:
    """The LANDED generation directory *fragment* names, if one exists.

    Generation names carry ``checkpoint_id[:12]``; a parked backup
    carries ``[:8]`` — the match is the prefix relation between them,
    which is exact for the checkpoint that minted both.
    """
    if not fragment:
        return None
    for candidate in sorted(parent.glob(f"{_GENERATION_PREFIX}*")):
        if candidate.is_dir() and candidate.name.removeprefix(_GENERATION_PREFIX).startswith(
            fragment
        ):
            return candidate
    return None


def _write_generation_pointer(
    root: Path, *, work_id: str, checkpoint_id: str, generation: Path
) -> None:
    """Record the ACTIVE generation at ``root/.forge/workspace-generation``.

    R32-01's CI contract: the parent shell (and every step after the
    lane) resolves the workspace the restored WIP lives in through this
    POINTER — never through an inherited cwd that a promotion may have
    retired. The document names the checkpoint that produced the
    generation, its directory NAME beside the workspace, and the
    absolute path for convenience.
    """
    pointer = root / _GENERATION_POINTER
    pointer.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "schema": _GENERATION_POINTER_SCHEMA,
        "work_id": work_id,
        "checkpoint_id": checkpoint_id,
        "generation": generation.name,
        "generation_path": str(generation),
    }
    pointer.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def _staging_dir(target: Path, work_id: str) -> tuple[Path, bool]:
    """A scratch directory for the staged next generation of the target.

    Beside the target when the parent allows it (the same filesystem —
    the promotion's directory renames are atomic there), OWNED by
    *work_id* (R32-02), and the returned flag is ``True``. A read-only
    parent falls back to ``.forge/`` INSIDE the target — excluded from
    every capture walk, and still discarded whole on any failure —
    where the whole-tree switch is IMPOSSIBLE, so the caller promotes
    per-file under a savepoint instead (flag ``False``).
    """
    try:
        staged = Path(
            tempfile.mkdtemp(dir=target.parent, prefix=f"{_STAGING_PREFIX}{_work_token(work_id)}-")
        )
        return staged, True
    except OSError:
        inside = target / ".forge"
        inside.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(dir=inside, prefix="restore-")), False


def _recover_abandoned_promotions(
    target: Path,
    *,
    work_id: str,
    checkpoint_id: str = "",
    subject: Path | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Resolve the leftovers of THIS WORK's restore that died mid-promotion.

    NEXT-04's crash recovery, R32-02's ownership scope: every directory
    examined lives under the target's parent (a SHARED parent in
    multi-workspace deployments), so a leftover is only ever touched
    when its name carries the CURRENT work's id — everything else under
    the prefixes is inventoried in ``unrecognized`` and left untouched
    (another workspace's backup is not this restore's to resolve, and a
    legacy pre-ownership name carries no binding at all). For an OWNED
    leftover:

    - a parked backup whose checkpoint fragment names a LANDED
      generation means the interrupted promotion completed and only the
      cleanup died — the landed generation stands, the parked copy is
      discarded;
    - a parked backup of THIS checkpoint's promotion with the promoted
      path (*subject* — the target in switch mode, the generation in
      generation mode) MISSING means the promotion died between its two
      renames — the original is rolled back into place (the honest
      posture: the restore as a whole failed, so the workspace returns
      to its pre-restore state); with the subject present, the parked
      copy is the retired pre-promotion original and is discarded;
    - an owned backup of ANOTHER checkpoint's promotion has no
      deterministic destination under this restore — inventoried, never
      guessed;
    - an abandoned OWNED ``.forge-restore-<work>-*`` staging directory
      is collected (removed) — a fresh generation is always built from
      scratch, never resumed out of a stale staging tree.

    Returns ``(recovery_notes, unrecognized_inventory)``.
    """
    notes: list[str] = []
    unrecognized: list[str] = []
    parent = target.parent
    if not parent.is_dir():
        return (), ()
    subject = target if subject is None else subject
    token = _work_token(work_id)
    fragment_now = checkpoint_id[:8] if checkpoint_id else ""
    for backup in sorted(parent.glob(f"{_BACKUP_PREFIX}*")):
        if not backup.is_dir():
            continue
        if not backup.name.startswith(f"{_BACKUP_PREFIX}{token}-"):
            unrecognized.append(
                f"{backup.name}: not owned by work {work_id!r} — left untouched "
                "for the operator (another workspace's recovery asset)"
            )
            continue
        fragment = _backup_checkpoint_fragment(backup.name, token)
        landed = _landed_generation_for(parent, fragment)
        if landed is not None:
            shutil.rmtree(backup, ignore_errors=True)
            notes.append(
                f"discarded abandoned backup {backup.name}: the generation "
                f"{landed.name} stands — the interrupted restore died after "
                "landing it, before cleanup"
            )
            continue
        if not fragment or fragment != fragment_now:
            unrecognized.append(
                f"{backup.name}: owned by work {work_id!r} but bound to checkpoint "
                f"{fragment or '?'} which this restore does not continue — left "
                "untouched for the operator"
            )
            continue
        if subject.exists():
            shutil.rmtree(backup, ignore_errors=True)
            notes.append(
                f"discarded abandoned backup {backup.name}: the promotion it was "
                f"parked for has landed — {subject.name} stands and the parked "
                "copy is its retired original"
            )
            continue
        try:
            os.replace(backup, subject)
        except OSError as exc:
            notes.append(
                f"abandoned backup {backup.name} could not be resolved ({exc}) — "
                f"{subject.name} stays absent; rebuild from the approved base"
            )
            continue
        notes.append(
            f"rolled back abandoned backup {backup.name}: the interrupted "
            f"promotion never landed — {subject.name} is at its prior state"
        )
    for leftover in sorted(parent.glob(f"{_STAGING_PREFIX}*")):
        if not leftover.is_dir() or leftover.name.startswith(_BACKUP_PREFIX):
            continue  # the backup loop owns those names
        if leftover.name.startswith(f"{_STAGING_PREFIX}{token}-"):
            shutil.rmtree(leftover, ignore_errors=True)
            notes.append(f"collected abandoned staging directory {leftover.name}")
        else:
            unrecognized.append(
                f"{leftover.name}: not owned by work {work_id!r} — left untouched "
                "for the operator (another workspace's staging tree)"
            )
    return tuple(notes), tuple(unrecognized)


def _apply_plan_to_generation(
    tree: Path,
    planned: list[_PlannedFile],
    planned_deletions: list[tuple[str, PurePosixPath]],
    blobs: Mapping[str, bytes],
) -> list[FileRestore]:
    """Apply the VERIFIED plan to a private COPY of the workspace.

    Writes files, restores their modes, applies deletions — all inside
    *tree*, which nothing else can observe. Returns the per-deletion
    outcomes computed against the copy (identical to the original by
    construction). An OSError here aborts the promotion with the LIVE
    target never mutated.
    """
    for item in planned:
        staged = tree.joinpath(*item.parts)
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(blobs[item.digest])
        os.chmod(staged, item.mode)
    outcomes: list[FileRestore] = []
    for rel, pure in planned_deletions:
        doomed = tree.joinpath(*pure.parts)
        if doomed.exists():
            doomed.unlink()
            outcomes.append(FileRestore(rel, "deleted"))
        else:
            outcomes.append(FileRestore(rel, "already-absent"))
    return outcomes


def _switch_workspace_generation(
    target: Path, tree: Path, *, work_id: str, checkpoint_id: str
) -> None:
    """Activate the staged generation by ONE whole-tree switch (NEXT-04).

    Filesystem assumptions (documented, not guessed): *tree* sits on the
    same filesystem as *target* (the staging is created in the target's
    parent), and directory renames are atomic there. Crash semantics:
    the switch is two renames — ``target -> backup`` then ``tree ->
    target``. A crash before the first leaves everything as it was; a
    crash between the two leaves the original parked under an OWNED
    ``.forge-restore-backup-<work_id>-...`` name (R32-02) with the
    target ABSENT (explicitly unusable — never mixed) for
    :func:`_recover_abandoned_promotions` to roll back on the next run
    of the same work; a failure of the second rename under our own
    hands rolls the first back BEFORE raising, so a caught failure
    still leaves the original generation in place.

    The caller must NOT be sitting inside *target* (R32-01): the switch
    renames the directory a live process's cwd is bound to and then
    deletes the parked original — a lane restores with
    ``promote="generation"`` instead.
    """
    if not target.exists():
        # A fresh runner with no workspace yet: there is no original to
        # retire — the staged generation lands directly.
        try:
            os.replace(tree, target)
        except OSError as exc:
            raise _PromotionFailure(
                f"landing the staged workspace failed: {exc} — the target was "
                "never present, nothing to roll back",
                target_invalid=False,
            ) from exc
        return
    backup = target.parent / _backup_dir_name(work_id, checkpoint_id)
    try:
        os.replace(target, backup)
    except OSError as exc:
        raise _PromotionFailure(
            f"moving the current workspace generation aside failed: {exc} — "
            "the target was never mutated",
            target_invalid=False,
        ) from exc
    try:
        os.replace(tree, target)
    except OSError as landing:
        try:
            os.replace(backup, target)
        except OSError as rollback:
            raise _PromotionFailure(
                f"landing the staged workspace failed ({landing}) AND the rollback "
                f"of the original generation failed ({rollback}) — the workspace is "
                f"INVALID (absent; the original stays parked at {backup.name}): a "
                "retry must rebuild from the approved base",
                target_invalid=True,
            ) from rollback
        raise _PromotionFailure(
            f"landing the staged workspace failed: {landing} — the original "
            "workspace generation was rolled back; the restore failed as a whole",
            target_invalid=False,
        ) from landing
    shutil.rmtree(backup, ignore_errors=True)  # the retired generation is garbage


def _promote_whole_tree(
    target: Path,
    staging: Path,
    planned: list[_PlannedFile],
    planned_deletions: list[tuple[str, PurePosixPath]],
    blobs: Mapping[str, bytes],
    *,
    work_id: str = "",
    checkpoint_id: str = "",
) -> list[FileRestore]:
    """Build the COMPLETE next generation in staging, then switch once.

    The whole target (legitimate new files, modes, untracked content —
    everything the plan does not name rides along unchanged) is copied
    into ``staging/tree``, the verified plan is applied to the COPY, and
    the copy becomes the target by one whole-tree switch. No per-file
    exposure of the live target exists on this path at all.
    """
    tree = staging / "tree"
    try:
        if target.exists():
            shutil.copytree(target, tree, symlinks=True)
        else:
            tree.mkdir(parents=True)
        deletion_outcomes = _apply_plan_to_generation(tree, planned, planned_deletions, blobs)
        _switch_workspace_generation(target, tree, work_id=work_id, checkpoint_id=checkpoint_id)
    except _PromotionFailure:
        raise
    except OSError as exc:
        raise _PromotionFailure(
            f"building the staged workspace generation failed: {exc} — "
            "the live target was never mutated",
            target_invalid=False,
        ) from exc
    return [FileRestore(item.rel, "restored", digest=item.digest) for item in planned] + (
        deletion_outcomes
    )


def _promote_to_generation(
    target: Path,
    staging: Path,
    generation: Path,
    planned: list[_PlannedFile],
    planned_deletions: list[tuple[str, PurePosixPath]],
    blobs: Mapping[str, bytes],
    *,
    work_id: str = "",
    checkpoint_id: str = "",
) -> list[FileRestore]:
    """Build the COMPLETE next generation and land it as a SIBLING (R32-01).

    The ORIGINAL TARGET is never renamed and never removed — the lane
    process (and the CI shell that spawned it) may be sitting inside it,
    and the reviewer's P03 proved a whole-tree switch there leaves
    ``os.getcwd()`` resolving to a deleted directory. Instead:

    - the complete next generation (a copy of the target with the
      verified plan applied, plus its own self-describing
      ``.forge/workspace-generation`` pointer) lands at *generation* —
      ``<parent>/.forge-workspace-gen-<checkpoint_id[:12]>``, a STABLE
      path that outlives any single attempt;
    - a previous generation of the SAME checkpoint is retired aside
      under an OWNED backup name first, rolled back if the landing
      fails, and discarded after (the checkpoint reconstructs it);
    - the target only gains the ``.forge/workspace-generation`` POINTER
      naming the active generation — the collector step's contract.

    A failure at any boundary leaves the target at its ORIGINAL state
    with no usable generation reported — the caller (the lane's
    required-restore gate) treats that as a failed restore.
    """
    tree = staging / "tree"
    try:
        if target.exists():
            shutil.copytree(target, tree, symlinks=True)
        else:
            tree.mkdir(parents=True)
        _write_generation_pointer(
            tree, work_id=work_id, checkpoint_id=checkpoint_id, generation=generation
        )
        deletion_outcomes = _apply_plan_to_generation(tree, planned, planned_deletions, blobs)
        backup: Path | None = None
        if generation.exists():
            # A previous restore of the SAME checkpoint landed here: retire
            # it aside (owned name) so the landing stays ONE rename.
            backup = generation.parent / _backup_dir_name(work_id, checkpoint_id)
            try:
                os.replace(generation, backup)
            except OSError as exc:
                raise _PromotionFailure(
                    f"retiring the previous workspace generation failed: {exc} — "
                    "the live target was never mutated",
                    target_invalid=False,
                ) from exc
        try:
            os.replace(tree, generation)
        except OSError as landing:
            if backup is not None:
                try:
                    os.replace(backup, generation)
                except OSError as rollback:
                    raise _PromotionFailure(
                        f"landing the staged workspace generation failed ({landing}) AND "
                        f"rolling the retired generation back failed ({rollback}) — the "
                        f"generation is INVALID (absent; the original stays parked at "
                        f"{backup.name} for recovery): retry from the checkpoint",
                        target_invalid=False,
                    ) from rollback
            raise _PromotionFailure(
                f"landing the staged workspace generation failed: {landing} — the "
                "original workspace was never touched; the restore failed as a whole",
                target_invalid=False,
            ) from landing
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)  # the retired generation is garbage
        _write_generation_pointer(
            target, work_id=work_id, checkpoint_id=checkpoint_id, generation=generation
        )
    except _PromotionFailure:
        raise
    except OSError as exc:
        raise _PromotionFailure(
            f"building the staged workspace generation failed: {exc} — "
            "the live target was never mutated",
            target_invalid=False,
        ) from exc
    return [FileRestore(item.rel, "restored", digest=item.digest) for item in planned] + (
        deletion_outcomes
    )


def _promote_under_savepoint(
    target: Path,
    staging: Path,
    planned: list[_PlannedFile],
    planned_deletions: list[tuple[str, PurePosixPath]],
    blobs: Mapping[str, bytes],
) -> list[FileRestore]:
    """Per-file promotion under a FULL savepoint (the degraded path).

    Used only when the whole-tree switch is unavailable (a read-only
    target parent parked the staging inside ``target/.forge``). The
    original of EVERY file the plan touches — replaced or deleted — is
    captured into ``staging/savepoint`` BEFORE the first mutation; on
    any failure the inverse is replayed in reverse order (replaced
    originals moved back, new files removed, deleted originals
    recreated), so the target returns to its original state and the
    report carries ``promotion_failed``, never a mixed workspace. A
    rollback that itself fails marks the target explicitly invalid.
    """
    savepoint = staging / "savepoint"
    staged_files = staging / "files"
    for item in planned:
        dest = target.joinpath(*item.parts)
        if dest.exists():
            original = savepoint.joinpath(*item.parts)
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, original)
    for _rel, pure in planned_deletions:
        doomed = target.joinpath(*pure.parts)
        if doomed.exists():
            original = savepoint.joinpath(*pure.parts)
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(doomed, original)

    for item in planned:
        staged = staged_files.joinpath(*item.parts)
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(blobs[item.digest])
        os.chmod(staged, item.mode)

    applied: list[_PlannedFile] = []
    deleted: list[tuple[str, PurePosixPath]] = []
    try:
        for item in planned:
            os.replace(staged_files.joinpath(*item.parts), target.joinpath(*item.parts))
            applied.append(item)
        for entry in planned_deletions:
            doomed = target.joinpath(*entry[1].parts)
            if doomed.exists():
                doomed.unlink()
            deleted.append(entry)
    except OSError as exc:
        rollback_failures = _rollback_to_savepoint(target, savepoint, applied, deleted)
        reason = (
            f"applying the verified checkpoint failed: {exc} — the savepoint "
            "restored the target to its original state"
            if not rollback_failures
            else (
                f"applying the verified checkpoint failed ({exc}) AND the savepoint "
                f"rollback failed ({'; '.join(rollback_failures)}) — the workspace is "
                "INVALID: a retry must rebuild from the approved base"
            )
        )
        raise _PromotionFailure(reason, target_invalid=bool(rollback_failures)) from exc

    outcomes = [FileRestore(item.rel, "restored", digest=item.digest) for item in planned]
    for rel, pure in planned_deletions:
        outcomes.append(
            FileRestore(
                rel, "deleted" if savepoint.joinpath(*pure.parts).exists() else "already-absent"
            )
        )
    return outcomes


def _rollback_to_savepoint(
    target: Path,
    savepoint: Path,
    applied: list[_PlannedFile],
    deleted: list[tuple[str, PurePosixPath]],
) -> list[str]:
    """Replay the inverse of the applied mutations, newest first.

    Returns the failures that made the rollback incomplete (empty =
    the target is back at its original state, byte for byte).
    """
    failures: list[str] = []
    for rel, pure in reversed(deleted):
        original = savepoint.joinpath(*pure.parts)
        if not original.exists():
            continue  # nothing had been deleted (or never existed)
        try:
            shutil.copy2(original, target.joinpath(*pure.parts))
        except OSError as exc:
            failures.append(f"recreating deleted {rel}: {exc}")
    for item in reversed(applied):
        original = savepoint.joinpath(*item.parts)
        dest = target.joinpath(*item.parts)
        try:
            if original.exists():
                os.replace(original, dest)  # the captured original returns
            else:
                dest.unlink(missing_ok=True)  # the file was NEW — remove it
        except OSError as exc:
            failures.append(f"restoring original {item.rel}: {exc}")
    return failures


def _promotion_failure_outcomes(
    planned: list[_PlannedFile],
    planned_deletions: list[tuple[str, PurePosixPath]],
    reason: str,
) -> tuple[FileRestore, ...]:
    """Per-file evidence for a failed promotion: NOTHING landed."""
    note = f"promotion failed: {reason}"
    outcomes = [
        FileRestore(item.rel, "failed", digest=item.digest, reason=note) for item in planned
    ]
    outcomes.extend(FileRestore(rel, "failed", reason=note) for rel, _pure in planned_deletions)
    return tuple(outcomes)


def restore_wip(
    *,
    artifact_id: str,
    store: ContentAddressedStore,
    target: Path,
    principal: str,
    download: RemoteCheckpointSource | None = None,
    work_id: str = "",
    promote: PromoteMode = "switch",
) -> RestoreReport:
    """Reconstruct the checkpointed WIP into *target* on a second runner.

    The manifest is fetched through the store's VERIFIED read under
    *principal* — an ungranted principal learns nothing (``None``, the
    same as absence) and tampered bytes raise before anything is
    written. Then the restore is TRANSACTIONAL (R28-02/NEXT-04): every
    path, kind, mode, blob and conflict is verified BEFORE the first
    write — escapes, reserved namespaces (``.git``, the checkpoint
    store, the generation pointer, credential-shaped names), symlinked
    ancestors (``lstat``, never followed), duplicate/aliasing normalized
    paths and file-versus-directory conflicts each refuse the WHOLE
    restore with the precise reason (``phase="preflight_failed"`` —
    nothing was written). Only a fully verified plan is promoted, in one
    of two modes (:data:`PromoteMode`):

    - ``promote="switch"`` (default) — the COMPLETE next generation is
      built in a STAGING directory beside the target and activated by
      ONE whole-tree switch: the current generation moves aside and the
      staged one lands, with the move-aside rolled back if the landing
      fails. For callers OUTSIDE the workspace only (R32-01: the switch
      renames the directory a process sitting in the target — and its
      parent shell — is bound to).
    - ``promote="generation"`` — the staged tree lands as a SIBLING
      ``<parent>/.forge-workspace-gen-<checkpoint_id[:12]>`` directory
      and the TARGET IS NEVER RENAMED OR REMOVED. The report carries the
      landed path in ``workspace_generation``, and the target gains the
      ``.forge/workspace-generation`` POINTER naming the active
      generation for the collector step. This is the lane's mode: a
      process restoring into its own ``Path.cwd()`` keeps a valid
      ``os.getcwd()`` and relative writes keep working after success.

    A failure during promotion reports ``phase="promotion_failed"`` with
    the target at its original state (``target_invalid=True`` only when
    even the rollback failed — then the workspace is explicitly unusable
    and a retry rebuilds from the approved base; the generation mode
    never risks the target, so it reports ``target_invalid=False``).
    Several atomic file renames are NEVER treated as an atomic
    multi-file transaction; where the read-only parent forces the
    per-file fallback in switch mode, a full savepoint (the captured
    original of every touched file) is replayed in reverse on any
    failure — the generation mode REFUSES there instead.

    Crash semantics, ownership and filesystem assumptions (R32-02): a
    restore killed mid-promotion never exposes a mixed workspace — it
    leaves either the untouched original, or the promoted path MISSING
    with the original parked under an OWNED
    ``.forge-restore-backup-<work_id>[-<checkpoint[:8]>]-<pid>-<uuid>``
    sibling. The NEXT restore resolves ONLY leftovers whose name carries
    ITS work's id (*work_id* here, falling back to the manifest's) — in
    a shared parent another workspace's backups and staging directories
    are inventoried in ``unrecognized`` and never touched, and a
    refused restore (failed fetch, corrupt or unauthorized manifest)
    resolves NOTHING at all: recovery assets only move once the
    requested checkpoint itself validated. The report's ``recovery``
    names everything it resolved. The promotion assumes the workspace
    holds regular files, directories and symlinks only (a checkout
    tree) and that directory renames are atomic on the target's
    filesystem — both hold on every supported runner.

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
    recovery: tuple[str, ...] = ()
    unrecognized: tuple[str, ...] = ()
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
                phase="preflight_failed",
                recovery=recovery,
            )
    try:
        manifest_bytes = store.get_verified(artifact_id, principal=principal)
    except CorruptArtifactError as exc:
        return RestoreReport(
            artifact_id=artifact_id,
            ok=False,
            files=(),
            failures=(f"manifest {artifact_id} is corrupt: {exc}",),
            phase="preflight_failed",
            recovery=recovery,
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
            phase="preflight_failed",
            recovery=recovery,
        )
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return RestoreReport(
            artifact_id=artifact_id,
            ok=False,
            files=(),
            failures=(f"manifest {artifact_id} is not valid JSON: {exc}",),
            phase="preflight_failed",
            recovery=recovery,
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
            phase="preflight_failed",
            recovery=recovery,
        )

    files = manifest.get("files")
    deletions = manifest.get("deletions")
    if not isinstance(files, dict) or not isinstance(deletions, list):
        return RestoreReport(
            artifact_id=artifact_id,
            ok=False,
            files=(),
            failures=("manifest is malformed: files/deletions sections missing",),
            phase="preflight_failed",
            recovery=recovery,
        )

    # -- R32-02: recover THIS WORK's abandoned promotion — but only now
    # that the requested checkpoint itself validated. A refused restore
    # (failed fetch, corrupt/unauthorized manifest) resolves NOTHING:
    # recovery assets move only for a checkpoint this runner could
    # actually restore, and only the ones the WORK's own name owns.
    owner = (work_id or "").strip() or str(manifest.get("work_id") or "").strip()
    subject = target if promote == "switch" else _generation_dir(target.parent, artifact_id)
    recovery, unrecognized = _recover_abandoned_promotions(
        target, work_id=owner, checkpoint_id=artifact_id, subject=subject
    )

    # -- verify the ENTIRE plan before touching the target (R28-02) ------
    outcomes: list[FileRestore] = []
    failures: list[str] = []
    planned: list[_PlannedFile] = []
    planned_deletions: list[tuple[str, PurePosixPath]] = []
    # Occupancy by NORMALIZED parts — catches path aliases (``src//a``,
    # ``src/./a``) and file-versus-directory conflicts inside the plan.
    occupied: dict[tuple[str, ...], str] = {}
    dir_prefixes: set[tuple[str, ...]] = set()

    def _claim(parts: tuple[str, ...], rel: str) -> str | None:
        if parts in occupied:
            return (
                f"{rel!r} normalizes to the same path as {occupied[parts]!r} — "
                "a manifest may not alias one path twice"
            )
        for index in range(1, len(parts)):
            prefix = parts[:index]
            if prefix in occupied:
                return (
                    f"{rel!r} needs {PurePosixPath(*prefix).as_posix()!r} as a directory, "
                    f"but the plan already places a file there ({occupied[prefix]!r})"
                )
        if parts in dir_prefixes:
            return (
                f"{rel!r} would have to be a directory, but the plan already "
                "places a file underneath it"
            )
        occupied[parts] = rel
        for index in range(1, len(parts)):
            dir_prefixes.add(parts[:index])
        return None

    for rel in sorted(files):
        entry = files[rel]
        if not isinstance(entry, dict):
            outcomes.append(
                FileRestore(str(rel), "failed", reason="manifest entry is not an object")
            )
            failures.append(f"{rel}: manifest entry is not an object")
            continue
        digest = entry.get("digest")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            outcomes.append(
                FileRestore(
                    str(rel), "failed", digest=str(digest), reason="entry carries no content digest"
                )
            )
            failures.append(
                f"{rel}: entry carries no content digest — a bare filename restores nothing"
            )
            continue
        mode = entry.get("mode", _MODE_REGULAR)
        if not isinstance(mode, int) or isinstance(mode, bool) or mode not in _MODES:
            outcomes.append(
                FileRestore(
                    str(rel), "failed", digest=digest, reason=f"unsupported file mode {mode!r}"
                )
            )
            failures.append(
                f"{rel}: unsupported file kind/mode {mode!r} — restore carries regular "
                f"files as {_MODE_REGULAR:o} or {_MODE_EXECUTABLE:o} only"
            )
            continue
        try:
            pure = _safe_manifest_path(str(rel))
        except ValueError as exc:
            outcomes.append(FileRestore(str(rel), "failed", digest=digest, reason=str(exc)))
            failures.append(str(exc))
            continue
        reserved = _reserved_manifest_path(pure)
        if reserved is not None:
            outcomes.append(FileRestore(str(rel), "failed", digest=digest, reason=reserved))
            failures.append(f"{rel}: {reserved}")
            continue
        conflict = _claim(pure.parts, str(rel))
        if conflict is not None:
            outcomes.append(FileRestore(str(rel), "failed", digest=digest, reason=conflict))
            failures.append(conflict)
            continue
        unsafe = _symlink_free_target_path(target, pure, final_is_file=True)
        if unsafe is not None:
            outcomes.append(FileRestore(str(rel), "failed", digest=digest, reason=unsafe))
            failures.append(f"{rel}: {unsafe}")
            continue
        planned.append(_PlannedFile(str(rel), pure.parts, digest, int(mode)))

    for rel in sorted(map(str, deletions)):
        try:
            pure = _safe_manifest_path(rel)
        except ValueError as exc:
            failures.append(str(exc))
            continue
        reserved = _reserved_manifest_path(pure)
        if reserved is not None:
            failures.append(f"{rel}: {reserved}")
            continue
        conflict = _claim(pure.parts, rel)
        if conflict is not None:
            failures.append(conflict)
            continue
        unsafe = _symlink_free_target_path(target, pure, final_is_file=False)
        if unsafe is not None:
            failures.append(f"{rel}: {unsafe}")
            continue
        planned_deletions.append((rel, pure))

    # Every blob is fetched digest-verified BEFORE any write: a missing
    # or corrupt final blob refuses the WHOLE restore (the target stays
    # clean — never a half-restored workspace marked usable).
    blobs: dict[str, bytes] = {}
    for item in planned:
        try:
            blob = store.get_verified(item.digest, principal=principal)
        except CorruptArtifactError as exc:
            outcomes.append(FileRestore(item.rel, "failed", digest=item.digest, reason=str(exc)))
            failures.append(f"{item.rel}: blob {item.digest} is corrupt: {exc}")
            continue
        if blob is None:
            outcomes.append(
                FileRestore(
                    item.rel,
                    "failed",
                    digest=item.digest,
                    reason="required blob absent from the store",
                )
            )
            failures.append(
                f"{item.rel}: required blob {item.digest} is absent — resume is blocked"
            )
            continue
        blobs[item.digest] = blob
    if failures:
        return RestoreReport(
            artifact_id=artifact_id,
            ok=False,
            files=tuple(outcomes),
            failures=tuple(failures),
            phase="preflight_failed",
            recovery=recovery,
            unrecognized=unrecognized,
        )

    # -- promote: ONE whole-generation activation, never a mixed workspace --
    staging, beside_target = _staging_dir(target, owner)
    workspace_generation = ""
    try:
        if promote == "generation" and not beside_target:
            # Typed refusal (R32-01): the generation mode exists so the live
            # target is never rewritten underneath a process sitting in it —
            # the in-target per-file fallback would do exactly that.
            raise _PromotionFailure(
                "the generation promotion requires a staging directory BESIDE "
                "the workspace (a writable parent): the in-target fallback "
                "rewrites the live target in place, which is exactly what the "
                "generation mode refuses",
                target_invalid=False,
            )
        if not beside_target:
            outcomes = _promote_under_savepoint(target, staging, planned, planned_deletions, blobs)
        elif promote == "generation":
            generation = _generation_dir(target.parent, artifact_id)
            outcomes = _promote_to_generation(
                target,
                staging,
                generation,
                planned,
                planned_deletions,
                blobs,
                work_id=owner,
                checkpoint_id=artifact_id,
            )
            workspace_generation = str(generation)
        else:
            outcomes = _promote_whole_tree(
                target,
                staging,
                planned,
                planned_deletions,
                blobs,
                work_id=owner,
                checkpoint_id=artifact_id,
            )
    except _PromotionFailure as exc:
        failures.append(exc.reason)
        return RestoreReport(
            artifact_id=artifact_id,
            ok=False,
            files=_promotion_failure_outcomes(planned, planned_deletions, exc.reason),
            failures=tuple(failures),
            phase="promotion_failed",
            target_invalid=exc.target_invalid,
            recovery=recovery,
            unrecognized=unrecognized,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return RestoreReport(
        artifact_id=artifact_id,
        ok=not failures,
        files=tuple(outcomes),
        failures=tuple(failures),
        phase="completed",
        recovery=recovery,
        workspace_generation=workspace_generation,
        unrecognized=unrecognized,
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
