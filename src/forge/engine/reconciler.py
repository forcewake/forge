"""Bounded journal recovery and polling hooks; effect probes own their evidence."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from forge.engine.effects import probe_due
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
    next_delay: float | None = None


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
        deferred: list[float] = []
        now = self.clock.now()
        for record in records:
            if (
                record.kind in {"gw_mint", "gw_update", "gw_block"}
                and record.state in {"dispatched", "unknown"}
                and await self.recovery.terminal_run(record.run_id)
            ):
                continue
            if record.state in {"dispatched", "unknown"} and not probe_due(record, now):
                evidence = probe_due_delay(record, now)
                if evidence is not None:
                    deferred.append(evidence)
                continue
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
        return ReconcilePass(
            len(records),
            recovered,
            requeued,
            conflicts,
            unavailable,
            polled,
            next_delay=min(deferred) if deferred else None,
        )


def probe_due_delay(record: EffectRecord, now: object) -> float | None:
    from datetime import datetime
    import json

    if not isinstance(now, datetime) or record.outcome_json is None:
        return None
    try:
        body = json.loads(record.outcome_json)
        evidence = body.get("evidence")
        if isinstance(evidence, str):
            evidence = json.loads(evidence)
        stamp = evidence.get("next_probe_at") if isinstance(evidence, dict) else None
        if not isinstance(stamp, str):
            return None
        return max(0.0, (datetime.fromisoformat(stamp) - now).total_seconds())
    except (ValueError, TypeError):
        return None
