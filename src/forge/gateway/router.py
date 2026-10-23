from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
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
from forge.deferred import DEFERRED_NOTE_COMMANDS, unavailable_reply
from forge.adaptive.command_router import (
    ADAPTIVE_NOTE_COMMANDS as _ADAPTIVE_NOTE_COMMANDS,
    adaptive_command_set,
)
from forge.adaptive.revisions import parse_review_feedback_note
from forge.durable.models import FlowRun, StepRun
from forge.gateway.durable_ingress import (
    cache_confirms_duplicate,
    mark_delivered_best_effort,
)
from forge.gateway.feedback import (
    REVIEW_FEEDBACK_NOTE_COMMANDS as _REVIEW_FEEDBACK_NOTE_COMMANDS,
    review_feedback_command_set,
)
from forge.gateway.github_webhook import github_router
from forge.gateway.mention import extract_mention
from forge.gateway.parser import parse_webhook
from forge.gateway.validator import is_bot_event, validate_webhook_token
from forge.gitlab.events import GitLabEvent, IssueEvent, NoteEvent, PipelineEvent

logger = logging.getLogger(__name__)

router = APIRouter()

# GitHub ingress (ADR-0019 slice) is mounted unconditionally; the endpoint itself answers
# 503 unless FORGE_GITHUB_ENABLED and the webhook secret are set — fail closed.
router.include_router(github_router)

#: Active durable run commands; deferred verbs are recognized only to refuse them.
_RUN_COMMANDS = frozenset(
    {"/implement", "/go", "/cancel", "/retry", "/status", "/why-blocked", "/reconcile"}
)

#: the review-feedback verbs (``/fix``/``/ask``) join the parsed set ONLY while
#: ``FORGE_REVIEW_FEEDBACK_ENABLED`` is on — with the default OFF the verbs are not
#: recognized at all (zero routing).


def _match_run_command(event: GitLabEvent, settings) -> dict[str, Any] | None:
    """Parse issue commands, MR feedback and explicit deferred-feature refusals."""
    if not isinstance(event, NoteEvent):
        return None

    bot_username = getattr(settings, "FORGE_BOT_USERNAME", "forge-bot")
    if event.user and event.user.username == bot_username:
        # Forge's own comments contain /go lines; a bot-authored note must never act as a
        # trigger or an approval, whatever the author's role.
        return None

    mention_pattern = getattr(settings, "FORGE_MENTION_PATTERN", "@forge")
    note_text = (event.object_attributes.note or "").strip()
    # the adaptive control verbs (pause /resume /steer /answer) join the parsed set ONLY
    # while FORGE_ADAPTIVE_COMMANDS_ENABLED is on — with the default OFF the verbs are not
    # recognized at all (zero routing).: the review-feedback verbs (fix /ask) follow the
    # same pattern behind FORGE_REVIEW_FEEDBACK_ENABLED.
    commands = (
        _RUN_COMMANDS
        | adaptive_command_set()
        | review_feedback_command_set()
        | DEFERRED_NOTE_COMMANDS
    )
    mention = extract_mention(note_text, mention_pattern, extra_commands=commands)

    slash_command: str | None = None
    if mention.is_mention and mention.slash_command in commands:
        slash_command = mention.slash_command
    else:
        # Bare commands are equally valid: extract_mention only parses @mentions, so
        # requiring one here silently rerouted "/implement" notes into the legacy path,
        # where nothing handles them.
        first_token = note_text.split(None, 1)[0] if note_text else ""
        if first_token in commands:
            slash_command = first_token
    if slash_command is None:
        return None

    on_issue = event.issue is not None
    feedback_command = slash_command in _REVIEW_FEEDBACK_NOTE_COMMANDS
    feedback_mr_iid: int | None = None
    if feedback_command:
        if on_issue or event.merge_request is None:
            # review feedback lives on MR discussions — the Draft MR the candidate opened.
            return None
        feedback_mr_iid = event.merge_request.iid
    elif not on_issue and slash_command != "/security":
        # Gate notes are posted on issues; MR notes keep the legacy paths — except the
        # unavailable /security reply and the review-feedback verbs, which are MR-bound BY
        # design.
        return None

    common = {
        "project_id": event.project.id if event.project else 0,
        "issue_iid": event.issue.iid if event.issue is not None else None,
        "author_username": event.user.username if event.user else "",
        "author_user_id": event.user.id if event.user else 0,
        # the note id is the /retry delivery identity — it keys the revival attempt's
        # idempotency (a redelivered webhook is a no-op).
        "note_id": event.object_attributes.id,
    }
    if feedback_command:
        # the reviewer's comment ON the Draft MR — the same durable run-command dispatch,
        # carrying the note identity (project + note id — a GitLab note id is unique inside
        # a project, the logical request identity), the MR and the discussion the request
        # binds to.
        if parse_review_feedback_note(note_text) is None:
            # A recognized verb with a malformed body (``/fix`` with no description): the
            # typed INGRESS refusal.
            return {
                **common,
                "command": "review_feedback_refused",
                "provider": "gitlab",
                "refusal_reason": "malformed_feedback_command",
            }
        return {
            **common,
            "command": "review_feedback",
            "provider": "gitlab",
            "mr_iid": feedback_mr_iid,
            "note_text": note_text,
            "discussion_id": event.object_attributes.discussion_id or "",
        }
    if slash_command in _ADAPTIVE_NOTE_COMMANDS:
        # the adaptive verbs normalize to ONE command and route to the ControlCommandRouter
        # (approver gate, work-scoped run resolution, OperatorControlService mailbox) — not
        # the classic run-command step path.
        return {
            **common,
            "command": "adaptive_control",
            "provider": "gitlab",
            "adaptive_verb": slash_command[1:],
            "note_text": note_text,
        }
    if slash_command == "/security":
        common["mr_iid"] = event.merge_request.iid if not on_issue and event.merge_request else None
        return {**common, "command": "feature_unavailable", "feature": "/security"}
    if slash_command == "/implement":
        # cutover: /implement takes the durable RunService path, not flows.
        return {**common, "command": "start_run"}
    if slash_command == "/cancel":
        return {**common, "command": "cancel", "note_text": event.object_attributes.note or ""}
    if slash_command == "/retry":
        # Tier 2: operator revival of a dead run on the same branch.
        return {**common, "command": "retry", "note_text": event.object_attributes.note or ""}
    if slash_command == "/status":
        # read-only operator snapshot of a run (bare = latest on the issue).
        return {**common, "command": "status", "note_text": event.object_attributes.note or ""}
    if slash_command == "/why-blocked":
        # read-only explanation of the terminal/blocked cause.
        return {
            **common,
            "command": "why_blocked",
            "note_text": event.object_attributes.note or "",
        }
    if slash_command == "/reconcile":
        # operator-driven publication-intent recovery (the ONE mutating new command;
        # approver-gated at the executor like /retry).
        return {**common, "command": "reconcile", "note_text": event.object_attributes.note or ""}
    return {**common, "command": "go", "note_text": event.object_attributes.note or ""}


