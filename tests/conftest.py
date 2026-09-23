import logging
import warnings

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


# R32-18: the clean-lifecycle gate. The suite must run with ZERO
# "coroutine ... was never awaited" RuntimeWarnings — a warning of that
# family means a fixture or double created a coroutine nobody drained
# (an AsyncMock standing in for a synchronous call, an un-awaited task).
# Rather than hoping the warnings stay absent, every test runs with the
# warning promoted to an error, so the leak fails the test that leaked
# it instead of smearing an unattributable warning across the report.
# The filter is deliberately narrow: unrelated RuntimeWarnings from the
# dependency forest are not part of this gate and stay visible as
# warnings, never silenced.
@pytest.fixture(autouse=True)
def _fail_on_unawaited_coroutines():
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "error", message=r"coroutine .* was never awaited", category=RuntimeWarning
        )
        yield


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
    """Create a FastAPI app with test settings.

    The lifespan context this fixture enters disposes the cached engine
    on exit (``forge.main.lifespan`` awaits ``dispose_engine`` — R32-18:
    the aiosqlite worker thread is joined before the event loop closes);
    the surrounding ``reset_engine()`` calls only clear the sync cache.
    """
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
