"""Both-provider native facts, real command ownership, SQL engine and lane protocol."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from forge_protocol import RunIdentity
from forge.command_values import CommandEnvelope
from forge.domain.states import State
from forge.engine.commands import Commands
from forge.engine.operator_actions import OperatorActions
from forge.ingress_values import Command
from forge.providers.fake.state import ref_key
from forge.runtime.actions import InitialActions
from forge.runtime.routing import NativePorts, Registry
from forge.runtime.settings import RuntimeSettings
from forge.store.command_effects import CommandEffectStore
from forge.store.command_runs import CommandRuns
from forge.store.commands import CommandStore
from forge.store.controls import ControlStore
from forge.store.operator import OperatorStore
from forge.store.resume import ResumeStore
from forge.store.retry import RetryStore
from tests.runtime_fixture import configuration
from tests.test_engine_delivery import (
    advance as advance,
    case as case,
    check,
    completed,
    delivery as delivery,
    env as env,
    settings as settings,
)


@pytest.fixture
async def lifecycle(delivery, tmp_path, monkeypatch):
    env = delivery["env"]
    config, _ = configuration(tmp_path)
    document = config.model_dump(mode="json", by_alias=True)
    connection = document["connections"][0]
    connection.update(key="test", provider=env.fake.provider)
    repo = connection["repositories"][0]
    repo.update(
        native_id="42",
        full_name="owner/project",
        bot_id="99",
        bot_login="project_42_bot_forge" if env.fake.provider == "gitlab" else "forge[bot]",
        policy=env.intent.policy.model_copy(update={"driver": "claude_code"}).model_dump(
            mode="json"
        ),
    )
    if env.fake.provider == "github":
        connection.update(app_issuer="123", app_private_key_env="SYNTHETIC_APP")
        repo.update(installation_id=456, read_token_env=None, write_token_env=None)
    config = RuntimeSettings.model_validate_json(json.dumps(document))
    connection, repo = config.repositories()["project"]
    monkeypatch.setattr(env.fake, "bot_identity", lambda: connection.bot(repo))
    actor = env.intent.actor.model_copy(update={"native_id": "42"})
    env.intent = env.intent.model_copy(update={"actor": actor, "policy": repo.policy})
    env.fake.approvers.clear()
    env.fake.approvers.add((ref_key(env.intent.task.subject), actor.native_id))
    ports = Registry({"project": NativePorts(connection.ref(repo), *([env.fake] * 6))})
    store = CommandStore(env.factory)
    runs = CommandRuns(env.factory)
    initial = InitialActions(config, ports, env.factory, env.clock)
    operator = OperatorActions(
        runs,
        RetryStore(env.factory),
        ControlStore(env.factory, {"claude_code": frozenset({"interrupt", "steer"})}),
        ResumeStore(env.factory),
        ports,
        env.clock,
    )
    commands = Commands(
        store,
        runs,
        OperatorStore(env.factory),
        env.effects,
        CommandEffectStore(env.factory),
        delivery["keys"],
        ports.actor_for,
        env.clock,
        actions={**initial.actions, **operator.actions},
    )
    delivery.update(
        config=config,
        ports=ports,
        initial=initial,
        commands=commands,
        command_store=store,
        template_plan=env.planner.script[0][1],
    )
    return delivery


def command(case, kind, *, argument="", native_id=None, target=None, actor=None, **fields):
    env = case["env"]
    return CommandEnvelope(
        source="webhook",
        native_id=native_id or "issue_comment:" + uuid4().hex,
        actor=actor or env.intent.actor,
        target=target or env.intent.task.subject,
        command=Command(kind=kind, argument=argument),
        **fields,
    )


async def planned(case, *, envelope=None):
    env = case["env"]
    result = await case["commands"].submit(envelope or command(case, "implement"))
    assert result.status == "accepted", result
    for _ in range(2):
        assert (await env.runner.tick()).completed == 1
    run = await env.runs.load(result.run_id)
    assert run.state == State.AWAITING_APPROVAL, run.reason
    return run, await env.approvals.load(run.id)


async def started(case):
    env = case["env"]
    run, frozen = await planned(case)
    result = await case["commands"].submit(command(case, "go", argument=run.id))
    assert result.status == "accepted", result
    for _ in range(3):
        assert (await env.runner.tick()).completed == 1
    identity = RunIdentity(forge_run_id=run.id, attempt=1, generation=1)
    return run, frozen, await case["store"].pending(identity)


async def published(case):
    run, frozen, pending = await started(case)
    await completed(case, pending)
    assert (await case["env"].runner.tick()).completed == 1
    sha = await case["env"].fake.head(case["env"].intent.task.subject.repo, "forge/" + run.id)
    return run, frozen, pending, sha


async def ready(case):
    run, frozen, pending, sha = await published(case)
    check(case, sha)
    for _ in range(3):
        assert (await case["env"].runner.tick()).completed == 1
    assert (await case["env"].runs.load(run.id)).state == State.READY_FOR_HUMAN
    return run, frozen, pending, sha


async def rows(case, model):
    async with case["env"].factory() as session:
        return list(await session.scalars(select(model)))


def neighbor(case, key, *, native_id="43", provider=None):
    from copy import deepcopy
    from forge.providers.fake import Blob, FakeProvider

    document = case["initial"].config.model_dump(mode="json", by_alias=True)
    primary = document["connections"][0]
    primary["repositories"][0]["neighbors"].append({"repository": key, "allowed_globs": ["b.py"]})
    repo = deepcopy(primary["repositories"][0])
    repo.update(key=key, native_id=native_id, full_name="owner/" + key, neighbors=[])
    provider = provider or primary["provider"]
    repo.update(
        bot_id="199",
        bot_login=f"project_{native_id}_bot_forge" if provider == "gitlab" else "neighbor[bot]",
    )
    if provider == "github":
        repo.update(installation_id=789, read_token_env=None, write_token_env=None)
    else:
        repo.update(
            installation_id=None,
            read_token_env="SYNTHETIC_NEIGHBOR_READ",
            write_token_env="SYNTHETIC_NEIGHBOR_WRITE",
        )
    connection = deepcopy(primary)
    connection.update(key=key, provider=provider, repositories=[repo])
    connection.update(
        app_issuer="123" if provider == "github" else None,
        app_private_key_env="SYNTHETIC_APP" if provider == "github" else None,
    )
    document["connections"].append(connection)
    config = RuntimeSettings.model_validate_json(json.dumps(document))
    configured, repo = config.repositories()[key]
    fake = FakeProvider(provider=provider, connection_id=configured.key)
    ref = configured.ref(repo)
    sha = fake.seed_repository(ref, {"b.py": Blob(b"read only neighbor\n")})
    ports = Registry({**case["ports"].entries, key: NativePorts(ref, *([fake] * 6))})
    case["config"], case["ports"] = config, ports
    case["initial"].config, case["initial"].ports = config, ports
    case["commands"].actor_for = ports.actor_for
    return fake, ref, sha