def _label_title(label: Any) -> str:
    """The label's title across GitLab payload generations."""
    if isinstance(label, dict):
        return str(label.get("title") or "").strip()
    return str(label or "").strip()


def _match_issue_lifecycle(event: GitLabEvent, settings) -> dict[str, Any] | None:
    """Detect issue-update events that belong to the #29 lifecycle commands."""
    if not isinstance(event, IssueEvent):
        return None
    attrs = event.object_attributes
    if (attrs.action or "") != "update":
        return None

    project_id = event.project.id if event.project else 0
    author = event.user.username if event.user else ""
    updated_at = str(attrs.updated_at or "")
    trigger_label = (
        str(getattr(settings, "FORGE_TRIGGER_LABEL", "forge") or "forge").strip().lower()
    )

    labels_change = (event.changes or {}).get("labels")
    if isinstance(labels_change, dict):
        previous = {_label_title(label).lower() for label in (labels_change.get("previous") or [])}
        current = {_label_title(label).lower() for label in (labels_change.get("current") or [])}
        if previous and trigger_label in previous and trigger_label not in current:
            delivery_key = f"unlabel:{attrs.id}:{updated_at}"
            return {
                "command": "unlabeled",
                "provider": "gitlab",
                "project_id": project_id,
                "issue_iid": attrs.iid,
                "author_username": author,
                "note_text": "",
                "note_id": delivery_key,
                "delivery_key": delivery_key,
            }

    changes = event.changes or {}
    if "title" in changes or "description" in changes:
        title = str(attrs.title or "")
        body = str(attrs.description or "")
        digest = hashlib.sha256(f"{title}\n{body}".encode("utf-8")).hexdigest()
        delivery_key = f"edit:{attrs.id}:{digest}:{updated_at}"
        return {
            "command": "issue_edited",
            "provider": "gitlab",
            "project_id": project_id,
            "issue_iid": attrs.iid,
            "issue_title": title,
            "issue_body": body,
            "author_username": author,
            "note_text": "",
            "note_id": delivery_key,
            "delivery_key": delivery_key,
        }
    return None


