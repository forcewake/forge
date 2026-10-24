"""Liveness and readiness report different facts on the sole application."""

import httpx

import forge
from sqlalchemy import text
from tests.operator_native_fixture import operator_native as operator_native
from tests.test_budget_engine import NativeAdmin


async def test_health_returns_ok_when_all_services_up(operator_native):
    env = operator_native
    response = await env.client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"service": "forge", "version": forge.__version__}
    assert (await env.client.get("/readyz")).json() == {
        "schema": "checked",
        "native_budget": "verified",
    }
    assert not env.peer.calls and not env.gateway.writes
    assert (await env.client.get("/health")).status_code == 404


async def test_health_includes_database_status(operator_native):
    env = operator_native
    assert (await env.client.get("/readyz")).status_code == 200
    async with env.service.engine.begin() as connection:
        await connection.execute(text("DROP TABLE schema_installation"))
    response = await env.client.get("/readyz")
    assert response.status_code == 503
    assert "schema" in response.json()["detail"]
    assert (await env.client.get("/healthz")).status_code == 200
    assert not env.gateway.writes


async def test_health_degraded_when_litellm_unreachable(operator_native):
    env = operator_native
    env.gateway.unavailable = True
    response = await env.client.get("/readyz")
    assert response.status_code == 503
    assert "native model guard unavailable" in response.json()["detail"]
    assert (await env.client.get("/healthz")).status_code == 200
    assert not env.gateway.writes


async def test_health_degraded_when_litellm_returns_error(operator_native, monkeypatch):
    env = operator_native

    def failure(self, request):
        return httpx.Response(503, json={"secret": "synthetic-response-secret"})

    monkeypatch.setattr(NativeAdmin, "__call__", failure)
    response = await env.client.get("/readyz")
    assert response.status_code == 503
    assert "synthetic-response-secret" not in response.text
    assert not env.peer.calls and not env.gateway.writes
