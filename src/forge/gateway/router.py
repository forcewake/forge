from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from forge import __version__
from forge.gateway.mention import extract_mention
from forge.gateway.parser import parse_webhook
from forge.gateway.validator import is_bot_event, validate_webhook_token
from forge.gitlab.events import GitLabEvent, NoteEvent
from forge.orchestrator.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

router = APIRouter()

#: Commands served by the durable run service (M1), not the legacy flow engine.
_RUN_COMMANDS = frozenset({"/implement", "/go"})


def _match_run_command(event: GitLabEvent, settings) -> dict[str, Any] | None:
    """Detect note events that belong to the durable run loop (M1).

    - ``<mention> /implement`` on an issue → ``start_run``.
    - ``<mention> /go <run-id>`` on an issue → ``handle_command_note``.

    Respects ``FORGE_MENTION_PATTERN``. Returns the run_command metadata dict,
    or None when the event should take its legacy path.
    """
    if not isinstance(event, NoteEvent) or event.issue is None:
        # Gate notes are posted on issues; MR notes keep the legacy paths.
        return None

    mention_pattern = getattr(settings, "FORGE_MENTION_PATTERN", "@forge")
    mention = extract_mention(
        event.object_attributes.note or "",
        mention_pattern,
        extra_commands=_RUN_COMMANDS,
    )
    if not mention.is_mention or mention.slash_command not in _RUN_COMMANDS:
        return None

    common = {
        "project_id": event.project.id if event.project else 0,
        "issue_iid": event.issue.iid,
        "author_username": event.user.username if event.user else "",
        "author_user_id": event.user.id if event.user else 0,
    }
    if mention.slash_command == "/implement":
        # M1 cutover: /implement takes the durable RunService path, not flows.
        return {**common, "command": "start_run"}
    return {**common, "command": "go", "note_text": event.object_attributes.note or ""}


def _capture_webhook_payload(settings: Any, event_header: str, payload: dict[str, Any]) -> None:
    """Persist a raw webhook payload for diagnostics (FORGE_CAPTURE_DIR).

    Stores the event header and payload only — never the webhook secret.
    Failures to capture must never break ingestion.
    """
    capture_dir = getattr(settings, "FORGE_CAPTURE_DIR", None)
    if not capture_dir:
        return
    try:
        path = Path(capture_dir)
        path.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        safe_event = event_header.replace("/", "_")
        name = f"{stamp}-{safe_event}-{uuid4().hex[:6]}.json"
        record = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "x_gitlab_event": event_header,
            "payload": payload,
        }
        target = path / name
        target.write_text(json.dumps(record, indent=2, sort_keys=True))
        logger.info("Captured webhook payload: %s", target)
    except OSError:
        logger.warning("Failed to capture webhook payload", exc_info=True)


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """Health check endpoint with DB, LiteLLM, and Redis connectivity."""
    result: dict[str, Any] = {"status": "ok", "version": __version__}

    # Check database connectivity
    session_factory = request.app.state.session_factory
    if session_factory is not None:
        try:
            async with session_factory() as session:
                await session.execute(__import__("sqlalchemy").text("SELECT 1"))
            result["database"] = "ok"
        except Exception:
            result["database"] = "error"

    # Check LiteLLM proxy connectivity
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=5.0) as hc:
            resp = await hc.get(f"{settings.LITELLM_URL}/health")
            result["litellm"] = "ok" if resp.status_code == 200 else "error"
    except Exception:
        result["litellm"] = "unreachable"

    # Check Redis via shared manager (if configured)
    redis_manager = getattr(request.app.state, "redis_manager", None)
    if redis_manager is not None:
        try:
            await redis_manager.ping()
            result["redis"] = "ok"
            task_queue = getattr(request.app.state, "task_queue", None)
            if task_queue:
                result["queue_depth"] = await task_queue.depth()
                result["dlq_depth"] = await task_queue.dlq_depth()
        except Exception:
            result["redis"] = "error"

    # Determine overall status
    checks = {
        k: v
        for k, v in result.items()
        if k not in ("status", "version", "queue_depth", "dlq_depth")
    }
    if any(v != "ok" for v in checks.values()):
        result["status"] = "degraded"

    return result


@router.get("/metrics")
async def metrics(request: Request) -> JSONResponse:
    """Processing metrics. Requires Redis."""
    redis_manager = getattr(request.app.state, "redis_manager", None)
    task_queue = getattr(request.app.state, "task_queue", None)

    if redis_manager is None or task_queue is None:
        return JSONResponse(
            status_code=503,
            content={"error": "Redis not configured — metrics unavailable"},
        )

    worker_keys = await redis_manager.scan_keys("forge:worker:*")

    return JSONResponse(
        content={
            "queue_depth": await task_queue.depth(),
            "dlq_depth": await task_queue.dlq_depth(),
            "workers_active": len(worker_keys),
            "tasks_processed_1h": await redis_manager.get_stat("processed", hours=1),
            "tasks_failed_1h": await redis_manager.get_stat("failed", hours=1),
        }
    )


