"""Local work bounds; the server gateway separately enforces the monetary cap."""

from typing import Annotated

from pydantic import Field

from forge.domain.value import Value

Positive = Annotated[int, Field(gt=0)]


class PlanningLimits(Value):
    model_calls: Positive = 4
    tool_calls: Positive = 16
    output_tokens: Positive = 4096
    wallclock_seconds: Positive = 90
    blob_bytes: Positive = 256 * 1024
    total_read_bytes: Positive = 2 * 1024 * 1024
    tool_output_bytes: Annotated[int, Field(ge=256)] = 8192
    prompt_bytes: Positive = 65536
    response_bytes: Positive = 32768
    scan_files: Positive = 32


class ModelTransportError(Exception):
    """The model was not reached. The planning step may retry with backoff."""


class PlanningRefused(Exception):
    """A precise refusal, never a guessed plan after a failed read or model call."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
