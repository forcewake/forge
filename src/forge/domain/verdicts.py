"""Candidate-bound check decisions and unflattened write outcomes."""

from typing import Annotated, Literal
from datetime import datetime

from pydantic import AwareDatetime, Field, model_validator

from .value import Value

Sha = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
# Evidence is encoded opaque JSON, so nested mutable provider objects cannot alter an outcome.
Evidence = Annotated[str, Field(min_length=1)]


class Committed(Value):
    kind: Literal["committed"] = "committed"
    sha: Sha
    evidence: Evidence


class Conflict(Value):
    kind: Literal["conflict"] = "conflict"
    actual_head: Sha | None
    evidence: Evidence


class Unknown(Value):
    kind: Literal["unknown"] = "unknown"
    evidence: Evidence


CommitOutcome = Committed | Conflict | Unknown


class CheckVerdict(Value):
    status: Literal["pending", "passed", "failed", "unknown", "not_configured", "stale"]
    tested_sha: Sha
    required: tuple[str, ...]
    successful: tuple[str, ...]
    observed_at: AwareDatetime
    evidence: Evidence

    @model_validator(mode="after")
    def positive_proof(self) -> "CheckVerdict":
        for names in (self.required, self.successful):
            if any(not name.strip() for name in names) or len(set(names)) != len(names):
                raise ValueError("check identities must be nonempty and unique")
        if self.status == "passed" and (
            not self.required or not set(self.required) <= set(self.successful)
        ):
            raise ValueError("passed requires every independent required check")
        return self

    def verified_for(
        self, candidate_sha: str, required: tuple[str, ...], *, now: datetime, max_age_seconds: int
    ) -> bool:
        if now.utcoffset() is None or isinstance(max_age_seconds, bool) or max_age_seconds <= 0:
            raise ValueError("verification freshness requires an aware time and positive age limit")
        age = (now - self.observed_at).total_seconds()
        return (
            self.status == "passed"
            and self.tested_sha == candidate_sha
            and set(self.required) == set(required)
            and len(set(required)) == len(required)
            and 0 <= age <= max_age_seconds
        )
