"""Budget orchestration: reserve, seal, journal gateway writes, then observe."""

from datetime import datetime
import json
from typing import Literal

from forge_protocol import RunIdentity, ModelKeyGrant

from forge.budget.credentials import Purpose, CredentialUnavailable
from forge.budget.admission import StrictGatewayAdmission
from forge.budget.settlement import Settlement
from forge.domain.budget import Resources
from forge.domain.run_spec import RunPolicy
from forge.domain.verdicts import Unknown
from forge.engine.effects import Effects
from forge.port_values import Applied, GatewayKeyRef, GatewayOutcome, Refused
from forge.ports import Clock
from forge.store.budgets import BudgetStore, PoolPurpose
from forge.store.gateway_keys import GatewayKeyStore, KeyRecord
from forge.store.records import Lease, StoreConflict


def alias_for(identity: RunIdentity, purpose: Purpose) -> str:
    core = f"forge:{identity.forge_run_id}:{identity.attempt}:{identity.generation}"
    return core if purpose == "implementation" else f"{core}:{purpose}"


def reservation_for(identity: RunIdentity, purpose: Purpose) -> str:
    return f"{purpose}:{identity.attempt}:{identity.generation}"


class BudgetService:
    def __init__(
        self,
        store: BudgetStore,
        keys: GatewayKeyStore,
        effects: Effects,
        settlement: Settlement,
        clock: Clock,
        admission: StrictGatewayAdmission,
    ) -> None:
        self.store, self.keys, self.effects = store, keys, effects
        self.settlement, self.clock, self.admission = settlement, clock, admission

    async def prepare(
        self,
        lease: Lease,
        spec: RunPolicy,
        purpose: Purpose,
        requested: Resources,
        minimum: Resources,
        expires_at: datetime,
    ) -> GatewayOutcome:
        if requested.usd <= 0 or minimum.usd <= 0:
            return Refused(
                reason="positive_metered_usd_required",
                evidence='{"reason":"positive_metered_usd_required"}',
            )
        if purpose == "implementation" and spec.metering != "metered":
            return Refused(
                reason="unmetered_driver_has_no_gateway_key",
                evidence='{"reason":"unmetered_driver"}',
            )
        refused = await self.admission.readiness(spec.model)
        if refused is not None:
            return refused
        await self.store.initialize(lease, spec, now=self.clock.now())
        identity = RunIdentity(
            forge_run_id=lease.run_id, attempt=lease.attempt, generation=lease.generation
        )
        alias, key = alias_for(identity, purpose), reservation_for(identity, purpose)
        pool: PoolPurpose = "closing" if purpose == "closing" else "implementation"
        for old in await self.keys.previous(lease.run_id, purpose):
            if old.scope.alias != alias and old.state not in ("blocked", "purged"):
                outcome = await self.effects.gw_block(
                    lease, f"{old.scope.alias}:block", old.scope.alias
                )
                if not isinstance(outcome, Applied) or not outcome.value.blocked:
                    return Unknown(evidence='{"reason":"previous_key_not_blocked"}')
                await self.keys.purge(old.scope.alias)
        reservation = await self.store.reserve(
            lease,
            pool,
            key,
            requested,
            minimum,
            planning=purpose == "planning",
            now=self.clock.now(),
        )
        reference = GatewayKeyRef(
            alias=alias,
            model=spec.model,
            allowance=reservation.allowance,
            expires_at=expires_at,
            blocked=False,
        )
        await self.keys.prepare(lease, purpose, reference, now=self.clock.now())
        prior = await self.effects.store.get(f"{alias}:mint")
        original = reservation.allowance
        if prior is not None:
            original = Resources.model_validate_json(
                json.loads(prior.payload_json)["allowance_json"]
            )
        outcome = await self.effects.gw_mint(
            lease, f"{alias}:mint", identity, alias, spec.model, original, expires_at
        )
        latest = await self.keys.get(alias)
        return self._admitted(outcome, latest, identity, spec, expires_at, reservation.allowance)

    @staticmethod
    def _admitted(
        outcome: GatewayOutcome,
        latest: KeyRecord | None,
        identity: RunIdentity,
        spec: RunPolicy,
        expires_at: datetime,
        allowance: Resources,
    ) -> GatewayOutcome:
        alias = alias_for(identity, latest.scope.purpose) if latest is not None else ""
        if isinstance(outcome, Applied):
            if (
                latest is None
                or latest.state != "active"
                or latest.reference.blocked
                or (
                    latest.scope.identity,
                    latest.scope.model,
                    latest.scope.expires_at,
                    latest.reference.allowance,
                    outcome.value.alias,
                    outcome.value.model,
                    outcome.value.expires_at,
                    outcome.value.blocked,
                )
                != (
                    identity,
                    spec.model,
                    expires_at,
                    allowance,
                    alias,
                    spec.model,
                    expires_at,
                    False,
                )
            ):
                return Unknown(evidence='{"reason":"durable_gateway_grant_unavailable"}')
            return Applied(value=latest.reference, evidence='{"reason":"durable_active_grant"}')
        return outcome

    async def finish(
        self, lease: Lease, purpose: Purpose
    ) -> Literal["settled", "accrued", "unknown"]:
        identity = RunIdentity(
            forge_run_id=lease.run_id, attempt=lease.attempt, generation=lease.generation
        )
        alias, key = alias_for(identity, purpose), reservation_for(identity, purpose)
        outcome = await self.effects.gw_block(lease, f"{alias}:block", alias)
        pool: PoolPurpose = "closing" if purpose == "closing" else "implementation"
        if not isinstance(outcome, Applied) or not outcome.value.blocked:
            await self.store.observe(lease.run_id, pool, key, None, final=False)
            return "unknown"
        await self.keys.purge(alias)
        state = await self.settlement.observe(lease.run_id, pool, key, alias)
        if state not in ("settled", "accrued", "unknown"):
            raise StoreConflict("unexpected settlement result")
        return state

    async def amend(
        self,
        lease: Lease,
        purpose: Purpose,
        command: str,
        amount: Resources,
        actor: str,
        reason: str,
    ) -> GatewayOutcome:
        identity = RunIdentity(
            forge_run_id=lease.run_id, attempt=lease.attempt, generation=lease.generation
        )
        alias, key = alias_for(identity, purpose), reservation_for(identity, purpose)
        if purpose == "planning":
            return Refused(
                reason="planning_cap_is_frozen", evidence='{"reason":"planning_cap_is_frozen"}'
            )
        pool: PoolPurpose = "closing" if purpose == "closing" else "implementation"
        target = await self.store.authorize_amendment(
            lease, pool, command, key, amount, actor, reason, now=self.clock.now()
        )
        outcome = await self.effects.gw_update(
            lease, f"{lease.run_id}:amend:{command}", alias, target
        )
        if isinstance(outcome, Applied) and outcome.value.allowance == target:
            await self.store.apply_amendment(lease.run_id, command)
        elif isinstance(outcome, Refused):
            await self.store.refuse_amendment(lease.run_id, command)
        return outcome

    async def grant(self, identity: RunIdentity, gateway_url: str) -> ModelKeyGrant:
        record = await self.keys.get(alias_for(identity, "implementation"))
        if record is None:
            raise CredentialUnavailable("gateway grant is not prepared")
        refused = await self.admission.readiness(record.reference.model)
        if refused is not None:
            raise CredentialUnavailable(refused.reason)
        return await self.keys.grant(identity, gateway_url, now=self.clock.now())

    async def copilot(self, lease: Lease, spec: RunPolicy) -> None:
        if spec.driver != "copilot_cli" or spec.metering != "copilot-scoped-unmetered":
            raise StoreConflict("Copilot requires canonical scoped unmetered authorization")
        await self.store.initialize(lease, spec, now=self.clock.now())
        await self.store.record_unmetered(
            lease,
            f"{lease.run_id}:{lease.attempt}:{lease.generation}:copilot",
            now=self.clock.now(),
        )
