"""Provider-neutral signed event facts; engine authorization happens after parsing."""

from typing import Literal

from forge.domain.value import Value
from forge.domain.verdicts import Sha
from forge.port_values import Actor, RepoRef, ReviewRef, SubjectRef

CommandKind = Literal[
    "implement",
    "go",
    "cancel",
    "retry",
    "status",
    "why_blocked",
    "reconcile",
    "pause",
    "resume",
    "steer",
    "fix",
    "ask",
    "approve_revision",
    "amend",
]


class Command(Value):
    kind: CommandKind
    argument: str = ""


class IngressEvent(Value):
    delivery_key: str
    event: str
    action: str
    repo: RepoRef
    actor: Actor
    target: SubjectRef | ReviewRef | None = None
    commands: tuple[Command, ...] = ()
    issue_action: Literal["edited", "unlabeled"] | None = None
    native_id: str | None = None
    native_attempt: int | None = None
    correlation_id: str | None = None
    head_sha: Sha | None = None
