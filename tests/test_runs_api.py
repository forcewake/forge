"""F30 — the /runs read model, its optional bearer token, and /metrics.prometheus.

- GET /runs: durable flow runs newest-first, limit/offset (cap 100).
- GET /runs/{id}: detail + steps + a compact evidence summary (bulky
  evidence fields never leave the database).
- FORGE_API_READ_TOKEN set → Bearer required; unset → open (dev default).
- /metrics.prometheus: Prometheus exposition, correct content type and
  series names; never 5xx even without Redis.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from forge.config import Settings
from forge.database import dispose_engine, reset_engine
from forge.durable.models import FlowRun, StepRun
from forge.main import create_app
from tests.conftest import TEST_WEBHOOK_SECRET

_NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


def _settings(**overrides) -> Settings:
    return Settings(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test-token"),
        GITLAB_WEBHOOK_SECRET=SecretStr(TEST_WEBHOOK_SECRET),
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        LITELLM_URL="http://litellm:4000",
        LOG_LEVEL="DEBUG",
        **overrides,
    )


async def _seed_run(
    app,
    run_id: str,
    *,
    status: str = "ready_for_human",
    reason: str | None = None,
    issue_iid: int = 7,
    created_at: datetime = _NOW,
    evidence: dict | None = None,
    commit_cycle: int = 1,
) -> None:
    async with app.state.session_factory() as session:
        session.add(
            FlowRun(
                id=run_id,
                project_id=42,
                issue_iid=issue_iid,
                status=status,
                status_reason=reason,
                commit_cycle=commit_cycle,
                evidence=evidence,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        await session.commit()


async def _seed_step(app, run_id: str, step_name: str = "start_run", status: str = "succeeded"):
    async with app.state.session_factory() as session:
        session.add(
            StepRun(
                flow_run_id=run_id,
                step_name=step_name,
                status=status,
                attempt=1,
                finished_at=None,
            )
        )
        await session.commit()


# ---------------------------------------------------------------- GET /runs


async def test_runs_lists_durable_runs_newest_first(client, app):
    await _seed_run(app, "older-run", created_at=_NOW - timedelta(hours=1))
    await _seed_run(
        app, "newer-run", status="waiting_ci", reason="pipeline", commit_cycle=2, created_at=_NOW
    )

    resp = await client.get("/runs", params={"limit": 10})

    assert resp.status_code == 200
    body = resp.json()
    runs = body["runs"]
    assert [r["id"] for r in runs] == ["newer-run", "older-run"]
    newest = runs[0]
    for field in (
        "id",
        "project_id",
        "issue_iid",
        "status",
        "status_reason",
        "commit_cycle",
        "created_at",
        "updated_at",
    ):
        assert field in newest
    assert newest["status"] == "waiting_ci"
    assert newest["status_reason"] == "pipeline"
    assert newest["commit_cycle"] == 2
    assert newest["project_id"] == 42
    assert newest["issue_iid"] == 7


async def test_runs_offset_skips_rows(client, app):
    await _seed_run(app, "run-1", created_at=_NOW - timedelta(hours=2))
    await _seed_run(app, "run-2", created_at=_NOW - timedelta(hours=1))
    await _seed_run(app, "run-3", created_at=_NOW)

    resp = await client.get("/runs", params={"limit": 1, "offset": 1})

    assert resp.status_code == 200
    assert [r["id"] for r in resp.json()["runs"]] == ["run-2"]


async def test_runs_limit_is_capped_at_100(client, app):
    for i in range(105):
        await _seed_run(app, f"run-{i:03d}", created_at=_NOW + timedelta(seconds=i))

    # The cap: at most 100 per page even when more runs exist.
    resp = await client.get("/runs", params={"limit": 100})
    assert resp.status_code == 200
    assert len(resp.json()["runs"]) == 100

    # Over the cap is a validation error, not a silent clamp.
    resp = await client.get("/runs", params={"limit": 500})
    assert resp.status_code == 422


async def test_run_detail_includes_steps_and_evidence_summary(client, app):
    evidence = {
        "backend": "ci_harness:claude-code",
        "plan": {"digest": "d" * 64, "summary": "Add widget", "files_hint": ["a.py", "b.py"]},
        "pipeline": {
            "id": 91,
            "url": "https://gitlab.test/p/91",
            "status": "success",
            "sha": "c" * 40,
        },
        "review": {
            "verdict": "approve",
            "sha": "c" * 40,
            "summary": "fine",
            "findings": [{"path": "a.py", "line": 1, "severity": "info", "message": "x" * 500}],
        },
        "harness": {
            "handle": {"driver": "claude-code", "big": "y" * 4096},
            "pipeline_id": 91,
            "job_id": 7711,
            "branch": "factory/7/abc",
        },
    }
    await _seed_run(app, "full-run", evidence=evidence)
    await _seed_step(app, "full-run", "start_run", "succeeded")
    await _seed_step(app, "full-run", "execute_step", "running")

    resp = await client.get("/runs/full-run")

    assert resp.status_code == 200
    detail = resp.json()
    assert detail["id"] == "full-run"
    assert detail["status"] == "ready_for_human"

    # Steps come from step_runs, ordered.
    assert [s["step_name"] for s in detail["steps"]] == ["start_run", "execute_step"]
    assert detail["steps"][0]["status"] == "succeeded"
    assert "id" in detail["steps"][0] and "attempt" in detail["steps"][0]

    # Evidence summary keeps the small proof fields…
    ev = detail["evidence"]
    assert ev["backend"] == "ci_harness:claude-code"
    assert ev["plan"]["digest"] == "d" * 64
    assert ev["pipeline"]["status"] == "success"
    assert ev["review"]["verdict"] == "approve"
    assert ev["harness"]["job_id"] == 7711
    # …and drops the bulky ones: files_hint, findings, handle.
    assert "files_hint" not in ev["plan"]
    assert "findings" not in ev["review"]
    assert "handle" not in ev["harness"]


async def test_run_detail_404_for_unknown_run(client):
    resp = await client.get("/runs/does-not-exist")
    assert resp.status_code == 404


# ------------------------------------------------- FORGE_API_READ_TOKEN


@pytest.fixture()
async def authed_app_client():
    reset_engine()
    application = create_app(settings=_settings(FORGE_API_READ_TOKEN=SecretStr("read-token")))
    async with application.router.lifespan_context(application):
        await _seed_run(application, "secret-run")
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    await dispose_engine()


async def test_runs_require_bearer_when_token_configured(authed_app_client):
    resp = await authed_app_client.get("/runs")
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"

    resp = await authed_app_client.get("/runs", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401

    # Unknown run behind the token must 401 first — never leak existence.
    resp = await authed_app_client.get("/runs/nope", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


async def test_runs_accept_correct_bearer_token(authed_app_client):
    headers = {"Authorization": "Bearer read-token"}
    resp = await authed_app_client.get("/runs", headers=headers)
    assert resp.status_code == 200
    assert [r["id"] for r in resp.json()["runs"]] == ["secret-run"]

    resp = await authed_app_client.get("/runs/secret-run", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == "secret-run"


async def test_runs_open_when_token_unset(client, app):
    """Dev default: no FORGE_API_READ_TOKEN → no auth on /runs."""
    await _seed_run(app, "open-run")

    resp = await client.get("/runs")
    assert resp.status_code == 200
    assert [r["id"] for r in resp.json()["runs"]] == ["open-run"]


# ---------------------------------------------------- /metrics.prometheus


async def test_metrics_prometheus_content_type_and_series(client, app):
    await _seed_run(app, "prom-run", status="failed")

    resp = await client.get("/metrics.prometheus")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "version=4.0.4" in resp.headers["content-type"]
    body = resp.text
    # A couple of expected series (gauges + counters).
    assert "forge_queue_depth " in body
    assert "forge_dlq_depth " in body
    assert "forge_workers_active " in body
    assert "forge_tasks_processed_total " in body
    assert "forge_tasks_failed_total " in body
    assert 'forge_runs_by_status{status="failed"} 1' in body


async def test_metrics_prometheus_without_redis_reports_zero_queue(client, app):
    app.state.redis_manager = None
    app.state.task_queue = None

    resp = await client.get("/metrics.prometheus")

    assert resp.status_code == 200  # never 5xx, unlike the JSON /metrics
    assert "forge_queue_depth 0" in resp.text
