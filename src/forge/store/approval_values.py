"""Immutable approval claims and the narrow atomic step-commit vocabulary."""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from pydantic import Field

from forge.domain.approved_input import ApprovedInput, Digest
from forge.domain.run_spec import RunSpec2
from forge.domain.value import Value
from forge.domain.verdicts import Sha
from forge.port_values import Actor


class ApprovalClaim(Value):
    gate_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    actor: Actor
    approved_digest: Digest
    plan_digest: Digest
    spec_digest: Digest
    base_sha: Sha
    revision: Annotated[int, Field(gt=0)]


@dataclass(frozen=True)
class PreparedPlan:
    run_id: str
    gate_id: str
    spec: RunSpec2
    expires_at: datetime
    state: str

    @property
    def approved(self) -> ApprovedInput:
        return ApprovedInput.from_spec(self.spec)

    @property
    def marker(self) -> str:
        return f"forge:plan:{self.run_id}:{self.spec.digest}"

    @property
    def comment(self) -> str:
        approval = self.approved
        return (
            self.spec.brief_text
            + "\n\n## Implementation\n"
            + f"Gate: `{self.gate_id}`\nExpires: `{self.expires_at.isoformat()}`\n"
            + f"Approved digest: `{approval.approved_digest}`\n"
            + f"Plan digest: `{approval.plan_digest}`\nSpec digest: `{approval.spec_digest}`\n"
            + f"Base SHA: `{approval.base_sha}`\nRevision: `{approval.revision}`\n\n"
            + "```json\n"
            + self.spec.spec_json
            + "\n```"
        )


@dataclass(frozen=True)
class OpenGate:
    gate_id: str
    comment_effect_key: str


@dataclass(frozen=True)
class ConsumeGate:
    claim: ApprovalClaim
    observed_base: Sha | None


ApprovalCommit = OpenGate | ConsumeGate


@dataclass(frozen=True)
class ApprovalDecision:
    accepted: bool
    reason: str | None = None
    block: bool = False
