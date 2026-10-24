"""Signed native GitHub hooks use actual command/admission SQL and unpaid port contracts."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from forge.budget.credentials import CredentialVault
from forge.command_values import CommandResult
from forge.domain.states import Handler
from forge.engine.approve import ApproveHandler
from forge.engine.commands import Commands
from forge.engine.effects import Effects
from forge.engine.plan import PlanHandler
from forge.engine.operator_actions import OperatorActions
from forge.store.controls import ControlStore
from forge.store.resume import ResumeStore
from forge.store.retry import RetryStore
from forge.engine.runner import StepRunner
from forge.engine.wakeups import Wakeups
from forge.port_values import TaskSnapshot, SubjectRef
from forge.providers.fake import Blob, FakeProvider
from forge.providers.fake.state import ref_key
from forge.providers.github.ingress import GitHubIngress
from forge.runtime.actions import InitialActions
from forge.runtime.app import create_app
from forge.runtime.authority import Operators
from forge.runtime.routing import NativePorts, Registry
from forge.runtime.settings import RuntimeSettings
from forge.store.approval import ApprovalStore
from forge.store.command_effects import CommandEffectStore
from forge.store.command_runs import CommandRuns
from forge.store.commands import CommandStore
from forge.store.effects import EffectStore
from forge.store.gateway_keys import GatewayKeyStore
from forge.store.operator import OperatorStore
from forge.store.runs import RunStore
from forge.store.steps import StepStore
from tests.fakes.ports import FixedClock, ScriptedPorts
from tests.native_store_fixture import native_store as native_store
from tests.runtime_fixture import configuration, OPERATOR_TOKEN
from tests.test_domain_approved_input import spec

SECRET = b"synthetic-github-hook-secret-at-least-32-bytes"
DELIVERY = "12345678-1234-1234-1234-123456789abc"
BOT_LOGIN = "forcewake-forge[bot]"
FIXTURES = Path(__file__).parent / "fixtures" / "github_payloads"


def payload(name="issue_comment_created.json", **changes):
    document = json.loads((FIXTURES / name).read_text())
    if "issue" in document:
        document["issue"]["updated_at"] = "2026-10-24T10:00:00Z"
    if "comment" in document:
        document["comment"]["body"] = "/implement"
    if "pull_request" in document and "head" in document["pull_request"]:
        document["pull_request"]["head"]["repo"] = {"id": 70010}
    document.update(changes)
    return document


def signed(document, event="issue_comment", *, delivery=DELIVERY, secret=SECRET):
    raw = document if isinstance(document, bytes) else json.dumps(document).encode()
    return {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest(),
    }, raw


def configured(tmp_path):
    original, _ = configuration(tmp_path)
    document = original.model_dump(mode="json", by_alias=True)
    repository = {
        **document["connections"][0]["repositories"][0],
        "native_id": "70010",
        "full_name": "acme/acme-widget",
        "bot_id": "99",
        "bot_login": BOT_LOGIN,
        "approver_ids": ["501"],
        "installation_id": 12345,
        "read_token_env": None,
        "write_token_env": None,
    }
    document["connections"] = [
        {
            "key": "github",
            "provider": "github",
            "api_url": "https://api.github.com",
            "git_url": "https://github.com",
            "oidc_issuer": "https://token.actions.githubusercontent.com",
            "oidc_jwks_url": "https://token.actions.githubusercontent.com/.well-known/jwks",
            "webhook_secret_env": "SYNTHETIC_WEBHOOK",
            "app_issuer": "100",
            "app_private_key_env": "SYNTHETIC_APP_KEY",
            "repositories": [repository],
        }
    ]
    document["operators"][0]["grants"] = [
        {"repository": "project", "native_id": "501", "login": "alice"}
    ]
    return RuntimeSettings.model_validate_json(json.dumps(document))


class NativeFacts:
    def __init__(self):
        self.events = []

    async def submit(self, event):
        self.events.append(event)
        return CommandResult(status="observed", reason="review_not_enabled")


class Wakes(Wakeups):
    def __init__(self):
        super().__init__()
        self.count = 0

    def signal(self):
        self.count += 1
        super().signal()


async def rows(case, model):
    async with case.factory() as session:
        return list((await session.scalars(select(model))).all())


async def post(case, document=None, event="issue_comment", *, delivery=DELIVERY):
    headers, body = signed(payload() if document is None else document, event, delivery=delivery)
    return await case.client.post("/hooks/github", content=body, headers=headers)


async def planned(case):
    response = await post(case)
    assert response.status_code == 202, response.text
    run_id = response.json()["commands"][0]["run_id"]
    assert (await case.runner.tick()).completed == 1
    assert (await case.runner.tick()).completed == 1
    return await case.runs.load(run_id), await case.approvals.load(run_id)


def components(native_store, tmp_path):
    config = configured(tmp_path)
    connection, repository = config.repositories()["project"]
    fake = FakeProvider(provider="github", connection_id="github")
    fake.bot_identity = lambda: connection.bot(repository)
    clock = FixedClock(datetime.now(timezone.utc))
    fake.now = clock.now()
    ref = connection.ref(repository)
    fake.seed_repository(ref, {"src/example.py": Blob(b"old\n")})
    task = TaskSnapshot(
        subject=SubjectRef(repo=ref, number=42),
        title="Add password reset",
        description="Users cannot reset their password.",
        version="issue-1",
    )
    fake.tasks[ref_key(task.subject)] = task
    fake.approvers.add((ref_key(task.subject), "501"))
    registry = Registry({"project": NativePorts(ref, fake, fake, fake, fake, fake, fake)})
    effects = Effects(
        EffectStore(native_store), fake, fake, fake, fake, fake, ScriptedPorts(()), clock
    )
    case = SimpleNamespace(
        factory=native_store,
        config=config,
        fake=fake,
        clock=clock,
        ref=ref,
        task=task,
        registry=registry,
        effects=effects,
        runs=RunStore(native_store),
        steps=StepStore(native_store),
        approvals=ApprovalStore(native_store),
        facts=NativeFacts(),
        wakes=Wakes(),
    )
    case.runner, case.commands = workers(case)
    case.runtime = SimpleNamespace(
        config=config,
        factory=native_store,
        connections=SimpleNamespace(
            registry=registry,
            ingress={
                "github": GitHubIngress(
                    "github", SECRET, repositories={"70010": 12345}, bot=connection.bot(repository)
                )
            },
        ),
        commands=case.commands,
        wakeups=case.wakes,
        lane=None,
        operators=Operators(config, {"SYNTHETIC_OPERATOR": OPERATOR_TOKEN}),
        reactive=SimpleNamespace(reviews=case.facts),
    )
    return case


def workers(case):
    planner = ScriptedPorts(tuple(("plan", spec().plan) for _ in range(4)))
    runner = StepRunner(
        case.steps,
        {
            Handler.PLAN: PlanHandler(
                case.runs, case.approvals, lambda current, policy: planner, case.fake, case.clock
            ),
            Handler.APPROVE: ApproveHandler(
                case.runs, case.steps, case.approvals, case.fake, case.fake, case.clock
            ),
        },
        effects=case.effects,
        clock=case.clock,
        owner="native-hook",
    )
    actions = dict(InitialActions(case.config, case.registry, case.factory, case.clock).actions)
    actions.update(
        OperatorActions(
            CommandRuns(case.factory),
            RetryStore(case.factory),
            ControlStore(case.factory, {"claude_code": frozenset({"interrupt", "steer"})}),
            ResumeStore(case.factory),
            case.fake,
            case.clock,
        ).actions
    )
    commands = Commands(
        CommandStore(case.factory),
        CommandRuns(case.factory),
        OperatorStore(case.factory),
        case.effects,
        CommandEffectStore(case.factory),
        GatewayKeyStore(case.factory, CredentialVault(b"synthetic-server-secret-32-bytes!!")),
        case.registry.actor_for,
        case.clock,
        actions=actions,
    )
    return runner, commands


@pytest.fixture
async def hooks(native_store, tmp_path):
    case = components(native_store, tmp_path)

    @asynccontextmanager
    async def opener(_):
        yield case.runtime

    app = create_app(case.config, opener=opener)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="https://forge.example",
        ) as client:
            case.app, case.client = app, client
            yield case
