from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from sqlalchemy import func, select

from forge import __version__
from forge.durable.models import FlowRun, StepRun
from forge.gateway.github_webhook import github_router
from forge.gateway.mention import extract_mention
from forge.gateway.parser import parse_webhook
from forge.gateway.validator import is_bot_event, validate_webhook_token
from forge.gitlab.events import GitLabEvent, NoteEvent, PipelineEvent
from forge.orchestrator.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

router = APIRouter()

# GitHub ingress (ADR-0019 slice) is mounted unconditionally; the endpoint
# itself answers 503 unless FORGE_GITHUB_ENABLED and the webhook secret are
# set — fail closed.
router.include_router(github_router)

#: Commands served by the durable run service (M1), not the legacy flow engine.
#: ``/security`` (v0.7) joins them — the durable triage step — and is the ONLY
#: run command also accepted on MR notes (triage targets MRs, issues and PRs;
#: /implement, /go and /cancel stay issue-bound).
_RUN_COMMANDS = frozenset({"/implement", "/go", "/cancel", "/retry", "/security"})


def _match_run_command(event: GitLabEvent, settings) -> dict[str, Any] | None:
    """Detect note events that belong to the durable run loop (M1 + v0.7).

    - ``<mention> /implement`` on an issue → ``start_run``.
    - ``<mention> /go <run-id>`` on an issue → ``handle_command_note``.
    - ``<mention> /security`` on an issue **or MR** → ``security_triage``
      (the provider-neutral durable triage step, v0.7).

    Respects ``FORGE_MENTION_PATTERN``. Returns the run_command metadata dict,
    or None when the event should take its legacy path.
    """
    if not isinstance(event, NoteEvent):
        return None

    bot_username = getattr(settings, "FORGE_BOT_USERNAME", "forge-bot")
    if event.user and event.user.username == bot_username:
        # Forge's own comments contain /go lines; a bot-authored note must
        # never act as a trigger or an approval, whatever the author's role.
        return None

    mention_pattern = getattr(settings, "FORGE_MENTION_PATTERN", "@forge")
    note_text = (event.object_attributes.note or "").strip()
    mention = extract_mention(note_text, mention_pattern, extra_commands=_RUN_COMMANDS)

    slash_command: str | None = None
    if mention.is_mention and mention.slash_command in _RUN_COMMANDS:
        slash_command = mention.slash_command
    else:
        # Bare commands are equally valid: extract_mention only parses
        # @mentions, so requiring one here silently rerouted "/implement"
        # notes into the legacy path, where nothing handles them.
        first_token = note_text.split(None, 1)[0] if note_text else ""
        if first_token in _RUN_COMMANDS:
            slash_command = first_token
    if slash_command is None:
        return None

    on_issue = event.issue is not None
    if not on_issue and slash_command != "/security":
        # Gate notes are posted on issues; MR notes keep the legacy paths —
        # except /security, which is MR/issue/PR-neutral by contract.
        return None

    common = {
        "project_id": event.project.id if event.project else 0,
        "issue_iid": event.issue.iid if event.issue is not None else None,
        "author_username": event.user.username if event.user else "",
        "author_user_id": event.user.id if event.user else 0,
    }
    if slash_command == "/security":
        common["mr_iid"] = event.merge_request.iid if not on_issue and event.merge_request else None
        return {**common, "command": "security_triage", "note_text": note_text}
    if slash_command == "/implement":
        # M1 cutover: /implement takes the durable RunService path, not flows.
        return {**common, "command": "start_run"}
    if slash_command == "/cancel":
        return {**common, "command": "cancel", "note_text": event.object_attributes.note or ""}
    if slash_command == "/retry":
        return {**common, "command": "retry", "note_text": event.object_attributes.note or ""}
    return {**common, "command": "go", "note_text": event.object_attributes.note or ""}


