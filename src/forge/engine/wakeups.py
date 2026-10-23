"""PostgreSQL wake-ups are hints; a bounded poll always repairs missed notices."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
import logging
from typing import Protocol

from sqlalchemy.engine import make_url

logger = logging.getLogger(__name__)
CHANNEL = "forge_engine_steps"


class ListenerConnection(Protocol):
    async def add_listener(self, channel: str, callback: Callable[..., None]) -> None: ...
    async def close(self) -> None: ...


class Wakeups:
    def __init__(self, poll_seconds: float = 1) -> None:
        if not 0 < poll_seconds <= 60:
            raise ValueError("poll interval must be bounded")
        self.poll_seconds = poll_seconds
        self._event = asyncio.Event()

    def signal(self) -> None:
        self._event.set()

    async def wait(self) -> None:
        try:
            await asyncio.wait_for(self._event.wait(), timeout=self.poll_seconds)
        except TimeoutError:
            pass
        finally:
            self._event.clear()

    def _notice(self, *args: object) -> None:
        self.signal()

    @asynccontextmanager
    async def listen(self, database_url: str) -> AsyncIterator["Wakeups"]:
        connection: ListenerConnection | None = None
        url = make_url(database_url)
        if url.drivername.startswith("postgresql"):
            try:
                import asyncpg  # type: ignore[import-untyped]

                connection = await asyncpg.connect(
                    url.set(drivername="postgresql").render_as_string(hide_password=False),
                    timeout=5,
                )
                await connection.add_listener(CHANNEL, self._notice)
            except Exception:
                logger.warning("engine listener unavailable; polling remains active")
                if connection is not None:
                    await connection.close()
                connection = None
        try:
            yield self
        finally:
            if connection is not None:
                await connection.close()
