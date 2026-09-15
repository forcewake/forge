from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from forge import __version__
from forge.agents.registry import AgentRegistry
from forge.config import Settings, get_forge_config
from forge.database import dispose_engine, get_session_factory, init_db
from forge.flows.loader import FlowLoader
from forge.flows.state import FlowStateManager
from forge.gateway.router import router
from forge.mcp_client.manager import MCPConnectionManager
from forge.mcp_client.registry import MCPRegistry
from forge.mcp_server.server import MCPAuthMiddleware, create_mcp_server
from forge.utils.logging import setup_logging
from forge.utils.redis_client import RedisManager
from forge.worker.queue import TaskQueue
from forge.worker.retry import RetryPolicy

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    settings: Settings = app.state.settings
    setup_logging(settings.LOG_LEVEL)

    # Ensure data directory exists for SQLite
    db_url = settings.DATABASE_URL
    if db_url.startswith("sqlite"):
        db_path = db_url.split("///")[-1]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    await init_db(db_url)
    app.state.session_factory = get_session_factory(db_url)

    # Load agent definitions
    registry = AgentRegistry(settings.FORGE_AGENTS_DIR)
    registry.load()
    app.state.agent_registry = registry

    # Initialize Redis + TaskQueue (optional — falls back to BackgroundTasks)
    redis_manager = await RedisManager.from_settings(settings)
    app.state.redis_manager = redis_manager
    if redis_manager is not None:
        app.state.task_queue = TaskQueue(redis_manager, RetryPolicy())
        app.state.flow_state_mgr = FlowStateManager(redis_manager)
        # Wire Redis into MCP server for project path caching
        if hasattr(app.state, "mcp_server"):
            app.state.mcp_server._forge_redis = redis_manager  # type: ignore[attr-defined]
        logger.info("Redis task queue enabled")
    else:
        app.state.task_queue = None
        app.state.flow_state_mgr = None
        logger.info("Redis not configured — using in-process background tasks")

    # Load flow definitions
    flow_loader = FlowLoader("agents/flows")
    flow_loader.load()
    app.state.flow_loader = flow_loader

    # Initialize MCP client (external MCP server connections)
    forge_config = app.state.forge_config
    mcp_registry = MCPRegistry(forge_config.mcp_servers)
    mcp_manager = MCPConnectionManager(mcp_registry)
    app.state.mcp_registry = mcp_registry
    app.state.mcp_manager = mcp_manager
    if mcp_registry.list_enabled():
        logger.info(
            "MCP client configured with %d server(s): %s",
            len(mcp_registry.list_enabled()),
            ", ".join(s.name for s in mcp_registry.list_enabled()),
        )

    logger.info(
        "Forge v%s started — database: %s",
        __version__,
        "sqlite" if "sqlite" in db_url else db_url.split("+")[0],
    )

    # The mounted MCP streamable app's own lifespan never runs under a
    # FastAPI mount — its session manager must be started (and stopped)
    # here, or every /mcp request fails with "Task group is not
    # initialized". The context is anyio-task-bound, so a dedicated holder
    # task enters AND exits it; the lifespan merely signals shutdown.
    mcp_session_task: asyncio.Task | None = None
    mcp_shutdown = asyncio.Event()
    if hasattr(app.state, "mcp_server"):
        session_started = asyncio.Event()

        async def _hold_mcp_session() -> None:
            async with app.state.mcp_server.session_manager.run():
                session_started.set()
                await mcp_shutdown.wait()

        mcp_session_task = asyncio.create_task(_hold_mcp_session())
        await session_started.wait()
    yield

    # Shutdown
    mcp_shutdown.set()
    if mcp_session_task is not None:
        try:
            await mcp_session_task
        except RuntimeError:
            pass  # task-group teardown races the lifespan on hard stops
    if hasattr(app.state, "mcp_manager") and app.state.mcp_manager is not None:
        await app.state.mcp_manager.close_all()
    if app.state.redis_manager is not None:
        await app.state.redis_manager.close()
    await dispose_engine()  # F26: close pooled connections, not just the cache
    logger.info("Forge shutting down")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if settings is None:
        settings = Settings()  # type: ignore[call-arg]

    application = FastAPI(
        title="Forge",
        version=__version__,
        description="AI agent platform for GitLab CE",
        lifespan=lifespan,
    )
    application.state.settings = settings
    application.state.forge_config = get_forge_config()
    application.state.session_factory = None
    application.state.redis_manager = None
    application.state.task_queue = None

    application.include_router(router)

    # Azure DevOps ingress (ADR-0024 AZ-2) is mounted unconditionally; the
    # endpoint itself answers 503 unless FORGE_AZDO_ENABLED and the webhook
    # Basic credentials are set — fail closed (Azure service hooks have no
    # HMAC; the Basic pair IS the authenticator).
    from forge.gateway.azure_webhook import azure_router

    application.include_router(azure_router)

    # Mount MCP server at /mcp — fail closed: only with an auth key, since
    # an unauthenticated endpoint is never exposed. Scoped principals
    # (FORGE_MCP_SCOPED_TOKENS) get per-call scope enforcement on the run
    # surface; FORGE_MCP_KEY stays the all-scope master. FORGE_MCP_ENABLED=false
    # opts out for deployments that front /mcp with their own auth.
    if settings.FORGE_MCP_ENABLED and settings.FORGE_MCP_KEY:
        from forge.mcp_server.server import scoped_principals_from_settings

        mcp_server = create_mcp_server(settings)
        mcp_app = mcp_server.streamable_http_app()
        mcp_app = MCPAuthMiddleware(
            mcp_app,
            settings.FORGE_MCP_KEY.get_secret_value(),
            scoped_principals=scoped_principals_from_settings(settings),
        )
        application.mount("/mcp", mcp_app)
        application.state.mcp_server = mcp_server
    else:
        logger.warning("MCP server disabled: FORGE_MCP_KEY not configured")

    return application


# Uvicorn entry point: uvicorn forge.main:app --factory
# Using a factory avoids requiring env vars at import time.
app = create_app
