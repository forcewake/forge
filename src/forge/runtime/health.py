"""Readiness is current schema and native guard evidence, without a model request."""

from fastapi import APIRouter, HTTPException

from forge import __version__
from forge.runtime.operator_api import RuntimeDep, PrincipalDep
from fastapi.responses import PlainTextResponse
from forge.store.schema import check_schema

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def health(service: RuntimeDep) -> dict[str, str]:
    return {"service": "forge", "version": __version__}


@router.get("/readyz")
async def ready(service: RuntimeDep) -> dict[str, str]:
    try:
        await check_schema(service.engine)
        model = service.config.connections[0].repositories[0].policy.model
        if await service.budget.admission.readiness(model) is not None:
            raise ValueError("guard unavailable")
    except Exception:
        raise HTTPException(503, "schema or native model guard unavailable") from None
    return {"schema": "checked", "native_budget": "verified"}


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(service: RuntimeDep, who: PrincipalDep) -> PlainTextResponse:
    return PlainTextResponse(
        await service.projection.metrics(who), media_type="text/plain; version=0.0.4"
    )
