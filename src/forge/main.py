from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from forge import __version__
from forge.agents.registry import AgentRegistry
from forge.config import Settings, get_forge_config
from forge.database import get_session_factory, init_db, reset_engine
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
    yield

    # Shutdown
    if hasattr(app.state, "mcp_manager") and app.state.mcp_manager is not None:
        await app.state.mcp_manager.close_all()
    if app.state.redis_manager is not None:
        await app.state.redis_manager.close()
    reset_engine()
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

    # Mount MCP server at /mcp
    mcp_server = create_mcp_server(settings)
    mcp_app = mcp_server.streamable_http_app()
    if settings.FORGE_MCP_KEY:
        mcp_app = MCPAuthMiddleware(mcp_app, settings.FORGE_MCP_KEY.get_secret_value())
    application.mount("/mcp", mcp_app)
    application.state.mcp_server = mcp_server

    return application


# Uvicorn entry point: uvicorn forge.main:app --factory
# Using a factory avoids requiring env vars at import time.
app = create_app
