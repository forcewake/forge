"""The lane's vendor-independent execution port."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from forge_protocol import Brief, ModelKeyGrant, Usage

Capability = Literal["interrupt", "steer", "resume_session"]


@dataclass(frozen=True)
class DriverEnv:
    model: str
    model_key: ModelKeyGrant | None = None
    resume_session: str | None = None
    resume_note: str = ""


@dataclass(frozen=True)
class DriverEvent:
    kind: Literal["message", "completed", "failed"]
    text: str = ""
    terminal_reason: str = ""
    usage: Usage | None = None
    resume_session: str | None = None


class Session(Protocol):
    def events(self) -> AsyncIterator[DriverEvent]: ...

    async def interrupt(self) -> None:
        """Return only after the driver can no longer mutate the workspace."""
        ...

    async def steer(self, text: str) -> None: ...


class Driver(Protocol):
    name: str
    capabilities: frozenset[Capability]
    requires_model_key: bool

    async def start(self, brief: Brief, workspace: Path, env: DriverEnv) -> Session: ...
