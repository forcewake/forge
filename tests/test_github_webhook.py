"""Retained signed-ingress, durable admission and self-trigger lessons on native runtime."""

from datetime import timedelta
import hashlib
import hmac
import json
from uuid import uuid4

import pytest

from forge.command_values import CommandEnvelope
from forge.domain.states import State
from forge.providers.fake.state import ref_key
from forge.providers.github.ingress import GitHubIngress
from forge.runtime.webhooks import command_id
from forge.store.models_commands import EngineCommand
from forge.store.models_execution import EngineEffect, EngineStep
from forge.store.models_runs import EngineRun
from tests.github_webhook_fixture import (
    hooks as hooks,
    native_store as native_store,
    payload,
    post,
    rows,
    planned,
    signed,
    SECRET,
    DELIVERY,
    BOT_LOGIN,
)


def parsed(case, document=None, event="issues", *, label="forge", delivery=DELIVERY):
    parser = GitHubIngress(
        "github",
        SECRET,
        repositories={"70010": 12345},
        bot=case.fake.bot_identity(),
        implement_label=label,
    )
    return parser.parse(
        *signed(payload() if document is None else document, event, delivery=delivery)
    )


def bot(document, *, login=BOT_LOGIN, native_id=99, kind="Bot"):
    author = {"id": native_id, "login": login, "type": kind}
    document["sender"] = author
    if "comment" in document:
        document["comment"]["user"] = author
    return document


async def no_work(case):
    assert await rows(case, EngineCommand) == []
    assert await rows(case, EngineStep) == []
    assert await rows(case, EngineRun) == []
    assert await rows(case, EngineEffect) == []


class TestFailClosed:
    async def test_disabled_returns_503(self, hooks):
        hooks.app.state.runtime = None
        assert (await post(hooks)).status_code == 503
        await no_work(hooks)

    async def test_missing_secret_returns_503(self, hooks):
        with pytest.raises(ValueError, match="strong webhook secret"):
            GitHubIngress(
                "github", b"", repositories={"70010": 12345}, bot=hooks.fake.bot_identity()
            )
        hooks.app.state.runtime = None
        assert (await post(hooks)).status_code == 503
        await no_work(hooks)


class TestSignature:
    async def test_missing_signature_rejected(self, hooks):
        headers, body = signed(payload())
        del headers["X-Hub-Signature-256"]
        response = await hooks.client.post("/hooks/github", content=body, headers=headers)
        assert response.status_code == 400
        await no_work(hooks)

    async def test_wrong_signature_rejected(self, hooks):
        headers, body = signed(payload(), secret=b"different-synthetic-key-at-least-32bytes")
        assert (
            await hooks.client.post("/hooks/github", content=body, headers=headers)
        ).status_code == 400
        await no_work(hooks)

    async def test_tampered_body_rejected(self, hooks):
        headers, body = signed(payload())
        changed = body.replace(b"Add password reset", b"Changed password reset")
        assert changed != body
        assert (
            await hooks.client.post("/hooks/github", content=changed, headers=headers)
        ).status_code == 400
        await no_work(hooks)

    async def test_ping_answers_pong(self, hooks):
        """An unsupported handshake cannot claim durable command acceptance."""
        response = await post(hooks, payload("ping.json"), "ping")
        assert response.status_code == 400
        assert "accepted" not in response.json()
        await no_work(hooks)

    async def test_malformed_signed_native_fields_never_gain_authority(self, hooks):
        document = payload("pull_request_synchronize.json")
        del document["pull_request"]["head"]["repo"]
        response = await post(hooks, document, "pull_request")
        assert response.status_code == 400
        await no_work(hooks)

    def test_official_test_vector(self):
        body, key = b"Hello, World!", b"It's a Secret to Everybody"
        assert "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest() == (
            "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
        )


