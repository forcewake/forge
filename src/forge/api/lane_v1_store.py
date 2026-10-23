"""Transactional lane API receipts, controls and manifests on the current run."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import TypeVar, cast

from forge_protocol import CheckpointManifest, ControlCommand, RunIdentity
from sqlalchemy import select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.api.lane_auth_store import (
    PendingDispatch,
    check_current,
    current_conditions,
    identity_of,
)
from forge.durable.models import FlowRun

API_NAMESPACE = "lane_v1_api"
T = TypeVar("T")


class LaneConflict(ValueError):
    pass


class LaneNotFound(ValueError):
    pass


class SqlLaneV1Store:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self.session_factory = session_factory

    async def access(
        self,
        identity: RunIdentity,
        operation: Callable[[PendingDispatch, dict, dict], T],
        *,
        write: bool = False,
    ) -> T:
        identity = identity_of(identity)
        async with self.session_factory() as session, session.begin():
            if write and session.get_bind().dialect.name == "sqlite":
                # SQLite ignores FOR UPDATE; reserve its write transaction first.
                await session.execute(text("BEGIN IMMEDIATE"))
            row = await session.scalar(
                select(FlowRun).where(FlowRun.id == identity.forge_run_id).with_for_update()
            )
            pending = check_current(row, identity)
            assert row is not None
            namespace = copy.deepcopy((row.evidence or {}).get(API_NAMESPACE) or {})
            state = namespace.get("current", {})
            if state.get("identity") != identity.model_dump(mode="json"):
                state = {"identity": identity.model_dump(mode="json")}
                namespace["current"] = state
            result = operation(pending, state, namespace)
            if write:
                evidence = {**(row.evidence or {}), API_NAMESPACE: namespace}
                changed = await session.execute(
                    update(FlowRun)
                    .where(
                        *current_conditions(identity, pending.cancellation_generation),
                        FlowRun.updated_at == row.updated_at,
                    )
                    .values(evidence=evidence)
                    .execution_options(synchronize_session=False)
                )
                if cast(CursorResult, changed).rowcount != 1:
                    raise LaneConflict("run changed during lane mutation")
            return result

    async def enqueue_control(self, command: ControlCommand) -> ControlCommand:
        """Trusted engine composition; the lane cannot enqueue its own commands."""

        def append(pending, state, namespace):
            controls = state.setdefault("controls", [])
            raw = command.model_dump(mode="json")
            for previous in controls:
                if previous["command_id"] == command.command_id:
                    if previous != raw:
                        raise LaneConflict("control id is already registered differently")
                    return command
            if len(controls) >= 256 or command.sequence != len(controls) + 1:
                raise LaneConflict("control sequence must be contiguous and bounded")
            controls.append(raw)
            return command

        return await self.access(command, append, write=True)


def resume_checkpoint(pending: PendingDispatch, namespace: dict) -> CheckpointManifest | None:
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
        or manifest.base_sha != pending.dispatch.base_sha
        or manifest.approved_digest != pending.dispatch.approved_digest
        or manifest.driver != pending.dispatch.driver
    ):
        raise LaneConflict("resume checkpoint does not match dispatch")
    return manifest


def blob_size(pending: PendingDispatch, state: dict, namespace: dict, digest: str) -> int:
    if digest in state.get("blobs", {}):
        return state["blobs"][digest]
    manifest = resume_checkpoint(pending, namespace)
    for entry in manifest.entries if manifest else ():
        if entry.sha256 == digest:
            return entry.size_bytes
    raise LaneNotFound("blob is not authorized for this dispatch")


def bind_submission(pending: PendingDispatch, value) -> None:
    if (
        value.forge_run_id != pending.identity.forge_run_id
        or value.attempt != pending.identity.attempt
        or value.generation != pending.identity.generation
        or value.approved_digest != pending.dispatch.approved_digest
        or value.driver != pending.dispatch.driver
        or getattr(value, "base_sha", pending.dispatch.base_sha) != pending.dispatch.base_sha
        or getattr(value, "model", pending.dispatch.model) != pending.dispatch.model
    ):
        raise LaneConflict("submission does not match the authenticated dispatch")


def accept_once(state: dict, key: str, value: dict) -> bool:
    previous = state.get(key)
    if previous is not None:
        if previous != value:
            raise LaneConflict("a different terminal submission is already recorded")
        return False
    state[key] = value
    return True
