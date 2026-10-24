"""Authenticated operator requests select configured repositories, never actor claims."""

from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field

from forge.command_values import CommandEnvelope, CommandResult
from forge.api.lane_v1 import LaneRoute
from forge.domain.value import Value
from forge.ingress_values import Command
from forge.port_values import ReviewRef, SubjectRef
from forge.runtime.assembly import Runtime
from forge.runtime.authority import NotAuthorized, Principal

router = APIRouter(prefix="/api/v1", tags=["operator"], route_class=LaneRoute)


def runtime(request: Request) -> Runtime:
    value = getattr(request.app.state, "runtime", None)
    if value is None:
        raise HTTPException(503, "runtime is not ready")
    return cast(Runtime, value)


RuntimeDep = Annotated[Runtime, Depends(runtime)]


def principal(request: Request, service: RuntimeDep) -> Principal:
    try:
        if len(request.headers.getlist("authorization")) != 1:
            raise NotAuthorized("ambiguous operator credential")
        return service.operators.authenticate(request.headers.get("authorization", ""))
    except NotAuthorized:
        raise HTTPException(
            401, "operator credential required", headers={"WWW-Authenticate": "Bearer"}
        ) from None


PrincipalDep = Annotated[Principal, Depends(principal)]


def query_shape(request: Request, allowed: frozenset[str]) -> None:
    if set(request.query_params) - allowed or any(
        len(request.query_params.getlist(key)) != 1 for key in request.query_params
    ):
        raise HTTPException(400, "operator query shape refused")


class CommandRequest(Value):
    repository: Annotated[str, Field(min_length=1, max_length=64)]
    target_kind: Literal["issue", "review"] = "issue"
    number: Annotated[int, Field(gt=0)]
    request_id: Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")]
    command: Command
    feedback_reference: (
        Annotated[
            str,
            Field(pattern=r"^(issue_comment|review_comment|review):[1-9][0-9]*$"),
        ]
        | None
    ) = None


@router.get("/runs")
async def runs(
    request: Request,
    service: RuntimeDep,
    who: PrincipalDep,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10000),
) -> dict[str, object]:
    query_shape(request, frozenset(("limit", "offset")))
    return {
        "runs": await service.projection.list(who, limit=limit, offset=offset),
        "offset": offset,
        "limit": limit,
    }


@router.get("/runs/{run_id}")
async def run_detail(
    run_id: str, request: Request, service: RuntimeDep, who: PrincipalDep
) -> dict[str, object]:
    query_shape(request, frozenset())
    if len(run_id) != 32:
        raise HTTPException(404, "run not found")
    try:
        UUID(hex=run_id)
    except ValueError:
        raise HTTPException(404, "run not found") from None
    detail = await service.projection.detail(who, run_id)
    if detail is None:
        raise HTTPException(404, "run not found")
    return detail


@router.post("/commands", response_model=CommandResult, status_code=202)
async def command(body: CommandRequest, service: RuntimeDep, who: PrincipalDep) -> CommandResult:
    actor = who.actor(body.repository)
    repo = service.connections.registry.named(body.repository).ref
    target = (
        SubjectRef(repo=repo, number=body.number)
        if body.target_kind == "issue"
        else ReviewRef(repo=repo, number=body.number)
    )
    feedback = body.command.kind in ("fix", "ask", "approve_revision")
    if feedback and body.feedback_reference is None:
        raise HTTPException(422, "review commands require a canonical native feedback reference")
    if not feedback and body.feedback_reference is not None:
        raise HTTPException(422, "feedback reference is only valid for review commands")
    native_id = body.feedback_reference or "operator:" + who.key + ":" + body.request_id
    envelope = CommandEnvelope(
        source="api", native_id=native_id, actor=actor, target=target, command=body.command
    )
    result = await service.commands.submit(envelope)
    service.wakeups.signal()
    return result
