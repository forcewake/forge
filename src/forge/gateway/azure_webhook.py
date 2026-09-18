"""Azure DevOps webhook ingress (ADR-0024, milestone AZ-2).

Fail-closed by design:

- The route only accepts deliveries when ``FORGE_AZDO_ENABLED`` is true AND
  ``FORGE_AZDO_WEBHOOK_USERNAME``/``FORGE_AZDO_WEBHOOK_PASSWORD`` are set —
  anything else answers ``503 azure devops ingress disabled``.
- Authenticity is the delivery's Basic Authorization header, compared in
  constant time against the configured pair. Azure service hooks have NO
  HMAC (research §2.0) — the webHooks consumer's Basic credentials ARE the
  authenticator, which is why an uncredentialed endpoint is never exposed.
  Azure DevOps sends Basic creds on every delivery; a missing header is
  invalid.
- Responses are answered from durable state only: the ingestion commits an
  inbox row (and, for run commands, the first scheduled step) BEFORE the
  ``202`` — the same transactional contract as the GitLab and GitHub
  ingresses (ADR-0017 §1). The EventInbox identity is content-stable per
  event (``azure_source_event_id``), so Azure's redeliveries collapse onto
  one row instead of double-executing.

Event routing (brief §3 normalization table; route on ``eventType`` ONLY —
``publisherId`` is unstable across API versions, ``tfs`` AND
``azure-devops`` both appear in documented samples):

- ``workitem.commented`` → run commands. The payload has NO comment object
  (research correction #3): text = ``fields["System.History"]``, author =
  ``fields["System.ChangedBy"]``; delivery key ``workitem:{id}:comment:{rev}``
  (the rev bumps per change). Commands parse via the SAME mention/first-token
  parser as GitHub/GitLab (``/implement`` ``/go`` ``/cancel`` ``/security``).
- ``workitem.updated`` → the #29 lifecycle commands: an edit of the issue
  text normalizes to ``issue_edited`` (new title/body travel in the
  metadata; the executor's frozen-snapshot digest compare filters no-op
  updates), and the trigger label no longer among ``fields["System.Tags"]``
  normalizes to ``unlabeled`` (the detection's exact reach and its honest
  limits are documented on :func:`normalize_workitem_updated`).
- ``git.pullrequest.commented-on`` (eventType
  ``ms.vss-code.git-pullrequest-comment-event`` — both spellings route) →
  the same command set on PRs; bot-loop + ``forge/*`` head-branch guards.
- ``git.pullrequest.created``/``git.pullrequest.updated`` → the reactive
  review lane (``review_pr``): bot authors and forge's own ``forge/*``
  branches are skipped — the reviewer never reviews forge's own output.
  The incremental delta (before/after) comes from the PR iterations API,
  not the payload (research §4.3) — only the new head SHA travels here.
- ``build.complete`` with ``result == "failed"`` → the CI debug lane
  (``debug_ci``). Forge's own lane runs
  (``resource.definition.id == FORGE_AZDO_LANE_PIPELINE_ID``) are skipped —
  their failures have their own triage. Other results are inbox-only.
- ``git.push`` and everything else → inbox row only; reconciliation hooks
  arrive later.

Identity conventions (research §2.8): ``uniqueName`` is the e-mail-like
stable identity everywhere; project ids are GUIDs, so the durable int
``project_id`` columns carry a deterministic digest of the project GUID
(:func:`azure_project_key`) — the full string identity travels in the
command payload.
"""

from __future__ import annotations

import base64
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

azure_router = APIRouter()

#: Commands served by the durable run loop — the same set the GitLab and
#: GitHub ingresses route; the normalized Azure command lands on the same
#: durable step path. The mention parser is provider-agnostic (ADR-0024 §7).
_AZDO_RUN_COMMANDS = frozenset(
    {"/implement", "/go", "/cancel", "/retry", "/security", "/status", "/why-blocked", "/reconcile"}
)

_COMMAND_MAP = {
    "/implement": "start_run",
    "/go": "go",
    "/cancel": "cancel",
    "/retry": "retry",
    "/security": "security_triage",
    # R29 operator surface around dead/stuck runs: two read-only commands
    # and the ONE mutating recovery command (/reconcile — approver-gated
    # at the executor, like /retry).
    "/status": "status",
    "/why-blocked": "why_blocked",
    "/reconcile": "reconcile",
}

