import logging

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from forge.config import Settings
from forge.database import reset_engine
from forge.main import create_app

# Suppress debug logs from libraries whose background threads can write to
# stderr after pytest has already torn down the stream, causing
# "I/O operation on closed file" noise.
logging.getLogger("aiosqlite").setLevel(logging.WARNING)
logging.getLogger("LiteLLM").setLevel(logging.WARNING)
logging.getLogger("litellm").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

TEST_WEBHOOK_SECRET = "test-secret-token"


@pytest.fixture()
def test_settings() -> Settings:
    """Settings configured for testing with in-memory SQLite."""
    return Settings(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test-token"),
        GITLAB_WEBHOOK_SECRET=SecretStr(TEST_WEBHOOK_SECRET),
        FORGE_BOT_USERNAME="forge-bot",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        LITELLM_URL="http://litellm:4000",
        LOG_LEVEL="DEBUG",
    )


@pytest.fixture()
async def app(test_settings: Settings):
    """Create a FastAPI app with test settings."""
    reset_engine()
    application = create_app(settings=test_settings)
    async with application.router.lifespan_context(application):
        yield application
    reset_engine()


@pytest.fixture()
async def client(app) -> AsyncClient:
    """Async HTTP client for testing."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
