"""Pydantic models for the adaptive contracts (review 05868e9 package).

Shapes follow the review's ``contracts/*.example.json`` verbatim — the
proposals become executable models; the examples become tests.
"""

from __future__ import annotations

import re
import warnings
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Every contract redeclares ``schema`` with its own Literal tag — the
# discriminator pattern. Pydantic warns about the shadow; it is the design.
warnings.filterwarnings(
    "ignore", message='Field name "schema"', category=UserWarning, module=__name__
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")


class _Contract(BaseModel):
    """Common rig: strict, no stray fields, a validated schema tag."""

    model_config = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())

    @field_validator("schema", check_fields=False)
    @classmethod
    def _schema_tag(cls, value: str) -> str:
        import typing

        expected = typing.get_args(cls.model_fields["schema"].annotation)  # type: ignore[index]
        if expected and value not in expected:
            raise ValueError(f"schema must be one of {expected}, got {value!r}")
        return value


def _digest64(value: str) -> str:
    if not _HEX64.fullmatch(value or ""):
        raise ValueError("digest must be a lowercase 64-hex sha256")
    return value


def _oid40(value: str) -> str:
    if not _HEX40.fullmatch(value or ""):
        raise ValueError("oid must be a lowercase 40-hex git sha")
    return value


class PathScope(_Contract):
    schema: Literal["forge.proposal.path-scope/1"] = "forge.proposal.path-scope/1"  # type: ignore[assignment]
    repository_id: str = Field(min_length=1)
    paths: list[str] = Field(min_length=1)


class WorkContract(_Contract):
    """WHAT must result and what is authorized — the approved object."""

    schema: Literal["forge.proposal.work-contract/1"] = "forge.proposal.work-contract/1"  # type: ignore[assignment]
    work_id: str = Field(min_length=1)
    contract_revision: int = Field(ge=1)
    objective: str = Field(min_length=1)
    non_goals: list[str] = Field(default_factory=list)
    invariants: list[str] = Field(default_factory=list)
    read_scope: list[PathScope] = Field(min_length=1)
    write_scope: list[PathScope] = Field(default_factory=list)
    allowed_effects: list[str] = Field(min_length=1)
    acceptance: list[dict[str, Any]] = Field(default_factory=list)
    budget: dict[str, Any] = Field(default_factory=dict)
    tactical_revision_policy: str = ""
    required_decision_profile: str = ""

    @field_validator("write_scope")
    @classmethod
    def _write_inside_read(cls, value: list[PathScope], info) -> list[PathScope]:
        read_ids = {scope.repository_id for scope in (info.data.get("read_scope") or [])}
        outside = [s.repository_id for s in value if s.repository_id not in read_ids]
        if outside:
            raise ValueError(f"write scope outside read scope: {sorted(set(outside))}")
        return value


