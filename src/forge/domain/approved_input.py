"""One six-field approval contract shared by the engine and CI lane."""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import Field, model_validator

from forge_protocol import Brief, RunIdentity, approved_input_digest

from .run_spec import RunSpec2
from .value import Value
from .verdicts import Sha

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ApprovedInput(Value):
    brief_text: Annotated[str, Field(min_length=1, max_length=1024 * 1024)]
    approved_digest: Digest
    plan_digest: Digest
    spec_digest: Digest
    base_sha: Sha
    revision: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        if (
            approved_input_digest(
                brief_text=self.brief_text,
                plan_digest=self.plan_digest,
                spec_digest=self.spec_digest,
                base_sha=self.base_sha,
                revision=self.revision,
            )
            != self.approved_digest
        ):
            raise ValueError("approved input digest mismatch")
        return self

    @classmethod
    def from_spec(cls, spec: RunSpec2) -> Self:
        approval = approved_input_digest(
            brief_text=spec.brief_text,
            plan_digest=spec.plan.digest,
            spec_digest=spec.digest,
            base_sha=spec.base_sha,
            revision=spec.revision,
        )
        return cls(
            brief_text=spec.brief_text,
            approved_digest=approval,
            plan_digest=spec.plan.digest,
            spec_digest=spec.digest,
            base_sha=spec.base_sha,
            revision=spec.revision,
        )

    def to_brief(self, identity: RunIdentity, spec: RunSpec2) -> Brief:
        if self != type(self).from_spec(spec):
            raise ValueError("approved input does not match this frozen specification")
        return Brief(**identity.model_dump(), **self.model_dump(), spec_json=spec.spec_json)
