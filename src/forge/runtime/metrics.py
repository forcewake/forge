"""Scoped metrics report durable observations; absent usage is never a zero price."""

from collections import Counter, defaultdict
from decimal import Decimal, localcontext
import json

from sqlalchemy import JSON, Select, cast, func, or_, select, tuple_, type_coerce
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.budget import Pool
from forge.port_values import EffectKind
from forge.store.models_budget import EngineBudgetPool, EngineUsage
from forge.store.models_commands import EngineCommand
from forge.store.models_execution import EngineEffect
from forge.store.models_runs import EngineRun
from forge.store.models_timing import EngineStepTiming
from forge.store.transactions import SessionFactory

MAX_ROWS = 1000
Scope = tuple[tuple[str, str, str], ...]


def _runs(scope: Scope) -> Select[tuple[str]]:
    return select(EngineRun.id).where(
        tuple_(EngineRun.provider, EngineRun.connection_id, EngineRun.repository_id).in_(scope)
    )


def _commands(session: AsyncSession, scope: Scope) -> Select[tuple[str]]:
    document = (
        type_coerce(EngineCommand.document_json, JSON)
        if session.get_bind().dialect.name == "sqlite"
        else cast(EngineCommand.document_json, JSON)
    )
    identity = document["target"]["repo"]["identity"]
    return select(EngineCommand.id).where(
        tuple_(
            identity["provider"].as_string(),
            identity["connection_id"].as_string(),
            identity["repository_id"].as_string(),
        ).in_(scope)
    )


def _sample(name: str, labels: dict[str, str], value: object) -> str:
    encoded = ",".join(key + "=" + json.dumps(label) for key, label in labels.items())
    return name + ("{" + encoded + "}" if encoded else "") + " " + str(value) + "\n"


