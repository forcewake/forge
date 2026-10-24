"""The sole application crosses real owned TLS/native and ASGI authority boundaries."""

from contextlib import asynccontextmanager
import json

import httpx
import pytest
from sqlalchemy import func, select

from forge.runtime.app import create_app
from forge.runtime.assembly import database, open_runtime
from forge.store.models_execution import EngineEffect, EngineStep
from forge.store.models_runs import EngineRun
from forge.store.models_commands import EngineCommand
from forge.store.schema import initialize, SchemaMismatch
from tests import gitlab_engine_fixture as native
from tests.runtime_fixture import configuration, native_server, OPERATOR_TOKEN, WEBHOOK_SECRET
from tests.test_budget_engine import NativeAdmin
from tests.fakes.ports import FixedClock
from tests.test_gitlab_engine_ingress import payload
from datetime import datetime, timezone


@asynccontextmanager
async def application(config, values, wire, *, installed=True, model_handler=None):
    engine = database(values["SYNTHETIC_DATABASE_URL"])
    if installed:
        await initialize(engine)
    await engine.dispose()
    clock = FixedClock(datetime.now(timezone.utc))
    gateway = NativeAdmin(clock)

    def gateway_http(request):
        if request.url.path == "/v1/chat/completions" and model_handler is not None:
            return model_handler(request)
        return gateway(request)

    @asynccontextmanager
    async def opener(settings):
        async with open_runtime(
            settings,
            environ=values,
            gateway_transport=httpx.MockTransport(gateway_http),
            clock=clock,
        ) as runtime:
            yield runtime

    app = create_app(config, opener=opener)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://forge.invalid"
        ) as client:
            yield app, client, gateway


def auth(token=OPERATOR_TOKEN):
    return {"Authorization": "Bearer " + token}


def command(
    kind="implement", *, argument="", request_id="request-1", repository="project", **changes
):
    return {
        "repository": repository,
        "number": 7,
        "request_id": request_id,
        "command": {"kind": kind, "argument": argument},
        **changes,
    }


async def test_startup_refuses_missing_schema_before_acceptance_or_any_native_request(tmp_path):
    with native_server(tmp_path) as (wire, url, ca):
        config, values = configuration(tmp_path, native_url=url, ca=ca)
        with pytest.raises(SchemaMismatch):
            async with application(config, values, wire, installed=False):
                pytest.fail("startup must refuse before accepting work")
        assert not wire.calls and not (tmp_path / "runtime.db").exists()


async def test_actual_app_admits_one_run_via_scoped_native_reads_then_cancels_atomically(tmp_path):
    with native_server(tmp_path) as (wire, url, ca):
        config, values = configuration(tmp_path, native_url=url, ca=ca)
        async with application(config, values, wire) as (app, client, gateway):
            assert not wire.calls and not gateway.writes
            service = app.state.runtime
            ports = service.connections.registry.named("project")
            assert ports.ci.dispatch_lookup.__self__ is service.effects.store
            response = await client.post("/api/v1/commands", headers=auth(), json=command())
            assert response.status_code == 202, response.text
            result = response.json()
            run_id = result["run_id"]
            assert result["reason"] == "implementation_queued"
            replay = await client.post("/api/v1/commands", headers=auth(), json=command())
            assert replay.json() == result
            listing = await client.get("/api/v1/runs", headers=auth())
            assert [r["id"] for r in listing.json()["runs"]] == [run_id]
            detail = await client.get("/api/v1/runs/" + run_id, headers=auth())
            assert detail.json()["state"] == "queued"
            assert all(request.method == "GET" for request in wire.calls)
            assert not gateway.writes
            async with service.factory() as session:
                rows = (await session.scalars(select(EngineCommand))).all()
                assert len(rows) == 1 and rows[0].state == "completed" and rows[0].run_id == run_id
                assert await session.scalar(select(func.count()).select_from(EngineRun)) == 1
                assert await session.scalar(select(func.count()).select_from(EngineEffect)) == 0
            tick = await service.worker.tick()
            assert tick.execution.completed == 1
            cancelled = await client.post(
                "/api/v1/commands",
                headers=auth(),
                json=command("cancel", argument=run_id, request_id="cancel-1"),
            )
            assert cancelled.json()["reason"] == "cancelled"
            detail = await client.get("/api/v1/runs/" + run_id, headers=auth())
            assert (
                detail.json()["state"] == "cancelled" and detail.json()["cancel_requested"] is True
            )
            async with service.factory() as session:
                steps = (await session.scalars(select(EngineStep))).all()
                assert all(row.state in ("succeeded", "cancelled") for row in steps)


