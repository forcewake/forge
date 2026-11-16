"""One checked schema, one engine and explicit native capabilities."""

from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from forge.api.lane_auth import LaneAuthService
from forge.api.lane_settings import LaneAuthSettings, OidcAuthority
from forge.api.lane_v1_blobs import LaneBlobStore
from forge.api.lane_v1_service import LaneV1Service
from forge.budget.credentials import CredentialVault
from forge.budget.gateway import LiteLLMGateway
from forge.budget.server_models import ServerModels, server_model_timeout
from forge.budget.service import BudgetService
from forge.budget.settlement import Settlement
from forge.budget.strict import TrustedPolicy
from forge.engine.commands import Commands
from forge.engine.budget_actions import BudgetActions
from forge.engine.effects import Effects
from forge.engine.model_services import ServerPlanners, ServerReviewers
from forge.engine.operator_actions import OperatorActions
from forge.engine.reconciler import Reconciler
from forge.engine.rounds import RoundActions
from forge.engine.runner import StepRunner
from forge.engine.wakeups import Wakeups
from forge.engine.worker import EngineWorker
from forge.ports import Clock
from forge.runtime.actions import InitialActions
from forge.runtime.blobs import Blobs
from forge.store.records import EffectRecord, Lease
from pydantic import SecretStr
from forge.runtime.authority import Operators
from forge.runtime.connections import Connections, assemble_connections
from forge.runtime.handlers import Handlers
from forge.runtime.projection import Projection
from forge.runtime.reactive import ReactiveServices, reactive_services
from forge.runtime.settings import RuntimeSettings, environment, secret
from forge.store.budget_calls import ModelCallStore
from forge.store.budget_commands import BudgetCommands
from forge.store.budgets import BudgetStore
from forge.store.command_effects import CommandEffectStore
from forge.store.command_runs import CommandRuns
from forge.store.commands import CommandStore
from forge.store.controls import ControlStore
from forge.store.effects import EffectStore
from forge.store.gateway_keys import GatewayKeyStore
from forge.store.lane_auth import EngineLaneAuthStore
from forge.store.lane_v1 import EngineLaneV1Store
from forge.store.model_inputs import ModelInputStore
from forge.store.operator import OperatorStore
from forge.store.recovery import RecoveryStore
from forge.store.resume import ResumeStore
from forge.store.retry import RetryStore
from forge.store.rounds import RoundStore
from forge.store.schema import check_schema
from forge.store.steps import StepStore
from forge.store.transactions import SessionFactory


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Runtime:
    config: RuntimeSettings
    engine: AsyncEngine
    factory: SessionFactory
    connections: Connections
    operators: Operators
    projection: Projection
    commands: Commands
    effects: Effects
    budget: BudgetService
    models: ServerModels
    lane: LaneV1Service
    worker: EngineWorker[Effects]
    wakeups: Wakeups
    reactive: ReactiveServices


def database(url: str) -> AsyncEngine:
    from sqlalchemy.engine import make_url

    if make_url(url).drivername not in ("postgresql+asyncpg", "sqlite+aiosqlite"):
        raise ValueError("runtime requires explicit asyncpg or aiosqlite database authority")
    engine = create_async_engine(url, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine.sync_engine, "connect")
        def foreign_keys(connection: Any, _: Any) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


