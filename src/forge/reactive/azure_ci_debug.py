"""Azure DevOps CI failure debugging on the durable step lane (AZ-3).

The AzDO member of :mod:`forge.reactive.ci_debug`'s executor family: a
``build.complete`` service hook with ``result == "failed"`` schedules one
durable ``debug_ci`` step (same inbox/step mechanics, ADR-0017) whose
executor is :func:`execute_azure_debug_ci_command`. Mirrors the GitHub
``debug_ci`` executor's durable-step shape, adapted to the documented
Azure DevOps ground truths:

1. **Guards**: forge's OWN lane builds are skipped by pipeline id
   (``FORGE_AZDO_LANE_PIPELINE_ID`` — their failures have the run's own
   harness triage), non-failed results are skipped (the ingress filter,
   defended here too), and the associated PR must NOT be authored by the
   forge identity (recursion guard).
2. **Fork-safe correlation** (research §6.6 ground truth): the failing
   commit is the build's ``sourceVersion`` — taken from the webhook
   payload carried in the step metadata, falling back to a
   :meth:`~forge.integrations.azure.AzureDevOpsClient.get_build` read.
   **The builds list API has NO ``sourceVersion`` query parameter** (it is
   a response field only), so the join to the open PR happens CLIENT-side:
   the PR whose merge SHAs (``lastMergeSourceCommit`` /
   ``lastMergeCommit``) carry that version. No open PR → nothing to debug
   on. (Policy builds build the merge commit, so both merge SHAs match.)
3. **Bounded evidence**: the build timeline's failed tasks
   (``type == "Task"``, ``result == "failed"``, research §6.4), capped at
   :data:`~forge.reactive.ci_debug.DEBUG_MAX_FAILED_JOBS`, each log
   tail-bounded and redacted (F23: CI logs are untrusted).
4. **The pipeline-debugger agent** runs over the bounded evidence (the
   same runner contract the GitHub/GitLab lanes inject) and the
   root-cause comment is posted as a **sticky PR thread** — the GitHub
   lane's target choice (the PR, keyed by the head SHA marker
   ``<!-- forge:ci-debug:azdo:{head_sha} -->``); a re-debug replies into
   the existing thread and re-activates it instead of posting another.

No budget is applied (no RunSpec on this lane): model usage lands in the
``llm_calls`` ledger like every other call (ADR-0013).

The gateway normalizer produces the step metadata this executor consumes
(``project``, ``repo``, ``build_id``, ``definition_id``, ``result``,
``source_version``); connection settings are the typed ``Settings``
fields AZ-2 landed (``FORGE_AZDO_LANE_PIPELINE_ID`` /
``FORGE_AZDO_BOT_NAME`` / ``FORGE_AZDO_ORG_URL`` / ``FORGE_AZDO_PAT``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from forge.agents.models import PipelineDebugResult
from forge.config import Settings
from forge.gitlab.schemas import Job
from forge.integrations.azure import AzureDevOpsClient, AzureDevOpsError
from forge.reactive.ci_debug import (
    DEBUG_MAX_FAILED_JOBS,
    DebugRunner,
    _bounded_log,
    _default_debug_runner,
    format_debug_comment,
)
from forge.reactive.azure_review import THREAD_STATUS_ACTIVE

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

logger = logging.getLogger(__name__)

#: The hidden sticky-comment marker (mirror of the GitHub ``debug_ci``
#: marker), keyed by the PR HEAD sha so a re-debug reuses the thread.
DEBUG_MARKER_TEMPLATE = "<!-- forge:ci-debug:azdo:{head_sha} -->"

__all__ = [
    "DEBUG_MARKER_TEMPLATE",
    "execute_azure_debug_ci_command",
    "is_forge_lane_build",
]


def is_forge_lane_build(definition_id: int, lane_pipeline_id: int) -> bool:
    """Whether this build is forge's own harness lane execution.

    The lane pipeline only ever runs when forge dispatched it; its
    failures are triaged by the run's own harness path, never re-debugged
    here (the ``build.complete`` router guard, brief §3). Zero ids never
    match — an unconfigured lane id disables the guard.
    """
    return bool(lane_pipeline_id) and bool(definition_id) and definition_id == lane_pipeline_id


async def execute_azure_debug_ci_command(
    settings: Settings,
    forge_config: Any,
    session_factory: "async_sessionmaker[AsyncSession] | None",
    metadata: dict[str, Any],
    *,
    client: AzureDevOpsClient | None = None,
    debug_runner: DebugRunner | None = None,
) -> dict[str, Any] | None:
    """Execute a ``debug_ci`` step payload — the Azure Pipelines debugger.

    *client* / *debug_runner* are the test seams (fake client / fake
    agent), mirroring :func:`forge.reactive.ci_debug.execute_debug_ci_command`.
    Returns the outcome dict recorded on the step.
    """
    project = str(metadata.get("project") or "")
    repo = str(metadata.get("repo") or "")
    build_id = int(metadata.get("build_id") or 0)
    definition_id = int(metadata.get("definition_id") or 0)
    result = str(metadata.get("result") or "")
    source_version = str(metadata.get("source_version") or "")

    lane_pipeline_id = int(settings.FORGE_AZDO_LANE_PIPELINE_ID or 0)

    # -- guards (defense in depth; the ingress normalizer filters first) --
    if is_forge_lane_build(definition_id, lane_pipeline_id):
        logger.info(
            "debug_ci skipped on %s build %d — forge's own lane run (pipeline %d)",
            project,
            build_id,
            lane_pipeline_id,
        )
        return {"status": "skipped", "reason": "forge_lane_build"}
    if result and result.strip().lower() != "failed":
        logger.info(
            "debug_ci skipped on %s build %d — result %s is not a failure",
            project,
            build_id,
            result,
        )
        return {"status": "skipped", "reason": "not_failed"}
    if not project or not repo or not build_id:
        logger.warning("debug_ci metadata incomplete — ignoring")
        return {"status": "skipped", "reason": "incomplete_metadata"}

    if client is None:
        client = _client_from_settings(settings)
        owned_client = True
    else:
        owned_client = False

    try:
        # -- correlate: sourceVersion → open PR (research §6.6, fork-safe) --
        if not source_version:
            try:
                build = await client.get_build(project, build_id)
                source_version = str(build.get("sourceVersion") or "")
            except AzureDevOpsError:
                logger.warning(
                    "Build %d read failed on %s — debugging without correlation evidence",
                    build_id,
                    project,
                    exc_info=True,
                )
        try:
            pull = await _find_pr_for_version(client, project, repo, source_version)
        except AzureDevOpsError:
            logger.warning(
                "PR correlation read failed for %s build %d — skipping debug",
                project,
                build_id,
                exc_info=True,
            )
            return {"status": "skipped", "reason": "pr_correlation_failed"}
        if pull is None:
            # A failed build with no open PR (a branch push) has no comment
            # surface — forge never comments on raw commits here.
            logger.info(
                "debug_ci skipped on %s build %d — no open PR for %s",
                project,
                build_id,
                source_version[:8] or "<unknown>",
            )
            return {"status": "skipped", "reason": "no_associated_pr"}
        pr_id = int(pull.get("pullRequestId") or 0)
        head_sha = _pr_head_sha(pull) or source_version
        author = str(((pull.get("createdBy") or {}).get("uniqueName")) or "")
        if _matches_bot(settings, author):
            # Recursion guard: forge-authored PRs (the durable flow's own
            # output) are never debugged by forge.
            logger.info(
                "debug_ci skipped on %s PR #%d — PR authored by the forge identity",
                project,
                pr_id,
            )
            return {"status": "skipped", "reason": "bot_authored_pr"}

        # -- bounded evidence: failed timeline tasks + tail logs -----------
        failed, job_logs = await _failed_task_evidence(client, settings, project, build_id)

        # -- diagnose ---------------------------------------------------------
        if debug_runner is None:

            async def runner(failed_jobs: list[Job], logs: dict[int, str]) -> PipelineDebugResult:
                return await _default_debug_runner(
                    settings,
                    forge_config,
                    failed_jobs,
                    logs,
                    project_path=f"{project}/{repo}",
                )

            debug_runner = runner
        result_obj = await debug_runner(failed, job_logs)

        # -- surface: sticky root-cause thread on the PR ----------------------
        subject = f"build {build_id} at `{head_sha[:8]}`"
        await _upsert_debug_thread(
            client,
            project,
            repo,
            pr_id,
            DEBUG_MARKER_TEMPLATE.format(head_sha=head_sha),
            format_debug_comment(result_obj, subject=subject),
            bot_identity=_bot_identity(settings),
        )
        logger.info(
            "debug_ci posted root-cause thread on %s PR #%d (build %d, head %s)",
            project,
            pr_id,
            build_id,
            head_sha[:8],
        )
        return {
            "status": "debugged",
            "pr_id": pr_id,
            "build_id": build_id,
            "head_sha": head_sha,
            "is_flaky": result_obj.is_flaky,
        }
    finally:
        if owned_client:
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                await aclose()


# ----------------------------------------------------------------------
# Correlation + evidence helpers
# ----------------------------------------------------------------------


async def _find_pr_for_version(
    client: AzureDevOpsClient, project: str, repo: str, source_version: str
) -> dict[str, Any] | None:
    """The open PR whose merge SHAs carry *source_version* (client-side).

    Ground truth (research §6.6): there is no server-side join — builds
    cannot be filtered by ``sourceVersion`` — so the open-PR list is
    matched locally against both merge SHAs (policy builds build the merge
    commit: ``lastMergeCommit``; head correlation:
    ``lastMergeSourceCommit``).
    """
    if not source_version:
        return None
    pulls = await client.list_pull_requests(project, repo, status="active")
    for pull in pulls:
        if source_version in _pr_merge_shas(pull):
            return pull
    return None


def _pr_merge_shas(pull: dict[str, Any]) -> set[str]:
    """Both merge SHAs a PR carries, defensively."""
    shas: set[str] = set()
    for key in ("lastMergeSourceCommit", "lastMergeCommit"):
        commit = pull.get(key)
        if isinstance(commit, dict) and isinstance(commit.get("commitId"), str):
            shas.add(commit["commitId"])
    return shas


def _pr_head_sha(pull: dict[str, Any]) -> str:
    """The PR's source-branch head (the sticky marker's key)."""
    commit = pull.get("lastMergeSourceCommit")
    if isinstance(commit, dict) and isinstance(commit.get("commitId"), str):
        return commit["commitId"]
    return ""


async def _failed_task_evidence(
    client: AzureDevOpsClient, settings: Any, project: str, build_id: int
) -> tuple[list[Job], dict[int, str]]:
    """Failed timeline tasks + their tail-bounded, redacted logs.

    The research §6.4 debug recipe: ``type == "Task"`` records with
    ``result == "failed"``, logs fetched via the record's ``log.id`` —
    capped and tail-bounded (evidence budgets; never whole-run logs).
    """
    try:
        timeline = await client.get_timeline(project, build_id)
    except AzureDevOpsError:
        logger.warning(
            "Timeline read failed for build %d — debugging without tasks", build_id, exc_info=True
        )
        return [], {}
    records = timeline.get("records") if isinstance(timeline, dict) else None
    failed_records = [
        record
        for record in records or []
        if isinstance(record, dict)
        and str(record.get("type") or "").lower() == "task"
        and str(record.get("result") or "").lower() == "failed"
    ][:DEBUG_MAX_FAILED_JOBS]

    failed: list[Job] = []
    job_logs: dict[int, str] = {}
    for index, record in enumerate(failed_records):
        raw_log = record.get("log")
        log: dict[str, Any] = raw_log if isinstance(raw_log, dict) else {}
        raw_id = log.get("id")
        log_id = raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) else 0
        job_id = log_id or (10_000 + index)
        name = str(record.get("name") or record.get("identifier") or f"task {job_id}")
        failed.append(
            Job(
                id=job_id,
                name=name,
                stage=str(record.get("identifier") or ""),
                status="failed",
                failure_reason="failed",
            )
        )
        if not log_id:
            continue
        try:
            raw_log = await client.get_task_log(project, build_id, log_id)
        except AzureDevOpsError:
            logger.warning(
                "Task log %d read failed for build %d — debugging without it",
                log_id,
                build_id,
                exc_info=True,
            )
            continue
        if raw_log:
            job_logs[job_id] = _bounded_log(raw_log, settings)
    return failed, job_logs


async def _upsert_debug_thread(
    client: AzureDevOpsClient,
    project: str,
    repo: str,
    pr_id: int,
    marker: str,
    body: str,
    *,
    bot_identity: str,
) -> None:
    """Sticky-thread upsert: reply into the marker thread, else POST one."""
    full_body = f"{marker} {body}"
    try:
        for thread in await client.list_pr_threads(project, repo, pr_id):
            for comment in thread.get("comments") or []:
                if marker not in str(comment.get("content") or ""):
                    continue
                author = str(((comment.get("author") or {}).get("uniqueName")) or "")
                if bot_identity and not _identity_eq(author, bot_identity):
                    break  # someone quoted our marker — only reuse our own
                thread_id = int(thread.get("id") or 0)
                if not thread_id:
                    break
                # The updated analysis is APPENDED (threads are not edited
                # comment-by-comment) and the thread re-activated so the
                # humans see the new diagnosis.
                await client.reply_pr_thread(project, repo, pr_id, thread_id, full_body)
                await client.update_thread_status(
                    project, repo, pr_id, thread_id, THREAD_STATUS_ACTIVE
                )
                return
    except AzureDevOpsError:
        logger.warning("Debug thread lookup failed on PR #%d — posting fresh", pr_id)
    await client.create_pr_thread(project, repo, pr_id, full_body, status=THREAD_STATUS_ACTIVE)


# ----------------------------------------------------------------------
# Settings plumbing (typed Settings fields, AZ-2)
# ----------------------------------------------------------------------


def _bot_identity(settings: Settings) -> str:
    return str(settings.FORGE_AZDO_BOT_NAME or "forge-bot")


def _matches_bot(settings: Settings, identity: str) -> bool:
    return bool(identity) and _identity_eq(identity, _bot_identity(settings))


def _identity_eq(left: str, right: str) -> bool:
    return left.strip().lower() == right.strip().lower()


def _client_from_settings(settings: Settings) -> AzureDevOpsClient:
    pat = settings.FORGE_AZDO_PAT
    token = pat.get_secret_value() if pat is not None else ""
    return AzureDevOpsClient(
        base_url=settings.FORGE_AZDO_ORG_URL,
        token=token,
    )