def _match_pipeline_debug(event: GitLabEvent, settings) -> dict[str, Any] | None:
    """Detect failed pipeline events that belong to the durable CI debug lane.

    v0.7 (research F3 port): a ``pipeline`` hook with ``status == "failed"``
    is normalized to a ``debug_pipeline`` command and routed through the
    SAME durable step path as the run commands — the durable equivalent of
    the legacy reactive pipeline-debugger (which no longer sees failed
    pipeline events; successful/active pipelines keep the legacy path).
    The executor (:mod:`forge.reactive.ci_debug`) skips forge's own
    ``factory/`` branches — the run's bounded repair loop already fetches
    failed-job logs and posts the repair-cycle MR note — and correlates the
    remaining failures to their MR for the root-cause comment.
    """
    if not isinstance(event, PipelineEvent):
        return None
    attrs = event.object_attributes
    if (attrs.status or "") != "failed":
        return None
    return {
        "command": "debug_pipeline",
        "project_id": event.project.id if event.project else 0,
        "pipeline_id": attrs.id,
        "branch": attrs.ref or "",
        "sha": attrs.sha or "",
        "mr_iid": event.merge_request.iid if event.merge_request else None,
        "author_username": event.user.username if event.user else "",
    }


def _capture_webhook_payload(
    settings: Any,
    event_header: str,
    payload: dict[str, Any],
    *,
    header_field: str = "x_gitlab_event",
) -> None:
    """Persist a raw webhook payload for diagnostics (FORGE_CAPTURE_DIR).

    Stores the event header and payload only — never the webhook secret.
    Failures to capture must never break ingestion. *header_field* names the
    record key for the event header (the GitHub ingress passes its own).
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
            header_field: event_header,
            "payload": payload,
        }
        target = path / name
        target.write_text(json.dumps(record, indent=2, sort_keys=True))
        logger.info("Captured webhook payload: %s", target)
    except OSError:
        logger.warning("Failed to capture webhook payload", exc_info=True)


async def _ingest_run_command(
    request: Request,
    background_tasks: BackgroundTasks,
    event: GitLabEvent,
    run_command: dict[str, Any],
) -> dict[str, Any]:
    """Transactional ingress for run commands (ADR-0017 §1).

    ONE transaction persists the inbox row (native delivery identity) and the
    first scheduled step; the ``202`` is answered only after that commit, so
    the command is durable in Postgres before it is acknowledged. The Redis
    dedup check in front of the transaction is an accelerator, not the
    authority — the inbox unique index decides; the queue submit and the
    step-wake push after the commit are accelerators too, and losing them
    only costs one step-worker poll interval.
    """
    from forge.durable import ingest_event
    from forge.worker.steps import (
        STEP_WAKE_KEY,
        command_source_event_id,
        run_pending_command_step,
        schedule_command_step,
    )
    from forge.worker.tasks import create_run_command_task

    settings = request.app.state.settings
    session_factory = getattr(request.app.state, "session_factory", None)
    queue = getattr(request.app.state, "task_queue", None)
    # Only the two dispatchers above reach this ingest path — note events
    # (run commands) and pipeline events (the CI debug lane), the two
    # GitLabEvent models that carry ``object_attributes.id``.
    if isinstance(event, (NoteEvent, PipelineEvent)):
        note_id = event.object_attributes.id
    else:  # pragma: no cover — the dispatchers guarantee the attribute
        note_id = 0
    source_event_id = command_source_event_id(
        run_command["command"], run_command["project_id"], note_id
    )

    if queue is not None:
        # Content-stable identity: re-delivered note webhooks collapse. This
        # SET-NX check is the fast path only — a false negative still gets
        # caught by the inbox unique index below.
        if await queue.is_duplicate(f"run:{run_command['project_id']}:{note_id}"):
            return {"status": "accepted", "event": event.object_kind, "deduplicated": True}

    if session_factory is not None:
        deduplicated = False
        async with session_factory() as session:
            async with session.begin():
                _, created = await ingest_event(
                    session,
                    source_event_id=source_event_id,
                    project_id=run_command["project_id"],
                    event_type="run_command",
                    payload={**run_command, "note_id": note_id},
                )
                if created:
                    # The scheduled step is the durability contract behind
                    # the 202: once this commits, a worker WILL attempt it.
                    await schedule_command_step(
                        session, run_command, source_event_id=source_event_id
                    )
                else:
                    deduplicated = True
        if deduplicated:
            return {"status": "accepted", "event": event.object_kind, "deduplicated": True}

    if queue is not None:
        # Wake-up accelerators only — Postgres owns the work (ADR-0017).
        try:
            await queue.submit(create_run_command_task(run_command, note_id=note_id))
        except Exception:
            logger.warning("Run command queue wake-up failed", exc_info=True)
        redis_manager = getattr(request.app.state, "redis_manager", None)
        if redis_manager is not None:
            try:
                await redis_manager.lpush(STEP_WAKE_KEY, source_event_id)
            except Exception:
                logger.debug("Run command step wake-up failed", exc_info=True)
        return {
            "status": "accepted",
            "event": event.object_kind,
            "queued": True,
            "run_command": True,
        }

    if session_factory is not None:
        # No Redis — dev fallback: execute the persisted step in-process,
        # through the SAME claim/lease/fence protocol as the worker.
        background_tasks.add_task(
            run_pending_command_step,
            session_factory,
            settings,
            request.app.state.forge_config,
            source_event_id,
            owner=f"gateway-{uuid4().hex[:6]}",
        )
        return {"status": "accepted", "event": event.object_kind, "run_command": True}

    return {"status": "accepted", "event": event.object_kind}


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


async def _gather_metrics(request: Request) -> dict[str, Any]:
    """Collect queue/worker stats (Redis-backed) and run counts (DB-backed).

    Redis and the database are optional at read time: without either, the
    corresponding numbers degrade to zero/empty rather than failing the
    endpoint (the JSON /metrics keeps its historical 503-without-Redis
    contract; the Prometheus exposition never fails — F30).
    """
    redis_manager = getattr(request.app.state, "redis_manager", None)
    task_queue = getattr(request.app.state, "task_queue", None)

    snapshot: dict[str, Any] = {
        "queue_depth": 0,
        "dlq_depth": 0,
        "workers_active": 0,
        "tasks_processed_1h": 0,
        "tasks_failed_1h": 0,
        "runs_by_status": {},
    }

    if redis_manager is not None and task_queue is not None:
        worker_keys = await redis_manager.scan_keys("forge:worker:*")
        snapshot["queue_depth"] = await task_queue.depth()
        snapshot["dlq_depth"] = await task_queue.dlq_depth()
        snapshot["workers_active"] = len(worker_keys)
        snapshot["tasks_processed_1h"] = await redis_manager.get_stat("processed", hours=1)
        snapshot["tasks_failed_1h"] = await redis_manager.get_stat("failed", hours=1)

    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is not None:
        async with session_factory() as session:
            rows = await session.execute(
                select(FlowRun.status, func.count()).group_by(FlowRun.status)
            )
            snapshot["runs_by_status"] = {status: count for status, count in rows.all()}
        snapshot["delivery_ladder"] = _delivery_ladder(snapshot["runs_by_status"])

    return snapshot


#: The acceptance ladder (ADR-0021 §5, F34): a run's CURRENT status mapped to
#: the furthest ladder rung that status implies. Runs that regressed (a
#: failed repair after CI) count only at their present rung — this is a
#: gauge of where work packages stand, not a cumulative funnel; the
#: merged-without-rework rung lives provider-side (the bot never merges) and
#: is intentionally absent.
_LADDER_RUNGS: tuple[str, ...] = (
    "started",
    "planned",
    "gate_approved",
    "candidate_published",
    "ci_passed",
    "ready_for_human",
)

#: furthest rung per lifecycle status (statuses below a rung's bar are
#: omitted — they default to "started").
_LADDER_BY_STATUS: dict[str, str] = {
    "waiting_approval": "planned",
    "proposing": "gate_approved",
    "validating": "gate_approved",
    "committing": "gate_approved",
    "waiting_harness": "candidate_published",
    "ensuring_draft_mr": "candidate_published",
    "waiting_ci": "candidate_published",
    "evaluating_ci": "candidate_published",
    "reviewing": "ci_passed",
    "ready_for_human": "ready_for_human",
}


def _delivery_ladder(runs_by_status: dict[str, int]) -> dict[str, int]:
    """Bucket runs by the furthest ladder rung their status has reached.

    Terminal failure states (``failed``/``blocked``/``cancelled``) and
    in-flight early states count at ``started`` — the work package entered
    the factory but has not (yet) cleared the gate.
    """
    ladder = {rung: 0 for rung in _LADDER_RUNGS}
    for status, count in runs_by_status.items():
        rung = _LADDER_BY_STATUS.get(status, "started")
        ladder[rung] += count
    return ladder


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

    snapshot = await _gather_metrics(request)

    return JSONResponse(
        content={
            "queue_depth": snapshot["queue_depth"],
            "dlq_depth": snapshot["dlq_depth"],
            "workers_active": snapshot["workers_active"],
            "tasks_processed_1h": snapshot["tasks_processed_1h"],
            "tasks_failed_1h": snapshot["tasks_failed_1h"],
            "runs_by_status": snapshot["runs_by_status"],
            "delivery_ladder": snapshot.get("delivery_ladder", {}),
        }
    )


@router.get("/metrics.prometheus")
async def metrics_prometheus(request: Request) -> Response:
    """Prometheus exposition of the processing metrics (F30).

    Same stat keys as the JSON /metrics, plus the durable-run gauge
    ``forge_runs_by_status``. Served as text/plain (Prometheus format
    version 4.0.4); never 5xx — missing backends report zero.
    """
    snapshot = await _gather_metrics(request)

    lines = [
        "# HELP forge_queue_depth Tasks waiting in the Redis queue.",
        "# TYPE forge_queue_depth gauge",
        f"forge_queue_depth {snapshot['queue_depth']}",
        "# HELP forge_dlq_depth Tasks parked in the dead-letter queue.",
        "# TYPE forge_dlq_depth gauge",
        f"forge_dlq_depth {snapshot['dlq_depth']}",
        "# HELP forge_workers_active Workers with a live heartbeat key.",
        "# TYPE forge_workers_active gauge",
        f"forge_workers_active {snapshot['workers_active']}",
    ]
    for status in sorted(snapshot["runs_by_status"]):
        count = snapshot["runs_by_status"][status]
        lines += [
            "# HELP forge_runs_by_status Durable flow runs by lifecycle status.",
            "# TYPE forge_runs_by_status gauge",
            f'forge_runs_by_status{{status="{status}"}} {count}',
        ]
    for rung in _LADDER_RUNGS:
        count = snapshot.get("delivery_ladder", {}).get(rung, 0)
        lines += [
            "# HELP forge_delivery_ladder Acceptance ladder: runs at-or-past each rung (F34).",
            "# TYPE forge_delivery_ladder gauge",
            f'forge_delivery_ladder{{stage="{rung}"}} {count}',
        ]
    lines += [
        "# HELP forge_tasks_processed_total Tasks processed (last hour window).",
        "# TYPE forge_tasks_processed_total counter",
        f"forge_tasks_processed_total {snapshot['tasks_processed_1h']}",
        "# HELP forge_tasks_failed_total Tasks failed (last hour window).",
        "# TYPE forge_tasks_failed_total counter",
        f"forge_tasks_failed_total {snapshot['tasks_failed_1h']}",
    ]
    return Response(
        content="\n".join(lines) + "\n",
        media_type="text/plain; version=4.0.4; charset=utf-8",
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
        return await _ingest_run_command(request, background_tasks, event, run_command)

    # v0.7 durable CI debug lane: failed pipelines bypass the legacy
    # orchestrator the same way (the durable step IS the replacement).
    pipeline_debug = _match_pipeline_debug(event, settings)
    if pipeline_debug is not None:
        return await _ingest_run_command(request, background_tasks, event, pipeline_debug)

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


def _require_api_read_auth(request: Request) -> None:
    """Bearer-token gate for the management read API (F30).

    When ``FORGE_API_READ_TOKEN`` is set, GET /runs* require
    ``Authorization: Bearer <token>`` (constant-time compare). When unset —
    dev default — the routes are open. The legacy unauthenticated
    ``/flows/{id}`` endpoint (raw Redis flow state) was removed entirely.
    """
    settings = request.app.state.settings
    token = getattr(settings, "FORGE_API_READ_TOKEN", None)
    if token is None:
        return
    expected = token.get_secret_value()
    header = request.headers.get("authorization", "")
    supplied = header[len("Bearer ") :].strip() if header.startswith("Bearer ") else ""
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API read token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _run_summary(run: FlowRun) -> dict[str, Any]:
    """Compact read-model projection of a durable flow run (F30)."""
    return {
        "id": run.id,
        "project_id": run.project_id,
        "issue_iid": run.issue_iid,
        "mr_iid": run.mr_iid,
        "status": run.status,
        "status_reason": run.status_reason,
        "commit_cycle": run.commit_cycle,
        "cancel_requested": run.cancel_requested,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
    }


#: run.evidence keys kept in the read model, nested per section. Everything
#: else (plan.files_hint, review.findings, harness.handle, ...) is bulky
#: internal context and is dropped from the API (F30).
_EVIDENCE_SUMMARY_FIELDS: dict[str, tuple[str, ...]] = {
    "plan": ("digest", "summary"),
    "pipeline": ("id", "url", "status", "sha"),
    "review": ("verdict", "sha", "summary"),
    "harness": ("pipeline_id", "job_id", "branch"),
}


def _evidence_summary(evidence: dict | None) -> dict[str, Any]:
    """Latest evidence summary — small proof fields only, never the payload."""
    if not evidence:
        return {}
    summary: dict[str, Any] = {}
    if evidence.get("backend"):
        summary["backend"] = evidence["backend"]
    for section, fields in _EVIDENCE_SUMMARY_FIELDS.items():
        values = evidence.get(section)
        if not isinstance(values, dict):
            continue
        picked = {key: values[key] for key in fields if values.get(key) is not None}
        if picked:
            summary[section] = picked
    return summary


@router.get("/runs", dependencies=[Depends(_require_api_read_auth)])
async def list_runs(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List durable flow runs, newest first (F30 read model)."""
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    async with session_factory() as session:
        runs = (
            (
                await session.execute(
                    select(FlowRun)
                    .order_by(FlowRun.created_at.desc(), FlowRun.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return {"runs": [_run_summary(run) for run in runs]}


@router.get("/runs/{run_id}", dependencies=[Depends(_require_api_read_auth)])
async def get_run(run_id: str, request: Request) -> dict[str, Any]:
    """One durable run: detail, steps and the evidence summary (F30)."""
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    async with session_factory() as session:
        run = await session.get(FlowRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
        steps = (
            (
                await session.execute(
                    select(StepRun).where(StepRun.flow_run_id == run_id).order_by(StepRun.id)
                )
            )
            .scalars()
            .all()
        )

    detail = _run_summary(run)
    detail["evidence"] = _evidence_summary(run.evidence)
    detail["steps"] = [
        {
            "id": step.id,
            "step_name": step.step_name,
            "status": step.status,
            "attempt": step.attempt,
            "started_at": step.started_at.isoformat() if step.started_at else None,
            "finished_at": step.finished_at.isoformat() if step.finished_at else None,
        }
        for step in steps
    ]
    return detail