@router.post("/webhook", status_code=202)
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_gitlab_token: str | None = Header(None),
    x_gitlab_event: str | None = Header(None),
) -> dict[str, Any]:
    """Accept GitLab webhook events."""
    settings = request.app.state.settings
    validate_webhook_token(
        expected_secret=settings.GITLAB_WEBHOOK_SECRET.get_secret_value(),
        x_gitlab_token=x_gitlab_token,
    )

    event_header = x_gitlab_event or "unknown"
    raw_body = await request.body()
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    _capture_webhook_payload(settings, event_header, payload)

    # Parse into typed event model
    try:
        event = parse_webhook(event_header, payload)
    except ValidationError:
        logger.warning(
            "Failed to parse webhook payload for event: %s",
            event_header,
            exc_info=True,
        )
        return {"status": "accepted", "event": event_header, "parsed": False}

    # Bot-loop prevention
    if is_bot_event(event, settings.FORGE_BOT_USERNAME):
        logger.info(
            "Skipping bot-authored event: %s",
            event.object_kind,
            extra={"event": event.object_kind, "reason": "bot-loop"},
        )
        return {"status": "skipped", "reason": "bot-loop"}

    # Structured logging
    log_extra: dict[str, Any] = {
        "event": event.object_kind,
    }
    if event.project:
        log_extra["project"] = event.project.path_with_namespace
    if event.user:
        log_extra["user"] = event.user.username
    if hasattr(event, "object_attributes"):
        attrs = event.object_attributes
        if hasattr(attrs, "action") and attrs.action:
            log_extra["action"] = attrs.action
        if hasattr(attrs, "iid"):
            log_extra["iid"] = attrs.iid

    logger.info("Webhook event: %s", event.object_kind, extra=log_extra)

    # M1 durable run loop: /implement and /go notes bypass the legacy
    # orchestrator/flow dispatch entirely (they never need an LLM here).
    run_command = _match_run_command(event, settings)
    if run_command is not None:
        from forge.worker.tasks import create_run_command_task

        queue = getattr(request.app.state, "task_queue", None)
        note_id = event.object_attributes.id
        if queue is not None:
            # Content-stable identity: re-delivered note webhooks collapse.
            is_dup = await queue.is_duplicate(f"run:{run_command['project_id']}:{note_id}")
            if is_dup:
                return {
                    "status": "accepted",
                    "event": event.object_kind,
                    "deduplicated": True,
                }
            await queue.submit(create_run_command_task(run_command, note_id=note_id))
            return {
                "status": "accepted",
                "event": event.object_kind,
                "queued": True,
                "run_command": True,
            }

        # No Redis — execute in-process via BackgroundTasks.
        from forge.runs import execute_run_command

        session_factory = getattr(request.app.state, "session_factory", None)
        if session_factory is not None:
            background_tasks.add_task(
                execute_run_command,
                settings,
                request.app.state.forge_config,
                session_factory,
                run_command,
            )
            return {"status": "accepted", "event": event.object_kind, "run_command": True}

    # enqueue to Redis if available
    task_queue = getattr(request.app.state, "task_queue", None)
    if task_queue is not None:
        from forge.worker.tasks import compute_fingerprint, create_task

        fingerprint = compute_fingerprint(event)
        if await task_queue.is_duplicate(fingerprint):
            logger.info("Duplicate event suppressed: %s", fingerprint[:16])
            return {"status": "accepted", "event": event.object_kind, "deduplicated": True}

        task = create_task(event)
        await task_queue.submit(task)
        return {"status": "accepted", "event": event.object_kind, "queued": True}

    # fallback: in-process background task
    session_factory = request.app.state.session_factory
    if session_factory is not None and hasattr(request.app.state, "agent_registry"):
        orchestrator = Orchestrator(
            settings=settings,
            forge_config=request.app.state.forge_config,
            session_factory=session_factory,
            registry=request.app.state.agent_registry,
            mcp_manager=getattr(request.app.state, "mcp_manager", None),
        )
        background_tasks.add_task(orchestrator.handle_event, event)

    return {"status": "accepted", "event": event.object_kind}


@router.get("/mcp-tools")
async def list_mcp_tools(request: Request) -> list[dict[str, Any]]:
    """List all configured external MCP servers and their status."""
    mcp_manager = getattr(request.app.state, "mcp_manager", None)
    mcp_registry = getattr(request.app.state, "mcp_registry", None)

    if mcp_registry is None:
        return []

    result: list[dict[str, Any]] = []
    for config in mcp_registry.list_enabled():
        entry: dict[str, Any] = {
            "server": config.name,
            "url": config.url,
            "transport": config.transport,
            "description": config.description,
            "status": "unknown",
        }
        if mcp_manager is not None:
            try:
                mcp = await mcp_manager.get_tools(config.name)
                if mcp:
                    entry["status"] = "connected"
                    entry["tools"] = list(mcp.functions.keys())
                else:
                    entry["status"] = "unavailable"
            except Exception:
                entry["status"] = "error"
        result.append(entry)

    return result


@router.get("/flows/{flow_id}")
async def get_flow_status(flow_id: str, request: Request) -> JSONResponse:
    """Get the current status of a flow instance."""
    flow_state_mgr = getattr(request.app.state, "flow_state_mgr", None)
    if flow_state_mgr is None:
        return JSONResponse(
            status_code=503,
            content={"error": "Flows require Redis — not configured"},
        )

    flow = await flow_state_mgr.get(flow_id)
    if flow is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"Flow {flow_id} not found"},
        )

    # Look up flow definition for total steps count
    flow_loader = getattr(request.app.state, "flow_loader", None)
    total_steps = 0
    if flow_loader:
        flow_def = flow_loader.get(flow.flow_name)
        if flow_def:
            total_steps = len(flow_def.steps)

    return JSONResponse(
        content={
            "id": flow.id,
            "name": flow.flow_name,
            "status": flow.status,
            "current_step": flow.current_step,
            "total_steps": total_steps,
            "started_at": flow.started_at,
            "updated_at": flow.updated_at,
            "state": flow.state,
            "error": flow.error,
        }
    )
