"""Durable run read and isolation contracts on the actual operator API."""

from tests.fixtures.operator_http import (
    operator_app as operator_app,
    operator_client as operator_client,
)


from sqlalchemy import select, func
import pytest
from forge.durable.models import FlowRun, ActionLog, PublicationIntent
from tests.fixtures.operator_http import (
    SUBJECT_A,
    SUBJECT_B,
    scope,
    seed_run,
)


@pytest.mark.parametrize("suffix", ["", "/support-bundle"])
async def test_a_foreign_run_is_indistinguishable_from_a_missing_run(
    operator_app, operator_client, suffix
):
    run_id = "a" * 32
    params, headers = scope()
    path = f"/operator/runs/{run_id}{suffix}"
    absent = await operator_client.get(path, params=params, headers=headers)
    await seed_run(operator_app, run_id, subject=SUBJECT_B)
    denied = await operator_client.get(path, params=params, headers=headers)
    assert denied.status_code == absent.status_code == 404
    assert denied.content == absent.content


async def test_lists_filter_subjects_before_serializing_runs(operator_app, operator_client):
    own = await seed_run(operator_app)
    foreign = await seed_run(operator_app, "b" * 32, SUBJECT_B)
    params, headers = scope()
    result = await operator_client.get("/operator/runs", params=params, headers=headers)
    assert result.status_code == 200
    assert [row["run_id"] for row in result.json()["runs"]] == [own]
    assert foreign not in result.text and SUBJECT_B.native_id not in result.text


async def test_scope_filtering_cannot_be_widened_by_a_query_override(operator_app, operator_client):
    await seed_run(operator_app, subject=SUBJECT_B)
    params, headers = scope((SUBJECT_B,), signed=(SUBJECT_A,))
    result = await operator_client.get("/operator/runs", params=params, headers=headers)
    assert result.status_code == 403


@pytest.mark.parametrize("limit", [0, -1, 10001])
async def test_list_bounds_cannot_be_expanded_by_the_caller(operator_client, limit):
    params, headers = scope()
    result = await operator_client.get(
        "/operator/runs", params={**params, "limit": limit}, headers=headers
    )
    assert result.status_code == 422


async def test_own_run_reads_the_persisted_candidate_without_advancing_it(
    operator_app, operator_client
):
    run_id = await seed_run(operator_app)

    async def counts():
        async with operator_app.state.session_factory() as session:
            return [
                await session.scalar(select(func.count()).select_from(model))
                for model in (FlowRun, ActionLog, PublicationIntent)
            ]

    before = await counts()
    params, headers = scope()
    result = await operator_client.get(f"/operator/runs/{run_id}", params=params, headers=headers)
    assert result.status_code == 200
    assert result.json()["run_id"] == run_id
    assert result.json()["subject_id"] == SUBJECT_A.subject_id()
    assert await counts() == before
    async with operator_app.state.session_factory() as session:
        row = await session.get(FlowRun, run_id)
        assert row.status == "waiting_ci" and row.candidate_shas == ["c" * 40]


async def test_a_cursor_is_bound_to_the_serving_subject_set(operator_app, operator_client):
    await seed_run(operator_app)
    await seed_run(operator_app, "b" * 32)
    params, headers = scope()
    first = await operator_client.get(
        "/operator/runs", params={**params, "limit": 1}, headers=headers
    )
    cursor = first.json()["next_cursor"]
    assert cursor
    foreign_params, foreign_headers = scope((SUBJECT_B,))
    second = await operator_client.get(
        "/operator/runs", params={**foreign_params, "cursor": cursor}, headers=foreign_headers
    )
    assert second.status_code == 400


async def test_durable_metrics_do_not_depend_on_a_removed_mcp_tool(operator_app, operator_client):
    await seed_run(operator_app, status="ready_for_human")
    result = await operator_client.get("/metrics.prometheus")
    assert result.status_code == 200
    assert 'forge_delivery_ladder{stage="ready_for_human"} 1' in result.text
