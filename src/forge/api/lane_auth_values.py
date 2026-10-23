"""Lane identity values and storage protocol, without a run-table dependency."""

import hashlib
from typing import Literal, Protocol

from forge_protocol import Brief, LaneDispatch, RunIdentity
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class LaneAuthError(ValueError):
    """An authentication request was refused without exposing its credentials."""


def identity_of(value: RunIdentity) -> RunIdentity:
    return RunIdentity(
        forge_run_id=value.forge_run_id, attempt=value.attempt, generation=value.generation
    )


class PendingDispatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dispatch: LaneDispatch
    brief: Brief
    expires_at: AwareDatetime
    cancellation_generation: int = Field(strict=True, ge=0)
    repository_id: str | None = None
    github_workflow_ref: str | None = None
    github_caller_ref: str | None = None
    github_workflow_claim: Literal["workflow_ref", "job_workflow_ref"] = "job_workflow_ref"
    gitlab_pipeline_id: str | None = None
    provider_run_id: str | None = None
    provider_attempt: str | None = None
    gitlab_job_id: str | None = None
    nonce_consumed: bool = False

    @property
    def identity(self) -> RunIdentity:
        return identity_of(self.dispatch)


class LaneAuthStore(Protocol):
    async def load(self, identity: RunIdentity) -> PendingDispatch: ...

    async def register(self, pending: PendingDispatch) -> PendingDispatch: ...

    async def claim(
        self, identity: RunIdentity, *, jti_hash: str, expires_at: int, binding: dict[str, str]
    ) -> PendingDispatch: ...


def replay_hash(value: str) -> str:
    return hashlib.sha256(("lane-oidc-jti:" + value).encode()).hexdigest()
