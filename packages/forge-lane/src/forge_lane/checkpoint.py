"""Bounded checkpoint capture and verified restore over a fresh checkout."""

import os
from pathlib import Path

from forge_protocol import CheckpointManifest, LaneDispatch, canonical_json

from .client import LaneClient
from .errors import ExitCode, LaneFailure
from .workspace import Workspace


def _confined(root: Path, path: str, removed: frozenset[str] = frozenset()) -> Path:
    target = root / path
    parent = root
    for part in Path(path).parts[:-1]:
        parent /= part
        if parent.relative_to(root).as_posix() in removed:
            break  # This ancestor is unlinked before any new entry is written.
        if parent.is_symlink():
            raise LaneFailure(
                ExitCode.CHECKPOINT, "unsafe_checkpoint", "Checkpoint traverses a symlink"
            )
    return target


class Checkpoints:
    def __init__(self, dispatch: LaneDispatch, client: LaneClient, workspace: Workspace):
        self.dispatch = dispatch
        self.client = client
        self.workspace = workspace
        self.sequence = 0
        self.uploaded: set[str] = set()

    async def capture(self, resume_session: str | None = None) -> CheckpointManifest:
        entries, blobs = await self.workspace.capture()
        manifest = CheckpointManifest(
            **self.client.identity,
            base_sha=self.dispatch.base_sha,
            approved_digest=self.dispatch.approved_digest,
            sequence=self.sequence + 1,
            entries=entries,
            driver=self.dispatch.driver,
            resume_session=resume_session,
        )
        if len(canonical_json(manifest)) > self.workspace.limits.manifest_bytes:
            raise LaneFailure(
                ExitCode.CHECKPOINT,
                "checkpoint_too_large",
                "Checkpoint manifest exceeds byte limit",
            )
        for identity, data in blobs.items():
            if identity not in self.uploaded:
                await self.client.put_blob(data)
                self.uploaded.add(identity)
        await self.client.checkpoint(manifest)
        self.sequence = manifest.sequence
        return manifest

    async def restore(self) -> tuple[str, str | None]:
        manifest = await self.client.latest_checkpoint()
        if (
            manifest.checkpoint_id != self.dispatch.checkpoint_id
            or manifest.forge_run_id != self.dispatch.forge_run_id
            or manifest.attempt != self.dispatch.attempt
            or manifest.generation >= self.dispatch.generation
            or manifest.base_sha != self.dispatch.base_sha
            or manifest.approved_digest != self.dispatch.approved_digest
            or manifest.driver != self.dispatch.driver
        ):
            raise LaneFailure(
                ExitCode.CHECKPOINT,
                "checkpoint_mismatch",
                "Checkpoint is not this attempt's previous generation",
            )
        blobs = await self._verified_blobs(manifest)
        try:
            self._apply(manifest, blobs)
        except (OSError, ValueError):
            raise LaneFailure(
                ExitCode.CHECKPOINT, "restore_failed", "Checkpoint could not be restored"
            ) from None
        self.sequence = manifest.sequence
        self.uploaded.update(blobs)
        note = "Restored work in progress:\n" + "\n".join(
            f"{entry.state}: {entry.path}" for entry in manifest.entries
        )
        return note, manifest.resume_session

    async def _verified_blobs(self, manifest: CheckpointManifest) -> dict[str, bytes]:
        limits = self.workspace.limits
        if (
            len(manifest.entries) > limits.entries
            or len(canonical_json(manifest)) > limits.manifest_bytes
            or sum(entry.size_bytes for entry in manifest.entries) > limits.total_bytes
        ):
            raise LaneFailure(
                ExitCode.CHECKPOINT, "checkpoint_too_large", "Checkpoint exceeds restore limits"
            )
        blobs = {}
        paths = {entry.path for entry in manifest.entries if entry.state != "deleted"}
        removed = frozenset(entry.path for entry in manifest.entries if entry.state == "deleted")
        for entry in manifest.entries:
            _confined(
                self.workspace.path,
                entry.path,
                removed if entry.state != "deleted" else frozenset(),
            )
            if entry.state != "deleted" and any(
                str(parent) in paths for parent in Path(entry.path).parents if str(parent) != "."
            ):
                raise LaneFailure(
                    ExitCode.CHECKPOINT,
                    "unsafe_checkpoint",
                    "Checkpoint file contains another entry",
                )
            if entry.sha256 is not None:
                if entry.sha256 not in blobs:
                    blobs[entry.sha256] = await self.client.blob(
                        entry.sha256, limit=limits.blob_bytes
                    )
                if len(blobs[entry.sha256]) != entry.size_bytes:
                    raise LaneFailure(
                        ExitCode.CHECKPOINT, "corrupt_blob", "Checkpoint blob size differs"
                    )
                if entry.mode == "120000":
                    try:
                        target = blobs[entry.sha256].decode("utf-8", errors="strict")
                    except UnicodeDecodeError:
                        raise LaneFailure(
                            ExitCode.CHECKPOINT, "unsafe_checkpoint", "Invalid symlink target"
                        ) from None
                    if not target or "\x00" in target:
                        raise LaneFailure(
                            ExitCode.CHECKPOINT, "unsafe_checkpoint", "Invalid symlink target"
                        )
        return blobs

    def _apply(self, manifest: CheckpointManifest, blobs: dict[str, bytes]) -> None:
        root = self.workspace.path
        for entry in sorted(manifest.entries, key=lambda item: item.path.count("/"), reverse=True):
            if entry.state == "deleted":
                target = _confined(root, entry.path)
                target.unlink(missing_ok=True)
                parent = target.parent
                while parent != root:
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
        for entry in manifest.entries:
            if entry.sha256 is None:
                continue
            target = _confined(root, entry.path)
            if target.is_symlink() or target.is_file():
                target.unlink()
            if target.is_dir():
                target.rmdir()
            target.parent.mkdir(parents=True, exist_ok=True)
            data = blobs[entry.sha256]
            if entry.mode == "120000":
                target.symlink_to(data.decode("utf-8"))
            else:
                target.write_bytes(data)
                os.chmod(target, 0o755 if entry.mode == "100755" else 0o644)
