"""Closed immutable values with the protocol's canonical content identity."""

from pydantic import BaseModel, ConfigDict

from forge_protocol import canonical_json, digest


class Value(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid", validate_default=True)

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json(self)

    @property
    def digest(self) -> str:
        return digest(self)