class TestIngress:
    async def test_implement_comment_persists_inbox_and_step(self, hooks):
        response = await post(hooks)
        assert response.status_code == 202, response.text
        (command,) = await rows(hooks, EngineCommand)
        (run,) = await rows(hooks, EngineRun)
        (step,) = await rows(hooks, EngineStep)
        document = json.loads(command.document_json)
        assert command.state == "completed" and command.run_id == run.id == step.run_id
        assert document["actor"]["native_id"] == "501" and document["target"]["number"] == 42
        assert document["target"]["repo"]["identity"] == hooks.ref.identity.model_dump(mode="json")
        assert step.state == "scheduled" and step.key == f"{run.id}:plan"
        assert hooks.wakes.count == 1

    async def test_pr_comment_is_flagged_as_pr(self, hooks):
        doc = payload("issue_comment_pr.json")
        event = parsed(hooks, doc, "issue_comment")
        assert event.target.kind == "review"
        response = await post(hooks, doc)
        assert response.status_code == 202 and response.json()["commands"][0]["status"] == "refused"
        assert await rows(hooks, EngineRun) == [] and await rows(hooks, EngineStep) == []

    async def test_redelivered_command_is_deduplicated(self, hooks):
        first = await post(hooks)
        second = await post(hooks, delivery=str(uuid4()))
        assert first.json() == second.json()
        assert len(await rows(hooks, EngineCommand)) == len(await rows(hooks, EngineRun)) == 1
        assert len(await rows(hooks, EngineStep)) == 1
        assert sum(name == "post_comment" for name, _ in hooks.fake.calls) == 1

    async def test_command_without_a_database_is_refused(self, hooks):
        async with hooks.factory.begin() as session:
            await session.run_sync(lambda sync: EngineCommand.__table__.drop(sync.connection()))
        response = await post(hooks)
        assert response.status_code == 503 and "accepted" not in response.json()
        assert await rows(hooks, EngineRun) == [] and await rows(hooks, EngineStep) == []
        assert hooks.fake.comments == {}

    async def test_non_command_comment_recorded_without_step(self, hooks):
        doc = payload()
        doc["comment"]["body"] = "Looks great, thanks!"
        response = await post(hooks, doc)
        assert response.json() == {"accepted": True, "commands": []}
        await no_work(hooks)

    async def test_bot_comment_is_skipped(self, hooks):
        response = await post(hooks, bot(payload()))
        assert response.json() == {"accepted": False, "commands": []}
        assert hooks.wakes.count == 0
        await no_work(hooks)

    async def test_push_event_recorded_inbox_only(self, hooks):
        doc = payload("pull_request_synchronize.json")
        response = await post(hooks, doc, "pull_request")
        assert response.status_code == 202, response.text
        assert response.json()["commands"] == []
        assert len(hooks.facts.events) == 1 and hooks.facts.events[0].head_sha is not None
        await no_work(hooks)

    async def test_installation_removed_records_disabled_connection(self, hooks):
        response = await post(hooks, payload("installation_deleted.json"), "installation")
        assert response.status_code == 400
        assert "github" in hooks.runtime.connections.ingress
        hooks.runtime.connections.ingress.clear()  # trusted configuration revocation
        assert (await post(hooks)).status_code == 404
        await no_work(hooks)

    async def test_repeated_push_delivery_collapses(self, hooks):
        doc = payload("pull_request_synchronize.json")
        first = await post(hooks, doc, "pull_request")
        second = await post(hooks, doc, "pull_request")
        assert first.json() == second.json() and first.status_code == 202
        assert {event.head_sha for event in hooks.facts.events} == {hooks.facts.events[0].head_sha}
        await no_work(hooks)


class TestSignatureUnit:
    def test_verify_signature_accepts_correct_hmac(self, hooks):
        event = parsed(hooks, payload(), "issue_comment")
        assert event.commands[0].kind == "implement" and event.actor.native_id == "501"

    def test_verify_signature_rejects_missing_or_bad(self, hooks):
        parser = hooks.runtime.connections.ingress["github"]
        headers, body = signed(payload())
        for signature in (None, "", "sha256=00", "sha1=abcdef"):
            bad = {**headers, "X-Hub-Signature-256": signature or ""}
            with pytest.raises(ValueError):
                parser.parse(bad, body)


