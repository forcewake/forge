"""Frozen admission inputs; the final specification adds the actual planner result."""

from typing import Annotated

from pydantic import Field, model_validator
from forge_protocol import canonical_json

from forge.domain.plan import Plan
from forge.domain.run_spec import RunPolicy, RunSpec2
from forge.domain.value import Value
from forge.port_values import Actor, PlanningSnapshot, TaskSnapshot


class PlanIntent(Value):
    actor: Actor
    revision: Annotated[int, Field(gt=0)] = 1
    task: TaskSnapshot
    snapshot: PlanningSnapshot
    policy: RunPolicy
    max_active: Annotated[int, Field(gt=0, le=1000)] = 4
    gate_ttl_seconds: Annotated[int, Field(gt=0, le=86400)] = 3600

    @model_validator(mode="after")
    def repository_is_bound(self) -> "PlanIntent":
        if self.task.subject.repo.identity != self.snapshot.primary.repo.identity:
            raise ValueError("task and primary snapshot repository disagree")
        return self

    def freeze(self, plan: Plan) -> RunSpec2:
        document = self.policy.model_dump(mode="json")
        document.update(
            task_title=self.task.title,
            task_description=self.task.description,
            plan=plan.model_dump(mode="json"),
            base_sha=self.snapshot.primary.sha,
            revision=self.revision,
        )
        return RunSpec2.model_validate_json(canonical_json(document))