async def test_operator_identity_cannot_be_supplied_or_cross_scope_and_errors_hide_values(tmp_path):
    with native_server(tmp_path) as (wire, url, ca):
        config, values = configuration(tmp_path, native_url=url, ca=ca)
        async with application(config, values, wire) as (_, client, gateway):
            assert (await client.get("/api/v1/runs")).status_code == 401
            assert (
                await client.post("/api/v1/commands", headers=auth("wrong-secret"), json=command())
            ).status_code == 401
            denied = await client.post(
                "/api/v1/commands", headers=auth(), json=command(repository="other")
            )
            assert denied.status_code == 403
            injected = await client.post(
                "/api/v1/commands",
                headers=auth(),
                json=command(actor={"native_id": "99", "token": "private-never-echo"}),
            )
            assert injected.status_code == 422 and "private-never-echo" not in injected.text
            huge = await client.post("/api/v1/commands", headers=auth(), content=b"x" * 1048577)
            assert huge.status_code == 413
            assert not wire.calls and not gateway.writes


async def test_signed_bot_hook_returns202_without_command_or_effect_and_bad_signature_refuses(
    tmp_path,
):
    with native_server(tmp_path) as (wire, url, ca):
        config, values = configuration(tmp_path, native_url=url, ca=ca)
        async with application(config, values, wire) as (app, client, gateway):
            headers = {
                "X-Gitlab-Token": WEBHOOK_SECRET,
                "X-Gitlab-Event-UUID": "12345678-1234-1234-1234-123456789abc",
                "X-Gitlab-Event": "Note Hook",
            }
            document = payload(user=native.user())
            document["object_attributes"]["author_id"] = 99
            response = await client.post(
                "/hooks/ce-1", headers=headers, content=json.dumps(document)
            )
            assert response.status_code == 202 and response.json() == {
                "accepted": False,
                "commands": [],
            }
            refused = await client.post(
                "/hooks/ce-1",
                headers={**headers, "X-Gitlab-Token": "bad"},
                content=json.dumps(document),
            )
            assert refused.status_code == 400
            assert (await client.post("/hooks/unknown", content=b"{}")).status_code == 404
            assert not wire.calls and not gateway.writes
            async with app.state.runtime.factory() as session:
                assert await session.scalar(select(func.count()).select_from(EngineCommand)) == 0
                assert await session.scalar(select(func.count()).select_from(EngineRun)) == 0
                assert await session.scalar(select(func.count()).select_from(EngineEffect)) == 0


async def test_plan_and_go_use_actual_scoped_model_calls_native_effect_and_consumed_gate(tmp_path):
    from forge.store.models_runs import EngineApprovedInput
    from forge.store.models_budget import EngineUsage
    from tests.test_server_models import wire as response_wire

    with native_server(tmp_path) as (peer, url, ca):
        config, values = configuration(tmp_path, native_url=url, ca=ca)
        requests = []

        def model(request):
            requests.append(request)
            if len(requests) == 1:
                answer = {
                    "kind": "tools",
                    "calls": [
                        {
                            "tool": "read_file",
                            "repo": "primary",
                            "path": "old.txt",
                            "start_line": 1,
                            "line_count": 1,
                        }
                    ],
                }
            else:
                answer = {
                    "kind": "plan",
                    "plan": {
                        "summary": "Correct source",
                        "steps": [
                            {
                                "step_id": "change",
                                "title": "Change",
                                "instructions": "Preserve source",
                                "files": ["old.txt"],
                                "citations": [
                                    {
                                        "repository": {
                                            "provider": "gitlab",
                                            "connection_id": "ce-1",
                                            "repository_id": "123",
                                        },
                                        "source_sha": native.SHA,
                                        "path": "old.txt",
                                        "start_line": 1,
                                        "end_line": 1,
                                    }
                                ],
                            }
                        ],
                    },
                }
            return response_wire(
                body={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": json.dumps(answer)},
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
                }
            )

        def post_note(request):
            body = json.loads(request.content)["body"]
            row = native.note(ident=51, body=body)
            peer.routes["issues/7/notes"].append(row)
            peer.routes["issues/7/notes/51"] = row
            return httpx.Response(201, json=row)

        peer.mutations[("POST", "issues/7/notes")] = post_note
        async with application(config, values, peer, model_handler=model) as (app, client, gateway):
            result = (await client.post("/api/v1/commands", headers=auth(), json=command())).json()
            run_id = result["run_id"]
            service = app.state.runtime
            assert (await service.worker.tick()).execution.completed == 1
            assert (await service.worker.tick()).execution.completed == 1
            detail = (await client.get("/api/v1/runs/" + run_id, headers=auth())).json()
            assert detail["state"] == "awaiting_approval", detail
            assert len(requests) == 2 and gateway.writes == ["/key/generate", "/key/block"]
            assert len([request for request in peer.calls if request.method == "POST"]) == 1
            approved = await client.post(
                "/api/v1/commands",
                headers=auth(),
                json=command("go", argument=run_id, request_id="go-1"),
            )
            assert approved.json()["reason"] == "approval_queued", approved.text
            assert (await service.worker.tick()).execution.completed == 1
            detail = (await client.get("/api/v1/runs/" + run_id, headers=auth())).json()
            assert detail["state"] == "running"
            async with service.factory() as session:
                row = await session.scalar(select(EngineApprovedInput))
                assert row is not None and row.run_id == run_id and row.revision == 1
                rows = (await session.scalars(select(EngineUsage))).all()
                assert len(rows) == 2 and all(row.final for row in rows)
                effects = (await session.scalars(select(EngineEffect))).all()
                assert all(row.state == "committed" for row in effects)


