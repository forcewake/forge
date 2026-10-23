"""The new frozen run specification; legacy numeric-schema bytes stay separate."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import ConfigDict, Field, field_serializer, model_validator

from forge_protocol import ExecutionPolicy

from .budget import BudgetLimits, Resources, partition
from .paths import PathPolicy
from .plan import Plan, ShortText
from .value import Value
from .verdicts import Sha


class VerificationContract(Value):
    """Named required native checks, never a successful aggregate substituted for them."""

    required_checks: Annotated[tuple[ShortText, ...], Field(min_length=1, max_length=128)]

    @model_validator(mode="after")
    def names_are_unique(self) -> Self:
        if len(set(self.required_checks)) != len(self.required_checks):
            raise ValueError("required verification checks must be unique")
        return self


class RunPolicy(Value):
    """Server-resolved policy frozen before research and inherited by the final spec."""

    driver: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")]
    model: ShortText
    target_branch: ShortText
    verification: VerificationContract
    write_policy: PathPolicy
    execution_policy: ExecutionPolicy = ExecutionPolicy()
    limits: BudgetLimits
    closing_reserve: Resources
    planning_allowance: Resources

    @model_validator(mode="after")
    def planning_is_inside_the_total(self) -> Self:
        pools = partition(self.limits, self.closing_reserve)
        if not pools.implementation.covers(self.planning_allowance):
            raise ValueError("planning allowance must fit inside total minus closing reserve")
        return self

    @field_serializer("write_policy")
    def canonical_write_policy(self, policy: PathPolicy) -> dict[str, Any]:
        # Set iteration is process-specific; the approved bytes must not be.
        return {
            "name": policy.name,
            "denied_paths": sorted(policy.denied_paths),
            "denied_prefixes": policy.denied_prefixes,
            "sensitive_paths": sorted(policy.sensitive_paths),
            "denied_globs": policy.denied_globs,
            "permitted_globs": policy.permitted_globs,
            "allowed_globs": policy.allowed_globs,
            "deny_lockfiles": policy.deny_lockfiles,
            "require_special_approval": policy.require_special_approval,
        }


class RunSpec2(RunPolicy):
    model_config = ConfigDict(serialize_by_alias=True, validate_by_name=True)

    schema_version: Literal["forge.run-spec/2"] = Field(default="forge.run-spec/2", alias="schema")
    task_title: ShortText
    task_description: Annotated[str, Field(max_length=131072)] = ""
    plan: Plan
    base_sha: Sha
    revision: Annotated[int, Field(gt=0)] = 1

    @property
    def spec_json(self) -> str:
        return self.canonical_bytes.decode("utf-8")

    @property
    def brief_text(self) -> str:
        task = f"# {self.task_title}\n\n{self.task_description}".rstrip()
        return f"{task}\n\n{self.plan.render()}"