def _capture_webhook_payload(
    settings: Any,
    event_header: str,
    payload: dict[str, Any],
    *,
    header_field: str = "x_gitlab_event",
) -> None:
    """Persist a raw webhook payload for diagnostics (FORGE_CAPTURE_DIR)."""
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


# parity: the authoritative duplicate lookup lives in forge.gateway.durable_ingress
# (imported above) — ONE implementation shared by all three provider gateways, never
# duplicated per gateway. closed the residual: this gateway CONSUMES the shared
# cache_confirms_duplicate/mark_delivered_best_effort helpers like the GitHub gateways do —
# the probe→confirm sequence is no longer spelled inline here (a second spelling of the
# decision).


async def _ingest_run_command(
    request: Request,
    background_tasks: BackgroundTasks,
    event: GitLabEvent,
    run_command: dict[str, Any],
) -> dict[str, Any]:
    """Transactional ingress for run commands (ADR-0017 §1,)."""
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
    # Only the two dispatchers above reach this ingest path — note events (run commands),
    # the GitLabEvent models that carry ``object_attributes.id``.
    if isinstance(event, (NoteEvent, PipelineEvent)):
        note_id: int | str = event.object_attributes.id
    else:  # pragma: no cover — the dispatchers guarantee the attribute
        note_id = 0
    # Lifecycle commands (issue_edited/unlabeled) carry a per-delivery content key (digest +
    # updated_at): two distinct edits of ONE issue must not collapse onto one inbox
    # identity, while a redelivered edit still does.
    note_id = str(run_command.get("delivery_key") or note_id)
    source_event_id = command_source_event_id(
        run_command["command"], run_command["project_id"], note_id
    )
    delivery_uuid = str(run_command.get("delivery_uuid") or "")
    is_feedback = run_command.get("command") == "review_feedback"
    marker_keys = [f"run:{run_command['project_id']}:{note_id}"]
    if delivery_uuid:
        marker_keys.insert(0, f"delivery:{delivery_uuid}")

    if session_factory is None:
        # Honesty: without the durable store there is NOTHING to accept against — a 2xx here
        # would certify a command no worker can ever find. Refuse with the retryable 5xx
        # instead.
        logger.error("Run command without a database — refusing (no durable acceptance possible)")
        raise HTTPException(status_code=503, detail="Database not configured — command refused")

    if queue is not None and await cache_confirms_duplicate(
        queue, marker_keys, session_factory, source_event_id
    ):
        # the ONE shared decision helper answers — the GitLab gateway no longer spells the
        # probe→confirm sequence inline (the residual duplicate left behind on this leg).
        if is_feedback:
            logger.info("feedback.duplicate_delivery layer=cache-confirmed note=%s", note_id)
        return {"status": "accepted", "event": event.object_kind, "deduplicated": True}

    deduplicated = False
    started = time.monotonic()
    try:
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
                    # The scheduled step is the durability contract behind the 202: once
                    # this commits, a worker WILL attempt it.
                    await schedule_command_step(
                        session, run_command, source_event_id=source_event_id
                    )
                else:
                    deduplicated = True
    except Exception as exc:
        # Bounded retryable failure: the provider redelivers on 5xx and the next attempt
        # finds NO marker (markers are post-commit only), so the command lands exactly once.
        logger.exception(
            "Run command ingress transaction failed — refusing (retryable), identity=%s",
            source_event_id[:12],
        )
        raise HTTPException(
            status_code=503,
            detail="Durable ingress failed — delivery not acknowledged",
        ) from exc
    logger.info(
        "ingress.durable_accept command=%s identity=%s latency_ms=%.1f deduplicated=%s",
        run_command["command"],
        source_event_id[:12],
        (time.monotonic() - started) * 1000.0,
        deduplicated,
    )

    if queue is not None:
        # Wake-up accelerators only — Postgres owns the work (ADR-0017).
        await mark_delivered_best_effort(queue, marker_keys)
        if deduplicated:
            if is_feedback:
                logger.info("feedback.duplicate_delivery layer=sql note=%s", note_id)
            return {"status": "accepted", "event": event.object_kind, "deduplicated": True}
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

    if deduplicated:
        # No queue to refresh — the authoritative read already proved the record; nothing to
        # execute (the original delivery's step stands).
        return {"status": "accepted", "event": event.object_kind, "deduplicated": True}

    # No Redis — dev fallback: execute the ALREADY-COMMITTED step in-process, through the
    # SAME claim/lease/fence protocol as the worker.
    background_tasks.add_task(
        run_pending_command_step,
        session_factory,
        settings,
        request.app.state.forge_config,
        source_event_id,
        owner=f"gateway-{uuid4().hex[:6]}",
    )
    return {"status": "accepted", "event": event.object_kind, "run_command": True}


