"""GitHub webhook ingress (ADR-0019 first slice).

Fail-closed by design:

- The route only accepts deliveries when ``FORGE_GITHUB_ENABLED`` is true AND
  ``FORGE_GITHUB_WEBHOOK_SECRET`` is set — anything else answers
  ``503 github ingress disabled`` (an unsigned delivery is never processed).
- Authenticity is the ``X-Hub-Signature-256`` HMAC-SHA256 over the RAW
  request bytes, compared in constant time (research §2.1). A missing header
  is invalid — if no secret is configured on the GitHub side, GitHub does not
  send the header at all.
- Responses are answered from durable state only: the ingestion commits an
  inbox row (and, for run commands, the first scheduled step) BEFORE the
  ``202`` — the same transactional contract as the GitLab ingress
  (ADR-0017 §1). GitHub retries nothing, so a dropped connection costs a
  redelivery, which the inbox identity collapses.

Event routing for this slice:

- ``ping`` → ``200 pong`` (the cheapest end-to-end check of URL + secret).
- ``issue_comment`` ``created`` → normalized run command routed through the
  SAME durable step path as GitLab commands (inbox row + scheduled step in
  one transaction). The EventInbox identity is connection-scoped:
  ``github:{installation_id}:{repo_full_name}`` + event + comment id.
- ``issues`` ``labeled`` → when the label name matches
  ``FORGE_TRIGGER_LABEL`` (default ``forge``, case-insensitive) the SAME
  run command is normalized (``start_run``) with the labeler as the actor
  (ADR-0020 §4: the honest agent-UX fallback — a label trigger, not a
  partner-program listing). Admission still applies downstream: only
  FORGE_APPROVERS logins actually start runs.
- ``pull_request`` ``opened``/``synchronize`` → the reactive review lane
  (v0.7, docs/research/github-reactive.md F1): Draft PRs in tracked repos
  are normalized to a ``review_pr`` command routed through the SAME durable
  step path (inbox row + scheduled step in one transaction). Bot senders,
  bot-authored PRs and forge's own ``forge/*`` branches are skipped — the
  reactive reviewer never reviews forge's own output.
- ``workflow_job`` ``completed`` with ``conclusion == "failure"`` → the CI
  debug lane (v0.7, github-reactive §4 F3): normalized to a ``debug_ci``
  command on the same durable step path. Forge's own harness runs
  (``forge/`` branches × ``FORGE_GITHUB_HARNESS_WORKFLOW``) are skipped at
  normalization — their failures have their own triage; the PR-author
  recursion guard and fork-safe ``head_sha`` correlation run in the
  executor (:mod:`forge.reactive.ci_debug`). Any other ``workflow_job``
  action is inbox-only.
- ``installation`` ``deleted``/``removed`` → the connection is disabled:
  logged and persisted as an inbox row (reconciliation hooks come with the
  v0.5 contracts extraction).
- everything else (``push``, ``workflow_run``, …) → inbox row only;
  reconciliation hooks arrive later.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from forge.durable import ingest_event
from forge.gateway.mention import extract_mention
from forge.worker.steps import STEP_WAKE_KEY, schedule_command_step
from forge.worker.tasks import create_run_command_task

logger = logging.getLogger(__name__)

github_router = APIRouter()

#: Commands served by the durable run loop — the same set the GitLab
#: ingress routes (forge.gateway.router._RUN_COMMANDS); the normalized
#: GitHub command lands on the same durable step path. ``/security`` (v0.7)
#: is the provider-neutral triage step — comments on issues AND PRs route
#: identically (``issue_is_pr`` is surfaced but does not change routing).
_GITHUB_RUN_COMMANDS = frozenset({"/implement", "/go", "/cancel", "/security"})

_COMMAND_MAP = {
    "/implement": "start_run",
    "/go": "go",
    "/cancel": "cancel",
    "/security": "security_triage",
}

#: installation webhook actions that mean "this connection is gone".
_INSTALLATION_REMOVED_ACTIONS = frozenset({"deleted", "removed"})

#: ``pull_request`` actions the reactive review lane reacts to (v0.7 F1):
#: a new Draft PR, or new commits pushed to one. Everything else
#: (``closed``, ``ready_for_review``, ``edited``, …) is inbox-only.
_PR_REVIEW_ACTIONS = frozenset({"opened", "synchronize"})


def verify_github_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Constant-time check of ``X-Hub-Signature-256`` over the RAW bytes.

    Value format: ``sha256=<hex hmac-sha256(secret, body)>``. A missing
    header is invalid (research §2.1).
    """
    if not signature_header or not secret:
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def github_connection_id(installation_id: Any, repo_full_name: str) -> str:
    """The connection-scoped identity prefix for inbox rows."""
    return f"github:{installation_id or 0}:{repo_full_name or ''}"


