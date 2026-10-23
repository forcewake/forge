"""A blocked key remains fully held until complete stable observations settle."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

from forge.ports import BudgetGateway
from forge.port_values import Applied, Found
from forge.store.budgets import BudgetStore, PoolPurpose


class Settlement:
    def __init__(
        self,
        gateway: BudgetGateway,
        store: BudgetStore,
        *,
        window: float = 30,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not 0 <= window <= 3600:
            raise ValueError("settlement window must be bounded")
        self.gateway, self.store, self.window, self.sleep = gateway, store, window, sleep

    async def observe(
        self, run_id: str, purpose: PoolPurpose, key: str, alias: str
    ) -> Literal["settled", "accrued", "unknown"]:
        try:
            proof = await self.gateway.probe_block(alias)
            if (
                not isinstance(proof, Found)
                or not isinstance(proof.value, Applied)
                or not proof.value.value.blocked
            ):
                raise ValueError("gateway block is not confirmed")
            first = await self.gateway.read_spend(alias)
        except Exception:
            await self.store.observe(run_id, purpose, key, None, final=False)
            return "unknown"
        await self.store.observe(run_id, purpose, key, first, final=False)
        await self.sleep(self.window)
        try:
            second = await self.gateway.read_spend(alias)
        except Exception:
            await self.store.observe(run_id, purpose, key, None, final=False)
            return "unknown"
        pool = await self.store.load(run_id, purpose)
        previous = next(item for item in pool.reservations if item.identity == key).observed
        stable = (
            first == second
            and second.complete
            and (previous is None or second.lower_bound.covers(previous.lower_bound))
        )
        await self.store.observe(run_id, purpose, key, second, final=stable)
        return "settled" if stable else "accrued"
