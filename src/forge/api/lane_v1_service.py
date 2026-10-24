"""Lane operations with durable authorization and idempotent submission receipts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from forge_protocol import (
    CandidateBundle,
    CheckpointManifest,
    ControlAck,
    ControlCommand,
    Heartbeat,
    LaneReport,
    ModelKeyGrant,
    RunIdentity,
)

from forge.api.lane_auth import LaneAuthService
from forge.api.lane_auth_values import PendingDispatch, identity_of
from forge.api.lane_model_key_vault import ModelKeyVault, VaultError
from forge.api.lane_v1_blobs import LaneBlobStore
from forge.api.lane_v1_values import (
    LaneConflict,
    LaneNotFound,
    LaneV1Store,
    accept_once,
    bind_submission,
    blob_size,
    resume_checkpoint,
)

ModelKeyProvider = Callable[[PendingDispatch], Awaitable[ModelKeyGrant]]


class ModelKeyUnavailable(ValueError):
    pass


class LaneV1Service:
    def __init__(
        self,
        auth: LaneAuthService,
        store: LaneV1Store,
        blobs: LaneBlobStore,
        *,
        model_key_provider: ModelKeyProvider | None = None,
    ):
        self.auth, self.store, self.blobs = auth, store, blobs
        self.model_key_provider = model_key_provider
        self.vault = ModelKeyVault(auth.model_key_encryption_key())

    @staticmethod
    def body_identity(identity: RunIdentity, body: RunIdentity) -> None:
        if identity != identity_of(body):
            raise LaneConflict("request identity differs from bearer identity")

    async def controls(self, identity: RunIdentity, after: int) -> list[ControlCommand]:
        return await self.store.access(
            identity,
            lambda p, s, n: [
                ControlCommand.model_validate(raw)
                for raw in n["pending_controls"]
                if raw["sequence"] > after
            ],
        )

    async def acknowledge(self, identity: RunIdentity, ack: ControlAck) -> ControlAck:
        self.body_identity(identity, ack)

        def apply(
            pending: PendingDispatch, state: dict[str, Any], namespace: dict[str, Any]
        ) -> ControlAck:
            commands = {raw["command_id"]: raw for raw in state.get("controls", [])}
            command = commands.get(ack.command_id)
            if command is None or command["sequence"] != ack.sequence:
                raise LaneConflict("acknowledgment does not match a dispatched command")
            if ack.checkpoint_id and ack.checkpoint_id not in namespace.get("checkpoints", {}):
                raise LaneConflict("acknowledgment references an unknown checkpoint")
            if ack.checkpoint_id:
                bind_submission(
                    pending,
                    CheckpointManifest.model_validate(namespace["checkpoints"][ack.checkpoint_id]),
                )
            acknowledgments = state.setdefault("acks", {})
            if state.get("report") is not None and ack.command_id not in acknowledgments:
                raise LaneConflict("dispatch already has a terminal report")
            accept_once(acknowledgments, ack.command_id, ack.model_dump(mode="json"))
            return ack

        return await self.store.access(identity, apply, write=True)

    async def record_blob(self, identity: RunIdentity, digest: str, size: int) -> None:
        def record(
            pending: PendingDispatch, state: dict[str, Any], namespace: dict[str, Any]
        ) -> None:
            blobs = state.setdefault("blobs", {})
            if state.get("report") is not None and digest not in blobs:
                raise LaneConflict("dispatch already has a terminal report")
            if digest not in blobs and len(blobs) >= 4096:
                raise LaneConflict("dispatch blob reference limit reached")
            blobs[digest] = size

        await self.store.access(identity, record, write=True)

    async def read_blob(self, identity: RunIdentity, digest: str) -> bytes:
        size = await self.store.access(identity, lambda p, s, n: blob_size(p, s, n, digest))
        body = await self.blobs.read(digest)
        if len(body) != size:
            raise LaneConflict("blob size differs from its authorized reference")
        return body

    async def checkpoint(
        self,
        identity: RunIdentity,
        checkpoint_id: str,
        manifest: CheckpointManifest,
    ) -> bool:
        self.body_identity(identity, manifest)
        if checkpoint_id != manifest.checkpoint_id or len(manifest.entries) > 4096:
            raise LaneConflict("checkpoint digest or entry limit is invalid")
        for entry in manifest.entries:
            if entry.sha256 is not None:
                body = await self.read_blob(identity, entry.sha256)
                if len(body) != entry.size_bytes:
                    raise LaneConflict("checkpoint blob size is incorrect")

        def record(
            pending: PendingDispatch, state: dict[str, Any], namespace: dict[str, Any]
        ) -> bool:
            bind_submission(pending, manifest)
            checkpoints = namespace.setdefault("checkpoints", {})
            if state.get("report") is not None and checkpoint_id not in checkpoints:
                raise LaneConflict("dispatch already has a terminal report")
            if checkpoint_id not in checkpoints and len(checkpoints) >= 256:
                raise LaneConflict("run checkpoint limit reached")
            previous = state.get("latest_checkpoint")
            if previous and previous != checkpoint_id:
                last = CheckpointManifest.model_validate(checkpoints[previous])
                if manifest.sequence <= last.sequence:
                    raise LaneConflict("checkpoint sequence is not increasing")
            first = accept_once(checkpoints, checkpoint_id, manifest.model_dump(mode="json"))
            state["latest_checkpoint"] = checkpoint_id
            return first

        return await self.store.access(identity, record, write=True)

    async def latest_checkpoint(self, identity: RunIdentity) -> CheckpointManifest:
        def latest(
            pending: PendingDispatch, state: dict[str, Any], namespace: dict[str, Any]
        ) -> CheckpointManifest:
            key = state.get("latest_checkpoint")
            if key:
                return CheckpointManifest.model_validate(namespace["checkpoints"][key])
            manifest = resume_checkpoint(pending, namespace)
            if manifest is None:
                raise LaneNotFound("checkpoint is not present")
            return manifest

        return await self.store.access(identity, latest)

    async def candidate(self, identity: RunIdentity, candidate: CandidateBundle) -> bool:
        self.body_identity(identity, candidate)
        if len(await self.read_blob(identity, candidate.diff_sha256)) != candidate.diff_size_bytes:
            raise LaneConflict("candidate diff size is incorrect")

        def record(
            pending: PendingDispatch, state: dict[str, Any], namespace: dict[str, Any]
        ) -> bool:
            bind_submission(pending, candidate)
            report = state.get("report")
            if report is not None and report.get("candidate_diff_sha256") != candidate.diff_sha256:
                raise LaneConflict("terminal report has already closed this dispatch")
            return accept_once(state, "candidate", candidate.model_dump(mode="json"))

        return await self.store.access(identity, record, write=True)

    async def report(self, identity: RunIdentity, report: LaneReport) -> bool:
        self.body_identity(identity, report)

        def record(
            pending: PendingDispatch, state: dict[str, Any], namespace: dict[str, Any]
        ) -> bool:
            bind_submission(pending, report)
            candidate = state.get("candidate")
            if report.candidate_diff_sha256 is not None and (
                candidate is None or candidate["diff_sha256"] != report.candidate_diff_sha256
            ):
                raise LaneConflict("terminal report does not reference the accepted candidate")
            if report.status == "completed" and report.candidate_diff_sha256 is None:
                raise LaneConflict("completed report requires an accepted candidate")
            if report.checkpoint_id is not None and report.checkpoint_id not in namespace.get(
                "checkpoints", {}
            ):
                raise LaneConflict("terminal report references an unknown checkpoint")
            if report.checkpoint_id is not None:
                bind_submission(
                    pending,
                    CheckpointManifest.model_validate(
                        namespace["checkpoints"][report.checkpoint_id]
                    ),
                )
            return accept_once(state, "report", report.model_dump(mode="json"))

        return await self.store.access(identity, record, write=True)

    async def heartbeat(self, identity: RunIdentity, heartbeat: Heartbeat) -> Heartbeat:
        self.body_identity(identity, heartbeat)

        def record(
            pending: PendingDispatch, state: dict[str, Any], namespace: dict[str, Any]
        ) -> Heartbeat:
            previous = state.get("heartbeat")
            raw = heartbeat.model_dump(mode="json")
            if previous is not None and heartbeat.sequence <= previous["sequence"]:
                if raw != previous:
                    raise LaneConflict("heartbeat sequence is stale or conflicting")
                return heartbeat
            if state.get("report") is not None:
                raise LaneConflict("dispatch already has a terminal report")
            state["heartbeat"] = raw
            return heartbeat

        return await self.store.access(identity, record, write=True)

    async def model_key(self, identity: RunIdentity) -> ModelKeyGrant:
        def reserve(
            pending: PendingDispatch, state: dict[str, Any], namespace: dict[str, Any]
        ) -> ModelKeyGrant | PendingDispatch:
            if state.get("report") is not None:
                raise LaneConflict("dispatch is terminal")
            if state.get("model_key_grant") is not None:
                try:
                    grant = self.vault.open(identity, state["model_key_grant"])
                except VaultError:
                    raise ModelKeyUnavailable("cached model grant is unavailable") from None
                if (
                    grant.expires_at <= datetime.now(timezone.utc)
                    or grant.model != pending.dispatch.model
                ):
                    raise ModelKeyUnavailable(
                        "cached model grant is expired or no longer applicable"
                    )
                return grant
            if state.get("model_key_requested"):
                raise LaneConflict("model key outcome is unresolved; redemption remains reserved")
            if self.model_key_provider is None:
                raise ModelKeyUnavailable("model gateway is not configured")
            state["model_key_requested"] = True
            return pending

        pending = await self.store.access(identity, reserve, write=True)
        if isinstance(pending, ModelKeyGrant):
            await self.auth.store.load(identity)
            return pending
        assert self.model_key_provider is not None
        try:
            grant = await self.model_key_provider(pending)
        except Exception:
            raise ModelKeyUnavailable(
                "model gateway outcome is unavailable; redemption remains reserved"
            ) from None
        if (
            identity_of(grant) != identity
            or grant.model != pending.dispatch.model
            or grant.expires_at <= datetime.now(timezone.utc)
        ):
            raise ModelKeyUnavailable("model gateway returned an invalid attempt grant")
        try:
            encrypted = self.vault.seal(identity, grant)
        except VaultError:
            raise ModelKeyUnavailable("model gateway returned an invalid attempt grant") from None

        def cache(
            current: PendingDispatch, state: dict[str, Any], namespace: dict[str, Any]
        ) -> None:
            if state.get("report") is not None or not state.get("model_key_requested"):
                raise LaneConflict("dispatch changed during model key mint")
            state["model_key_grant"] = encrypted

        await self.store.access(identity, cache, write=True)
        # Cancellation during gateway I/O also fences delivery of its credential.
        await self.auth.store.load(identity)
        return grant
