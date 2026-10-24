"""Actual authenticated ASGI commands raise only the approved run's native gateway cap."""

import json
from datetime import timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select

from forge.domain.budget import Resources
from forge.store.lane_work import LaneWorkStore
from forge.store.models_budget import EngineBudgetAmendment
from forge.store.models_execution import EngineEffect
from forge.store.steps import StepStore
from tests import gitlab_engine_fixture as native
from tests.runtime_fixture import configuration, native_server
from tests.test_runtime_app import application, auth, command
from tests.test_server_model_services import text_wire


async def test_authenticated_api_amendment_uses_trusted_actor_current_scope_and_shared_journal(
    tmp_path,
):
    with native_server(tmp_path) as (peer, url, ca):
        config, values = configuration(tmp_path, native_url=url, ca=ca)
        calls = []

        def model(request):
            calls.append(request)
            if len(calls) == 1:
                value = {
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
                value = {
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
            return text_wire(value)

        def note(request):
            row = native.note(ident=51, body=json.loads(request.content)["body"])
            peer.routes["issues/7/notes"].append(row)
            peer.routes["issues/7/notes/51"] = row
            return httpx.Response(201, json=row)

        peer.mutations[("POST", "issues/7/notes")] = note
        async with application(config, values, peer, model_handler=model) as (app, client, gateway):
            queued = (await client.post("/api/v1/commands", headers=auth(), json=command())).json()
            run_id, service = queued["run_id"], app.state.runtime
            for _ in range(2):
                assert (await service.worker.tick()).execution.completed == 1
            result = await client.post(
                "/api/v1/commands",
                headers=auth(),
                json=command("go", argument=run_id, request_id="go"),
            )
            assert result.json()["reason"] == "approval_queued"
            assert (await service.worker.tick()).execution.completed == 1
            (lease,) = await StepStore(service.factory).claim_due(
                "owned-dispatch", service.budget.clock.now(), 60
            )
            approved = await LaneWorkStore(service.factory).approved(lease)
            grant = await service.budget.prepare(
                lease,
                approved.spec,
                "implementation",
                Resources(usd=Decimal("2"), calls=4, tokens=2000),
                Resources(usd=Decimal(".72"), calls=1, tokens=1),
                service.budget.clock.now().replace(microsecond=0) + timedelta(seconds=120),
            )
            item = command(
                "amend",
                argument=f"{run_id} implementation usd=1 calls=2 tokens=100 reason=continue bounded work",
                request_id="amend-one",
            )
            response = await client.post("/api/v1/commands", headers=auth(), json=item)
            assert (
                response.status_code == 202
                and response.json()["reason"] == "budget_amendment_authorized"
            ), response.text
            assert (
                await client.post("/api/v1/commands", headers=auth(), json=item)
            ).json() == response.json()
            detail = (await client.get("/api/v1/runs/" + run_id, headers=auth())).json()
            assert detail["evidence"]["budget_amendments"]["latest"][0]["state"] == "applied"
            async with service.factory() as session:
                row = await session.scalar(select(EngineBudgetAmendment))
                assert row.actor == "42" and row.state == "applied"
                effect = await session.scalar(
                    select(EngineEffect).where(EngineEffect.kind == "gw_update")
                )
                assert (
                    effect.command_id is not None
                    and effect.step_id is None
                    and effect.run_id == run_id
                )
            key = await service.budget.keys.get(grant.value.alias)
            assert key.reference.allowance == Resources(usd=Decimal("3"), calls=6, tokens=2100)
            assert gateway.writes.count("/key/update") == 1 and len(calls) == 2
            assert len([request for request in peer.calls if request.method == "POST"]) == 1


def test_signed_github_note_carries_the_exact_bounded_amendment_command():
    from forge.providers.github.ingress import GitHubIngress
    from tests.test_github_engine_ingress import payload, signed, SECRET
    from tests.test_github_rest import BOT

    document = payload()
    argument = "a" * 32 + " implementation usd=1 calls=0 tokens=0 reason=explicit increment"
    document["comment"]["body"] = "/amend " + argument
    event = GitHubIngress("conn-1", SECRET, repositories={"123": 900}, bot=BOT).parse(
        *signed(document)
    )
    assert event.commands[0].kind == "amend" and event.commands[0].argument == argument


def test_signed_gitlab_note_carries_the_exact_bounded_amendment_command():
    from forge.providers.gitlab.ingress import GitLabIngress
    from tests.test_gitlab_engine_ingress import payload, signed, SECRET

    document = payload()
    argument = "a" * 32 + " closing usd=0 calls=1 tokens=0 reason=explicit increment"
    document["object_attributes"]["note"] = "/amend " + argument
    event = GitLabIngress("ce-1", SECRET, bots={"123": native.BOT}).parse(*signed(document))
    assert event.commands[0].kind == "amend" and event.commands[0].argument == argument
