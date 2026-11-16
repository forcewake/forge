"""Typed lane storage interface and pure submission/authorization rules."""

from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from forge_protocol import CheckpointManifest, ControlCommand, RunIdentity

from forge.api.lane_auth_values import PendingDispatch

T = TypeVar("T")


class LaneConflict(ValueError):
    pass


class LaneNotFound(ValueError):
    pass


class LaneV1Store(Protocol):
    async def access(
        self,
        identity: RunIdentity,
        operation: Callable[[PendingDispatch, dict[str, Any], dict[str, Any]], T],
        *,
        write: bool = False,
    ) -> T: ...
    async def enqueue_control(self, command: ControlCommand) -> ControlCommand: ...
    async def terminal_only_digests(self, limit: int) -> list[str]: ...
    async def forget_terminal_digests(self, digests: list[str]) -> None: ...


def resume_checkpoint(
    pending: PendingDispatch, namespace: dict[str, Any]
) -> CheckpointManifest | None:
    checkpoint_id = pending.dispatch.checkpoint_id
    if checkpoint_id is None:
        return None
    raw = namespace.get("checkpoints", {}).get(checkpoint_id)
    if raw is None:
        raise LaneNotFound("required resume checkpoint is not present")
    manifest = CheckpointManifest.model_validate(raw)
    if (
        manifest.checkpoint_id != checkpoint_id
        or manifest.forge_run_id != pending.identity.forge_run_id
        or manifest.attempt != pending.identity.attempt
        or manifest.generation >= pending.identity.generation
        or manifest.base_sha != pending.dispatch.execution_base_sha
        or manifest.approved_digest != pending.dispatch.approved_digest
        or manifest.driver != pending.dispatch.driver
    ):
        raise LaneConflict("resume checkpoint does not match dispatch")
    return manifest


def blob_size(
    pending: PendingDispatch, state: dict[str, Any], namespace: dict[str, Any], digest: str
) -> int:
    if digest in state.get("blobs", {}):
        size = state["blobs"][digest]
        if type(size) is not int or size < 0:
            raise LaneConflict("blob reference has an invalid size")
        return size
    manifest = resume_checkpoint(pending, namespace)
    for entry in manifest.entries if manifest else ():
        if entry.sha256 == digest:
            return entry.size_bytes
    raise LaneNotFound("blob is not authorized for this dispatch")


def bind_submission(pending: PendingDispatch, value: Any) -> None:
    if (
        value.forge_run_id != pending.identity.forge_run_id
        or value.attempt != pending.identity.attempt
        or value.generation != pending.identity.generation
        or value.approved_digest != pending.dispatch.approved_digest
        or value.driver != pending.dispatch.driver
        or getattr(value, "base_sha", pending.dispatch.execution_base_sha)
        != pending.dispatch.execution_base_sha
        or getattr(value, "model", pending.dispatch.model) != pending.dispatch.model
    ):
        raise LaneConflict("submission does not match the authenticated dispatch")


def accept_once(state: dict[str, Any], key: str, value: dict[str, Any]) -> bool:
    previous = state.get(key)
    if previous is not None:
        if previous != value:
            raise LaneConflict("a different terminal submission is already recorded")
        return False
    state[key] = value
    return True
