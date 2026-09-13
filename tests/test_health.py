from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_returns_ok_when_all_services_up(client: AsyncClient):
    """Health returns ok when DB and LiteLLM are reachable."""
    mock_response = httpx.Response(200, json={"status": "ok"})
    with patch("forge.gateway.router.httpx.AsyncClient") as MockHTTP:
        mock_hc = AsyncMock()
        mock_hc.get.return_value = mock_response
        mock_hc.__aenter__ = AsyncMock(return_value=mock_hc)
        mock_hc.__aexit__ = AsyncMock(return_value=False)
        MockHTTP.return_value = mock_hc

        response = await client.get("/health")
        data = response.json()
        assert response.status_code == 200
        assert data["status"] == "ok"
        assert data["version"] == "0.1.1"
        assert data["database"] == "ok"
        assert data["litellm"] == "ok"


@pytest.mark.asyncio
async def test_health_includes_database_status(client: AsyncClient):
    """Database status is always included."""
    with patch("forge.gateway.router.httpx.AsyncClient") as MockHTTP:
        mock_hc = AsyncMock()
        mock_hc.get.side_effect = Exception("unreachable")
        mock_hc.__aenter__ = AsyncMock(return_value=mock_hc)
        mock_hc.__aexit__ = AsyncMock(return_value=False)
        MockHTTP.return_value = mock_hc

        response = await client.get("/health")
        data = response.json()
        assert data["database"] == "ok"


@pytest.mark.asyncio
async def test_health_degraded_when_litellm_unreachable(client: AsyncClient):
    """Status is degraded when LiteLLM cannot be reached."""
    with patch("forge.gateway.router.httpx.AsyncClient") as MockHTTP:
        mock_hc = AsyncMock()
        mock_hc.get.side_effect = Exception("connection refused")
        mock_hc.__aenter__ = AsyncMock(return_value=mock_hc)
        mock_hc.__aexit__ = AsyncMock(return_value=False)
        MockHTTP.return_value = mock_hc

        response = await client.get("/health")
        data = response.json()
        assert data["status"] == "degraded"
        assert data["litellm"] == "unreachable"


@pytest.mark.asyncio
async def test_health_degraded_when_litellm_returns_error(client: AsyncClient):
    """Status is degraded when LiteLLM returns non-200."""
    mock_response = httpx.Response(503, json={"status": "error"})
    with patch("forge.gateway.router.httpx.AsyncClient") as MockHTTP:
        mock_hc = AsyncMock()
        mock_hc.get.return_value = mock_response
        mock_hc.__aenter__ = AsyncMock(return_value=mock_hc)
        mock_hc.__aexit__ = AsyncMock(return_value=False)
        MockHTTP.return_value = mock_hc

        response = await client.get("/health")
        data = response.json()
        assert data["status"] == "degraded"
        assert data["litellm"] == "error"
