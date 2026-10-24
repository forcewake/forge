"""Current accounting operations shared by retained invariant lesson tests."""

from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, select

from forge.domain.approved_input import ApprovedInput
from forge.domain.budget import Observation, Pool, Resources
from forge.store.models_budget import EngineUsage
from forge.store.schema import baseline
from forge.store.schema.migration import script
from forge_lane.driver import DriverEnv
from forge_lane.drivers.claude_code import ClaudeCodeDriver
from forge_protocol import RunIdentity
from tests.test_domain_approved_input import spec
from tests.test_lane_claude_code import FakeSDK, Result, grant


def resources(calls=4, tokens=1000, usd="2"):
    return Resources(usd=Decimal(usd), calls=calls, tokens=tokens)


def pool(calls=4, tokens=1000, usd="2"):
    return Pool(identity="run:implementation", capacity=resources(calls, tokens, usd))


def observation(calls=1, tokens=100, usd="0.1"):
    return Observation(usd=None if usd is None else Decimal(usd), calls=calls, tokens=tokens)


async def initialize(case):
    await case["store"].initialize(case["lease"], case["frozen"], now=case["clock"].now())


async def hold(case, key="episode-1", requested=None, minimum=None):
    await initialize(case)
    requested = requested or resources()
    return await case["store"].reserve(
        case["lease"],
        "implementation",
        key,
        requested,
        requested if minimum is None else minimum,
        now=case["clock"].now(),
    )


async def current(case):
    return await case["store"].load(case["run"].id, "implementation")


async def observe(case, key="episode-1", usage=None, *, final=False):
    return await case["store"].observe(
        case["run"].id,
        "implementation",
        key,
        usage,
        final=final,
    )


async def usage_rows(case):
    async with case["factory"]() as session:
        return list(
            (await session.scalars(select(EngineUsage).order_by(EngineUsage.created_at))).all()
        )


async def claude_usage(tmp_path, counters, *, cost=0.1):
    sdk = FakeSDK(tmp_path, auto_result=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    frozen = spec()
    brief = ApprovedInput.from_spec(frozen).to_brief(
        RunIdentity(forge_run_id="run-1", attempt=0, generation=0),
        frozen,
    )
    session = await ClaudeCodeDriver(sdk).start(
        brief,
        workspace,
        DriverEnv("test-model", grant()),
    )
    sdk.clients[0].messages.put_nowait(Result(usage=counters, total_cost_usd=cost))
    events = [event async for event in session.events()]
    assert sdk.clients[0].disconnected
    return events[-1].usage


@contextmanager
def cold_schema():
    engine = create_engine("sqlite:///:memory:")
    module = script().get_revision(baseline.REVISION).module
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()
            yield SimpleNamespace(connection=connection, module=module)
    finally:
        engine.dispose()


def operator_commands(case):
    from forge.engine.commands import Commands
    from forge.engine.operator_actions import OperatorActions
    from forge.store.command_effects import CommandEffectStore
    from forge.store.command_runs import CommandRuns
    from forge.store.commands import CommandStore
    from forge.store.controls import ControlStore
    from forge.store.operator import OperatorStore
    from forge.store.resume import ResumeStore
    from forge.store.retry import RetryStore

    env = case["env"]
    actions = OperatorActions(
        CommandRuns(env.factory),
        RetryStore(env.factory),
        ControlStore(env.factory, {"test": frozenset({"interrupt", "steer"})}),
        ResumeStore(env.factory),
        env.fake,
        env.clock,
    )
    return Commands(
        CommandStore(env.factory),
        CommandRuns(env.factory),
        OperatorStore(env.factory),
        env.effects,
        CommandEffectStore(env.factory),
        case["keys"],
        lambda _: env.fake,
        env.clock,
        actions=actions.actions,
    )
