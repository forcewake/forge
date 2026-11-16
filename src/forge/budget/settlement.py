"""A blocked key remains fully held until complete stable observations settle."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

from forge.domain.budget import Observation
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
        pool = await self.store.load(run_id, purpose)
        current = next((item for item in pool.reservations if item.identity == key), None)
        if current is not None and current.status == "settled":
            return "settled"
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
        # USD is the enforced axis. Calls and tokens stay unknown when the
        # gateway only meters money; a stable USD reading is enough to settle.
        stable = (
            first == second
            and second.usd is not None
            and (previous is None or second.lower_bound.covers(previous.lower_bound))
        )
        observed = second
        if stable and not second.complete:
            observed = Observation(
                usd=second.usd,
                calls=0 if second.calls is None else second.calls,
                tokens=0 if second.tokens is None else second.tokens,
            )
        await self.store.observe(run_id, purpose, key, observed, final=stable)
        return "settled" if stable else "accrued"
