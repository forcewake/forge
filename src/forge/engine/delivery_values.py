"""Frozen delivery work and bounded independent verification policy."""

from typing import Annotated

from pydantic import AwareDatetime, Field

from forge.domain.budget import Resources
from forge.domain.value import Value
from forge.domain.verdicts import Sha
from forge.port_values import ReviewRef


class DeliverySettings(Value):
    poll_seconds: Annotated[int, Field(gt=0, le=900)] = 30
    verification_seconds: Annotated[int, Field(gt=0, le=86400)] = 3600
    missing_checks_grace_seconds: Annotated[int, Field(ge=0, le=3600)] = 120
    check_max_age_seconds: Annotated[int, Field(gt=0, le=86400)] = 3600
    repair_limit: Annotated[int, Field(ge=0, le=100)] = 2
    closing_seconds: Annotated[int, Field(gt=0, le=3600)] = 300
    repair_minimum: Resources
    closing_minimum: Resources


class DeliveryWork(Value):
    candidate_sha: Sha
    review: ReviewRef
    published_at: AwareDatetime
    settings: DeliverySettings


class ClosingWork(DeliveryWork):
    expires_at: AwareDatetime