class Snapshot(_Contract):
    schema: Literal["forge.proposal.snapshot/1"] = "forge.proposal.snapshot/1"  # type: ignore[assignment]
    repository_id: str = Field(min_length=1)
    source_oid: str
    resolved_from: str = Field(min_length=1)
    config_digest: str

    @field_validator("source_oid")
    @classmethod
    def _oid(cls, value: str) -> str:
        return _oid40(value)

    @field_validator("config_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _digest64(value)


class SnapshotSet(_Contract):
    """The immutable read-only source set a plan revision binds to."""

    schema: Literal["forge.proposal.snapshot-set/1"] = "forge.proposal.snapshot-set/1"  # type: ignore[assignment]
    snapshot_set_id: str = Field(min_length=1)
    snapshots: list[Snapshot] = Field(min_length=1)

    @field_validator("snapshots")
    @classmethod
    def _unique_repos(cls, value: list[Snapshot]) -> list[Snapshot]:
        ids = [snapshot.repository_id for snapshot in value]
        if len(ids) != len(set(ids)):
            raise ValueError("a repository appears twice in one snapshot set")
        return value


class PlanStep(_Contract):
    schema: Literal["forge.proposal.plan-step/1"] = "forge.proposal.plan-step/1"  # type: ignore[assignment]
    step_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    write_repository_id: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    acceptance_refs: list[str] = Field(default_factory=list)
    impact: list[str] = Field(default_factory=list)

    @field_validator("write_repository_id")
    @classmethod
    def _write_step_or_read(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("write_repository_id must be a name or null, not blank")
        return value


class PlanRevision(_Contract):
    """HOW to get there — replaceable without re-approving the contract."""

    schema: Literal["forge.proposal.plan-revision/1"] = "forge.proposal.plan-revision/1"  # type: ignore[assignment]
    plan_id: str = Field(min_length=1)
    work_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    parent_revision: int | None = None
    work_contract_digest: str
    snapshot_set_digest: str
    summary: str = ""
    steps: list[PlanStep] = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    #: The adaptive-lifecycle linkage: questions this revision leaves open,
    #: prior steps it preserves across a revision, and prior steps it
    #: invalidates (with their preserved WIP routed via ChangeProposal).
    open_question_ids: list[str] = Field(default_factory=list)
    preserved_step_ids: list[str] = Field(default_factory=list)
    invalidated_step_ids: list[str] = Field(default_factory=list)

    @field_validator("work_contract_digest", "snapshot_set_digest")
    @classmethod
    def _digests(cls, value: str) -> str:
        return _digest64(value)

    @field_validator("parent_revision")
    @classmethod
    def _parent_before(cls, value: int | None, info) -> int | None:
        revision = info.data.get("revision")
        if value is not None and revision is not None and value >= revision:
            raise ValueError("parent_revision must precede revision")
        return value

    @field_validator("steps")
    @classmethod
    def _deps_resolve(cls, value: list[PlanStep]) -> list[PlanStep]:
        ids = [step.step_id for step in value]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate step ids")
        known = set(ids)
        for step in value:
            missing = [dep for dep in step.depends_on if dep not in known]
            if missing:
                raise ValueError(f"step {step.step_id} depends on unknown steps {missing}")
            if step.step_id in step.depends_on:
                raise ValueError(f"step {step.step_id} depends on itself")
        return value


class ChangeProposal(_Contract):
    """WHY the plan must change — the material-change human gate."""

    schema: Literal["forge.proposal.change-proposal/1"] = "forge.proposal.change-proposal/1"  # type: ignore[assignment]
    proposal_id: str = Field(min_length=1)
    work_id: str = Field(min_length=1)
    from_revision: int = Field(ge=1)
    classification: Literal[
        "tactical_internal", "material_scope", "material_contract", "material_migration"
    ]
    rationale: str = Field(min_length=1)
    new_evidence: list[str] = Field(default_factory=list)
    proposed_revision_id: str | None = None
    preserves_wip: bool = True


class ControlCommand(_Contract):
    """One durable mailbox record of human control."""

    schema: Literal["forge.proposal.control-command/1"] = "forge.proposal.control-command/1"  # type: ignore[assignment]
    command_id: str = Field(min_length=1)
    work_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    kind: Literal["pause", "resume", "steer", "answer", "amend", "approve-revision"]
    actor_ref: str = Field(min_length=1)
    actor_origin: Literal["server_authenticated_human", "operator_token", "automation_reconciler"]
    idempotency_key: str = Field(min_length=1)
    expected_plan_revision: int | None = None
    expected_execution_epoch: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    #: The state ladder. ``received -> authorized -> applied ->
    #: checkpointed`` is the coarse in-memory run of CTL-04; the durable
    #: mailbox (NXT-12) refines the ``authorized -> applied`` leg into
    #: ``dispatching`` (effect intended, correlation/epoch persisted),
    #: ``vendor_accepted`` and ``outcome_unknown`` (the lost-response
    #: window), so "intended to send" is never misread as "the agent
    #: applied it". ``rejected`` / ``expired`` remain exits, never rungs.
    status: Literal[
        "received",
        "authorized",
        "dispatching",
        "vendor_accepted",
        "outcome_unknown",
        "applied",
        "checkpointed",
        "rejected",
        "expired",
    ]


class CandidateSetMember(_Contract):
    schema: Literal["forge.proposal.candidate-set-member/1"] = (
        "forge.proposal.candidate-set-member/1"  # type: ignore[assignment]
    )
    repository_id: str = Field(min_length=1)
    base_oid: str
    candidate_oid: str
    image_digest: str = Field(min_length=1)
    role: Literal["changed", "baseline"]


class CandidateSet(_Contract):
    """The unit of system verification — a result binds to THIS set."""

    schema: Literal["forge.proposal.candidate-set/1"] = "forge.proposal.candidate-set/1"  # type: ignore[assignment]
    work_id: str = Field(min_length=1)
    plan_revision: int = Field(ge=1)
    work_contract_digest: str
    members: list[CandidateSetMember] = Field(min_length=1)
    contract_bundle_digest: str | None = None
    test_bundle_digest: str | None = None
    environment_profile_digest: str | None = None

    @field_validator("work_contract_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _digest64(value)

    @field_validator("members")
    @classmethod
    def _unique_repos(cls, value: list[CandidateSetMember]) -> list[CandidateSetMember]:
        ids = [member.repository_id for member in value]
        if len(ids) != len(set(ids)):
            raise ValueError("a repository appears twice in one candidate set")
        return value


class Checkpoint(_Contract):
    """The portable execution checkpoint (FND-05 substrate)."""

    schema: Literal["forge.proposal.checkpoint/1"] = "forge.proposal.checkpoint/1"  # type: ignore[assignment]
    checkpoint_id: str = Field(min_length=1)
    work_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    execution_epoch: int = Field(ge=1)
    plan_revision: int = Field(ge=1)
    snapshot_set_digest: str
    work_contract_digest: str
    wip_artifact_id: str | None = None
    wip_digest: str | None = None
    last_applied_command_sequence: int = 0
    native_session_artifact_id: str | None = None
    profile_digest: str | None = None
    outstanding_effect_ids: list[str] = Field(default_factory=list)
    state: Literal["complete", "partial", "corrupt"]

    @field_validator("snapshot_set_digest", "work_contract_digest")
    @classmethod
    def _digests(cls, value: str) -> str:
        return _digest64(value)