def github_source_event_id(connection_id: str, event: str, action: str, delivery_key: str) -> str:
    """Content-stable inbox identity for a GitHub delivery.

    sha256 over connection id + event + action + a per-event delivery key
    (the comment id for ``issue_comment``, the ``X-GitHub-Delivery`` GUID
    otherwise). Re-delivered webhooks collapse onto one id — and since a
    manual redelivery intentionally REUSES the delivery GUID (research
    §2.2), dedupe means drop-duplicates, which is exactly what the inbox
    unique index does.
    """
    material = "|".join((connection_id, event, action, str(delivery_key)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def normalize_issue_comment(
    payload: dict[str, Any],
    mention_pattern: str = "@forge",
) -> dict[str, Any] | None:
    """Normalize an ``issue_comment`` payload into run-command metadata.

    Returns None when the comment carries no forge command. PR comments are
    distinguished by the ``pull_request`` key on the issue object (research
    §2.3) and surfaced as ``issue_is_pr`` — routing treats them the same for
    now; the flow decides what a PR-comment command means post-v0.5.
    """
    issue = payload.get("issue") or {}
    comment = payload.get("comment") or {}
    repository = payload.get("repository") or {}
    installation = payload.get("installation") or {}
    body = str(comment.get("body") or "").strip()

    slash_command: str | None = None
    mention = extract_mention(body, mention_pattern, extra_commands=_GITHUB_RUN_COMMANDS)
    if mention.is_mention and mention.slash_command in _GITHUB_RUN_COMMANDS:
        slash_command = mention.slash_command
    else:
        # Bare commands are equally valid (same rule as the GitLab ingress):
        # extract_mention only parses @mentions, so requiring one here would
        # silently drop "/implement" notes that lack it.
        first_token = body.split(None, 1)[0] if body else ""
        if first_token in _GITHUB_RUN_COMMANDS:
            slash_command = first_token
    if slash_command is None:
        return None

    return {
        "command": _COMMAND_MAP[slash_command],
        "provider": "github",
        "connection_id": github_connection_id(
            installation.get("id"), str(repository.get("full_name") or "")
        ),
        "project_id": int(repository.get("id") or 0),
        "repo_full_name": str(repository.get("full_name") or ""),
        "issue_number": int(issue.get("number") or 0),
        "issue_is_pr": "pull_request" in issue,
        "author_username": str(
            (comment.get("user") or {}).get("login")
            or (payload.get("sender") or {}).get("login")
            or ""
        ),
        "note_text": body,
        "note_id": comment.get("id"),
    }


def normalize_labeled_event(
    payload: dict[str, Any],
    trigger_label: str = "forge",
) -> dict[str, Any] | None:
    """Normalize an ``issues.labeled`` payload into run-command metadata.

    Fires only when the applied label matches *trigger_label*
    case-insensitively (ADR-0020 §4). The labeler (``sender.login``) becomes
    the acting user — the run then crosses the SAME admission gate as a
    /implement, so a non-approver's label starts nothing. Any other label
    (or an unlabeled issue payload) returns None → inbox-only recording.
    """
    issue = payload.get("issue") or {}
    label = payload.get("label") or {}
    repository = payload.get("repository") or {}
    installation = payload.get("installation") or {}

    name = str(label.get("name") or "").strip().lower()
    if not trigger_label or name != trigger_label.strip().lower():
        return None

    return {
        "command": "start_run",
        "provider": "github",
        "connection_id": github_connection_id(
            installation.get("id"), str(repository.get("full_name") or "")
        ),
        "project_id": int(repository.get("id") or 0),
        "repo_full_name": str(repository.get("full_name") or ""),
        "issue_number": int(issue.get("number") or 0),
        "issue_is_pr": "pull_request" in issue,
        "author_username": str((payload.get("sender") or {}).get("login") or ""),
        "note_text": "",
        # The ISSUE id, not the label id: the label id is constant across
        # every issue carrying it, so keying dedupe on it silently swallowed
        # every labeled event after the first (LIVE-found on forcewake/forge:
        # #28 auto-planned, #29 with the same label never did). Issue-scoped
        # identity keeps redelivery dedup intact while distinct issues fire.
        "note_id": issue.get("id"),
    }


def normalize_pull_request_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a ``pull_request`` payload into a reactive-review command.

    Fires only for ``opened``/``synchronize`` on **Draft** PRs (the reactive
    lane's v0.7 scope, research §8 F1) and never for forge's own output:
    bot-authored PRs and the ``forge/`` head-branch prefix — E1's durable
    flow publishes forge commits there — return None (inbox-only record).
    The sender-type Bot guard lives at the routing site (it answers
    ``skipped`` rather than recording).

    The payload's ``before``/``after`` SHAs travel in the metadata so the
    engine gets the synchronize delta without extra API reads (research
    §1.4); a zero ``before`` (branch creation) is blanked to force a full
    review. The delivery key is content-stable — ``pr:<n>:<action>:<after>``
    — so a redelivered push collapses onto one inbox identity even when the
    delivery GUID differs.
    """
    pull_request = payload.get("pull_request") or {}
    if not pull_request:
        return None
    action = str(payload.get("action") or "")
    if action not in _PR_REVIEW_ACTIONS:
        return None
    # Reviews fire for draft AND ready PRs alike — the draft marker is not
    # a skip signal (research §6: major agents review drafts too).

    head = pull_request.get("head") or {}
    head_branch = str(head.get("ref") or "")
    pr_author = pull_request.get("user") or {}
    if str(pr_author.get("type") or "") == "Bot":
        return None
    if head_branch.startswith("forge/"):
        # forge's own durable-flow branch: reviewing forge's own run output
        # would self-trigger (research §6.2).
        logger.info(
            "Skipping forge-owned branch %s on reactive review",
            head_branch,
            extra={"event": "pull_request"},
        )
        return None

    repository = payload.get("repository") or {}
    installation = payload.get("installation") or {}
    number = int(pull_request.get("number") or 0)
    after = str(payload.get("after") or head.get("sha") or "")
    before = str(payload.get("before") or "")
    if set(before) <= {"0"}:
        before = ""  # branch creation — no delta exists
    sender = payload.get("sender") or {}
    delivery_key = f"pr:{number}:{action}:{after}"

    return {
        "command": "review_pr",
        "provider": "github",
        "connection_id": github_connection_id(
            installation.get("id"), str(repository.get("full_name") or "")
        ),
        "project_id": int(repository.get("id") or 0),
        "repo_full_name": str(repository.get("full_name") or ""),
        "issue_number": number,
        "pr_number": number,
        "action": action,
        "head_sha": after,
        "after_sha": after,
        "before_sha": before,
        "head_branch": head_branch,
        "sender_type": str(sender.get("type") or ""),
        "pr_author_type": str(pr_author.get("type") or ""),
        "author_username": str(sender.get("login") or ""),
        "note_text": "",
        "note_id": delivery_key,  # stable fast-path dedup + task identity
        "delivery_key": delivery_key,
    }


def normalize_workflow_job_event(
    payload: dict[str, Any],
    harness_workflow: str = "",
) -> dict[str, Any] | None:
    """Normalize a failed ``workflow_job`` payload into a ``debug_ci`` command.

    Fires only for ``completed`` jobs with ``conclusion == "failure"`` (the
    per-step-granularity trigger, github-reactive research §4.1). Forge's
    own harness runs are skipped at normalization: a ``forge/`` head branch
    running ``FORGE_GITHUB_HARNESS_WORKFLOW`` is the durable flow's own
    execution, whose failures have their own triage — debugging them here
    would recurse into forge's own output. The PR-author recursion guard
    and the fork-safe ``head_sha`` → PR correlation live in the executor
    (:mod:`forge.reactive.ci_debug`), which needs API reads the ingress
    must not pay for.

    The delivery key is content-stable — ``wfjob:{job_id}:{conclusion}`` —
    so re-delivered completions collapse onto one inbox identity.
    """
    workflow_job = payload.get("workflow_job") or {}
    if not workflow_job:
        return None
    if str(payload.get("action") or "") != "completed":
        return None
    conclusion = str(workflow_job.get("conclusion") or "")
    if conclusion != "failure":
        return None

    head_branch = str(workflow_job.get("head_branch") or "")
    workflow_name = str(payload.get("workflow_name") or "")
    if (
        head_branch.startswith("forge/")
        and bool(harness_workflow)
        and workflow_name == harness_workflow
    ):
        logger.info(
            "Skipping forge harness run %s on %s — no CI debug",
            workflow_name,
            head_branch,
            extra={"event": "workflow_job"},
        )
        return None

    repository = payload.get("repository") or {}
    installation = payload.get("installation") or {}
    sender = payload.get("sender") or {}
    job_id = int(workflow_job.get("id") or 0)
    delivery_key = f"wfjob:{job_id}:{conclusion}"

    return {
        "command": "debug_ci",
        "provider": "github",
        "connection_id": github_connection_id(
            installation.get("id"), str(repository.get("full_name") or "")
        ),
        "project_id": int(repository.get("id") or 0),
        "repo_full_name": str(repository.get("full_name") or ""),
        "head_sha": str(workflow_job.get("head_sha") or ""),
        "head_branch": head_branch,
        "job_id": job_id,
        "run_id": int(workflow_job.get("run_id") or 0),
        "job_name": str(workflow_job.get("name") or ""),
        "workflow_name": workflow_name,
        "conclusion": conclusion,
        "html_url": str(workflow_job.get("html_url") or ""),
        "sender_type": str(sender.get("type") or ""),
        "author_username": str(sender.get("login") or ""),
        "note_text": "",
        "note_id": delivery_key,  # stable fast-path dedup + task identity
        "delivery_key": delivery_key,
    }


@github_router.post("/webhook/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(None),
    x_github_event: str | None = Header(None),
    x_github_delivery: str | None = Header(None),
) -> Any:
    """Accept GitHub webhook deliveries (fail-closed ingress)."""
    settings = request.app.state.settings
    secret_setting = getattr(settings, "FORGE_GITHUB_WEBHOOK_SECRET", None)
    secret = secret_setting.get_secret_value() if secret_setting is not None else ""
    enabled = bool(getattr(settings, "FORGE_GITHUB_ENABLED", False))
    if not enabled or not secret:
        return JSONResponse(status_code=503, content={"error": "github ingress disabled"})

    raw_body = await request.body()
    if not verify_github_signature(secret, raw_body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    event = x_github_event or "unknown"
    delivery = x_github_delivery or ""
    logger.info(
        "GitHub webhook event: %s (delivery %s)",
        event,
        delivery[:12] if delivery else "-",
        extra={"event": event, "provider": "github"},
    )

    # Raw-payload diagnostics: the GitHub ingress captures the same way the
    # GitLab one does — without this, a live routing gap is undiagnosable
    # (the inbox row alone says nothing about WHY a delivery didn't route).
    from forge.gateway.router import _capture_webhook_payload

    _capture_webhook_payload(settings, event, payload, header_field="x_github_event")

    if event == "ping":
        return {"status": "ok", "message": "pong"}

    return await _ingest_github_event(request, background_tasks, event, delivery, payload)


async def _ingest_github_event(
    request: Request,
    background_tasks: BackgroundTasks,
    event: str,
    delivery: str,
    payload: dict[str, Any],
) -> Any:
    """Route a validated delivery to its ingest path."""
    settings = request.app.state.settings
    action = str(payload.get("action") or "")
    repository = payload.get("repository") or {}
    installation = payload.get("installation") or {}
    connection_id = github_connection_id(
        installation.get("id"), str(repository.get("full_name") or "")
    )
    project_id = int(repository.get("id") or 0)

    if event == "issue_comment":
        if action != "created":
            return _record_inbox_only(
                request, background_tasks, event, delivery, connection_id, project_id, payload
            )
        comment_user = (payload.get("comment") or {}).get("user") or {}
        sender = payload.get("sender") or {}
        author = str(comment_user.get("login") or sender.get("login") or "")
        if author and author == settings.FORGE_BOT_USERNAME:
            # Forge's own comments never act as triggers (bot-loop guard).
            logger.info("Skipping bot-authored GitHub comment", extra={"event": event})
            return {"status": "skipped", "reason": "bot-loop"}
        run_command = normalize_issue_comment(
            payload, mention_pattern=getattr(settings, "FORGE_MENTION_PATTERN", "@forge")
        )
        if run_command is None:
            return _record_inbox_only(
                request, background_tasks, event, delivery, connection_id, project_id, payload
            )
        source_event_id = github_source_event_id(
            connection_id, event, action, f"comment:{run_command['note_id']}"
        )
        return await _ingest_github_run_command(
            request, background_tasks, run_command, source_event_id
        )

    if event == "issues" and action == "labeled":
        sender = payload.get("sender") or {}
        author = str(sender.get("login") or "")
        if author and author == settings.FORGE_BOT_USERNAME:
            # Forge never labels issues, but the guard stays symmetric with
            # the comment path (bot-loop safety by construction).
            logger.info("Skipping bot-authored GitHub label event", extra={"event": event})
            return {"status": "skipped", "reason": "bot-loop"}
        run_command = normalize_labeled_event(
            payload, trigger_label=getattr(settings, "FORGE_TRIGGER_LABEL", "forge")
        )
        if run_command is None:
            return _record_inbox_only(
                request, background_tasks, event, delivery, connection_id, project_id, payload
            )
        source_event_id = github_source_event_id(
            connection_id, event, action, f"label:{run_command['note_id']}"
        )
        return await _ingest_github_run_command(
            request, background_tasks, run_command, source_event_id, event=event
        )

    if event == "pull_request" and action in _PR_REVIEW_ACTIONS:
        sender = payload.get("sender") or {}
        if str(sender.get("type") or "") == "Bot":
            # Recursion guard (research §6.2): App-token events DO fire
            # webhooks — forge's own pushes must never re-trigger the
            # reviewer.
            logger.info("Skipping Bot-sent GitHub pull_request event", extra={"event": event})
            return {"status": "skipped", "reason": "bot-loop"}
        review_command = normalize_pull_request_event(payload)
        if review_command is None:
            return _record_inbox_only(
                request, background_tasks, event, delivery, connection_id, project_id, payload
            )
        source_event_id = github_source_event_id(
            connection_id, event, action, review_command["delivery_key"]
        )
        return await _ingest_github_run_command(
            request, background_tasks, review_command, source_event_id, event=event
        )

    if event == "workflow_job" and action == "completed":
        debug_command = normalize_workflow_job_event(
            payload,
            harness_workflow=str(
                getattr(settings, "FORGE_GITHUB_HARNESS_WORKFLOW", "") or ""
            ).strip(),
        )
        if debug_command is None:
            return _record_inbox_only(
                request, background_tasks, event, delivery, connection_id, project_id, payload
            )
        source_event_id = github_source_event_id(
            connection_id, event, action, debug_command["delivery_key"]
        )
        return await _ingest_github_run_command(
            request, background_tasks, debug_command, source_event_id, event=event
        )

    if event == "installation" and action in _INSTALLATION_REMOVED_ACTIONS:
        # Connection disabled: log loudly and persist the fact; reconciliation
        # hooks (pause runs, drain steps) come with the v0.5 contracts work.
        logger.warning(
            "GitHub installation removed — connection %s disabled",
            connection_id,
            extra={"event": event, "action": action},
        )
        return _record_inbox_only(
            request,
            background_tasks,
            event,
            delivery,
            connection_id,
            project_id,
            payload,
            handler_result={"connection_disabled": True},
        )

    # push / workflow_run / check_suite / … — persist the delivery only;
    # reconciliation hooks arrive later (ADR-0019 §3).
    return _record_inbox_only(
        request, background_tasks, event, delivery, connection_id, project_id, payload
    )


def _record_inbox_only(
    request: Request,
    background_tasks: BackgroundTasks,
    event: str,
    delivery: str,
    connection_id: str,
    project_id: int,
    payload: dict[str, Any],
    *,
    handler_result: dict[str, Any] | None = None,
) -> JSONResponse:
    """Persist a delivery as an inbox row (no side effects)."""
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        logger.warning("No session factory — GitHub %s delivery not persisted", event)
        return JSONResponse(status_code=202, content={"status": "accepted", "event": event})
    source_event_id = github_source_event_id(
        connection_id, event, str(payload.get("action") or ""), delivery
    )

    async def _write() -> None:
        try:
            async with session_factory() as session:
                async with session.begin():
                    row, _created = await ingest_event(
                        session,
                        source_event_id=source_event_id,
                        project_id=project_id,
                        event_type=f"github:{event}",
                        payload=payload,
                    )
                    if handler_result is not None:
                        row.handler_result = handler_result
        except Exception:
            logger.warning(
                "GitHub delivery %s could not be persisted", source_event_id[:12], exc_info=True
            )

    # Non-command deliveries are audit-only: persist via the request's
    # background tasks so the 2XX GitHub expects within 10 seconds is never
    # delayed by the write (research §2.2). Failures are logged, never fatal.
    background_tasks.add_task(_write)
    return JSONResponse(
        status_code=202, content={"status": "accepted", "event": event, "recorded": True}
    )


async def _ingest_github_run_command(
    request: Request,
    background_tasks: BackgroundTasks,
    run_command: dict[str, Any],
    source_event_id: str,
    *,
    event: str = "issue_comment",
) -> Any:
    """Transactional ingress for GitHub run commands (ADR-0017 §1 semantics).

    Mirrors the GitLab ``_ingest_run_command``: ONE transaction persists the
    inbox row and the first scheduled step; the ``202`` is answered only
    after that commit. Redis remains a wake-up accelerator — the inbox
    unique index is the dedup authority.
    """
    queue = getattr(request.app.state, "task_queue", None)
    session_factory = getattr(request.app.state, "session_factory", None)
    note_id = run_command["note_id"]

    if queue is not None:
        # Fast-path dedup only — a false negative is caught by the inbox index.
        if await queue.is_duplicate(f"run:{run_command['connection_id']}:{note_id}"):
            return JSONResponse(
                status_code=202,
                content={"status": "accepted", "event": event, "deduplicated": True},
            )

    if session_factory is not None:
        deduplicated = False
        async with session_factory() as session:
            async with session.begin():
                _, created = await ingest_event(
                    session,
                    source_event_id=source_event_id,
                    project_id=run_command["project_id"],
                    event_type="run_command",
                    payload={**run_command, "delivery_note_id": note_id},
                )
                if created:
                    # The durability contract behind the 202: once this
                    # commits, a worker WILL attempt the command.
                    await schedule_command_step(
                        session, run_command, source_event_id=source_event_id
                    )
                else:
                    deduplicated = True
        if deduplicated:
            return JSONResponse(
                status_code=202,
                content={"status": "accepted", "event": event, "deduplicated": True},
            )

    if queue is not None:
        try:
            # The wake task must address the step that was actually
            # scheduled (its inbox identity), not a recomputed one — two
            # different hashes meant the worker never found the step and
            # executed the command directly, doubling the run.
            run_command.setdefault("source_event_id", source_event_id)
            await queue.submit(create_run_command_task(run_command, note_id=note_id or 0))
        except Exception:
            logger.warning("GitHub run command queue wake-up failed", exc_info=True)
        redis_manager = getattr(request.app.state, "redis_manager", None)
        if redis_manager is not None:
            try:
                await redis_manager.lpush(STEP_WAKE_KEY, source_event_id)
            except Exception:
                logger.debug("GitHub run command step wake-up failed", exc_info=True)
        return JSONResponse(
            status_code=202,
            content={
                "status": "accepted",
                "event": event,
                "queued": True,
                "run_command": True,
            },
        )

    if session_factory is not None:
        # No Redis — dev fallback: execute the persisted step in-process,
        # through the SAME claim/lease/fence protocol as the worker.
        from forge.worker.steps import run_pending_command_step
        from uuid import uuid4

        background_tasks.add_task(
            run_pending_command_step,
            session_factory,
            request.app.state.settings,
            request.app.state.forge_config,
            source_event_id,
            owner=f"gateway-github-{uuid4().hex[:6]}",
        )
        return JSONResponse(
            status_code=202,
            content={"status": "accepted", "event": event, "run_command": True},
        )

    return JSONResponse(status_code=202, content={"status": "accepted", "event": event})
