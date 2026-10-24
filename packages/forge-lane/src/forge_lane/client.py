"""One authenticated HTTP protocol; no provider or control-plane imports."""

import asyncio
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from forge_protocol import (
    Brief,
    CandidateBundle,
    CheckpointManifest,
    ControlAck,
    ControlCommand,
    Heartbeat,
    LaneDispatch,
    LaneReport,
    ModelKeyGrant,
    RunIdentity,
    SessionRequest,
    SessionToken,
)

from .errors import ExitCode, LaneFailure

T = TypeVar("T", bound=BaseModel)


def same_identity(left: RunIdentity, right: RunIdentity) -> bool:
    return (left.forge_run_id, left.attempt, left.generation) == (
        right.forge_run_id,
        right.attempt,
        right.generation,
    )


class _SessionPending(LaneFailure):
    pass


class LaneClient:
    def __init__(self, dispatch: LaneDispatch, http: httpx.AsyncClient):
        self.dispatch = dispatch
        self.http = http
        self.token: SessionToken | None = None
        self.prefix = str(dispatch.server_url).rstrip("/") + "/lane/v1"

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        limit = kwargs.pop("response_limit", 2 * 1024 * 1024)
        headers = dict(kwargs.pop("headers", {}))
        if self.token is not None:
            headers["Authorization"] = f"Bearer {self.token.access_token}"
        try:
            async with self.http.stream(
                method, self.prefix + path, headers=headers, follow_redirects=False, **kwargs
            ) as response:
                response.raise_for_status()
                chunks, total = [], 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > limit:
                        raise LaneFailure(
                            ExitCode.TRANSPORT,
                            "response_too_large",
                            "Lane response exceeds byte limit",
                        )
                    chunks.append(chunk)
                return httpx.Response(
                    response.status_code,
                    headers={
                        key: value
                        for key, value in response.headers.items()
                        if key not in ("content-encoding", "content-length")
                    },
                    content=b"".join(chunks),
                    request=response.request,
                )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if method == "POST" and path == "/session" and status == 503:
                raise _SessionPending(
                    ExitCode.TRANSPORT, "session_pending", "Lane dispatch binding is pending"
                ) from None
            code = ExitCode.AUTHENTICATION if status in (401, 403) else ExitCode.TRANSPORT
            kind = "session_refused" if code == ExitCode.AUTHENTICATION else "server_refused"
            raise LaneFailure(
                code, kind, f"Lane server refused {method} {path}: HTTP {status}"
            ) from None
        except httpx.HTTPError:
            raise LaneFailure(
                ExitCode.TRANSPORT, "transport_error", f"Lane request failed: {method} {path}"
            ) from None

    def parse(self, model: type[T], response: httpx.Response) -> T:
        try:
            result = model.model_validate_json(response.content)
        except (ValueError, ValidationError):
            raise LaneFailure(
                ExitCode.TRANSPORT, "protocol_error", "Invalid lane response"
            ) from None
        if isinstance(result, RunIdentity) and not same_identity(result, self.dispatch):
            raise LaneFailure(
                ExitCode.TRANSPORT, "identity_mismatch", "Wrong lane response identity"
            )
        return result

    async def exchange(self, oidc_token: str) -> None:
        request = SessionRequest(
            **self.identity, provider=self.dispatch.provider, oidc_token=oidc_token
        )
        try:
            async with asyncio.timeout(min(60, self.dispatch.timeout_seconds)):
                while True:
                    try:
                        response = await self.request(
                            "POST", "/session", json=request.model_dump(mode="json")
                        )
                        break
                    except _SessionPending:
                        await asyncio.sleep(1)
        except TimeoutError:
            raise LaneFailure(
                ExitCode.TRANSPORT,
                "session_startup_timeout",
                "Lane provider binding did not become available before the startup deadline",
            ) from None
        token = self.parse(SessionToken, response)
        if token.expires_at <= datetime.now(timezone.utc):
            raise LaneFailure(
                ExitCode.AUTHENTICATION, "session_expired", "Lane session has expired"
            )
        self.token = token

    @property
    def identity(self) -> dict[str, Any]:
        return {
            "forge_run_id": self.dispatch.forge_run_id,
            "attempt": self.dispatch.attempt,
            "generation": self.dispatch.generation,
        }

    async def brief(self) -> Brief:
        try:
            brief = self.parse(Brief, await self.request("GET", "/brief"))
        except LaneFailure as exc:
            if exc.classification == "protocol_error":
                raise LaneFailure(
                    ExitCode.APPROVAL, "invalid_brief", "Brief digest or shape is invalid"
                ) from None
            raise
        if (brief.approved_digest, brief.base_sha, brief.repair) != (
            self.dispatch.approved_digest,
            self.dispatch.base_sha,
            self.dispatch.repair,
        ):
            raise LaneFailure(ExitCode.APPROVAL, "approval_mismatch", "Brief differs from dispatch")
        return brief

    async def controls(self, after: int) -> list[ControlCommand]:
        response = await self.request("GET", "/controls", params={"after": after})
        try:
            documents = response.json()
            if not isinstance(documents, list):
                raise ValueError("Expected a command array")
            commands = [ControlCommand.model_validate(item) for item in documents]
        except ValueError:
            raise LaneFailure(
                ExitCode.TRANSPORT, "protocol_error", "Invalid control response"
            ) from None
        if any(not same_identity(command, self.dispatch) for command in commands):
            raise LaneFailure(ExitCode.TRANSPORT, "identity_mismatch", "Wrong control identity")
        return commands

    async def send(self, path: str, value: T, *, method: str = "POST") -> T:
        response = await self.request(method, path, json=value.model_dump(mode="json"))
        result = self.parse(type(value), response)
        if result != value:
            raise LaneFailure(
                ExitCode.TRANSPORT, "echo_mismatch", "Server did not accept exact payload"
            )
        return result

    async def ack(self, value: ControlAck) -> None:
        await self.send(f"/controls/{value.command_id}/ack", value)

    async def put_blob(self, data: bytes) -> str:
        identity = sha256(data).hexdigest()
        response = await self.request(
            "PUT",
            f"/blobs/{identity}",
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        if response.status_code != 204:
            raise LaneFailure(
                ExitCode.TRANSPORT, "protocol_error", "Blob upload was not acknowledged"
            )
        return identity

    async def blob(self, identity: str, *, limit: int) -> bytes:
        response = await self.request("GET", f"/blobs/{identity}", response_limit=limit)
        data = response.content
        if len(data) > limit or sha256(data).hexdigest() != identity:
            raise LaneFailure(
                ExitCode.CHECKPOINT, "corrupt_blob", "Checkpoint blob failed verification"
            )
        return data

    async def latest_checkpoint(self) -> CheckpointManifest:
        response = await self.request("GET", "/checkpoints/latest")
        try:
            return CheckpointManifest.model_validate_json(response.content)
        except ValueError:
            raise LaneFailure(
                ExitCode.CHECKPOINT, "invalid_checkpoint", "Invalid checkpoint manifest"
            ) from None

    async def checkpoint(self, manifest: CheckpointManifest) -> None:
        await self.send(f"/checkpoints/{manifest.checkpoint_id}", manifest, method="PUT")

    async def candidate(self, value: CandidateBundle) -> None:
        await self.send("/candidate", value)

    async def report(self, value: LaneReport) -> None:
        await self.send("/report", value)

    async def heartbeat(self, value: Heartbeat) -> None:
        await self.send("/heartbeat", value)

    async def model_key(self) -> ModelKeyGrant:
        response = await self.request("POST", "/model-key")
        grant = self.parse(ModelKeyGrant, response)
        if grant.expires_at <= datetime.now(timezone.utc) or grant.model != self.dispatch.model:
            raise LaneFailure(
                ExitCode.AUTHENTICATION,
                "invalid_model_key",
                "Model grant is stale or differs from dispatch",
            )
        return grant
