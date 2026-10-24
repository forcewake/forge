"""Authorize once, then recover the same command-owned gateway update without model work."""

from forge.budget.command_values import parse_amendment
from forge.command_values import CommandEnvelope, CommandLease, CommandResult
from forge.engine.effects import Effects
from forge.port_values import Applied, Refused, EffectKind
from forge.ports import Clock
from forge.store.budget_command_values import effect_key
from forge.store.budget_commands import BudgetCommands
from forge.store.command_effects import CommandEffectStore
from forge.store.records import RunRecord, StaleLease, StoreConflict


class BudgetActions:
    def __init__(
        self, store: BudgetCommands, effects: Effects, authority: CommandEffectStore, clock: Clock
    ) -> None:
        self.store, self.effects, self.authority, self.clock = store, effects, authority, clock

    async def amend(
        self, lease: CommandLease, envelope: CommandEnvelope, run: RunRecord | None
    ) -> CommandResult:
        try:
            request = parse_amendment(envelope.command.argument)
            return await self.store.authorize(lease, request, now=self.clock.now())
        except StoreConflict:
            raise
        except ValueError:
            return CommandResult(status="refused", reason="malformed_command")

    async def finish(self, lease: CommandLease) -> bool:
        payload = await self.store.pending(lease, now=self.clock.now())
        if payload is None:
            return True
        key = effect_key(lease.command_id)
        prior = await self.effects.store.get(key)
        if prior is not None and prior.state in ("unknown", "dispatched"):
            await self.effects.recover(prior)
        try:
            outcome = await self.effects.command_effect(
                self.authority, lease, EffectKind.GW_UPDATE, key, payload
            )
            if isinstance(outcome, Applied):
                return await self.store.apply(lease, payload, now=self.clock.now())
            if isinstance(outcome, Refused):
                return await self.store.refuse(lease, payload, now=self.clock.now())
        except StaleLease:
            raise
        except StoreConflict:
            return await self.store.pending(lease, now=self.clock.now()) is None
        return False