class TestLabeledTrigger:
    def test_matching_label_normalizes_to_start_run(self, hooks):
        event = parsed(hooks, payload("issues_labeled.json"))
        assert event.commands[0].kind == "implement" and event.actor.native_id == "501"
        assert event.target.kind == "issue" and event.target.number == 42

    def test_distinct_issues_with_the_same_label_do_not_collide(self, hooks):
        first = parsed(hooks, payload("issues_labeled.json"))
        doc = payload("issues_labeled.json")
        doc["issue"]["number"], doc["issue"]["id"] = 43, 1011
        second = parsed(hooks, doc)
        assert command_id(first) != command_id(second)
        assert first.target != second.target

    async def test_redelivered_label_event_keeps_a_stable_identity(self, hooks):
        document = payload("issues_labeled.json")
        first = await post(hooks, document, "issues")
        second = await post(hooks, document, "issues", delivery=str(uuid4()))
        assert first.json() == second.json() and first.json()["commands"][0]["status"] == "accepted"
        assert len(await rows(hooks, EngineCommand)) == len(await rows(hooks, EngineRun)) == 1

    @pytest.mark.parametrize("label", ["FORGE", "forge", "FoRgE"])
    def test_label_match_is_case_insensitive(self, hooks, label):
        doc = payload("issues_labeled.json")
        doc["label"]["name"] = label
        assert parsed(hooks, doc).commands[0].kind == "implement"

    @pytest.mark.parametrize("label", ["bug", "forge-it"])
    def test_other_labels_are_ignored(self, hooks, label):
        doc = payload("issues_labeled.json")
        doc["label"]["name"] = label
        assert parsed(hooks, doc).commands == ()

    def test_trigger_label_is_configurable(self, hooks):
        doc = payload("issues_labeled.json")
        assert parsed(hooks, doc, label="run-forge").commands == ()
        doc["label"]["name"] = "Run-Forge"
        assert parsed(hooks, doc, label="run-forge").commands[0].kind == "implement"

    def test_pr_labeling_is_flagged(self, hooks):
        doc = payload("issues_labeled.json")
        doc["issue"]["pull_request"] = {"url": "https://github.example/x"}
        assert parsed(hooks, doc).target.kind == "review"

    async def test_labeled_delivery_ingests_a_durable_run_command(self, hooks):
        response = await post(hooks, payload("issues_labeled.json"), "issues")
        assert (
            response.status_code == 202
            and response.json()["commands"][0]["reason"] == "implementation_queued"
        )
        assert len(await rows(hooks, EngineCommand)) == len(await rows(hooks, EngineStep)) == 1

    async def test_non_matching_label_delivery_is_recorded_inbox_only(self, hooks):
        doc = payload("issues_labeled.json")
        doc["label"]["name"] = "bug"
        assert (await post(hooks, doc, "issues")).json() == {"accepted": True, "commands": []}
        await no_work(hooks)


class TestIssueEditedTrigger:
    def test_edited_issue_normalizes_to_issue_edited(self, hooks):
        event = parsed(hooks, payload("issues_edited.json"))
        assert event.issue_action == "edited" and event.commands[0].kind == "implement"
        assert event.target.number == 42 and event.actor.native_id == "501"

    def test_pr_edit_is_ignored(self, hooks):
        doc = payload("issues_edited.json")
        doc["issue"]["pull_request"] = {"url": "https://github.example/x"}
        event = parsed(hooks, doc)
        assert event.commands == () and event.issue_action is None

    def test_redelivered_edit_keeps_a_stable_identity(self, hooks):
        doc = payload("issues_edited.json")
        first = parsed(hooks, doc)
        assert command_id(first) == command_id(parsed(hooks, doc, delivery=str(uuid4())))
        doc["issue"]["body"] = "A real new edit"
        assert command_id(parsed(hooks, doc)) != command_id(first)

    def test_edit_back_to_a_seen_text_is_not_swallowed(self, hooks):
        identities = []
        for body, version in (("B", "t1"), ("A", "t2"), ("B", "t3")):
            doc = payload("issues_edited.json")
            doc["issue"].update(body=body, updated_at=version)
            identities.append(command_id(parsed(hooks, doc)))
        assert len(set(identities)) == 3

    async def test_edited_delivery_ingests_a_durable_run_command(self, hooks):
        old, frozen = await planned(hooks)
        task = hooks.task.model_copy(update={"description": "New approved task request"})
        hooks.fake.tasks[ref_key(task.subject)] = task
        doc = payload("issues_edited.json")
        doc["issue"]["body"] = task.description
        response = await post(hooks, doc, "issues")
        fresh_id = response.json()["commands"][0]["run_id"]
        assert fresh_id != old.id and (await hooks.runs.load(old.id)).state == State.CANCELLED
        assert (await hooks.approvals.load(old.id)).spec == frozen.spec
        fresh = await hooks.steps.get(f"{fresh_id}:plan")
        assert json.loads(fresh.payload_json)["task"]["description"] == task.description


class TestUnlabeledTrigger:
    def test_trigger_label_removal_normalizes_to_unlabeled(self, hooks):
        event = parsed(hooks, payload("issues_unlabeled.json"))
        assert event.issue_action == "unlabeled" and event.commands[0].kind == "cancel"
        assert event.target.number == 42 and event.actor.native_id == "501"

    def test_other_label_removal_is_ignored(self, hooks):
        doc = payload("issues_unlabeled.json")
        doc["label"]["name"] = "bug"
        assert parsed(hooks, doc).commands == ()

    def test_pr_unlabel_is_ignored(self, hooks):
        doc = payload("issues_unlabeled.json")
        doc["issue"]["pull_request"] = {"url": "https://github.example/x"}
        assert parsed(hooks, doc).commands == ()

    async def test_unlabeled_delivery_ingests_a_durable_run_command(self, hooks):
        run, _ = await planned(hooks)
        doc = payload("issues_unlabeled.json")
        first = await post(hooks, doc, "issues")
        again = await post(hooks, doc, "issues", delivery=str(uuid4()))
        assert first.json() == again.json() and first.json()["commands"][0]["reason"] == "cancelled"
        assert (await hooks.runs.load(run.id)).state == State.CANCELLED
        assert len(await rows(hooks, EngineCommand)) == 2


