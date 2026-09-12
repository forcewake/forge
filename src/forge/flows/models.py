from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


@dataclass
class FlowTrigger:
    """How a flow is triggered."""

    command: str  # e.g., "@forge /implement"
    target: str  # "issue" or "merge_request"
    auto: bool = False
    events: list[str] = field(default_factory=list)


@dataclass
class FlowStep:
    """An agent-based step in a flow."""

    name: str
    agent: str  # Agent name from registry
    action: str | None = None  # Optional action override
    input_from: str | None = None  # Previous step name for input
    output_key: str | None = None  # Key to store output in flow state
    condition: str | None = None  # Condition expression
    on_failure: str = "abort"  # "abort", "skip", "retry"
    timeout: int = 300


@dataclass
class FlowAction:
    """A GitLab API action performed between agent steps."""

    name: str
    action: str  # "create_branch", "open_merge_request", "post_comment", "add_label"
    params: dict = field(default_factory=dict)
    condition: str | None = None
    output_key: str | None = None


@dataclass
class FlowDefinition:
    """A complete flow definition loaded from YAML."""

    name: str
    version: str
    description: str
    trigger: FlowTrigger
    steps: list[FlowStep | FlowAction]
    timeout: int = 1800  # 30 minutes default


@dataclass
class FlowInstance:
    """A running instance of a flow, persisted in Redis."""

    id: str  # UUID
    flow_name: str
    project_id: int
    trigger_event: dict  # Serialized trigger event
    current_step: int
    state: dict  # Accumulated state from step outputs
    status: str  # "running", "completed", "failed", "aborted"
    started_at: str
    updated_at: str
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict) -> FlowInstance:
        return cls(**data)

    @classmethod
    def from_json(cls, raw: str) -> FlowInstance:
        return cls.from_dict(json.loads(raw))
