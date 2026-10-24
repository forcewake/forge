"""Explicit bounded operator increments never reinterpret one budget axis as another."""

from decimal import Decimal
import re
from typing import Annotated, Literal

from pydantic import Field, model_validator

from forge.domain.budget import Resources
from forge.domain.value import Value


class AmendmentRequest(Value):
    run_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    purpose: Literal["implementation", "closing"]
    amount: Resources
    reason: Annotated[str, Field(min_length=1, max_length=300)]

    @model_validator(mode="after")
    def explicit_increment(self) -> "AmendmentRequest":
        if self.amount == Resources() or not self.reason.strip():
            raise ValueError("amendment requires a positive explicit increment and reason")
        return self


def parse_amendment(argument: str) -> AmendmentRequest:
    if len(argument) > 1024:
        raise ValueError("bounded amendment argument required")
    fields, separator, reason = argument.partition(" reason=")
    match = re.fullmatch(
        r"([0-9a-f]{32}) (implementation|closing) usd=((?:0|[1-9][0-9]{0,19})(?:\.[0-9]{1,18})?) calls=(0|[1-9][0-9]{0,18}) tokens=(0|[1-9][0-9]{0,18})",
        fields,
    )
    if not separator or match is None:
        raise ValueError("explicit run, purpose, USD, calls, tokens and reason required")
    return AmendmentRequest.model_validate(
        {
            "run_id": match[1],
            "purpose": match[2],
            "amount": Resources(usd=Decimal(match[3]), calls=int(match[4]), tokens=int(match[5])),
            "reason": reason.strip(),
        }
    )
