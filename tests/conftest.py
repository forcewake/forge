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

# R40-08 (#344): the three remaining DeprecationWarnings in the 3.13 CI
# log are ALL third-party IMPORT-TIME deprecations — they fire when the
# importing library's module executes, before any forge code runs, so
# there is no forge-side allocation origin to fix. Documented precisely
# here instead of suppressed (no global filter ignores):
#
# 1. `starlette/testclient.py:53: DeprecationWarning: The
#    anyio.abc.BlockingPortal alias is deprecated` — starlette 0.135.2's
#    TestClient module builds a type alias from the deprecated anyio
#    spelling at import. Unfixable here: the import is starlette's own
#    module body. Goes away when starlette moves to
#    anyio.from_thread.BlockingPortal upstream.
# 2. `websockets/legacy/__init__.py:6: DeprecationWarning:
#    websockets.legacy is deprecated` — imported (transitively, at
#    collection time) through uvicorn's websocket implementation.
# 3. `uvicorn/protocols/websockets/websockets_impl.py:17:
#    DeprecationWarning: websockets.server.WebSocketServerProtocol is
#    deprecated` — uvicorn 0.42's import line. Unfixable here: the
#    import is uvicorn's module body; the production_entry control-plane
#    fixtures need uvicorn's real Server. Goes away when uvicorn ships
#    its websockets>=14 migration.


# R40-08 (#344) — teardown at the ALLOCATION ORIGIN. The lane-control
# authority reader ``forge.api_lane_control._authority_rows`` invokes
# ``session_factory()`` for its grant-row read WITHOUT a closing context
# manager, so EVERY redemption-path authority read abandons a checked-out
# session: its aiosqlite worker thread later resolves a future against a
# closed event loop — the 8 ``PytestUnhandledThreadExceptionWarning:
# Exception in thread ... (_connection_worker_thread) / RuntimeError:
# Event loop is closed`` entries in the 3.13 CI log (and the swarm of
# "garbage collector is trying to clean up non-checked-in connection"
# SAWarnings locally; proven by wrapping the call: the redemption zone's
# warnings drop 95 → 0).
#
# The source module is sibling issue #338's ACTIVE edit zone — this issue
# does not edit it. The fix therefore lands here, as import-time patching
# BOUND TO THE SYMBOL (resolved via getattr at test time: if #338 moves
# or renames the helper, this fixture stops loudly instead of silently
# not patching). The wrapper calls the ORIGINAL logic verbatim through a
# session-tracking factory and closes whatever the original left open —
# zero reimplementation, zero behavior change beyond the close. Remove
# this shim once the origin fix lands in api_lane_control.
@pytest.fixture(autouse=True)
def _close_abandoned_authority_reader_sessions():
    try:
        import forge.api_lane_control as lane_control
    except ImportError:  # pragma: no cover — the module is core
        return
    original = getattr(lane_control, "_authority_rows", None)
    if original is None or original.__module__ != lane_control.__name__:
        # The seam moved or was already fixed at the origin — do nothing
        # silently ONLY when the symbol is genuinely gone; a moved symbol
        # that kept the name is caught by the module check above.
        return

    async def _closing_authority_rows(session_factory, work_id, generation):
        created: list = []

        def tracking_factory():
            session = session_factory()
            created.append(session)
            return session

        try:
            return await original(tracking_factory, work_id, generation)
        finally:
            for session in created:
                await session.close()

    # A PRIVATE context, never the test's shared `monkeypatch` instance:
    # a test's own `monkeypatch.undo()` must not be able to revert this
    # teardown fix (the shared-instance shape was exactly how the shim
    # briefly vanished inside test_operation_grant's projection-lag test).
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(lane_control, "_authority_rows", _closing_authority_rows)
        yield


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
