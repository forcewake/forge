"""Signed native events become durable scoped commands; other facts only wake polling."""

from fastapi import APIRouter, HTTPException, Request

from forge.command_values import CommandEnvelope
from forge.ingress_values import IngressEvent
from forge.runtime.operator_api import RuntimeDep

router = APIRouter(tags=["native-hooks"])


def command_id(event: IngressEvent) -> str:
    if event.issue_action is not None or (event.event == "issues" and event.action == "labeled"):
        assert event.native_id is not None
        return event.native_id
    if event.native_id is None:
        return event.delivery_key
    kinds = {
        "issue_comment": "issue_comment",
        "pull_request_review_comment": "review_comment",
        "pull_request_review": "review",
    }
    if event.repo.identity.provider == "github":
        kind = kinds.get(event.event)
    else:
        kind = (
            "review_comment"
            if event.event == "note" and event.target is not None and event.target.kind == "review"
            else "issue_comment"
            if event.event == "note"
            else None
        )
    if kind is None:
        return event.delivery_key
    return kind + ":" + event.native_id


@router.post("/hooks/{connection}", status_code=202)
async def hook(connection: str, request: Request, service: RuntimeDep) -> dict[str, object]:
    names = [key.lower() for key, _ in request.scope["headers"]]
    if len(names) != len(set(names)):
        raise HTTPException(400, "ambiguous native event headers")
    parser = service.connections.ingress.get(connection)
    if parser is None:
        raise HTTPException(404, "connection not found")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 2 * 1024 * 1024:
            raise HTTPException(413, "webhook body exceeds its bound")
    try:
        event = parser.parse(dict(request.headers), bytes(body))
    except ValueError:
        raise HTTPException(400, "native event authentication or shape refused") from None
    if event is None:
        return {"accepted": False, "commands": []}
    service.connections.registry.for_repo(event.repo)
    results = []
    if event.target is not None:
        for action in event.commands:
            envelope = CommandEnvelope(
                source="webhook",
                native_id=command_id(event),
                actor=event.actor,
                target=event.target,
                command=action,
                native_reply=event.issue_action is None,
                issue_action=event.issue_action,
            )
            results.append((await service.commands.submit(envelope)).model_dump(mode="json"))
    review = (
        (await service.reactive.reviews.submit(event)).model_dump(mode="json")
        if event.head_sha is not None
        else None
    )
    service.wakeups.signal()
    return {"accepted": True, "commands": results, **({"review": review} if review else {})}
