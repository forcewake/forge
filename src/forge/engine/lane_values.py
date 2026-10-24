"""Frozen dispatch work and trusted operational bounds for one lane leg."""

from typing import Annotated, Literal

from forge_protocol import RepairContext

from pydantic import AwareDatetime, Field, HttpUrl, field_validator, model_validator

from forge.domain.budget import Resources
from forge.domain.value import Value
from forge.port_values import Digest, LaneHandle


class LaneSettings(Value):
    server_url: HttpUrl
    github_workflow_path: str = ".github/workflows/forge-lane.yml"
    github_workflow_claim: Literal["workflow_ref", "job_workflow_ref"] = "workflow_ref"
    requested: Resources
    minimum: Resources
    discovery_attempts: Annotated[int, Field(ge=1, le=1000)] = 20
    poll_seconds: Annotated[int, Field(ge=1, le=900)] = 30
    auto_revive_limit: Annotated[int, Field(ge=0, le=100)] = 2
    revive_backoff_seconds: Annotated[int, Field(ge=1, le=900)] = 60

    @model_validator(mode="after")
    def positive_admission(self) -> "LaneSettings":
        if self.minimum.usd <= 0 or not self.requested.covers(self.minimum):
            raise ValueError("metered lane admission requires a positive monetary minimum")
        return self

    @field_validator("github_workflow_path")
    @classmethod
    def workflow_path(cls, path: str) -> str:
        parts = path.split("/")
        if (
            len(parts) != 3
            or not path.startswith(".github/workflows/")
            or not path.endswith((".yml", ".yaml"))
            or any(part in ("", ".", "..") for part in parts)
            or any(char in path for char in ("@", "\\", "\x00", "\n", "\r"))
        ):
            raise ValueError("workflow must be a canonical repository workflow path")
        return path


class DispatchInput(Value):
    approved_digest: Digest
    revision: Annotated[int, Field(gt=0)]
    revivals: Annotated[int, Field(ge=0)] = 0
    repair: RepairContext | None = None


class LaneWork(DispatchInput):
    settings: LaneSettings
    expires_at: AwareDatetime


class AwaitLane(LaneWork):
    handle: LaneHandle