@asynccontextmanager
async def open_runtime(
    config: RuntimeSettings,
    *,
    environ: Mapping[str, str] | None = None,
    native_transport: httpx.AsyncBaseTransport | None = None,
    gateway_transport: httpx.AsyncBaseTransport | None = None,
    clock: Clock | None = None,
) -> AsyncIterator[Runtime]:
    values = environment() if environ is None else environ
    engine = database(secret(config.database_url_env, values).get_secret_value())
    async with AsyncExitStack() as resources:
        resources.push_async_callback(engine.dispose)
        await check_schema(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        current = clock or SystemClock()
        signing = secret(config.secret_key_env, values, minimum=32)
        operators = Operators(config, values)
        effects_store = EffectStore(factory)
        native = assemble_connections(
            config, values, current, effects_store, transport=native_transport
        )
        for close in native.close:
            resources.push_async_callback(close)
        budget, models, effects, keys = budget_services(
            config,
            values,
            factory,
            native,
            current,
            signing,
            resources,
            gateway_transport,
            effects_store,
        )
        auth = lane_auth(config, signing, factory)
        blobs = LaneBlobStore(config.blob_directory)
        lane = LaneV1Service(
            auth,
            EngineLaneV1Store(factory),
            blobs,
            model_key_provider=lambda pending: budget.grant(pending.identity, config.gateway.url),
        )
        planners = ServerPlanners(
            native.registry, models, budget, ModelInputStore(factory), current
        )
        reviewers = ServerReviewers(models, ModelInputStore(factory), current)
        handlers = Handlers(
            config,
            factory,
            native.registry,
            auth,
            budget,
            models,
            planners,
            reviewers,
            Blobs(blobs),
            current,
        )
        wakeups = Wakeups()
        worker = make_worker(config, factory, handlers, effects, current, wakeups)
        commands = command_service(config, factory, native, budget, keys, effects, current)
        yield runtime_value(
            config,
            engine,
            factory,
            native,
            operators,
            commands,
            effects,
            budget,
            models,
            lane,
            worker,
            wakeups,
        )


def lane_auth(
    config: RuntimeSettings, signing: SecretStr, factory: SessionFactory
) -> LaneAuthService:
    settings = LaneAuthSettings(
        secret_key=signing,
        public_url=config.public_url,
        session_ttl_seconds=config.session_ttl_seconds,
        clock_skew_seconds=config.oidc_clock_skew_seconds,
        jwks_cache_seconds=config.oidc_cache_seconds,
        authorities=tuple(
            OidcAuthority(
                connection_id=c.key,
                provider=c.provider,
                issuer=c.oidc_issuer,
                jwks_url=c.oidc_jwks_url,
                tls_ca_path=c.tls_ca_path,
            )
            for c in config.connections
        ),
    )
    return LaneAuthService(EngineLaneAuthStore(factory), settings)


def command_service(
    config: RuntimeSettings,
    factory: SessionFactory,
    native: Connections,
    budget: BudgetService,
    keys: GatewayKeyStore,
    effects: Effects,
    clock: Clock,
) -> Commands:
    runs = CommandRuns(factory)
    capabilities = {
        "claude_code": frozenset(("interrupt", "steer")),
        "copilot_cli": frozenset(("interrupt",)),
    }
    initial = InitialActions(config, native.registry, factory, clock)
    operator = OperatorActions(
        runs,
        RetryStore(factory),
        ControlStore(factory, capabilities),
        ResumeStore(factory),
        native.registry,
        clock,
    )
    rounds = RoundActions(
        RoundStore(factory), native.registry, native.registry, clock, native.registry.actor_for
    )
    actions = {**initial.actions, **operator.actions, **rounds.actions()}
    amendments = BudgetActions(BudgetCommands(factory), effects, CommandEffectStore(factory), clock)
    actions["amend"] = amendments.amend
    return Commands(
        CommandStore(factory),
        runs,
        OperatorStore(factory),
        effects,
        CommandEffectStore(factory),
        keys,
        native.registry.actor_for,
        clock,
        actions=actions,
        finishers={"amend": amendments.finish},
        owner="commands:" + uuid4().hex,
    )


def budget_services(
    config: RuntimeSettings,
    values: Mapping[str, str],
    factory: SessionFactory,
    native: Connections,
    current: Clock,
    signing: SecretStr,
    resources: AsyncExitStack,
    transport: httpx.AsyncBaseTransport | None,
    effects_store: EffectStore,
) -> tuple[BudgetService, ServerModels, Effects, GatewayKeyStore]:
    keys = GatewayKeyStore(
        factory,
        CredentialVault(
            hmac.digest(
                signing.get_secret_value().encode(), b"forge/runtime/gateway-keys/v1", "sha256"
            )
        ),
    )
    policy_raw = config.gateway.policy_path.read_bytes()
    policy = TrustedPolicy.parse(policy_raw)
    configured_models = {
        model
        for connection in config.connections
        for repository in connection.repositories
        for model in (
            repository.policy.model,
            *((repository.native_review.budget.model,) if repository.native_review else ()),
        )
    }
    if not configured_models.issubset(policy.models):
        raise ValueError("every configured model requires an explicit native tariff")
    gateway = LiteLLMGateway(
        config.gateway.url,
        secret(config.gateway.admin_key_env, values),
        keys,
        current,
        transport=transport,
        strict_policy=policy_raw,
        strict_artifact_digest=config.gateway.artifact_digest,
    )
    resources.push_async_callback(gateway.aclose)
    effects = Effects(
        effects_store,
        native.registry,
        native.registry,
        native.registry,
        native.registry,
        native.registry,
        gateway,
        current,
    )
    budgets = BudgetStore(factory)
    budget = BudgetService(
        budgets,
        keys,
        effects,
        Settlement(gateway, budgets, window=config.gateway.settle_seconds),
        current,
        gateway,
    )
    models = ServerModels(
        config.gateway.url,
        keys,
        ModelCallStore(factory),
        gateway,
        policy,
        current,
        transport=transport,
        timeout=server_model_timeout(),
    )
    resources.push_async_callback(models.aclose)
    return budget, models, effects, keys


def runtime_value(
    config: RuntimeSettings,
    engine: AsyncEngine,
    factory: SessionFactory,
    native: Connections,
    operators: Operators,
    commands: Commands,
    effects: Effects,
    budget: BudgetService,
    models: ServerModels,
    lane: LaneV1Service,
    worker: EngineWorker[Effects],
    wakeups: Wakeups,
) -> Runtime:
    return Runtime(
        config,
        engine,
        factory,
        native,
        operators,
        Projection(factory, config),
        commands,
        effects,
        budget,
        models,
        lane,
        worker,
        wakeups,
        reactive_services(config, factory, native.registry, effects, budget.clock),
    )


def make_worker(
    config: RuntimeSettings,
    factory: SessionFactory,
    handlers: Handlers,
    effects: Effects,
    current: Clock,
    wakeups: Wakeups,
) -> EngineWorker[Effects]:
    owner = "worker:" + uuid4().hex

    async def announce_block(lease: Lease, reason: str) -> None:
        from forge.command_values import blocked_comment
        from forge.store.command_runs import CommandRuns
        from forge.store.records import StoreConflict

        try:
            subject = await CommandRuns(factory).subject(lease.run_id)
        except StoreConflict:
            return
        await effects.post_comment(
            lease,
            f"{lease.run_id}:blocked:{reason}"[:300],
            subject,
            blocked_comment(reason),
            f"forge:block:{lease.run_id}",
        )

    runner = StepRunner(
        StepStore(factory),
        handlers.mapping,
        effects=effects,
        clock=current,
        owner=owner,
        lease_seconds=config.lease_seconds,
        on_blocked=announce_block,
    )

    async def recover_effect(effect: EffectRecord) -> EffectRecord:
        return await effects.recover(effect)

    worker = EngineWorker(
        runner,
        Reconciler(effects.store, RecoveryStore(factory), probe=recover_effect, clock=current),
        wakeups,
        concurrency=config.worker_concurrency,
    )
    return worker