async def test_expired_command_owner_cannot_admit_after_native_reads(tmp_path):
    from datetime import timedelta
    from forge.command_values import CommandEnvelope
    from forge.ingress_values import Command
    from forge.port_values import SubjectRef
    from forge.runtime.actions import InitialActions
    from forge.store.commands import CommandStore
    from forge.store.records import StaleLease

    with native_server(tmp_path) as (wire, url, ca):
        config, values = configuration(tmp_path, native_url=url, ca=ca)
        async with application(config, values, wire) as (app, _, gateway):
            service = app.state.runtime
            store = CommandStore(service.factory)
            clock = service.budget.clock
            actor = service.operators.authenticate("Bearer " + OPERATOR_TOKEN).actor("project")
            envelope = CommandEnvelope(
                source="api",
                native_id="expired-command",
                actor=actor,
                target=SubjectRef(repo=service.connections.registry.named("project").ref, number=7),
                command=Command(kind="implement"),
            )
            saved = await store.accept(envelope, now=clock.now())
            lease = await store.claim(
                "expired-owner", now=clock.now(), seconds=1, command_id=saved.id
            )
            clock.instant += timedelta(seconds=2)
            with pytest.raises(StaleLease):
                await InitialActions(
                    config, service.connections.registry, service.factory, clock
                ).implement(lease, envelope, None)
            async with service.factory() as session:
                assert await session.scalar(select(func.count()).select_from(EngineRun)) == 0
                assert await session.scalar(select(func.count()).select_from(EngineStep)) == 0
            assert wire.calls and not gateway.writes


async def test_admission_decision_recovers_without_research_after_crash_before_ack(
    tmp_path, monkeypatch
):
    from datetime import timedelta
    from forge.store.commands import CommandStore

    with native_server(tmp_path) as (wire, url, ca):
        config, values = configuration(tmp_path, native_url=url, ca=ca)
        async with application(config, values, wire) as (app, client, _):

            async def lost_ack(*args, **kwargs):
                raise RuntimeError("synthetic crash after atomic command admission")

            original = CommandStore.resolve
            monkeypatch.setattr(CommandStore, "resolve", lost_ack)
            with pytest.raises(RuntimeError, match="synthetic crash"):
                await client.post("/api/v1/commands", headers=auth(), json=command())
            service = app.state.runtime
            async with service.factory() as session:
                row = await session.scalar(select(EngineCommand))
                assert row.state == "resolved" and row.run_id is not None
                assert await session.scalar(select(func.count()).select_from(EngineRun)) == 1
            previous = len(wire.calls)
            monkeypatch.setattr(CommandStore, "resolve", original)
            service.budget.clock.instant += timedelta(seconds=121)
            assert await service.commands.tick() == 1
            assert len(wire.calls) == previous
            async with service.factory() as session:
                row = await session.scalar(select(EngineCommand))
                assert row.state == "completed"
                assert await session.scalar(select(func.count()).select_from(EngineRun)) == 1
