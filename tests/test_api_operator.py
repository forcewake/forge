"""Retained operator lessons through the sole authenticated API and actual SQL evidence."""

import json

import httpx
from sqlalchemy import select

from forge.command_values import CommandResult, render
from forge.domain.states import Handler
from forge.runtime.app import create_app
from forge.store.command_runs import CommandRuns
from forge.store.models_execution import EngineCandidate, EngineStep
from forge.store.records import StepRequest
from forge.store.runs import RunStore
from forge.store.steps import StepStore
from tests.operator_evidence_fixture import (
    SHA,
    candidate,
    checkpoint,
    control,
    edit_run,
    round_child,
)
from tests.operator_surface_fixture import (
    surface as surface,
    detail,
    headers,
    listing,
    seed_run,
    table_counts,
)
from tests.runtime_fixture import configuration


async def test_no_secret_means_503_never_open(tmp_path):
    config, _ = configuration(tmp_path)
    app = create_app(config)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://forge.invalid"
    ) as client:
        response = await client.get("/api/v1/runs", headers=headers())
    assert response.status_code == 503


async def test_missing_bearer_is_401(surface):
    response = await surface.client.get("/api/v1/runs")
    assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"


async def test_no_declared_scope_is_400(surface):
    # Scope is entirely server-bound; a client cannot introduce a scope declaration.
    assert (await listing(surface, subject="")).status_code == 400
    assert (await listing(surface)).status_code == 200


async def test_a_malformed_subject_reference_is_400(surface):
    response = await listing(surface, subject="not-a-native-subject")
    assert response.status_code == 400 and "not-a-native-subject" not in response.text


async def test_declaring_both_grant_families_is_400(surface):
    assert (await listing(surface, subject="alpha", repo="new/repository")).status_code == 400


async def test_a_token_for_alpha_cannot_declare_betas_scope(surface):
    run = await seed_run(surface, "beta")
    assert (await detail(surface, run, "alpha")).status_code == 404
    response = await surface.client.post(
        "/api/v1/commands",
        headers=headers("alpha"),
        json={
            "repository": "beta",
            "number": 7,
            "request_id": "foreign",
            "command": {"kind": "cancel", "argument": run.id},
        },
    )
    assert response.status_code == 403
    assert (await RunStore(surface.service.factory).load(run.id)).cancel_requested is False


async def test_a_legacy_name_token_is_not_a_canonical_subject_token(surface):
    response = await surface.client.get(
        "/api/v1/runs", headers={"Authorization": "Bearer new/repository"}
    )
    assert response.status_code == 401


async def test_list_and_detail_are_subject_scoped(surface):
    own, foreign = await seed_run(surface), await seed_run(surface, "alpha")
    assert [row["id"] for row in (await listing(surface)).json()["runs"]] == [own.id]
    assert (await detail(surface, own)).status_code == 200
    assert (await detail(surface, foreign)).json() == (await detail(surface, "0" * 32)).json()


async def test_the_list_renders_thin_state_summaries(surface):
    run = await seed_run(surface, reason="waiting for native CI")
    row = (await listing(surface)).json()["runs"][0]
    assert (
        row["id"] == run.id
        and row["state"] == "queued"
        and row["reason"] == "waiting for native CI"
    )
    assert set(row) == {
        "id",
        "provider",
        "connection_id",
        "repository_id",
        "subject_kind",
        "subject_id",
        "state",
        "root_run_id",
        "parent_run_id",
        "attempt",
        "generation",
        "cancel_requested",
        "version",
        "failed_handler",
        "reason",
    }
    assert not ({"document_json", "payload_json", "credentials", "prompts"} & set(row))


async def test_the_detail_renders_the_projection_contract(surface):
    run = await seed_run(surface)
    document = (await detail(surface, run)).json()
    assert document["evidence"] == json.loads(
        await CommandRuns(surface.service.factory).snapshot(run)
    )
    assert document["version"] == document["evidence"]["run_version"] == 1
    assert document["evidence"]["acceptance"] == {"state": "unobserved"}


async def test_detail_and_reader_preserve_failed_attempt_history(surface):
    run = await seed_run(surface)
    async with surface.service.factory() as session, session.begin():
        row = await session.scalar(select(EngineStep).where(EngineStep.run_id == run.id))
        row.state = "failed"
    await edit_run(surface, run, attempt=2, version=2)
    await StepStore(surface.service.factory).enqueue(
        StepRequest(
            run.id, Handler.DISPATCH, run.id + ":attempt2", "{}", surface.service.budget.clock.now()
        )
    )
    history = (await detail(surface, run)).json()["evidence"]["steps"]
    assert history["complete"] and {
        (row["attempt"], row["state"]) for row in history["latest"]
    } == {(1, "failed"), (2, "scheduled")}


