"""Frozen request and response bodies for /lane/v1."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    model_validator,
)

from .canonical import approved_input_digest, digest

PROTOCOL: Literal["forge.lane/1"] = "forge.lane/1"
Text = Annotated[str, Field(strict=True, min_length=1)]
Identifier = Annotated[str, Field(strict=True, min_length=1, max_length=256, pattern=r"^[\w.-]+$")]
Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
CommitSha = Annotated[str, Field(strict=True, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
Counter = Annotated[int, Field(strict=True, ge=0)]
Positive = Annotated[int, Field(strict=True, ge=1)]


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


UtcTime = Annotated[AwareDatetime, AfterValidator(_utc)]


def _relative_path(value: str) -> str:
    parts = value.split("/")
    if (
        "\\" in value
        or "\x00" in value
        or any(part in ("", ".", "..") for part in parts)
        or any(part.lower() in (".git", ".forge") for part in parts)
        or ":" in parts[0]
    ):
        raise ValueError("path must be relative, confined and outside repository metadata")
    return value


ArtifactPath = Annotated[Text, AfterValidator(_relative_path)]


class WireModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    protocol: Literal["forge.lane/1"] = PROTOCOL


class RunIdentity(WireModel):
    forge_run_id: Identifier
    attempt: Counter
    generation: Counter


class LaneDispatch(RunIdentity):
    server_url: HttpUrl
    provider: Literal["github", "gitlab"]
    repository: Text
    ref: Text
    base_sha: CommitSha
    approved_digest: Digest
    driver: Identifier
    model: Text
    timeout_seconds: Positive
    nonce: Identifier | None = None
    checkpoint_id: Digest | None = None

    @model_validator(mode="after")
    def github_requires_nonce(self) -> Self:
        if self.provider == "github" and self.nonce is None:
            raise ValueError("GitHub dispatch requires an OIDC audience nonce")
        return self


class SessionRequest(RunIdentity):
    provider: Literal["github", "gitlab"]
    oidc_token: Text = Field(repr=False, json_schema_extra={"writeOnly": True})


class SessionToken(RunIdentity):
    access_token: Text = Field(repr=False)
    token_type: Literal["Bearer"] = "Bearer"
    expires_at: UtcTime


class Brief(RunIdentity):
    brief_text: Text
    approved_digest: Digest
    plan_digest: Digest
    spec_digest: Digest
    base_sha: CommitSha
    revision: Positive

    @model_validator(mode="after")
    def verify_approved_input(self) -> Self:
        expected = approved_input_digest(
            brief_text=self.brief_text,
            plan_digest=self.plan_digest,
            spec_digest=self.spec_digest,
            base_sha=self.base_sha,
            revision=self.revision,
        )
        if expected != self.approved_digest:
            raise ValueError("approved input digest mismatch")
        return self


class ControlCommand(RunIdentity):
    command_id: Identifier
    sequence: Positive
    action: Literal["pause", "resume", "steer", "interrupt", "checkpoint"]
    text: Text | None = None

    @model_validator(mode="after")
    def steering_has_input(self) -> Self:
        if self.action == "steer" and (self.text is None or not self.text.strip()):
            raise ValueError("steering requires text")
        if self.action != "steer" and self.text is not None:
            raise ValueError("only steering commands carry text")
        return self


class ControlAck(RunIdentity):
    command_id: Identifier
    sequence: Positive
    status: Literal["applied", "refused"]
    detail: str = ""
    checkpoint_id: Digest | None = None


class CheckpointEntry(WireModel):
    path: ArtifactPath
    mode: Literal["100644", "100755", "120000"]
    sha256: Digest | None
    state: Literal["new", "modified", "deleted"]
    size_bytes: Counter

    @model_validator(mode="after")
    def deletion_has_no_blob(self) -> Self:
        if self.state == "deleted":
            if self.sha256 is not None or self.size_bytes != 0:
                raise ValueError("deleted entries have no blob and zero size")
        elif self.sha256 is None:
            raise ValueError("new and modified entries require a blob digest")
        return self


class CheckpointManifest(RunIdentity):
    base_sha: CommitSha
    approved_digest: Digest
    sequence: Positive
    entries: tuple[CheckpointEntry, ...]
    driver: Identifier
    resume_session: str | None = None

    @model_validator(mode="after")
    def paths_are_unique(self) -> Self:
        paths = [entry.path for entry in self.entries]
        if paths != sorted(paths) or len(set(paths)) != len(paths):
            raise ValueError("checkpoint entries must have unique paths in sorted order")
        return self

    @property
    def checkpoint_id(self) -> str:
        return digest(self)


class Usage(WireModel):
    input_tokens: Counter
    output_tokens: Counter
    cost_usd: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)] | None = None


class CandidateBundle(RunIdentity):
    diff_sha256: Digest
    diff_size_bytes: Positive
    base_sha: CommitSha
    approved_digest: Digest
    driver: Identifier
    model: Text
    terminal_reason: Text
    usage: Usage | None = None

    @property
    def identity(self) -> tuple[str, int, int, str, str, str]:
        return (
            self.forge_run_id,
            self.attempt,
            self.generation,
            self.base_sha,
            self.approved_digest,
            self.diff_sha256,
        )


class LaneReport(RunIdentity):
    approved_digest: Digest
    status: Literal["completed", "failed", "cancelled", "paused", "timed_out"]
    classification: Identifier
    driver: Identifier
    model: Text
    terminal_reason: Text
    candidate_diff_sha256: Digest | None = None
    checkpoint_id: Digest | None = None
    usage: Usage | None = None

    @model_validator(mode="after")
    def pause_is_restorable(self) -> Self:
        if self.status == "paused" and self.checkpoint_id is None:
            raise ValueError("a paused report requires a checkpoint")
        return self


class ModelKeyGrant(RunIdentity):
    gateway_url: HttpUrl
    key: Text = Field(repr=False)
    model: Text
    expires_at: UtcTime


class Heartbeat(RunIdentity):
    sequence: Positive
    observed_at: UtcTime
    phase: Literal["starting", "restoring", "running", "checkpointing", "collecting", "uploading"]
