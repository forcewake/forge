"""Immutable exact repository bytes with canonical JSON journal encoding."""

from pydantic import ConfigDict

from .value import Value


class BlobContent(Value):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )
    raw: bytes

    @property
    def size(self) -> int:
        return len(self.raw)