#: PR-comment event spellings: the display name (``git.pullrequest.commented-on``)
#: and the documented eventType id on the wire (research §2.4/§10.5). Routing
#: keys off ``eventType`` only — never ``publisherId``.
_PR_COMMENT_EVENTS = frozenset(
    {"git.pullrequest.commented-on", "ms.vss-code.git-pullrequest-comment-event"}
)

#: Reactive-review events (brief §3): a new PR, or new commits/votes/status
#: on one. Everything else is inbox-only.
_PR_REVIEW_EVENTS = frozenset({"git.pullrequest.created", "git.pullrequest.updated"})


def verify_azure_basic_auth(
    expected_username: str, expected_password: str, authorization_header: str | None
) -> bool:
    """Constant-time check of the Basic pair (ADR-0024 §3).

    ``Authorization: Basic base64(username:password)`` — both halves compared
    in constant time. A missing header, a non-Basic scheme, undecodable
    bytes or a credential without the ``:`` separator are all invalid.
    """
    if not authorization_header or not expected_username or not expected_password:
        return False
    scheme, _, encoded = authorization_header.partition(" ")
    if scheme.lower() != "basic" or not encoded.strip():
        return False
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    username, sep, password = decoded.partition(":")
    if not sep:
        return False
    return hmac.compare_digest(
        username.encode("utf-8"), expected_username.encode("utf-8")
    ) and hmac.compare_digest(password.encode("utf-8"), expected_password.encode("utf-8"))


def org_from_payload(payload: dict[str, Any]) -> str:
    """The org/collection URL the delivery originated from (research §2.8).

    ``resourceContainers.*.baseUrl`` is the cleanest origin marker across
    Services and Server provenance; it scopes the connection identity so a
    multi-org deployment never cross-matches deliveries.
    """
    containers = payload.get("resourceContainers") or {}
    for key in ("account", "collection"):
        entry = containers.get(key) or {}
        base_url = entry.get("baseUrl")
        if isinstance(base_url, str) and base_url:
            return base_url.rstrip("/")
    return ""


def azure_connection_id(org_url: str, project: str) -> str:
    """The connection-scoped identity prefix for inbox rows."""
    return f"azure_devops:{org_url or ''}:{project or ''}"


def azure_project_key(project_id: str) -> int:
    """A stable numeric project key for the durable int columns.

    Azure DevOps project ids are GUIDs while ``EventInbox.project_id`` and
    ``FlowRun.project_id`` are ints (the schema GitHub reuses for its numeric
    repository id). The key is the low 31 bits of sha256 over the GUID —
    deterministic across deliveries and processes, so inbox identities and
    the one-active-run index stay stable. The full string identity always
    travels in the command payload.
    """
    return (
        int(hashlib.sha256(str(project_id or "").encode("utf-8")).hexdigest()[:8], 16) & 0x7FFFFFFF
    )


