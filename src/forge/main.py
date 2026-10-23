from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from forge import __version__
from forge.api.lane_v1 import configure_lane_v1, router as lane_v1_router
from forge.config import Settings, get_forge_config
from forge.database import dispose_engine, get_session_factory, init_db
from forge.gateway.router import router
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

    db_url = settings.DATABASE_URL
    if db_url.startswith("sqlite"):
        db_path = db_url.split("///")[-1]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    await init_db(db_url)
    app.state.session_factory = get_session_factory(db_url)
    configure_lane_v1(app)

    # R36-05 (issue #264): the checkpoint authority marker's state is
    # observable at startup — one line naming the CONFIGURED checkpoint
    # authority (FORGE_CHECKPOINT_DURABILITY), the authority the
    # deployment's marker declares ACTIVE, and the marker's generation.
    # Observability only: the composition's fail-closed refusals live in
    # resolve_repository, and this line never breaks startup.
    try:
        from forge.adaptive.checkpoint_repository import authority_state_report

        state = authority_state_report()
        logger.info(
            "migration.configured_vs_active_authority: state=%s configured=%s "
            "active=%s generation=%s",
            state["state"],
            state["configured_authority"],
            state["active_authority"],
            state["marker_generation"],
        )
        if state["state"] == "mismatch":
            logger.warning(
                "the checkpoint authority marker names %s but this process is "
                "configured for %s — its checkpoint mutations will be refused "
                "with MutationsFencedError until it restarts on the configured "
                "authority or the marker is rolled back",
                state["active_authority"],
                state["configured_authority"],
            )
    except Exception:  # noqa: BLE001 — a startup log line never blocks the app
        logger.debug("checkpoint authority state unavailable at startup", exc_info=True)

    # Initialize Redis + TaskQueue (optional — falls back to BackgroundTasks)
    redis_manager = await RedisManager.from_settings(settings)
    app.state.redis_manager = redis_manager
    if redis_manager is not None:
        app.state.task_queue = TaskQueue(redis_manager, RetryPolicy())
        logger.info("Redis task queue enabled")
    else:
        app.state.task_queue = None
        logger.info("Redis not configured — using in-process background tasks")

    logger.info(
        "Forge v%s started — database: %s",
        __version__,
        "sqlite" if "sqlite" in db_url else db_url.split("+")[0],
    )

    yield

    # Shutdown
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

    from forge.api_lane_control import lane_control_router

    application.include_router(lane_control_router)

    application.include_router(lane_v1_router)

    from forge.api_checkpoint_channel import checkpoint_channel_router

    application.include_router(checkpoint_channel_router)

    # The operator read API (R36-15) — the authenticated, subject-scoped
    # projection reads (list / detail / support-bundle). Same fail-closed
    # posture as the lane-control surface: mounted unconditionally, every
    # route answers 503 unless FORGE_LANE_CONTROL_SECRET is configured, and
    # the router is read-only by construction (no write endpoints).
    from forge.api_operator import operator_router

    application.include_router(operator_router)

    return application


# Uvicorn entry point: uvicorn forge.main:app --factory
# Using a factory avoids requiring env vars at import time.
app = create_app