async def test_at04_the_http_render_matches_the_reader_projection(surface):
    run = await seed_run(surface)
    await candidate(surface, run, verification="passed")
    await edit_run(surface, run, attempt=2, version=2)
    await candidate(surface, run, attempt=2, sha="e" * 40)
    history = (await detail(surface, run)).json()["evidence"]["candidates"]
    assert history[0]["current"] and history[0]["verification"] is None
    assert not history[1]["current"] and history[1]["verification"]["status"] == "passed"
    assert history[1]["verification"]["tested_sha"] == SHA


async def test_credential_looking_values_are_redacted_on_every_surface(surface):
    run = await seed_run(surface)
    for reason in (
        "Authorization: opaque-private-value",
        "https://user:private@host.invalid/path",
        "private_key=opaque-private",
        "Bearer ghp_abcdefgh12345678",
    ):
        await edit_run(surface, run, reason=reason)
        listed, document = await listing(surface), await detail(surface, run)
        assert listed.json()["runs"][0]["reason"] == document.json()["reason"] == "[redacted]"
        assert document.json()["evidence"]["reason"] == "[redacted]"
        assert reason not in listed.text + document.text


async def test_rendering_performs_zero_writes(surface):
    run = await seed_run(surface)
    await checkpoint(surface, run)
    await control(surface, run)
    before = await table_counts(surface)
    assert (await listing(surface)).status_code == (await detail(surface, run)).status_code == 200
    assert await table_counts(surface) == before
    assert not surface.peer.calls and not surface.gateway.writes


async def test_an_operator_bearer_cannot_drive_a_guarded_control_route(surface):
    run = await seed_run(surface)
    row = await control(surface, run)
    response = await surface.client.get("/lane/v1/controls", headers=headers())
    assert response.status_code == 401
    assert (await detail(surface, run)).json()["evidence"]["controls"]["latest"][0]["id"] == row.id
    assert (await detail(surface, run)).json()["evidence"]["controls"]["latest"][0][
        "state"
    ] == "pending"
    response = await surface.client.post(
        "/api/v1/commands",
        headers=headers("observer"),
        json={
            "repository": "project",
            "number": 7,
            "request_id": "observer-cancel",
            "command": {"kind": "cancel", "argument": run.id},
        },
    )
    assert response.status_code == 202 and response.json()["reason"] == "actor_not_authorized"
    assert not (await RunStore(surface.service.factory).load(run.id)).cancel_requested


async def test_a_stale_action_is_refused_by_the_real_guarded_route(surface):
    run = await seed_run(surface, state="running")
    assert (await detail(surface, run)).json()["state"] == "running"
    await edit_run(surface, run, state="blocked", reason="lane_paused", version=2)
    response = await surface.client.post(
        "/api/v1/commands",
        headers=headers(),
        json={
            "repository": "project",
            "number": 7,
            "request_id": "stale-pause",
            "command": {"kind": "pause", "argument": ""},
        },
    )
    assert response.status_code == 202 and response.json()["reason"] == "lane_not_active"
    assert (await detail(surface, run)).json()["state"] == "blocked"


async def test_decide_refuses_a_stale_hint_with_the_current_state_and_safe_alternative(surface):
    run = await seed_run(surface, state="running")
    await edit_run(
        surface, run, state="cancelled", cancel_requested=True, cancellation_generation=1, version=2
    )
    response = await surface.client.post(
        "/api/v1/commands",
        headers=headers(),
        json={
            "repository": "project",
            "number": 7,
            "request_id": "stale-retry",
            "command": {"kind": "retry", "argument": run.id},
        },
    )
    assert response.status_code == 202 and response.json()["reason"] == "cancelled"
    assert (await detail(surface, run)).json()["cancel_requested"]
    assert "start new work" in render(CommandResult.model_validate(response.json()))


async def test_native_comment_lines_and_the_api_render_agree(surface):
    run = await seed_run(surface)
    await candidate(surface, run, verification="unknown")
    document = (await detail(surface, run)).json()
    snapshot = await CommandRuns(surface.service.factory).snapshot(run)
    body = render(
        CommandResult(status="observed", reason=run.state.value, run_id=run.id, data_json=snapshot)
    )
    assert json.loads(snapshot) == document["evidence"] and snapshot in body and run.id in body


