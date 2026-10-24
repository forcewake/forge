"""Read-only native review authority is separate from implementation approval."""

from typing import Annotated, Protocol

from pydantic import Field, model_validator

from forge.domain.budget import Resources
from forge.domain.run_spec import RunPolicy
from forge.domain.value import Value
from forge.domain.verdicts import Sha
from forge.port_values import Actor, Digest, ReviewRef, ReviewVerdict


class NativeReviewPolicy(Value):
    budget: RunPolicy
    requested: Resources
    minimum: Resources
    seconds: Annotated[int, Field(gt=0, le=900)] = 120
    max_files: Annotated[int, Field(gt=0, le=128)] = 32
    max_bytes: Annotated[int, Field(gt=0, le=2 * 1024 * 1024)] = 262144
    max_blob_bytes: Annotated[int, Field(gt=0, le=1024 * 1024)] = 131072

    @model_validator(mode="after")
    def bounded_closing_budget(self) -> "NativeReviewPolicy":
        if (
            self.budget.metering != "metered"
            or self.minimum.usd <= 0
            or not self.requested.covers(self.minimum)
            or not self.budget.closing_reserve.covers(self.requested)
        ):
            raise ValueError("native review requires its own bounded metered closing allowance")
        return self


class NativeReviewInput(Value):
    request_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    review: ReviewRef
    base_sha: Sha
    head_sha: Sha
    actor: Actor
    diff_sha256: Digest
    policy_digest: Digest


class ReadOnlyReviewer(Protocol):
    async def review(self, diff: bytes, request: NativeReviewInput) -> ReviewVerdict: ...
