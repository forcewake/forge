"""Provider-neutral native command execution with one durable replay owner."""

from collections.abc import Awaitable, Callable, Mapping
import asyncio
import json
import re

from forge.command_values import CommandEnvelope, CommandLease, CommandResult, REFUSALS
from forge.domain.verdicts import Unknown
from forge.engine.effects import Effects
from forge.port_values import Applied, EffectKind, GatewayKeyRef, RepoRef
from forge.ports import ActorPort, Clock
from forge.store.command_effects import CommandEffectStore
from forge.store.command_runs import CommandRuns
from forge.store.commands import CommandStore
from forge.store.gateway_keys import GatewayKeyStore
from forge.store.operator import OperatorStore
from forge.store.records import RunRecord, StoreConflict

Action = Callable[[CommandLease, CommandEnvelope, RunRecord | None], Awaitable[CommandResult]]
Finisher = Callable[[CommandLease], Awaitable[bool]]
_READONLY = frozenset(("status", "why_blocked"))


def refusal(reason: str, run: RunRecord | None = None) -> CommandResult:
    return CommandResult(status="refused", reason=reason, run_id=run.id if run else None)


class Commands:
    def __init__(
        self,
        store: CommandStore,
        runs: CommandRuns,
        operator: OperatorStore,
        effects: Effects,
        authority: CommandEffectStore,
        keys: GatewayKeyStore,
        actor_for: Callable[[RepoRef], ActorPort],
        clock: Clock,
        *,
        actions: Mapping[str, Action],
        finishers: Mapping[str, Finisher] | None = None,
        owner: str = "commands",
    ) -> None:
        self.store, self.runs, self.operator, self.effects = store, runs, operator, effects
        self.authority, self.keys, self.actor_for, self.clock = authority, keys, actor_for, clock
        self.actions, self.owner = dict(actions), owner
        self.finishers = dict(finishers or {})

    async def submit(self, envelope: CommandEnvelope) -> CommandResult:
        if self.bot_authored(envelope):
            return refusal("bot_authored")
        command = await self.store.accept(envelope, now=self.clock.now())
        if command.state == "completed" and command.result is not None:
            return command.result
        lease = await self.store.claim(
            self.owner, now=self.clock.now(), seconds=120, command_id=command.id
        )
        if lease is None:
            return command.result or CommandResult(status="accepted", reason="command_pending")
        return await self.execute(lease)

    def bot_authored(self, envelope: CommandEnvelope) -> bool:
        bot = self.actor_for(envelope.target.repo).bot_identity()
        if (
            envelope.actor.is_bot
            or envelope.actor.native_id == bot.native_id
            or envelope.actor.login.casefold() == bot.login.casefold()
            or re.search(
                r"<!--\s*forge(?:[:\s]|-->)|(?:^|\n)forge:plan:", envelope.command.argument
            )
        ):
            return True
        return False

    async def tick(self, *, limit: int = 20) -> int:
        if not 1 <= limit <= 100:
            raise ValueError("command batch must be bounded")
        count = 0
        for _ in range(limit):
            lease = await self.store.claim(self.owner, now=self.clock.now(), seconds=120)
            if lease is None:
                break
            await self.execute(lease)
            count += 1
        return count

    async def execute(self, lease: CommandLease) -> CommandResult:
        command = await self.store.load(lease.command_id)
        result = command.result
        if result is None:
            try:
                result = await self.decide(lease, command.envelope)
            except StoreConflict as error:
                code = str(error) if str(error) in REFUSALS else "command_conflict"
                result = refusal(code)
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                if await self.store.release_for_retry(lease, now=self.clock.now()):
                    return CommandResult(
                        status="accepted", reason="command_retry", run_id=command.run_id
                    )
                result = refusal("command_failed")
            await self.store.resolve(lease, result, now=self.clock.now())
        finisher = self.finishers.get(command.envelope.command.kind)
        if finisher is not None and not await finisher(lease):
            return result
        if not await self.cleanup(lease):
            return result
        command = await self.store.load(lease.command_id)
        if command.envelope.native_reply:
            async with self.store.factory() as session:
                from forge.store.models_commands import EngineCommand

                row = await session.get(EngineCommand, command.id)
                if row is None or row.reply_json is None:
                    raise StoreConflict("resolved native command reply is missing")
                payload = row.reply_json
            key = f"command:{command.id}:reply"
            prior = await self.effects.store.get(key)
            if prior is not None and prior.state in ("dispatched", "unknown"):
                await self.effects.recover(prior)
            outcome = await self.effects.command_effect(
                self.authority, lease, EffectKind.POST_COMMENT, key, payload
            )
            if isinstance(outcome, Unknown):
                return result
        await self.store.complete(lease, now=self.clock.now())
        return result

    async def decide(self, lease: CommandLease, envelope: CommandEnvelope) -> CommandResult:
        if self.bot_authored(envelope):
            return refusal("bot_authored")
        kind, argument = envelope.command.kind, envelope.command.argument.strip()
        named = (
            argument.split(" ", 1)[0]
            if kind == "amend"
            else argument
            if kind
            in {"go", "cancel", "retry", "status", "why_blocked", "reconcile", "approve_revision"}
            else ""
        )
        run = await self.runs.locate(envelope, named)
        if kind in _READONLY:
            if run is None:
                return refusal("run_not_found")
            snapshot = await self.runs.snapshot(run)
            facts: dict[str, object] = json.loads(snapshot)
            reason = facts["reason"] or facts["state"]
            if not isinstance(reason, str):
                raise StoreConflict("operator snapshot has no reason or state")
            return CommandResult(
                status="observed",
                reason=reason,
                run_id=run.id,
                data_json=snapshot,
            )
        subject = (
            await self.runs.subject(run.id)
            if run is not None
            else (envelope.target if envelope.target.kind == "issue" else None)
        )
        if subject is None or not await self.actor_for(envelope.target.repo).is_approver(
            envelope.actor, subject
        ):
            return refusal("actor_not_authorized", run)
        if kind == "cancel":
            return (
                refusal("run_not_found")
                if run is None
                else await self.operator.cancel(lease, run.id, now=self.clock.now())
            )
        if kind == "reconcile":
            return refusal("run_not_found") if run is None else await self.reconcile(run)
        action = self.actions.get(kind)
        if action is None:
            return refusal("feature_not_available", run)
        return await action(lease, envelope, run)

    async def cleanup(self, lease: CommandLease) -> bool:
        aliases = await self.operator.cleanup_aliases(lease, now=self.clock.now())
        complete = True
        for alias in aliases:
            key = f"command:{lease.command_id}:block:{alias}"
            prior = await self.effects.store.get(key)
            if prior is not None and prior.state in ("dispatched", "unknown"):
                await self.effects.recover(prior)
            outcome = await self.effects.command_effect(
                self.authority,
                lease,
                EffectKind.GW_BLOCK,
                key,
                json.dumps({"alias": alias}, sort_keys=True, separators=(",", ":")),
            )
            if (
                isinstance(outcome, Applied)
                and isinstance(outcome.value, GatewayKeyRef)
                and outcome.value.blocked
            ):
                await self.keys.purge(alias)
            else:
                complete = False
        return complete

    async def reconcile(self, run: RunRecord) -> CommandResult:
        effects = [row for row in await self.effects.store.unresolved(1000) if row.run_id == run.id]
        if not effects:
            return refusal("no_effects", run)
        observed = []
        for row in effects:
            result = (
                await self.effects.recover(row) if row.state in ("unknown", "dispatched") else row
            )
            observed.append({"key": result.key, "state": result.state})
        return CommandResult(
            status="observed",
            reason="effects_observed",
            run_id=run.id,
            data_json=json.dumps({"effects": observed}, sort_keys=True, separators=(",", ":")),
        )
