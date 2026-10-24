"""Authenticated native facts never turn bot authorship into command authority."""

from datetime import datetime, timezone
import json

from sqlalchemy import func, select

from forge.budget.credentials import CredentialVault
from forge.command_values import CommandEnvelope
from forge.engine.commands import Commands
from forge.engine.effects import Effects
from forge.port_values import Actor
from forge.providers.fake import FakeProvider
from forge.providers.gitlab.ingress import GitLabIngress
from forge.store.command_effects import CommandEffectStore
from forge.store.command_runs import CommandRuns
from forge.store.commands import CommandStore
from forge.store.effects import EffectStore
from forge.store.gateway_keys import GatewayKeyStore
from forge.store.models_commands import EngineCommand
from forge.store.models_execution import EngineEffect
from forge.store.models_runs import EngineRun
from forge.store.operator import OperatorStore
from tests.fakes.ports import ScriptedPorts
from tests.native_store_fixture import native_store as native_store

SECRET = b"synthetic-webhook-secret-at-least-32-bytes"
BOT = Actor(provider="gitlab", connection_id="test", native_id="99", login="forge-bot", is_bot=True)


def note(*, bot=False):
    native_id, login = (99, "forge-bot") if bot else (7, "bob")
    return {
        "object_kind": "note",
        "project": {"id": 42, "path_with_namespace": "owner/project"},
        "user": {"id": native_id, "username": login, "bot": bot},
        "object_attributes": {
            "id": 91,
            "project_id": 42,
            "noteable_type": "Issue",
            "noteable_id": 70,
            "author_id": native_id,
            "action": "create",
            "note": "/status",
            "system": False,
        },
        "issue": {"id": 70, "iid": 7, "project_id": 42},
    }


def review(*, bot=False, commit_author="alice"):
    document = note(bot=bot)
    document.update(
        object_kind="merge_request",
        object_attributes={
            "id": 80,
            "iid": 8,
            "source_project_id": 42,
            "target_project_id": 42,
            "action": "update",
            "oldrev": "a" * 40,
            "last_commit": {"id": "b" * 40, "author": {"name": commit_author}},
        },
    )
    return document


def parse(document, event="Note Hook"):
    ingress = GitLabIngress("test", SECRET, bots={"42": BOT})
    return ingress.parse(
        {
            "X-Gitlab-Token": SECRET.decode(),
            "X-Gitlab-Event": event,
            "X-Gitlab-Event-UUID": "12345678-1234-1234-1234-123456789abc",
        },
        json.dumps(document).encode(),
    )


def test_note_from_bot_is_detected():
    assert parse(note(bot=True)) is None


def test_note_from_other_user_is_not_bot():
    event = parse(note())
    assert not event.actor.is_bot and event.actor.login == "bob"
    assert event.commands[0].kind == "status"


def test_mr_event_from_bot_user():
    assert parse(review(bot=True), "Merge Request Hook") is None


def test_mr_update_with_bot_commit_author():
    event = parse(review(commit_author=BOT.login), "Merge Request Hook")
    assert not event.actor.is_bot and event.target.kind == "review"
    assert event.commands == ()


def test_mr_update_with_human_commit_author():
    event = parse(review(), "Merge Request Hook")
    assert event.actor.login == "bob" and event.target.number == 8
    assert event.commands == ()


def test_push_from_regular_user():
    # The native MR-update hook observes a human push; it grants no command.
    event = parse(review(commit_author="bob"), "Merge Request Hook")
    assert event.action == "update" and not event.actor.is_bot
    assert event.commands == () and event.native_id == "80"


class Clock:
    def now(self):
        return datetime.now(timezone.utc)


def commands(factory):
    class Actors:
        def bot_identity(self):
            return BOT

        async def is_approver(self, actor, subject):
            raise AssertionError("read-only status must not request mutable authority")

    fake, clock = FakeProvider(provider="gitlab"), Clock()
    effects = Effects(EffectStore(factory), fake, fake, fake, fake, fake, ScriptedPorts(), clock)
    service = Commands(
        CommandStore(factory),
        CommandRuns(factory),
        OperatorStore(factory),
        effects,
        CommandEffectStore(factory),
        GatewayKeyStore(factory, CredentialVault(b"synthetic-server-secret-32-bytes!!")),
        lambda repo: Actors(),
        clock,
        actions={},
    )
    return service, fake


async def test_webhook_skips_bot_event(native_store):
    service, fake = commands(native_store)
    assert parse(note(bot=True)) is None
    assert await service.tick() == 0
    async with native_store() as session:
        for table in (EngineCommand, EngineRun, EngineEffect):
            assert await session.scalar(select(func.count()).select_from(table)) == 0
    assert fake.calls == []


async def test_webhook_accepts_non_bot_event(native_store):
    service, fake = commands(native_store)
    event = parse(note())
    value = CommandEnvelope(
        source="webhook",
        native_id=event.native_id,
        actor=event.actor,
        target=event.target,
        command=event.commands[0],
    )
    result = await service.submit(value)
    assert result.reason == "run_not_found"
    assert await service.submit(value) == result
    async with native_store() as session:
        assert await session.scalar(select(func.count()).select_from(EngineCommand)) == 1
        assert await session.scalar(select(func.count()).select_from(EngineRun)) == 0
        assert await session.scalar(select(func.count()).select_from(EngineEffect)) == 0
    assert fake.calls == []


def test_native_bot_ci_completion_is_an_observation_without_command_authority():
    document = note(bot=True)
    document.update(object_kind="pipeline", object_attributes={"id": 701, "status": "success"})
    event = parse(document, "Pipeline Hook")
    assert event.actor.is_bot and event.native_id == "701" and event.action == "success"
    assert event.commands == ()
