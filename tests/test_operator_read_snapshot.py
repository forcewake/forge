"""An operator read is one durable world and never an empty success on a failed source."""

import asyncio
import json

import httpx
from sqlalchemy import text

from forge.port_values import Refused
from forge.store.command_runs import CommandRuns
from forge.store.models_execution import EngineEffect, EngineCandidate
from tests.operator_evidence_fixture import candidate, control, edit_run, effect
from tests.operator_surface_fixture import surface as surface, detail, headers, seed_run


async def test_one_read_snapshot_does_not_mix_a_later_writer(surface, monkeypatch):
    import forge.store.command_runs as snapshots

    async with surface.service.engine.connect() as connection:
        assert (await connection.scalar(text("PRAGMA journal_mode=WAL"))) == "wal"
    run = await seed_run(surface)
    pending = await effect(surface, run)
    entered, release = asyncio.Event(), asyncio.Event()
    original = snapshots.read_evidence

    async def pause(session, selected):
        entered.set()
        await release.wait()
        return await original(session, selected)

    monkeypatch.setattr(snapshots, "read_evidence", pause)
    reader = CommandRuns(surface.service.factory)
    task = asyncio.create_task(reader.snapshot(run))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        await edit_run(surface, run, state="blocked", version=2)
        async with surface.service.factory() as session, session.begin():
            row = await session.get(EngineEffect, pending.id)
            row.state, row.outcome_json = (
                "failed",
                Refused(reason="native_refused", evidence="{}").model_dump_json(),
            )
        await control(surface, run)
    finally:
        release.set()
    before = json.loads(await asyncio.wait_for(task, 10))
    monkeypatch.setattr(snapshots, "read_evidence", original)
    after = json.loads(await reader.snapshot(run))
    assert (
        before["run_version"],
        before["state"],
        before["effects"][0]["state"],
        before["controls"]["latest"],
    ) == (1, "queued", "unknown", [])
    assert (after["run_version"], after["state"], after["effects"][0]["state"]) == (
        2,
        "blocked",
        "failed",
    )
    assert len(after["controls"]["latest"]) == 1


async def test_api_retries_an_old_run_version_before_returning_its_snapshot(surface, monkeypatch):
    run = await seed_run(surface)
    reader = surface.service.projection.commands
    original = reader.snapshot
    moved = False

    async def move(selected):
        nonlocal moved
        if not moved:
            moved = True
            await edit_run(surface, run, state="blocked", reason="fresh authority", version=2)
        return await original(selected)

    monkeypatch.setattr(reader, "snapshot", move)
    response = await detail(surface, run)
    document = response.json()
    assert response.status_code == 200
    assert document["version"] == document["evidence"]["run_version"] == 2
    assert document["state"] == document["evidence"]["state"] == "blocked"


async def test_corrupt_evidence_is_unavailable_instead_of_empty_or_unknown_success(surface):
    run = await seed_run(surface)
    saved = await candidate(surface, run)
    async with surface.service.factory() as session, session.begin():
        row = await session.get(EngineCandidate, saved.id)
        row.validation_json = '{"verification":{"status":"private-invalid-value"}}'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(surface.app, raise_app_exceptions=False),
        base_url="https://forge.invalid",
    ) as client:
        response = await client.get("/api/v1/runs/" + run.id, headers=headers())
    assert response.status_code == 503 and response.json() == {"detail": "service unavailable"}
    assert "private-invalid-value" not in response.text and "candidates" not in response.text