async def _ingest_adaptive_control(
    request: Request,
    background_tasks: BackgroundTasks,
    run_command: dict[str, Any],
    event: GitLabEvent,
) -> dict[str, Any]:
    """ingress for adaptive operator commands (pause /resume /steer /answer)."""
    result = await _ingest_run_command(request, background_tasks, event, run_command)
    return {**result, "adaptive_command": True}


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
    """Collect queue/worker stats (Redis-backed) and run counts (DB-backed)."""
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


#: The acceptance ladder (ADR-0021 §5, F34): a run's CURRENT status mapped to the furthest
#: ladder rung that status implies.
_LADDER_RUNGS: tuple[str, ...] = (
    "started",
    "planned",
    "gate_approved",
    "candidate_published",
    "ci_passed",
    "ready_for_human",
)

#: furthest rung per lifecycle status (statuses below a rung's bar are omitted — they
#: default to "started").
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
    """Bucket runs by the furthest ladder rung their status has reached."""
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
    """Prometheus exposition of the processing metrics (F30)."""
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
    x_gitlab_event_uuid: str | None = Header(None),
) -> dict[str, Any]:
    """Accept GitLab webhook events."""
    settings = request.app.state.settings
    gitlab_enabled = getattr(settings, "FORGE_GITLAB_ENABLED", True)
    validate_webhook_token(
        expected_secret=settings.GITLAB_WEBHOOK_SECRET.get_secret_value() if gitlab_enabled else "",
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

    # durable run loop: /implement and /go notes bypass the legacy command dispatch commits
    # durable acceptance without a model call here.
    run_command = _match_run_command(event, settings)
    if run_command is not None:
        if run_command.get("command") == "feature_unavailable":
            return unavailable_reply(run_command["feature"])
        if run_command.get("command") == "review_feedback_refused":
            # the typed ingress refusal for a malformed feedback verb — a 2xx WITH the
            # reason (a 4xx spike counts toward GitLab's hook auto-disable: 4 failures → 24h
            # backoff project-wide), and no run command (nothing can activate).
            logger.info(
                "feedback.refusal_reason=%s note=%s project=%s",
                run_command["refusal_reason"],
                run_command.get("note_id"),
                run_command.get("project_id"),
            )
            return {
                "status": "accepted",
                "event": event.object_kind,
                "feedback": "refused",
                "refusal_reason": run_command["refusal_reason"],
            }
        if run_command.get("command") == "adaptive_control":
            # the adaptive verbs take the SAME durable inbox + scheduled-step acceptance as
            # the classic commands; the step executor routes to the ControlCommandRouter.
            return await _ingest_adaptive_control(request, background_tasks, run_command, event)
        if run_command.get("command") == "review_feedback":
            # the transport identity rides the metadata (audit + transport-layer dedup); the
            # LOGICAL identity stays the note id — the request, the reply journal and the
            # revision decision all key on it, so an exact redelivery observes the original
            # request and never a second paid correction run.
            run_command = {**run_command, "delivery_uuid": x_gitlab_event_uuid or ""}
            logger.info(
                "feedback.ingress_received note=%s project=%s mr=%s delivery=%s",
                run_command.get("note_id"),
                run_command.get("project_id"),
                run_command.get("mr_iid"),
                x_gitlab_event_uuid or "-",
            )
        return await _ingest_run_command(request, background_tasks, event, run_command)

    # 29 lifecycle commands: issue updates (edit → replan, trigger-label removal → cancel at
    # the gate) route through the SAME durable step path.
    lifecycle_command = _match_issue_lifecycle(event, settings)
    if lifecycle_command is not None:
        return await _ingest_run_command(request, background_tasks, event, lifecycle_command)

    if isinstance(event, PipelineEvent) and event.object_attributes.status == "failed":
        return unavailable_reply("ci_debug")

    return {"status": "ignored", "event": event.object_kind, "reason": "no_supported_command"}


def _require_api_read_auth(request: Request) -> None:
    """Bearer-token gate for the management read API (F30)."""
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


#: run.evidence keys kept in the read model, nested per section.
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
