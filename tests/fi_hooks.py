"""Test-only crash-injection hooks for the ADR-0017 failure-injection suite.

Nothing here runs in production. The injector is armed with ONE checkpoint at
a time and kills worker 1 from inside its own call stack: an
``asyncio.CancelledError`` that no ``finally`` block survives — the shape of a
``kill -9``, where no failure is recorded and no cleanup runs. Hooks fire only
in the victim process's task stacks, so the survivor can pass through the same
code paths freely while converging.
"""

from __future__ import annotations

import asyncio
from typing import Any


class CrashInjector:
    """Fires one hard process death at one armed checkpoint, in the victim only."""

    def __init__(self) -> None:
        self._armed: str | None = None
        self.fired: list[str] = []
        self._victims: tuple[asyncio.Task, ...] = ()

    def arm(self, checkpoint: str) -> None:
        assert self._armed is None, f"checkpoint {checkpoint} armed while {self._armed} pending"
        self._armed = checkpoint

    def watch(self, tasks: list[asyncio.Task]) -> None:
        """The victim 'process': hooks only ever fire inside these tasks."""
        self._victims = tuple(tasks)

    def should_fire(self, checkpoint: str) -> bool:
        """True exactly once, for the armed checkpoint, inside a victim task."""
        if self._armed != checkpoint:
            return False
        task = asyncio.current_task()
        if task is None or task not in self._victims:
            return False
        self._armed = None
        self.fired.append(checkpoint)
        return True

    def crash_now(self) -> None:
        """Kill the victim process: cancel its sibling loops, then die hard.

        Raising CancelledError unwinds the current task without running any
        recovery (``except Exception`` does not catch it); the siblings are
        cancelled so nothing of the process keeps running.
        """
        me = asyncio.current_task()
        for task in self._victims:
            if task is not me and not task.done():
                task.cancel()
        assert me is not None
        me.cancel()
        raise asyncio.CancelledError


class CrashAfterPropose:
    """Implementer stand-in: worker 1 dies AFTER the proposal was computed.

    The result existed only in memory — nothing was persisted — so recovery
    legitimately recomputes it (ADR-0017 §5 allows recompute when the crash
    preceded persistence; the bar is no double external effect).
    """

    def __init__(self, inner: Any, injector: CrashInjector, checkpoint: str) -> None:
        self._inner = inner
        self._injector = injector
        self._checkpoint = checkpoint

    async def propose(self, *args, **kwargs):
        result = await self._inner.propose(*args, **kwargs)
        if self._injector.should_fire(self._checkpoint):
            self._injector.crash_now()
        return result
