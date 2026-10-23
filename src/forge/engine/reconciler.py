"""Bounded journal recovery and polling hooks; effect probes own their evidence."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from forge.ports import Clock
from forge.store.effects import EffectStore
from forge.store.records import EffectRecord, StoreConflict
from forge.store.recovery import RecoveryStore


class ProbeRecovery(Protocol):
    """Read-only effects-layer recovery; never dispatches a provider write."""

    async def __call__(self, effect: EffectRecord) -> EffectRecord: ...


@dataclass(frozen=True)
class ReconcilePass:
    scanned: int
    recovered: int
    requeued: int
    conflicts: int
    unavailable: int
    polled: int


class Reconciler:
    def __init__(
        self,
        effects: EffectStore,
        recovery: RecoveryStore,
        *,
        probe: ProbeRecovery,
        clock: Clock,
        pollers: tuple[Callable[[], Awaitable[None]], ...] = (),
    ) -> None:
        if len(pollers) > 16:
            raise ValueError("too many polling hooks")
        self.effects, self.recovery, self.probe, self.clock = effects, recovery, probe, clock
        self.pollers = tuple(pollers)

    async def pass_once(self, *, limit: int = 100) -> ReconcilePass:
        records = await self.effects.unresolved(limit)
        recovered = requeued = conflicts = unavailable = polled = 0
        for record in records:
            try:
                resolved = (
                    await self.probe(record)
                    if record.state in {"dispatched", "unknown"}
                    else record
                )
                if resolved.id != record.id or resolved.key != record.key:
                    raise StoreConflict("recovery returned another effect identity")
                recovered += resolved.state in {"adopted", "committed", "conflict", "failed"}
                if resolved.state == "requested":
                    requeued += await self.recovery.requeue_original(resolved, now=self.clock.now())
            except StoreConflict:
                conflicts += 1
            except Exception:
                unavailable += 1
        for poller in self.pollers:
            try:
                await poller()
                polled += 1
            except Exception:
                unavailable += 1
        return ReconcilePass(len(records), recovered, requeued, conflicts, unavailable, polled)
