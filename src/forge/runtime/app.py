"""The single ASGI application accepts work only after the installed schema is checked."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
import os

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from forge.api.lane_v1 import router as lane_router
from forge.runtime.assembly import Runtime, open_runtime
from forge.runtime.authority import NotAuthorized
from forge.runtime.operator_api import router as operator_router
from forge.runtime.settings import RuntimeSettings, load
from forge.runtime.webhooks import router as hooks_router
from forge.runtime.health import router as health_router
from forge.store.records import StaleLease, StoreConflict

Opener = Callable[[RuntimeSettings], AbstractAsyncContextManager[Runtime]]


def create_app(config: RuntimeSettings | None = None, *, opener: Opener = open_runtime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings = config or load(Path(os.environ.get("FORGE_CONFIG", "forge.toml")))
        async with opener(settings) as runtime:
            app.state.runtime = runtime
            app.state.lane_v1_service = runtime.lane
            try:
                yield
            finally:
                app.state.runtime = None
                app.state.lane_v1_service = None

    app = FastAPI(title="Forge", version="1.0.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.include_router(lane_router)
    app.include_router(operator_router)
    app.include_router(hooks_router)
    app.include_router(health_router)
    app.add_exception_handler(RequestValidationError, invalid_request)
    app.add_exception_handler(NotAuthorized, forbidden)
    app.add_exception_handler(StoreConflict, conflict)
    app.add_exception_handler(StaleLease, conflict)
    app.add_exception_handler(Exception, unavailable)
    return app


async def invalid_request(request: Request, error: Exception) -> JSONResponse:
    return JSONResponse({"detail": "request shape refused"}, status_code=422)


async def forbidden(request: Request, error: Exception) -> JSONResponse:
    return JSONResponse({"detail": "repository or run access refused"}, status_code=403)


async def conflict(request: Request, error: Exception) -> JSONResponse:
    return JSONResponse(
        {"detail": "command conflicts with current durable authority"}, status_code=409
    )


async def unavailable(request: Request, error: Exception) -> JSONResponse:
    return JSONResponse({"detail": "service unavailable"}, status_code=503)