def azure_source_event_id(connection_id: str, event: str, delivery_key: str) -> str:
    """Content-stable inbox identity for an Azure DevOps delivery.

    sha256 over connection id + eventType + the per-event delivery key (the
    brief §3 conventions: ``workitem:{id}:comment:{rev}``,
    ``pr:{id}:comment:{commentId}``, ``pr:{id}:{event}:{afterSha}``,
    ``build:{buildId}:{result}``; the delivery GUID otherwise). Re-delivered
    webhooks collapse onto one id — dedupe means drop-duplicates, which is
    exactly what the inbox unique index does.
    """
    material = "|".join((connection_id, event, str(delivery_key)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def identity_name(value: Any) -> str:
    """Normalize an Azure identity field to its stable name (research §2.8).

    Webhook fields carry either an IdentityRef object (``uniqueName`` wins,
    ``displayName`` falls back) or a bare string. AzDO renders bare-string
    identities as ``"Display Name <user@domain>"`` (live: System.ChangedBy
    on workitem.commented) — the uniqueName inside the angle brackets is
    the stable identity and is what admission/bot-loop consume. A bare
    display name without brackets passes through.
    """
    if isinstance(value, dict):
        return str(value.get("uniqueName") or value.get("displayName") or "")
    text = str(value or "").strip()
    import re

    bracketed = re.fullmatch(r".*<([^<>]+)>", text)
    if bracketed:
        return bracketed.group(1).strip()
    return text


def is_bot_identity(author: str, bot_name: str) -> bool:
    """Whether *author* is forge's own AzDO identity (bot-loop guard).

    Matches the configured ``FORGE_AZDO_BOT_NAME`` against the full
    uniqueName (``forge-bot@fabrikam.example``), its local part, or a bare
    display name — case-insensitively, since AzDO renders both forms.
    """
    author = (author or "").strip().lower()
    bot = (bot_name or "").strip().lower()
    if not author or not bot:
        return False
    return author == bot or author.split("@", 1)[0] == bot


def _parse_command(text: str, mention_pattern: str) -> str | None:
    """The SAME command parse as the GitHub ingress: ``@mention /cmd`` or a
    bare first-token command (extract_mention only parses @mentions, so
    requiring one would silently drop bare ``/implement`` notes)."""
    mention = extract_mention(text, mention_pattern, extra_commands=_AZDO_RUN_COMMANDS)
    if mention.is_mention and mention.slash_command in _AZDO_RUN_COMMANDS:
        return mention.slash_command
    first_token = text.split(None, 1)[0] if text else ""
    if first_token in _AZDO_RUN_COMMANDS:
        return first_token
    return None


def _strip_ref(ref: Any) -> str:
    """``refs/heads/x`` → ``x`` (Azure refs travel fully qualified)."""
    return str(ref or "").removeprefix("refs/heads/")


def normalize_workitem_comment(
    payload: dict[str, Any],
    mention_pattern: str = "@forge",
) -> dict[str, Any] | None:
    """Normalize a ``workitem.commented`` payload into run-command metadata.

    The payload has NO comment id (research correction #3): the text is
    ``fields["System.History"]`` and the author ``fields["System.ChangedBy"]``.
    Returns None when the comment carries no forge command.
    """
    resource = payload.get("resource") or {}
    fields = resource.get("fields") or {}
    text = str(fields.get("System.History") or "").strip()
    project = str(fields.get("System.TeamProject") or "")
    work_item_id = int(resource.get("id") or 0)
    if not work_item_id:
        return None

    slash_command = _parse_command(text, mention_pattern)
    if slash_command is None:
        return None

    org = org_from_payload(payload)
    delivery_key = f"workitem:{work_item_id}:comment:{resource.get('rev')}"
    return {
        "command": _COMMAND_MAP[slash_command],
        "provider": "azure_devops",
        "connection_id": azure_connection_id(org, project),
        "project_id": azure_project_key(
            str((payload.get("resourceContainers") or {}).get("project", {}).get("id") or "")
        ),
        "project": project,
        # No repository exists on a work-item payload — the run service
        # resolves the target repo lazily (azure_service._resolve_repo).
        "repo_full_name": "",
        "issue_number": work_item_id,
        "issue_is_pr": False,
        "work_item_rev": resource.get("rev"),
        "author_username": identity_name(fields.get("System.ChangedBy")),
        "note_text": text,
        "note_id": delivery_key,
    }


def _split_tags(raw: Any) -> list[str]:
    """``System.Tags`` travels as a semicolon-separated string (``"a; b"``)."""
    return [tag.strip() for tag in str(raw or "").split(";") if tag.strip()]


def normalize_workitem_updated(
    payload: dict[str, Any],
    trigger_label: str = "forge",
) -> dict[str, Any] | None:
    """Normalize a ``workitem.updated`` payload into a #29 lifecycle command.

    TWO commands come off this one event, decided in this order:

    - ``unlabeled`` — ``fields["System.Tags"]`` is present and the trigger
      label is NOT among the (semicolon-separated) tags. EXACT DETECTION,
      with its honest limits: the documented ``workitem.updated`` payload
      carries ``resource.fields`` as the work item's CURRENT field values —
      flat strings, no ``oldValue``/``newValue`` pairs — so a tag REMOVAL
      cannot be proven from one delivery. The consequence is bounded by
      construction: the executor cancels only runs parked in
      ``waiting_approval`` (a regenerable plan), never a run past the gate,
      and only for an admitted actor. The ambiguity disappears entirely
      when the Azure subscription is created with the ``changedFields:
      System.Tags`` filter — deliveries then fire only on tag changes,
      which is the recommended onboarding config.
    - ``issue_edited`` — otherwise: the new title/body travel in the
      metadata. ``System.Description`` is HTML and travels raw here; the
      executor strips it before digesting, the same way ``start_run``
      freezes the snapshot (research §5.1). The payload carries no
      changed-field marker, so an update of ANY field normalizes to
      ``issue_edited`` — the executor's frozen-snapshot digest compare is
      what filters no-op updates (state/iteration churn matches the
      snapshot and is ignored). When the delivery is sparse
      (``System.Title``/``System.Description`` absent — a "changed fields
      only" subscription), the presence flags tell the executor to restore
      the authoritative text with ONE API read instead of digesting
      empty strings as if the fields had been blanked.

    Delivery keys are content-stable per change: ``edit:{id}:{digest}:{rev}``
    and ``unlabel:{id}:{rev}`` (the rev bumps per change), so a redelivered
    update collapses onto one inbox identity while genuinely different
    updates never do.
    """
    resource = payload.get("resource") or {}
    fields = resource.get("fields") or {}
    project = str(fields.get("System.TeamProject") or "")
    work_item_id = int(resource.get("id") or 0)
    if not work_item_id:
        return None

    org = org_from_payload(payload)
    rev = resource.get("rev")
    title = str(fields.get("System.Title") or "")
    body = str(fields.get("System.Description") or "")

    tags = _split_tags(fields.get("System.Tags"))
    if tags and str(trigger_label or "").strip().lower() not in {tag.lower() for tag in tags}:
        delivery_key = f"unlabel:{work_item_id}:{rev}"
        return {
            "command": "unlabeled",
            "provider": "azure_devops",
            "connection_id": azure_connection_id(org, project),
            "project_id": azure_project_key(
                str((payload.get("resourceContainers") or {}).get("project", {}).get("id") or "")
            ),
            "project": project,
            # No repository exists on a work-item payload — the run service
            # resolves the target repo lazily (azure_service._resolve_repo).
            "repo_full_name": "",
            "issue_number": work_item_id,
            "issue_is_pr": False,
            "work_item_rev": rev,
            "tags": tags,
            "author_username": identity_name(fields.get("System.ChangedBy")),
            "note_text": "",
            "note_id": delivery_key,
            "delivery_key": delivery_key,
        }

    digest = hashlib.sha256(f"{title}\n{body}".encode("utf-8")).hexdigest()
    delivery_key = f"edit:{work_item_id}:{digest}:{rev}"
    return {
        "command": "issue_edited",
        "provider": "azure_devops",
        "connection_id": azure_connection_id(org, project),
        "project_id": azure_project_key(
            str((payload.get("resourceContainers") or {}).get("project", {}).get("id") or "")
        ),
        "project": project,
        "repo_full_name": "",
        "issue_number": work_item_id,
        "issue_is_pr": False,
        "work_item_rev": rev,
        "issue_title": title,
        "issue_body": body,
        # Sparse-delivery flags: a "changed fields only" subscription omits
        # untouched fields — absent means unknown, never empty.
        "issue_title_present": "System.Title" in fields,
        "issue_description_present": "System.Description" in fields,
        "author_username": identity_name(fields.get("System.ChangedBy")),
        "note_text": "",
        "note_id": delivery_key,
        "delivery_key": delivery_key,
    }


def normalize_pr_comment(
    payload: dict[str, Any],
    mention_pattern: str = "@forge",
) -> dict[str, Any] | None:
    """Normalize a PR-comment payload into run-command metadata.

    PR comment events DO carry the comment (research §2.4): content =
    ``resource.comment.content``, author = ``resource.comment.author``
    (NOT ``commentedBy``). Returns None when the comment carries no forge
    command or the head branch is forge-owned — commands on forge's own
    branch must not act as triggers (the forge/* guard, brief §3).
    """
    resource = payload.get("resource") or {}
    comment = resource.get("comment") or {}
    pull_request = resource.get("pullRequest") or {}
    repository = pull_request.get("repository") or {}
    project = str((repository.get("project") or {}).get("name") or "")
    pr_id = int(pull_request.get("pullRequestId") or 0)
    if not pr_id:
        return None

    head_branch = _strip_ref(pull_request.get("sourceRefName"))
    if head_branch.startswith("forge/"):
        # forge's own durable-flow branch: never a trigger surface.
        logger.info(
            "Skipping forge-owned branch %s on PR comment",
            head_branch,
            extra={"event": "pr-comment"},
        )
        return None

    text = str(comment.get("content") or "").strip()
    slash_command = _parse_command(text, mention_pattern)
    if slash_command is None:
        return None

    org = org_from_payload(payload)
    repo_full_name = f"{project}/{repository.get('name') or ''}".rstrip("/")
    delivery_key = f"pr:{pr_id}:comment:{comment.get('id')}"
    return {
        "command": _COMMAND_MAP[slash_command],
        "provider": "azure_devops",
        "connection_id": azure_connection_id(org, project),
        "project_id": azure_project_key(str((repository.get("project") or {}).get("id") or "")),
        "project": project,
        "repo_full_name": repo_full_name,
        "issue_number": pr_id,
        "issue_is_pr": True,
        "pr_id": pr_id,
        "head_branch": head_branch,
        "author_username": identity_name(comment.get("author")),
        "note_text": text,
        "note_id": delivery_key,
    }


def normalize_pull_request_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize ``git.pullrequest.created``/``updated`` into a review command.

    Bot-authored PRs and forge's own ``forge/*`` branches return None
    (inbox-only record) — the reactive reviewer never reviews forge's own
    output. The payload carries no before-SHA (research §2.3): the new head
    (``lastMergeSourceCommit.commitId``, the documented current head SHA) is
    the review trigger, and the incremental delta is derived from the PR
    iterations API by the executor — so ``before_sha`` stays empty by design.
    The delivery key is content-stable — ``pr:{id}:{event}:{after}`` — so a
    redelivered push collapses onto one inbox identity.
    """
    resource = payload.get("resource") or {}
    event = str(payload.get("eventType") or "")
    pr_id = int(resource.get("pullRequestId") or 0)
    if not pr_id:
        return None

    # ``head_branch`` travels as the FULL ref — the reactive lane's contract
    # (AZ-3 pinned: the engine strips ``refs/heads/`` itself). The forge/*
    # guard keys off the bare form.
    source_ref = str(resource.get("sourceRefName") or "")
    head_branch = _strip_ref(source_ref)
    if head_branch.startswith("forge/"):
        logger.info(
            "Skipping forge-owned branch %s on reactive review",
            head_branch,
            extra={"event": event},
        )
        return None

    repository = resource.get("repository") or {}
    project = str((repository.get("project") or {}).get("name") or "")
    org = org_from_payload(payload)
    after = str((resource.get("lastMergeSourceCommit") or {}).get("commitId") or "")
    if not after:
        commits = resource.get("commits") or []
        if commits:
            after = str((commits[-1] or {}).get("commitId") or "")
    delivery_key = f"pr:{pr_id}:{event}:{after}"
    repo_full_name = f"{project}/{repository.get('name') or ''}".rstrip("/")
    pr_author = identity_name(resource.get("createdBy"))

    return {
        "command": "review_pr",
        "provider": "azure_devops",
        "connection_id": azure_connection_id(org, project),
        "project_id": azure_project_key(str((repository.get("project") or {}).get("id") or "")),
        "project": project,
        "repo": str(repository.get("name") or ""),
        "repo_full_name": repo_full_name,
        "issue_number": pr_id,
        "pr_id": pr_id,
        "action": event,
        "head_sha": after,
        "after_sha": after,
        # The before-SHA is not in the payload (research §2.3): the delta
        # comes from the iterations API — empty here, never invented.
        "before_sha": "",
        "head_branch": source_ref,
        "pr_author": pr_author,
        "sender": pr_author,
        "author_username": pr_author,
        "note_text": "",
        "note_id": delivery_key,  # stable fast-path dedup + task identity
        "delivery_key": delivery_key,
    }


def normalize_build_event(
    payload: dict[str, Any],
    lane_pipeline_id: int | None = None,
) -> dict[str, Any] | None:
    """Normalize a failed ``build.complete`` payload into a ``debug_ci`` command.

    Fires only for ``result == "failed"``; forge's own lane runs
    (``resource.definition.id == FORGE_AZDO_LANE_PIPELINE_ID``) are skipped —
    their failures have their own triage and debugging them here would
    recurse into forge's own output. Any other result returns None →
    inbox-only recording. ``sourceVersion`` travels in the payload (research
    §2.5) — the PR correlation is the executor's job (:mod:`forge.reactive`,
    AZ-3).
    """
    resource = payload.get("resource") or {}
    if not resource:
        return None
    result = str(resource.get("result") or "")
    if result != "failed":
        return None

    definition = resource.get("definition") or {}
    definition_id = int(definition.get("id") or 0)
    if lane_pipeline_id is not None and definition_id == int(lane_pipeline_id):
        logger.info(
            "Skipping forge lane run %s (build %s) — no CI debug",
            definition.get("name"),
            resource.get("id"),
            extra={"event": "build.complete"},
        )
        return None

    project = str(((definition.get("project")) or resource.get("project") or {}).get("name") or "")
    repo = str((resource.get("repository") or {}).get("name") or "")
    org = org_from_payload(payload)
    build_id = int(resource.get("id") or 0)
    delivery_key = f"build:{build_id}:{result}"
    return {
        "command": "debug_ci",
        "provider": "azure_devops",
        "connection_id": azure_connection_id(org, project),
        "project_id": azure_project_key(
            str(((definition.get("project")) or resource.get("project") or {}).get("id") or "")
        ),
        "project": project,
        "repo": repo,
        "repo_full_name": f"{project}/{repo}".rstrip("/"),
        "build_id": build_id,
        "build_number": str(resource.get("buildNumber") or ""),
        "definition_id": definition_id,
        "pipeline_name": str(definition.get("name") or ""),
        "head_sha": str(resource.get("sourceVersion") or ""),
        "source_version": str(resource.get("sourceVersion") or ""),
        "head_branch": _strip_ref(resource.get("sourceBranch")),
        # ``result`` is the AZ-3 debugger's key; ``conclusion`` mirrors the
        # GitHub debug_ci naming.
        "result": result,
        "conclusion": result,
        "requested_for": identity_name(resource.get("requestedFor")),
        "author_username": identity_name(resource.get("requestedFor")),
        "note_text": "",
        "note_id": delivery_key,  # stable fast-path dedup + task identity
        "delivery_key": delivery_key,
    }


@azure_router.post("/webhook/azure_devops")
async def azure_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(None),
) -> Any:
    """Accept Azure DevOps service-hook deliveries (fail-closed ingress)."""
    settings = request.app.state.settings
    enabled = bool(getattr(settings, "FORGE_AZDO_ENABLED", False))
    username = str(getattr(settings, "FORGE_AZDO_WEBHOOK_USERNAME", "") or "")
    password_setting = getattr(settings, "FORGE_AZDO_WEBHOOK_PASSWORD", None)
    password = password_setting.get_secret_value() if password_setting is not None else ""
    if not enabled or not username or not password:
        return JSONResponse(status_code=503, content={"error": "azure devops ingress disabled"})

    raw_body = await request.body()
    if not verify_azure_basic_auth(username, password, authorization):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    # Route on eventType ONLY — publisherId is unstable across API versions
    # (research §2.0: both "tfs" and "azure-devops" appear in samples).
    event = str(payload.get("eventType") or "unknown")
    logger.info(
        "Azure DevOps webhook event: %s (delivery %s)",
        event,
        str(payload.get("id") or "")[:12] or "-",
        extra={"event": event, "provider": "azure_devops"},
    )

    # Raw-payload diagnostics: the Azure ingress captures the same way the
    # GitLab and GitHub ones do — without this, a live routing gap is
    # undiagnosable (the inbox row alone says nothing about WHY a delivery
    # didn't route).
    from forge.gateway.router import _capture_webhook_payload

    _capture_webhook_payload(settings, event, payload, header_field="x_ado_event")

    return await _ingest_azure_event(request, background_tasks, event, payload)


async def _ingest_azure_event(
    request: Request,
    background_tasks: BackgroundTasks,
    event: str,
    payload: dict[str, Any],
) -> Any:
    """Route a validated delivery to its ingest path."""
    settings = request.app.state.settings
    bot_name = str(getattr(settings, "FORGE_AZDO_BOT_NAME", "") or "")
    mention_pattern = str(getattr(settings, "FORGE_MENTION_PATTERN", "@forge") or "@forge")

    if event == "workitem.commented":
        author = identity_name(
            ((payload.get("resource") or {}).get("fields") or {}).get("System.ChangedBy")
        )
        if author and is_bot_identity(author, bot_name):
            # Forge's own plan comments re-trigger this event — they never
            # act as triggers (bot-loop guard, research §5.1).
            logger.info(
                "Skipping bot-authored Azure DevOps work-item comment", extra={"event": event}
            )
            return {"status": "skipped", "reason": "bot-loop"}
        run_command = normalize_workitem_comment(payload, mention_pattern=mention_pattern)
        if run_command is None:
            return _record_inbox_only(request, background_tasks, event, payload)
        source_event_id = azure_source_event_id(
            run_command["connection_id"], event, run_command["note_id"]
        )
        return await _ingest_azure_run_command(
            request, background_tasks, run_command, source_event_id, event=event
        )

    if event == "workitem.updated":
        author = identity_name(
            ((payload.get("resource") or {}).get("fields") or {}).get("System.ChangedBy")
        )
        if author and is_bot_identity(author, bot_name):
            # Forge's own field touches re-trigger this event — they never
            # act as triggers (bot-loop guard, research §5.1).
            logger.info(
                "Skipping bot-authored Azure DevOps work-item update", extra={"event": event}
            )
            return {"status": "skipped", "reason": "bot-loop"}
        lifecycle_command = normalize_workitem_updated(
            payload, trigger_label=str(getattr(settings, "FORGE_TRIGGER_LABEL", "forge") or "forge")
        )
        if lifecycle_command is None:
            return _record_inbox_only(request, background_tasks, event, payload)
        source_event_id = azure_source_event_id(
            lifecycle_command["connection_id"], event, lifecycle_command["delivery_key"]
        )
        return await _ingest_azure_run_command(
            request, background_tasks, lifecycle_command, source_event_id, event=event
        )

    if event in _PR_COMMENT_EVENTS:
        comment_author = identity_name(
            ((payload.get("resource") or {}).get("comment") or {}).get("author")
        )
        if comment_author and is_bot_identity(comment_author, bot_name):
            logger.info("Skipping bot-authored Azure DevOps PR comment", extra={"event": event})
            return {"status": "skipped", "reason": "bot-loop"}
        run_command = normalize_pr_comment(payload, mention_pattern=mention_pattern)
        if run_command is None:
            return _record_inbox_only(request, background_tasks, event, payload)
        source_event_id = azure_source_event_id(
            run_command["connection_id"], event, run_command["note_id"]
        )
        return await _ingest_azure_run_command(
            request, background_tasks, run_command, source_event_id, event=event
        )

    if event in _PR_REVIEW_EVENTS:
        pr_author = identity_name(((payload.get("resource") or {}).get("createdBy")))
        if pr_author and is_bot_identity(pr_author, bot_name):
            # Recursion guard: forge's own Draft PR creations/updates must
            # never re-trigger the reviewer.
            logger.info(
                "Skipping bot-authored Azure DevOps pull request event", extra={"event": event}
            )
            return {"status": "skipped", "reason": "bot-loop"}
        review_command = normalize_pull_request_event(payload)
        if review_command is None:
            return _record_inbox_only(request, background_tasks, event, payload)
        source_event_id = azure_source_event_id(
            review_command["connection_id"], event, review_command["delivery_key"]
        )
        return await _ingest_azure_run_command(
            request, background_tasks, review_command, source_event_id, event=event
        )

    if event == "build.complete":
        lane_pipeline_id = getattr(settings, "FORGE_AZDO_LANE_PIPELINE_ID", None)
        debug_command = normalize_build_event(
            payload, lane_pipeline_id=int(lane_pipeline_id) if lane_pipeline_id else None
        )
        if debug_command is None:
            return _record_inbox_only(request, background_tasks, event, payload)
        source_event_id = azure_source_event_id(
            debug_command["connection_id"], event, debug_command["delivery_key"]
        )
        return await _ingest_azure_run_command(
            request, background_tasks, debug_command, source_event_id, event=event
        )

    # git.push / tfvc.checkin / everything else — persist the delivery only;
    # reconciliation hooks arrive later (ADR-0024 §3).
    return _record_inbox_only(request, background_tasks, event, payload)


def _record_inbox_only(
    request: Request,
    background_tasks: BackgroundTasks,
    event: str,
    payload: dict[str, Any],
    *,
    handler_result: dict[str, Any] | None = None,
) -> Any:
    """Persist a delivery as an inbox row (no side effects)."""
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        logger.warning("No session factory — Azure DevOps %s delivery not persisted", event)
        return JSONResponse(status_code=202, content={"status": "accepted", "event": event})
    connection_id = azure_connection_id(
        org_from_payload(payload), _payload_project_name(event, payload)
    )
    source_event_id = azure_source_event_id(connection_id, event, str(payload.get("id") or ""))

    async def _write() -> None:
        try:
            async with session_factory() as session:
                async with session.begin():
                    row, _created = await ingest_event(
                        session,
                        source_event_id=source_event_id,
                        project_id=azure_project_key(
                            str(
                                (
                                    (payload.get("resourceContainers") or {}).get("project") or {}
                                ).get("id")
                                or ""
                            )
                        ),
                        event_type=f"azure_devops:{event}",
                        payload=payload,
                    )
                    if handler_result is not None:
                        row.handler_result = handler_result
        except Exception:
            logger.warning(
                "Azure DevOps delivery %s could not be persisted",
                source_event_id[:12],
                exc_info=True,
            )

    # Non-command deliveries are audit-only: persist via the request's
    # background tasks so the 2XX Azure DevOps expects is never delayed by
    # the write. Failures are logged, never fatal.
    background_tasks.add_task(_write)
    return JSONResponse(
        status_code=202, content={"status": "accepted", "event": event, "recorded": True}
    )


def _payload_project_name(event: str, payload: dict[str, Any]) -> str:
    """The delivery's project name, best-effort per event shape (inbox rows)."""
    resource = payload.get("resource") or {}
    if event in ("workitem.commented", "workitem.updated"):
        return str((resource.get("fields") or {}).get("System.TeamProject") or "")
    pull_request = resource.get("pullRequest") or {}
    if pull_request:
        repository = pull_request.get("repository") or {}
        return str((repository.get("project") or {}).get("name") or "")
    definition = resource.get("definition") or {}
    if definition:
        return str(((definition.get("project")) or resource.get("project") or {}).get("name") or "")
    repository = resource.get("repository") or {}
    if repository:
        return str((repository.get("project") or {}).get("name") or "")
    return ""


async def _ingest_azure_run_command(
    request: Request,
    background_tasks: BackgroundTasks,
    run_command: dict[str, Any],
    source_event_id: str,
    *,
    event: str = "workitem.commented",
) -> Any:
    """Transactional ingress for Azure DevOps run commands (ADR-0017 §1 semantics).

    Mirrors the GitHub ``_ingest_github_run_command``: ONE transaction
    persists the inbox row and the first scheduled step; the ``202`` is
    answered only after that commit. Redis remains a wake-up accelerator —
    the inbox unique index is the dedup authority.
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
            # executed the command directly, doubling the run (the 64243ff
            # rule, mirrored from the GitHub ingress).
            run_command.setdefault("source_event_id", source_event_id)
            await queue.submit(create_run_command_task(run_command, note_id=note_id or 0))
        except Exception:
            logger.warning("Azure DevOps run command queue wake-up failed", exc_info=True)
        redis_manager = getattr(request.app.state, "redis_manager", None)
        if redis_manager is not None:
            try:
                await redis_manager.lpush(STEP_WAKE_KEY, source_event_id)
            except Exception:
                logger.debug("Azure DevOps run command step wake-up failed", exc_info=True)
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
            owner=f"gateway-azure-{uuid4().hex[:6]}",
        )
        return JSONResponse(
            status_code=202, content={"status": "accepted", "event": event, "run_command": True}
        )

    return JSONResponse(status_code=202, content={"status": "accepted", "event": event})
