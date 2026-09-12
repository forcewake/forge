from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from forge.config import Settings
from forge.database import reset_engine
from forge.main import create_app

TEST_SECRET = "test-secret-token"

MR_PAYLOAD = {
    "object_kind": "merge_request",
    "event_type": "merge_request",
    "user": {"id": 1, "name": "Test", "username": "testuser"},
    "project": {
        "id": 42,
        "name": "test",
        "path_with_namespace": "group/test",
        "web_url": "https://gitlab.test/group/test",
        "default_branch": "main",
    },
    "object_attributes": {
        "id": 100,
        "iid": 10,
        "title": "Test MR",
        "action": "open",
        "state": "opened",
        "source_branch": "feature",
        "target_branch": "main",
    },
}


def _settings(redis_url: str | None = None) -> Settings:
    return Settings(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test-token"),
        GITLAB_WEBHOOK_SECRET=SecretStr(TEST_SECRET),
        FORGE_BOT_USERNAME="forge-bot",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        LITELLM_URL="http://litellm:4000",
        REDIS_URL=redis_url,
        LOG_LEVEL="DEBUG",
    )


@pytest.fixture()
async def app_no_redis():
    """App without Redis — should use BackgroundTasks fallback."""
    reset_engine()
    application = create_app(settings=_settings(redis_url=None))
    async with application.router.lifespan_context(application):
        yield application
    reset_engine()


@pytest.fixture()
async def client_no_redis(app_no_redis) -> AsyncClient:
    transport = ASGITransport(app=app_no_redis)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_webhook_uses_background_tasks_without_redis(client_no_redis):
    """When Redis is not configured, webhook should fall through to BackgroundTasks."""
    with patch("forge.gateway.router.Orchestrator") as MockOrch:
        mock_instance = MockOrch.return_value
        mock_instance.handle_event = AsyncMock()

        resp = await client_no_redis.post(
            "/webhook",
            json=MR_PAYLOAD,
            headers={
                "X-Gitlab-Token": TEST_SECRET,
                "X-Gitlab-Event": "Merge Request Hook",
            },
        )

    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert "queued" not in data  # Not using Redis path


async def test_webhook_enqueues_with_redis(app_no_redis):
    """When task_queue is set, webhook should enqueue instead of BackgroundTasks."""
    # Attach a mock task queue
    mock_queue = AsyncMock()
    mock_queue.is_duplicate = AsyncMock(return_value=False)
    mock_queue.submit = AsyncMock()
    app_no_redis.state.task_queue = mock_queue

    transport = ASGITransport(app=app_no_redis)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhook",
            json=MR_PAYLOAD,
            headers={
                "X-Gitlab-Token": TEST_SECRET,
                "X-Gitlab-Event": "Merge Request Hook",
            },
        )

    assert resp.status_code == 202
    data = resp.json()
    assert data.get("queued") is True
    mock_queue.submit.assert_called_once()


async def test_webhook_dedup_skips_duplicate(app_no_redis):
    """When task_queue reports duplicate, response should indicate it."""
    mock_queue = AsyncMock()
    mock_queue.is_duplicate = AsyncMock(return_value=True)
    app_no_redis.state.task_queue = mock_queue

    transport = ASGITransport(app=app_no_redis)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhook",
            json=MR_PAYLOAD,
            headers={
                "X-Gitlab-Token": TEST_SECRET,
                "X-Gitlab-Event": "Merge Request Hook",
            },
        )

    assert resp.status_code == 202
    data = resp.json()
    assert data.get("deduplicated") is True
    mock_queue.submit.assert_not_called()


async def test_health_reports_queue_depth_with_redis(app_no_redis):
    """When Redis is configured, /health should include queue_depth."""
    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)
    mock_queue = AsyncMock()
    mock_queue.depth = AsyncMock(return_value=5)
    mock_queue.dlq_depth = AsyncMock(return_value=1)

    app_no_redis.state.redis_manager = mock_redis
    app_no_redis.state.task_queue = mock_queue

    transport = ASGITransport(app=app_no_redis)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")

    data = resp.json()
    assert data["redis"] == "ok"
    assert data["queue_depth"] == 5
    assert data["dlq_depth"] == 1


async def test_health_without_redis_has_no_redis_key(client_no_redis):
    """Without Redis, /health should not include redis or queue keys."""
    resp = await client_no_redis.get("/health")
    data = resp.json()
    assert "redis" not in data
    assert "queue_depth" not in data


async def test_metrics_returns_503_without_redis(client_no_redis):
    """/metrics should return 503 when Redis is not configured."""
    resp = await client_no_redis.get("/metrics")
    assert resp.status_code == 503
    data = resp.json()
    assert "error" in data


async def test_metrics_returns_stats_with_redis(app_no_redis):
    """/metrics should return all stats when Redis is configured."""
    mock_redis = AsyncMock()
    mock_redis.scan_keys = AsyncMock(return_value=["forge:worker:w1", "forge:worker:w2"])
    mock_redis.get_stat = AsyncMock(
        side_effect=lambda name, hours=1: 42 if name == "processed" else 3
    )
    mock_queue = AsyncMock()
    mock_queue.depth = AsyncMock(return_value=7)
    mock_queue.dlq_depth = AsyncMock(return_value=2)

    app_no_redis.state.redis_manager = mock_redis
    app_no_redis.state.task_queue = mock_queue

    transport = ASGITransport(app=app_no_redis)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics")

    assert resp.status_code == 200
    data = resp.json()
    assert data["queue_depth"] == 7
    assert data["dlq_depth"] == 2
    assert data["workers_active"] == 2
    assert data["tasks_processed_1h"] == 42
    assert data["tasks_failed_1h"] == 3
