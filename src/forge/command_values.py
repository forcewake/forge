"""Bounded commands, one refusal vocabulary and native replay identities."""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal
import json

from forge_protocol import digest, canonical_json
from pydantic import Field, model_validator, field_validator

from forge.domain.plan import ShortText
from forge.domain.value import Value
from forge.ingress_values import Command
from forge.port_values import Actor, SubjectRef, ReviewRef


class CommandEnvelope(Value):
    source: Literal["webhook", "api"]
    native_id: ShortText
    actor: Actor
    target: SubjectRef | ReviewRef
    command: Command
    native_reply: bool = False
    issue_action: Literal["edited", "unlabeled"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def bounded_authority(self) -> "CommandEnvelope":
        if (self.actor.provider, self.actor.connection_id) != (
            self.target.repo.identity.provider,
            self.target.repo.identity.connection_id,
        ):
            raise ValueError("actor and target connection differ")
        if len(self.command.argument) > 32768:
            raise ValueError("command argument exceeds its bound")
        if self.issue_action is not None and (
            self.source != "webhook"
            or self.target.kind != "issue"
            or self.command.argument
            or self.command.kind
            != {"edited": "implement", "unlabeled": "cancel"}[self.issue_action]
        ):
            raise ValueError("issue lifecycle authority requires a signed native issue fact")
        return self

    @property
    def source_key(self) -> str:
        target = self.target
        repo = target.repo.identity
        return digest(
            {
                "source": self.source,
                "native_id": self.native_id,
                "provider": repo.provider,
                "connection": repo.connection_id,
                "repository": repo.repository_id,
                "target": target.kind,
                "number": target.number,
            }
        )


class CommandResult(Value):
    status: Literal["accepted", "observed", "refused"]
    reason: ShortText
    run_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")] | None = None
    data_json: Annotated[str, Field(max_length=131072)] = "{}"

    @field_validator("data_json")
    @classmethod
    def canonical_data(cls, value: str) -> str:
        if canonical_json(json.loads(value)).decode() != value:
            raise ValueError("command result data must be canonical JSON")
        return value


@dataclass(frozen=True)
class CommandLease:
    command_id: str
    epoch: int
    owner: str
    expires_at: datetime


@dataclass(frozen=True)
class CommandRecord:
    id: str
    envelope: CommandEnvelope
    run_id: str | None
    state: str
    result: CommandResult | None


REFUSALS = {
    "actor_not_authorized": "This command requires a current connection approver.",
    "bot_authored": "Bot-authored commands are ignored.",
    "run_not_found": "No matching run exists on this subject.",
    "run_scope_mismatch": "The run belongs to another subject or connection.",
    "malformed_command": "The command argument is invalid.",
    "another_run_active": "Another run is active on this subject.",
    "not_retryable": "Only blocked or failed work can be retried.",
    "cancelled": "An operator revoked this run; start new work with /implement.",
    "no_committed_candidate": "There is no committed work to continue; use /implement.",
    "no_effects": "There is no external effect to reconcile.",
    "branch_drift": "The branch moved; human commits are preserved.",
    "capability_unavailable": "The frozen driver does not support this control.",
    "lane_not_active": "No current lane can receive this control.",
    "checkpoint_unavailable": "No matching named checkpoint is available to resume.",
    "round_already_open": "A correction is already awaiting approval or running on this draft.",
    "revision_out_of_scope": "The proposed path is outside the frozen write authority.",
    "revision_approval_required": "Use /approve-revision on the original Draft for this correction.",
    "review_window_closed": "This draft has no eligible completed candidate to revise.",
}


def render(result: CommandResult) -> str:
    message = REFUSALS.get(result.reason, result.reason.replace("_", " "))
    run = f"\nRun: `{result.run_id}`" if result.run_id is not None else ""
    return message + run + ("\n" + result.data_json if result.status == "observed" else "")
