"""Actual runtime operator authority over colliding native repository identities."""

from copy import deepcopy
from datetime import timedelta
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from forge.domain.states import Handler
from forge.port_values import SubjectRef, TaskSnapshot
from forge.runtime.settings import RuntimeSettings
from forge.store.models_execution import EngineStep
from forge.store.models_runs import EngineRun
from forge.store.records import RunRequest, StepRequest
from forge.store.runs import RunStore
from forge.store.steps import StepStore
from tests.operator_native_fixture import opened_operator
from tests.runtime_fixture import configuration

TOKENS = {
    name: f"synthetic-{name}-operator-token-at-least-32-bytes"
    for name in ("alpha", "beta", "project", "wide", "observer")
}


def scoped_config(root):
    config, values = configuration(root)
    document = config.model_dump(mode="json", by_alias=True)
    project = document["connections"][0]["repositories"][0]
    for name, host in (("alpha", "github.example"), ("beta", "ghe.example")):
        repo = deepcopy(project)
        repo.update(key=name, bot_login="forge[bot]", installation_id=900)
        repo.pop("read_token_env")
        repo.pop("write_token_env")
        document["connections"].append(
            {
                "key": name,
                "provider": "github",
                "api_url": "https://" + host,
                "git_url": "https://" + host,
                "oidc_issuer": "https://" + host,
                "oidc_jwks_url": "https://" + host + "/keys",
                "webhook_secret_env": "SYNTHETIC_WEBHOOK",
                "app_issuer": "synthetic-app",
                "app_private_key_env": "SYNTHETIC_APP_KEY",
                "repositories": [repo],
            }
        )
    document["operators"] = [
        {
            "key": name,
            "token_env": "SYNTHETIC_OPERATOR_" + name.upper(),
            "grants": [
                {
                    "repository": repo,
                    "native_id": "43" if name == "observer" else "42",
                    "login": "observer" if name == "observer" else "reviewer",
                }
                for repo in (
                    ("alpha", "beta", "project")
                    if name == "wide"
                    else ("project" if name == "observer" else name,)
                )
            ],
        }
        for name in TOKENS
    ]
    values.update({"SYNTHETIC_OPERATOR_" + name.upper(): token for name, token in TOKENS.items()})
    values["SYNTHETIC_APP_KEY"] = "synthetic-never-used-signing-key"
    return RuntimeSettings.model_validate_json(json.dumps(document)), values


@pytest.fixture
async def surface(tmp_path):
    config, values = scoped_config(tmp_path)
    async with opened_operator(config, values) as fixture:
        yield fixture


def headers(name="project"):
    return {"Authorization": "Bearer " + TOKENS[name]}


async def seed_run(case, repository="project", *, run_id=None, number=7, **changes):
    ports = case.service.connections.registry.named(repository)
    identity = ports.ref.identity
    run_id = run_id or uuid4().hex
    run = await RunStore(case.service.factory).admit(
        RunRequest(
            run_id,
            identity.provider,
            identity.connection_id,
            identity.repository_id,
            "issue",
            str(number),
            root_run_id=changes.pop("root_run_id", None),
            parent_run_id=changes.pop("parent_run_id", None),
        )
    )
    task = TaskSnapshot(
        subject=SubjectRef(repo=ports.ref, number=number),
        title="Task",
        description="raw prompt excluded",
        version="native-v1",
    )
    await StepStore(case.service.factory).enqueue(
        StepRequest(
            run.id,
            Handler.PLAN,
            run.id + ":plan",
            json.dumps(
                {"task": task.model_dump(mode="json")}, sort_keys=True, separators=(",", ":")
            ),
            case.service.budget.clock.now(),
        )
    )
    async with case.service.factory() as session, session.begin():
        row = await session.get(EngineRun, run.id)
        for name, value in changes.items():
            setattr(row, name, value)
    return await RunStore(case.service.factory).load(run.id)


async def listing(case, name="project", **query):
    return await case.client.get("/api/v1/runs", headers=headers(name), params=query)


async def detail(case, run, name="project", **query):
    return await case.client.get(
        "/api/v1/runs/" + (run.id if hasattr(run, "id") else run),
        headers=headers(name),
        params=query,
    )


async def model_rows(case, model):
    async with case.service.factory() as session:
        return list((await session.scalars(select(model))).all())


async def table_counts(case):
    from sqlalchemy import func
    from forge.store.base import Base

    async with case.service.factory() as session:
        return {
            table.name: await session.scalar(select(func.count()).select_from(table))
            for table in Base.metadata.sorted_tables
        }


async def age_steps(case, run, seconds):
    async with case.service.factory() as session, session.begin():
        rows = await session.scalars(select(EngineStep).where(EngineStep.run_id == run.id))
        for row in rows:
            row.updated_at = case.service.budget.clock.now() - timedelta(seconds=seconds)