class TestPayloadCapture:
    async def test_pull_request_delivery_is_captured(self, hooks):
        response = await post(hooks, payload("pull_request_synchronize.json"), "pull_request")
        assert response.status_code == 202, response.text
        (event,) = hooks.facts.events
        assert event.event == "pull_request" and event.action == "synchronize"
        assert event.target.kind == "review" and event.head_sha is not None
        assert SECRET.decode() not in json.dumps(event.model_dump(mode="json")) + response.text
        await no_work(hooks)

    async def test_ping_is_captured(self, hooks):
        response = await post(hooks, payload("ping.json"), "ping")
        assert response.status_code == 400 and SECRET.decode() not in response.text
        assert "commands" not in response.json() and hooks.facts.events == []
        await no_work(hooks)


class TestGitHubBotLoginLoopGuard:
    async def test_bot_login_comment_does_not_create_a_run(self, hooks):
        doc = bot(payload(), login=BOT_LOGIN.upper(), native_id=987, kind="User")
        assert (await post(hooks, doc)).json() == {"accepted": False, "commands": []}
        assert hooks.wakes.count == 0
        await no_work(hooks)

    async def test_app_bot_login_label_event_does_not_create_a_run(self, hooks):
        document = bot(
            payload("issues_labeled.json"), login="renamed-native-bot", native_id=99, kind="User"
        )
        assert (await post(hooks, document, "issues")).json() == {"accepted": False, "commands": []}
        await no_work(hooks)

    async def test_human_actor_label_event_still_creates_a_run(self, hooks):
        response = await post(hooks, payload("issues_labeled.json"), "issues")
        assert response.json()["commands"][0]["status"] == "accepted"
        assert len(await rows(hooks, EngineRun)) == len(await rows(hooks, EngineStep)) == 1


class TestAdaptiveCommandIngress:
    async def test_disabled_default_is_zero_routing(self, hooks):
        doc = payload()
        doc["comment"]["body"] = "@forge /pause is quoted guidance, not a first-line slash request"
        assert (await post(hooks, doc)).json() == {"accepted": True, "commands": []}
        await no_work(hooks)

    async def test_enabled_routes_to_the_command_router(self, hooks):
        doc = payload()
        doc["comment"]["body"] = "/pause"
        response = await post(hooks, doc)
        (command,) = await rows(hooks, EngineCommand)
        assert response.status_code == 202 and command.kind == "pause"
        assert (
            command.state == "completed"
            and json.loads(command.result_json)["reason"] == "run_not_found"
        )
        assert await rows(hooks, EngineRun) == [] and await rows(hooks, EngineStep) == []

    async def test_the_step_executor_routes_the_persisted_command(self, hooks):
        doc = payload()
        doc["comment"]["body"] = "/pause"
        event = parsed(hooks, doc, "issue_comment")
        envelope = CommandEnvelope(
            source="webhook",
            native_id=command_id(event),
            actor=event.actor,
            target=event.target,
            command=event.commands[0],
        )
        await hooks.commands.store.accept(envelope, now=hooks.clock.now())
        original = hooks.commands.actions["pause"]

        async def failing(*args):
            raise RuntimeError("native action did not finish")

        hooks.commands.actions["pause"] = failing
        with pytest.raises(RuntimeError):
            await hooks.commands.tick()
        (row,) = await rows(hooks, EngineCommand)
        assert row.state == "running" and row.result_json is None
        hooks.commands.actions["pause"] = original
        hooks.clock.instant += timedelta(seconds=121)
        assert await hooks.commands.tick() == 1
        (row,) = await rows(hooks, EngineCommand)
        assert row.state == "completed" and json.loads(row.result_json)["reason"] == "run_not_found"

    async def test_a_redelivered_adaptive_note_collapses_onto_one_command(self, hooks):
        document = payload()
        document["comment"]["body"] = "/pause"
        first = await post(hooks, document)
        again = await post(hooks, document, delivery=str(uuid4()))
        assert first.json() == again.json()
        assert len(await rows(hooks, EngineCommand)) == 1 and await rows(hooks, EngineStep) == []
        assert sum(name == "post_comment" for name, _ in hooks.fake.calls) == 1
