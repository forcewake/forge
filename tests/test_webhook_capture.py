import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from forge.config import Settings
from forge.database import reset_engine
from forge.main import create_app
from tests.conftest import TEST_WEBHOOK_SECRET

FIXTURES = Path(__file__).parent / "fixtures"


def _capture_settings(capture_dir: Path | None) -> Settings:
    return Settings(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test-token"),
        GITLAB_WEBHOOK_SECRET=SecretStr(TEST_WEBHOOK_SECRET),
        FORGE_BOT_USERNAME="forge-bot",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        LITELLM_URL="http://litellm:4000",
        LOG_LEVEL="DEBUG",
        FORGE_CAPTURE_DIR=str(capture_dir) if capture_dir else None,
    )


def _push_payload() -> dict:
    return json.loads((FIXTURES / "push.json").read_text())


@pytest.mark.asyncio
async def test_capture_writes_payload_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """When FORGE_CAPTURE_DIR is set, every webhook body is persisted."""
    monkeypatch.chdir(tmp_path)
    capture_dir = tmp_path / "captured"
    reset_engine()
    application = create_app(settings=_capture_settings(capture_dir))
    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/webhook",
                json=_push_payload(),
                headers={
                    "X-Gitlab-Token": TEST_WEBHOOK_SECRET,
                    "X-Gitlab-Event": "Push Hook",
                },
            )
    reset_engine()

    assert response.status_code == 202
    files = list(capture_dir.glob("*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text())
    assert record["x_gitlab_event"] == "Push Hook"
    assert record["payload"]["object_kind"] == "push"
    # The webhook secret must never appear in captured records
    assert TEST_WEBHOOK_SECRET not in json.dumps(record)


@pytest.mark.asyncio
async def test_capture_disabled_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Without FORGE_CAPTURE_DIR nothing is written anywhere."""
    monkeypatch.chdir(tmp_path)
    reset_engine()
    application = create_app(settings=_capture_settings(None))
    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/webhook",
                json=_push_payload(),
                headers={
                    "X-Gitlab-Token": TEST_WEBHOOK_SECRET,
                    "X-Gitlab-Event": "Push Hook",
                },
            )
    reset_engine()

    assert response.status_code == 202
    assert not (tmp_path / "data" / "captured").exists()
    assert not (tmp_path / "captured").exists()


@pytest.mark.asyncio
async def test_invalid_json_returns_400(client: AsyncClient):
    """Garbage bodies are rejected with 400 (not a 500 traceback)."""
    response = await client.post(
        "/webhook",
        content=b"not-json{",
        headers={
            "X-Gitlab-Token": TEST_WEBHOOK_SECRET,
            "X-Gitlab-Event": "Push Hook",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 400
