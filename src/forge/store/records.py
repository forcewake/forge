"""Immutable repository receipts, frozen lease fences and closed vocabularies."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from forge.domain.states import Handler, State

StepState = Literal["scheduled", "running", "succeeded", "failed", "cancelled", "dead"]
EffectState = Literal[
    "requested", "dispatched", "committed", "adopted", "conflict", "failed", "unknown"
]
ObservationState = Literal["committed", "conflict", "failed", "unknown"]
ProbeState = Literal["adopted", "unknown", "requested"]
EFFECT_STATES = frozenset(
    {"requested", "dispatched", "committed", "adopted", "conflict", "failed", "unknown"}
)
KNOWN_OUTCOMES = frozenset({"committed", "adopted", "conflict", "failed"})


class StoreConflict(ValueError):
    """A frozen input, version or durable state disagrees with the request."""


class StaleLease(StoreConflict):
    """The worker no longer owns this run generation and step epoch."""


@dataclass(frozen=True)
class RunRequest:
    id: str
    provider: Literal["github", "gitlab"]
    connection_id: str
    repository_id: str
    subject_kind: Literal["issue", "review"]
    subject_id: str
    root_run_id: str | None = None
    parent_run_id: str | None = None


@dataclass(frozen=True)
class RunRecord:
    id: str
    provider: str
    connection_id: str
    repository_id: str
    subject_kind: str
    subject_id: str
    state: State
    root_run_id: str
    parent_run_id: str | None
    attempt: int
    generation: int
    cancellation_generation: int
    cancel_requested: bool
    version: int
    failed_handler: Handler | None
    reason: str | None


@dataclass(frozen=True)
class StepRequest:
    run_id: str
    handler: Handler
    key: str
    payload_json: str
    due_at: datetime
    max_attempts: int = 3
    deadline_at: datetime | None = None


@dataclass(frozen=True)
class StepRecord:
    id: str
    key: str
    run_id: str
    handler: Handler
    payload_json: str
    state: StepState
    due_at: datetime
    deadline_at: datetime | None
    attempts: int
    max_attempts: int
    lease_epoch: int
    result_json: str | None


@dataclass(frozen=True)
class Lease:
    step_id: str
    run_id: str
    handler: Handler
    epoch: int
    owner: str
    expires_at: datetime
    attempt: int
    generation: int
    cancellation_generation: int


@dataclass(frozen=True)
class EffectRecord:
    id: str
    key: str
    kind: str
    run_id: str | None
    step_id: str | None
    lease_epoch: int
    payload_digest: str
    payload_json: str
    state: EffectState
    version: int
    outcome_json: str | None
    observations: tuple[str, ...]
    repo_key: str | None
    head_branch: str | None
    dispatched_at: datetime | None
    dispatch_deadline_at: datetime | None
    command_id: str | None = None


@dataclass(frozen=True)
class RestartLane:
    reason: str

    def __post_init__(self) -> None:
        if not self.reason or len(self.reason) > 300:
            raise ValueError("lane revival requires a bounded reason")
