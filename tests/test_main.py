"""The sole application cannot restore removed endpoints through old settings."""

import json

import httpx
import pytest
from pydantic import ValidationError

from forge.runtime.app import create_app
from forge.runtime.settings import RuntimeSettings
from tests.runtime_fixture import configuration


@pytest.mark.parametrize(
    "legacy",
    ({"FORGE_MCP_ENABLED": True, "FORGE_MCP_KEY": "stale-key"}, {"FORGE_MCP_ENABLED": False}),
)
async def test_removed_mcp_cannot_be_reenabled_by_old_settings(tmp_path, legacy):
    config, _ = configuration(tmp_path)
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate_json(
            json.dumps({**config.model_dump(mode="json", by_alias=True), **legacy})
        )
    app = create_app(config)
    assert not hasattr(app.state, "mcp_server")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://forge.invalid"
    ) as client:
        for path in ("/mcp", "/mcp/", "/mcp/mcp"):
            assert (await client.post(path, json={})).status_code == 404
    assert any(getattr(route, "path", "") == "/api/v1/runs" for route in app.routes)
