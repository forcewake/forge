"""A trusted gateway adapter must prove strict admission before a credential is issued."""

from typing import Protocol

from forge.port_values import Refused


class StrictGatewayAdmission(Protocol):
    async def readiness(self, model: str) -> Refused | None:
        """None is a verified adapter result, never an operator flag or remote claim."""
        ...