async def test_the_detail_renders_the_recovery_section(surface):
    run = await seed_run(surface, state="blocked", reason="lane_paused")
    saved = await checkpoint(surface, run)
    await control(surface, run, state="acknowledged", checkpoint_id=saved.id)
    evidence = (await detail(surface, run)).json()["evidence"]
    assert evidence["controls"]["latest"][0]["acknowledged_status"] == "applied"
    assert evidence["checkpoints"]["latest"][0]["blob_availability"] == "unobserved"
    assert evidence["lane"] is None and evidence["acceptance"]["state"] == "unobserved"


async def test_the_detail_renders_the_five_linked_delivery_facts(surface):
    run = await seed_run(surface)
    await candidate(surface, run, verification="passed", review="approve")
    child, row = await round_child(surface, run)
    evidence = (await detail(surface, run)).json()["evidence"]
    assert evidence["rounds"]["latest"][0]["child_run_id"] == child.id
    assert evidence["rounds"]["latest"][0]["id"] == row.id
    current = evidence["candidates"][0]
    assert (
        current["sha"] == SHA
        and current["verification"]["bound_to_candidate"]
        and current["review"]["bound_to_candidate"]
    )
    assert evidence["acceptance"]["state"] == "unobserved"


async def test_the_child_detail_projects_its_own_round(surface):
    run = await seed_run(surface)
    await candidate(surface, run)
    child, row = await round_child(surface, run)
    document = (await detail(surface, child)).json()
    assert document["parent_run_id"] == run.id and document["root_run_id"] == run.id
    assert document["evidence"]["candidates"] == []
    assert document["evidence"]["rounds"]["latest"][0]["id"] == row.id


async def test_the_rounds_section_is_selectable_and_refused_when_unknown(surface):
    run = await seed_run(surface)
    for selection in ("run,rounds", "run,spurious"):
        assert (await detail(surface, run, sections=selection)).status_code == 400
    assert (await detail(surface, run)).json()["evidence"]["rounds"] == {
        "latest": [],
        "complete": True,
    }


async def test_a_foreign_subject_cannot_list_or_guess_the_round_lineage(surface):
    run = await seed_run(surface)
    child, row = await round_child(surface, run)
    foreign = await seed_run(surface, "alpha")
    assert [r["id"] for r in (await listing(surface, "alpha")).json()["runs"]] == [foreign.id]
    for hidden in (run, child):
        response = await detail(surface, hidden, "alpha")
        assert response.status_code == 404 and row.id not in response.text


async def test_one_snapshot_consistent_across_api_comment_and_native_lines(surface):
    run = await seed_run(surface)
    await candidate(surface, run, verification="passed")
    _, row = await round_child(surface, run)
    snapshot = await CommandRuns(surface.service.factory).snapshot(run)
    document = (await detail(surface, run)).json()
    body = render(
        CommandResult(
            status="observed", reason=document["state"], run_id=run.id, data_json=snapshot
        )
    )
    assert document["evidence"] == json.loads(snapshot)
    assert row.id in body and SHA in body and '"state":"unobserved"' in body


async def test_detail_stays_allowlisted_and_value_free_with_rounds_present(surface):
    run = await seed_run(surface)
    saved = await candidate(surface, run, review="approve")
    await round_child(surface, run)
    async with surface.service.factory() as session, session.begin():
        row = await session.get(EngineCandidate, saved.id)
        document = json.loads(row.manifest_json)
        document["terminal_reason"] = "glpat-private-review-prompt"
        row.manifest_json = json.dumps(document)
    response = await detail(surface, run)
    assert response.status_code == 200
    assert not any(
        value in response.text
        for value in (
            "glpat-private",
            "raw model text",
            "payload_json",
            "manifest_json",
            "resume_session",
        )
    )


async def test_removed_support_export_has_no_operator_route(surface):
    from forge.runtime.operator_api import router

    assert {route.path for route in router.routes} == {
        "/api/v1/runs",
        "/api/v1/runs/{run_id}",
        "/api/v1/commands",
    }
    run = await seed_run(surface)
    assert (
        await surface.client.get("/api/v1/runs/" + run.id + "/support-bundle", headers=headers())
    ).status_code == 404