class Metrics:
    def __init__(self, factory: SessionFactory, scope: Scope) -> None:
        self.factory, self.scope = factory, scope

    async def render(self) -> str:
        async with self.factory() as session:
            lines = [
                await self.runs(session),
                await self.timings(session),
                await self.effects(session),
                await self.spend(session),
            ]
        return "".join(lines)

    async def runs(self, session: AsyncSession) -> str:
        query = (
            select(EngineRun.provider, EngineRun.connection_id, EngineRun.state, func.count())
            .where(EngineRun.id.in_(_runs(self.scope)))
            .group_by(EngineRun.provider, EngineRun.connection_id, EngineRun.state)
            .order_by(EngineRun.provider, EngineRun.connection_id, EngineRun.state)
        )
        rows = (await session.execute(query)).all()
        return "# TYPE forge_runs gauge\n" + "".join(
            _sample(
                "forge_runs",
                {"provider": provider, "connection": connection, "state": state},
                count,
            )
            for provider, connection, state, count in rows
        )

    async def timings(self, session: AsyncSession) -> str:
        query = (
            select(EngineStepTiming.handler, func.sum(EngineStepTiming.seconds), func.count())
            .where(EngineStepTiming.run_id.in_(_runs(self.scope)))
            .group_by(EngineStepTiming.handler)
            .order_by(EngineStepTiming.handler)
        )
        rows = (await session.execute(query)).all()
        lines = [
            "# HELP forge_step_seconds Observed handler execution intervals; queue time and missing observations are excluded.\n# TYPE forge_step_seconds summary\n"
        ]
        for handler, total, count in rows:
            lines.extend(
                (
                    _sample("forge_step_seconds_sum", {"handler": handler}, total),
                    _sample("forge_step_seconds_count", {"handler": handler}, count),
                )
            )
        return "".join(lines)

    async def effects(self, session: AsyncSession) -> str:
        query = (
            select(EngineEffect)
            .where(
                or_(
                    EngineEffect.run_id.in_(_runs(self.scope)),
                    EngineEffect.command_id.in_(_commands(session, self.scope)),
                )
            )
            .limit(MAX_ROWS + 1)
        )
        rows = await session.stream_scalars(query.execution_options(yield_per=1))
        counts: Counter[tuple[str, str]] = Counter()
        observed = bytes_seen = unresolved = 0
        async for row in rows:
            observed += 1
            bytes_seen += len(row.observations_json)
            if observed > MAX_ROWS or bytes_seen > 8 * 1024 * 1024:
                raise ValueError("metric snapshot exceeds its complete bounded scope")
            unresolved += row.state in ("unknown", "dispatched")
            if (
                row.kind not in {kind.value for kind in EffectKind}
                or len(row.observations_json) > 65536
            ):
                raise ValueError("effect metric observation is outside its closed bound")
            for raw in json.loads(row.observations_json):
                observation = json.loads(raw)
                state = observation["state"]
                if state not in (
                    "committed",
                    "adopted",
                    "conflict",
                    "failed",
                    "unknown",
                    "requested",
                ):
                    raise ValueError("unknown effect metric outcome")
                counts[(row.kind, state)] += 1
        lines = [
            "# HELP forge_effects_total Append-only durable effect outcome observations; adoption preserves earlier unknown observations.\n# TYPE forge_effects_total counter\n"
        ]
        lines.extend(
            _sample("forge_effects_total", {"kind": kind, "outcome": outcome}, count)
            for (kind, outcome), count in sorted(counts.items())
        )
        lines.append("# TYPE forge_unknown_effects gauge\n")
        lines.append(
            _sample(
                "forge_unknown_effects",
                {},
                unresolved,
            )
        )
        return "".join(lines)

    async def spend(self, session: AsyncSession) -> str:
        query = (
            select(EngineBudgetPool)
            .where(EngineBudgetPool.run_id.in_(_runs(self.scope)))
            .limit(MAX_ROWS + 1)
        )
        pools = await session.stream_scalars(query.execution_options(yield_per=1))
        settled: dict[str, Decimal] = defaultdict(Decimal)
        held: dict[str, Decimal] = defaultdict(Decimal)
        unknown: Counter[str] = Counter()
        with localcontext() as context:
            context.prec = 80
            observed = bytes_seen = 0
            async for row in pools:
                observed += 1
                bytes_seen += len(row.state_json)
                if observed > MAX_ROWS or bytes_seen > 8 * 1024 * 1024:
                    raise ValueError("metric snapshot exceeds its complete bounded scope")
                if len(row.state_json) > 1048576:
                    raise ValueError("budget metric observation exceeds its bound")
                pool = Pool.model_validate_json(row.state_json)
                for reservation in pool.reservations:
                    kind = reservation.identity.split(":", 1)[0]
                    if kind not in ("implementation", "planning", "closing"):
                        raise ValueError("budget metric purpose is not canonical")
                    if reservation.status == "settled":
                        assert (
                            reservation.observed is not None
                            and reservation.observed.usd is not None
                        )
                        settled[kind] += reservation.observed.usd
                    else:
                        held[kind] += max(
                            reservation.allowance.usd,
                            reservation.observed.usd
                            if reservation.observed and reservation.observed.usd is not None
                            else Decimal(0),
                        )
                        if reservation.observed is None or reservation.observed.usd is None:
                            unknown[kind] += 1
        unmetered = await session.scalar(
            select(func.count())
            .select_from(EngineUsage)
            .where(
                EngineUsage.run_id.in_(_runs(self.scope)), EngineUsage.completeness == "unmetered"
            )
        )
        lines = [
            "# HELP forge_spend_usd_total Known immutable settled native cost; unobserved or unsettled usage is excluded.\n# TYPE forge_spend_usd_total counter\n"
        ]
        lines.extend(
            _sample("forge_spend_usd_total", {"kind": kind}, value)
            for kind, value in sorted(settled.items())
        )
        lines.append("# TYPE forge_held_usd gauge\n")
        lines.extend(
            _sample("forge_held_usd", {"kind": kind}, value) for kind, value in sorted(held.items())
        )
        lines.append("# TYPE forge_unknown_spend_reservations gauge\n")
        lines.extend(
            _sample("forge_unknown_spend_reservations", {"kind": kind}, value)
            for kind, value in sorted(unknown.items())
        )
        lines.extend(
            (
                "# TYPE forge_unmetered_usage gauge\n",
                _sample("forge_unmetered_usage", {}, unmetered),
            )
        )
        return "".join(lines)
