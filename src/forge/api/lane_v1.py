"""Typed lane v1 HTTP routes; registration and control production stay server-only."""

from __future__ import annotations

from collections.abc import Callable, Coroutine, MutableMapping
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from forge_protocol import (
    Brief,
    CandidateBundle,
    CheckpointManifest,
    ControlAck,
    ControlCommand,
    ControlDelivery,
    Heartbeat,
    LaneReport,
    ModelKeyGrant,
    RunIdentity,
    SessionRequest,
    SessionToken,
)

from forge.api.lane_auth_values import LaneAuthError, LaneAuthPending
from forge.api.lane_v1_blobs import BlobError, BlobTooLarge
from forge.api.lane_v1_service import LaneV1Service, ModelKeyUnavailable
from forge.api.lane_v1_values import LaneConflict, LaneNotFound


class LaneRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            receive, count = request.receive, 0

            async def bounded_receive() -> MutableMapping[str, Any]:
                nonlocal count
                message = await receive()
                count += len(message.get("body", b""))
                if count > 1024 * 1024:
                    raise HTTPException(413, "lane JSON body exceeds the request limit")
                return message

            if "/blobs/" not in request.url.path:
                request = Request(request.scope, receive=bounded_receive)
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # Validation must never echo an OIDC token or credential input.
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": [
                            {key: error[key] for key in ("type", "loc", "msg")}
                            for error in exc.errors()
                        ]
                    },
                )
            except LaneAuthPending as exc:
                raise HTTPException(503, str(exc), headers={"Retry-After": "1"}) from None
            except LaneAuthError as exc:
                raise HTTPException(401, str(exc), headers={"WWW-Authenticate": "Bearer"}) from None
            except LaneNotFound as exc:
                raise HTTPException(404, str(exc)) from None
            except BlobTooLarge as exc:
                raise HTTPException(413, str(exc)) from None
            except (LaneConflict, BlobError) as exc:
                raise HTTPException(409, str(exc)) from None
            except ModelKeyUnavailable as exc:
                raise HTTPException(503, str(exc)) from None

        return guarded


router = APIRouter(prefix="/lane/v1", tags=["lane-v1"], route_class=LaneRoute)


def service(request: Request) -> LaneV1Service:
    value = getattr(request.app.state, "lane_v1_service", None)
    if value is None:
        raise HTTPException(
            503, "lane v1 is unavailable: configure its signing key and HTTPS public URL"
        )
    return cast(LaneV1Service, value)


Service = Annotated[LaneV1Service, Depends(service)]


async def identity(request: Request, lane: Service) -> RunIdentity:
    return await lane.auth.authenticate_bearer(request.headers.get("authorization", ""))


Identity = Annotated[RunIdentity, Depends(identity)]


@router.post("/session", response_model=SessionToken)
async def session(body: SessionRequest, lane: Service) -> SessionToken:
    return await lane.auth.exchange(body)


@router.get("/brief", response_model=Brief)
async def brief(who: Identity, lane: Service) -> Brief:
    return (await lane.auth.store.load(who)).brief


@router.get("/controls", response_model=list[ControlCommand])
async def controls(
    who: Identity, lane: Service, after: int = Query(0, ge=0)
) -> list[ControlCommand]:
    return await lane.controls(who, after)


@router.post("/controls/{command_id}/begin", response_model=ControlDelivery)
async def begin_control(
    command_id: str, body: ControlDelivery, who: Identity, lane: Service
) -> ControlDelivery:
    if command_id != body.command_id:
        raise LaneConflict("delivery path and command id differ")
    return await lane.begin(who, body)


@router.post("/controls/{command_id}/ack", response_model=ControlAck)
async def acknowledge(
    command_id: str, body: ControlAck, who: Identity, lane: Service
) -> ControlAck:
    if command_id != body.command_id:
        raise LaneConflict("acknowledgment path and command id differ")
    return await lane.acknowledge(who, body)


@router.put("/blobs/{digest}", status_code=204)
async def put_blob(digest: str, request: Request, who: Identity, lane: Service) -> Response:
    size = await lane.blobs.put(digest, request.stream())
    await lane.record_blob(who, digest, size)
    return Response(status_code=204)


@router.get("/blobs/{digest}")
async def get_blob(digest: str, who: Identity, lane: Service) -> Response:
    return Response(await lane.read_blob(who, digest), media_type="application/octet-stream")


@router.put("/checkpoints/{checkpoint_id}", response_model=CheckpointManifest)
async def put_checkpoint(
    checkpoint_id: str,
    body: CheckpointManifest,
    who: Identity,
    lane: Service,
    response: Response,
) -> CheckpointManifest:
    response.status_code = 201 if await lane.checkpoint(who, checkpoint_id, body) else 200
    return body


@router.get("/checkpoints/latest", response_model=CheckpointManifest)
async def latest_checkpoint(who: Identity, lane: Service) -> CheckpointManifest:
    return await lane.latest_checkpoint(who)


@router.post("/candidate", response_model=CandidateBundle)
async def candidate(
    body: CandidateBundle, who: Identity, lane: Service, response: Response
) -> CandidateBundle:
    response.status_code = 201 if await lane.candidate(who, body) else 200
    return body


@router.post("/report", response_model=LaneReport)
async def report(body: LaneReport, who: Identity, lane: Service, response: Response) -> LaneReport:
    response.status_code = 201 if await lane.report(who, body) else 200
    return body


@router.post("/heartbeat", response_model=Heartbeat)
async def heartbeat(body: Heartbeat, who: Identity, lane: Service) -> Heartbeat:
    return await lane.heartbeat(who, body)


@router.post("/model-key", response_model=ModelKeyGrant)
async def model_key(who: Identity, lane: Service) -> ModelKeyGrant:
    return await lane.model_key(who)
