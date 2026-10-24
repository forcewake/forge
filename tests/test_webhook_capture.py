"""Raw capture is retired; authenticated facts do not spill bodies or hook secrets."""

import json

from pydantic import ValidationError
import pytest
from sqlalchemy import func, select

from forge.runtime.settings import RuntimeSettings
from forge.store.models_commands import EngineCommand
from tests.native_hook_fixture import post
from tests.operator_native_fixture import operator_native as operator_native
from tests.test_gitlab_engine_ingress import payload, signed


async def test_capture_writes_payload_file(operator_native, tmp_path, monkeypatch):
    env = operator_native
    capture = tmp_path / "captured"
    monkeypatch.setenv("FORGE_CAPTURE_DIR", str(capture))
    document = env.config.model_dump(mode="json", by_alias=True)
    document["capture_directory"] = str(capture)
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate_json(json.dumps(document))
    assert (await post(env)).status_code == 202
    assert not capture.exists() and not env.peer.calls and not env.gateway.writes


async def test_capture_disabled_by_default(operator_native, tmp_path):
    env = operator_native
    response = await post(env)
    assert response.status_code == 202
    assert all(value not in response.text for value in env.values.values())
    assert not (tmp_path / "captured").exists() and not (tmp_path / "data/captured").exists()
    async with env.service.factory() as session:
        assert await session.scalar(select(func.count()).select_from(EngineCommand)) == 0


async def test_invalid_json_returns_400(operator_native):
    env = operator_native
    headers, _ = signed(payload())
    response = await env.client.post("/hooks/ce-1", headers=headers, content=b"not-json{")
    assert response.status_code == 400 and "Traceback" not in response.text
    assert not env.peer.calls and not env.gateway.writes
