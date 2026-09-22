"""CI failure debugging on the durable step lane (v0.7, research F3 port).

The legacy reactive pipeline-debugger (:class:`~forge.agents.pipeline_debugger.
PipelineDebuggerAgent` behind the orchestrator) fires fire-and-forget from a
webhook-triggered queue task. This module is its durable equivalent (v0.7):
the same agent runs behind the SAME inbox/step mechanics as every other
command (ADR-0017) — one step in, one root-cause comment out, retries and
crash recovery included. Two executors share it:

**GitHub — ``debug_ci``** (docs/research/2026-09-14-github-reactive.md §4): a
``workflow_job`` webhook ``completed`` with ``conclusion == "failure"`` —
the preferred trigger (App-level, per-step granularity). The executor:

1. **Guards** (research §4.1/§6.2): forge's own harness runs — ``forge/``
   head branches running ``FORGE_GITHUB_HARNESS_WORKFLOW`` — are skipped
   (their failures have their own durable triage), and the associated PR
   must NOT be authored by the forge App (recursion guard).
2. **Correlates** the run to its PR via ``head_sha`` →
   ``GET /commits/{sha}/pulls`` — the universal key, because
   ``pull_requests[]`` is EMPTY on fork-PR runs (research §4.1 fork caveat).
   No open PR for the head → nothing to debug on.
3. **Fetches the failed job's log** (:meth:`GitHubClient.get_job_log`, the
   302-redirect endpoint, ci-security-surface §2.2), tail-bounded and
   redacted (F23: CI logs are untrusted).
4. **Runs the pipeline-debugger agent** over the bounded log (the same JSON
   contract the GitLab reactive lane uses) and **posts the root-cause
   comment** on the PR as a sticky comment keyed by head SHA
   (``<!-- forge:ci-debug:{head_sha} -->``, research §4.3) — edited in
   place on re-debug, never duplicated.

**GitLab — ``debug_pipeline``** (ci-security-surface §2.1): a failed
pipeline webhook routed at the ingress to a durable step (the legacy
orchestrator lane no longer sees ``pipeline.failed`` events). The executor:

1. **Skips forge's own factory branches** (``factory/`` prefix): those
   failures belong to the run's own bounded repair loop, which ALREADY
   fetches failed-job logs — ``RunService._build_repair_context`` tails the
   failed jobs' logs into the repair brief (ADR-0008/0013) — and posts the
   human-visible "Repair cycle" note on the Draft MR. Debugging here again
   would duplicate both; this module documents that boundary instead.
2. **Finds the associated MR** (the pipeline payload's ``merge_request``
   iid when present, else the open MR whose source branch matches the
   pipeline ref) and **fetches the failed jobs' logs** (capped at
   :data:`DEBUG_MAX_FAILED_JOBS`, tail-bounded, redacted).
3. **Runs the same pipeline-debugger agent** and **posts the root-cause
   note** on the MR.

No budget is applied (no RunSpec on this lane): model usage lands in the
``llm_calls`` ledger like every other call (ADR-0013).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from forge.agents.models import PipelineDebugResult
from forge.context.engine import AgentContext
from forge.gitlab.schemas import Job
from forge.integrations.github import GITHUB_BODY_MAX_CHARS, GitHubClient
from forge.policy.evidence import EvidencePolicy

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    from forge.config import ForgeConfig, Settings

logger = logging.getLogger(__name__)

#: The hidden sticky-comment marker (github-reactive research §3.3/§4.3):
#: keyed by the failed run's head SHA so a re-debug edits the existing
#: comment instead of posting another one.
DEBUG_MARKER_TEMPLATE = "<!-- forge:ci-debug:{head_sha} -->"

#: Failed jobs debugged per run (the reactive context engine's cap, §2.1:
#: evidence budgets — never paste whole-run logs).
DEBUG_MAX_FAILED_JOBS = 3

#: Per-job log tail kept for the agent (chars, research §4.2: "feed only the
#: failing step's log slice (tail N KB), not whole-run ZIPs").
DEBUG_LOG_PER_JOB_CHARS = 4000

#: Body cap with headroom under GitHub's 65,536-char comment limit.
DEBUG_BODY_SOFT_CAP = 60_000

#: DebugRunner: failed jobs + their bounded logs → the agent's diagnosis.
DebugRunner = Callable[[list[Job], dict[int, str]], Awaitable[PipelineDebugResult]]


# ----------------------------------------------------------------------
# Default agent runner (real LLM; tests inject debug_runner stubs)
# ----------------------------------------------------------------------


async def _default_debug_runner(
    settings: Any,
    forge_config: Any,
    failed_jobs: list[Job],
    job_logs: dict[int, str],
    *,
    project_path: str,
) -> PipelineDebugResult:
    """Run the real pipeline-debugger agent over the bounded evidence."""
    from forge.agents.pipeline_debugger import PipelineDebuggerAgent
    from forge.agents.registry import AgentRegistry
    from forge.llm.provider import get_model

    registry = AgentRegistry(getattr(settings, "FORGE_AGENTS_DIR", "agents"))
    registry.load()
    definition = registry.get("pipeline-debugger")
    if definition is None:
        raise RuntimeError("pipeline-debugger agent definition not found in FORGE_AGENTS_DIR")
    model = get_model(definition.model_alias, forge_config, settings)
    context = AgentContext(
        event_type="pipeline",
        project_id=0,
        project_path=project_path,
        failed_jobs=failed_jobs,
        job_logs=job_logs,
    )
    from forge.orchestrator.project_config import ProjectConfig

    agent = PipelineDebuggerAgent(
        definition=definition,
        model=model,
        context=context,
        project_config=ProjectConfig(),
        gitlab=None,  # type: ignore[arg-type]
    )
    outcome = await agent.run()
    if not outcome.success or outcome.pipeline_debug is None:
        raise RuntimeError(f"pipeline-debugger agent failed: {outcome.error or 'no output'}")
    return outcome.pipeline_debug


# ----------------------------------------------------------------------
# Comment formatting (pure)
# ----------------------------------------------------------------------


def format_debug_comment(result: PipelineDebugResult, *, subject: str) -> str:
    """The root-cause comment: summary, per-job analysis, ordered actions.

    Mirrors the legacy ``_format_pipeline_debug_note`` rendering so both the
    reactive and the durable lane produce the same shape; *subject* names
    the debugged surface (the Actions job line, or the GitLab pipeline).
    """
    flaky_badge = " \u26a0\ufe0f *(likely flaky)*" if result.is_flaky else ""
    parts: list[str] = [
        f"## \U0001f527 Forge CI Failure Analysis{flaky_badge}",
        "",
        result.summary,
        "",
        f"**Failed:** {subject}",
        "",
    ]
    for job in result.jobs:
        parts.append(f"### `{job.job_name}` — {job.job_stage or 'N/A'}")
        parts.append("")
        parts.append(f"**Root cause:** {job.root_cause}")
        parts.append("")
        parts.append(f"**Suggested fix:** {job.fix_suggestion}")
        if job.relevant_files:
            files = ", ".join(f"`{f}`" for f in job.relevant_files)
            parts.append("")
            parts.append(f"**Files:** {files}")
        parts.append("")
        parts.append(f"**Confidence:** {job.confidence}")
        parts.append("")
    if result.suggested_actions:
        parts.append("### Suggested actions")
        parts.append("")
        parts.extend(f"{i}. {action}" for i, action in enumerate(result.suggested_actions, 1))
        parts.append("")
    parts.append("*Diagnosed by forge \u00b7 pipeline-debugger \u00b7 merge is a human decision.*")
    body = "\n".join(parts)
    if len(body) > min(DEBUG_BODY_SOFT_CAP, GITHUB_BODY_MAX_CHARS):
        cut = min(DEBUG_BODY_SOFT_CAP, GITHUB_BODY_MAX_CHARS - 200)
        body = body[:cut].rstrip() + "\n\n*… [truncated]*"
    return body


def _bounded_log(raw_log: str, settings: Any) -> str:
    """Tail-bounded, redacted log slice (evidence budgets + F23 redaction)."""
    tail = raw_log[-DEBUG_LOG_PER_JOB_CHARS:]
    redacted, _ = EvidencePolicy.from_settings(settings).apply_policy(tail)
    return redacted


# ----------------------------------------------------------------------
# GitHub: the durable ``debug_ci`` step (workflow_job failure)
# ----------------------------------------------------------------------


def is_forge_harness_run(head_branch: str, workflow_name: str, harness_workflow: str) -> bool:
    """Whether this Actions run is forge's own harness execution.

    The harness workflow only ever runs on the durable flow's ``forge/``
    branches; its failures are triaged by the run's own harness path
    (``harness_<kind>`` blocking), never re-debugged here (research §4
    guard). Both conditions must hold — a human workflow that happens to
    fail on a ``forge/`` branch still gets debugged.
    """
    return bool(head_branch.startswith("forge/") and harness_workflow) and (
        workflow_name == harness_workflow
    )


async def execute_debug_ci_command(
    settings: Settings,
    forge_config: ForgeConfig,
    session_factory: "async_sessionmaker[AsyncSession]",
    metadata: dict[str, Any],
    *,
    client: GitHubClient | None = None,
    debug_runner: DebugRunner | None = None,
) -> dict[str, Any] | None:
    """Execute a ``debug_ci`` step payload — the Actions failure debugger.

    *client* / *debug_runner* are the test seams (fake transport / fake
    agent), mirroring :func:`forge.reactive.github_review.
    execute_reactive_review`. Returns the outcome dict recorded on the step.
    """
    repo_full_name = str(metadata.get("repo_full_name") or "")
    if "/" not in repo_full_name:
        logger.error("debug_ci without repo_full_name — ignoring")
        return None
    owner, repo = repo_full_name.split("/", 1)
    head_sha = str(metadata.get("head_sha") or "")
    head_branch = str(metadata.get("head_branch") or "")
    job_id = int(metadata.get("job_id") or 0)
    job_name = str(metadata.get("job_name") or "")
    workflow_name = str(metadata.get("workflow_name") or "")
    harness_workflow = str(getattr(settings, "FORGE_GITHUB_HARNESS_WORKFLOW", "") or "").strip()

    # -- guards (defense in depth; the ingress normalizer filters first) --
    if is_forge_harness_run(head_branch, workflow_name, harness_workflow):
        logger.info(
            "debug_ci skipped on %s@%s — forge's own harness run (%s)",
            repo_full_name,
            head_branch,
            workflow_name,
        )
        return {"status": "skipped", "reason": "forge_harness_run"}
    if not head_sha or not job_id:
        logger.warning("debug_ci metadata incomplete for %s — ignoring", repo_full_name)
        return {"status": "skipped", "reason": "incomplete_metadata"}

    if client is None:
        from forge.integrations.github_flow import credentials_from_settings

        client = GitHubClient(
            base_url=getattr(settings, "FORGE_GITHUB_API_URL", "https://api.github.com"),
            token_provider=credentials_from_settings(settings),
        )
        owned_client = True
    else:
        owned_client = False

    try:
        # -- correlate: head SHA → PR (research §4.1, fork-safe) ----------
        try:
            pulls = await client.list_pull_requests_for_commit(owner, repo, head_sha)
        except Exception:
            logger.warning(
                "PR correlation read failed for %s@%s — skipping debug",
                repo_full_name,
                head_sha[:8],
                exc_info=True,
            )
            return {"status": "skipped", "reason": "pr_correlation_failed"}
        pull = next((pr for pr in pulls if int(pr.get("number") or 0)), None)
        if pull is None:
            # A failed run with no associated PR (a branch push) has no
            # comment surface — forge never comments on raw commits here.
            logger.info(
                "debug_ci skipped on %s@%s — no PR for head %s",
                repo_full_name,
                head_branch,
                head_sha[:8],
            )
            return {"status": "skipped", "reason": "no_associated_pr"}
        pr_number = int(pull["number"])
        if str((pull.get("user") or {}).get("type") or "") == "Bot":
            # Recursion guard (research §6.2): forge App-authored PRs (the
            # durable flow's own output) are never debugged by forge.
            logger.info(
                "debug_ci skipped on %s#%d — PR authored by the forge App",
                repo_full_name,
                pr_number,
            )
            return {"status": "skipped", "reason": "bot_authored_pr"}

        # -- bounded evidence ----------------------------------------------
        try:
            raw_log = await client.get_job_log(owner, repo, job_id)
        except Exception:
            logger.warning(
                "Job log read failed for job %d on %s — debugging without log",
                job_id,
                repo_full_name,
                exc_info=True,
            )
            raw_log = ""
        job = Job(
            id=job_id,
            name=job_name or f"job {job_id}",
            status="failed",
            web_url=str(metadata.get("html_url") or "") or None,
            failure_reason="failure",
        )
        logs = {job_id: _bounded_log(raw_log, settings)} if raw_log else {}

        # -- diagnose --------------------------------------------------------
        if debug_runner is None:

            async def runner(
                failed_jobs: list[Job], job_logs: dict[int, str]
            ) -> PipelineDebugResult:
                return await _default_debug_runner(
                    settings,
                    forge_config,
                    failed_jobs,
                    job_logs,
                    project_path=repo_full_name,
                )

            debug_runner = runner
        result = await debug_runner([job], logs)

        # -- surface: sticky root-cause comment on the PR (research §4.3) --
        subject = f"job `{job.name}` on `{head_branch}` at `{head_sha[:8]}`"
        await _upsert_github_debug_comment(
            client,
            owner,
            repo,
            pr_number,
            DEBUG_MARKER_TEMPLATE.format(head_sha=head_sha),
            format_debug_comment(result, subject=subject),
            bot_login=str(getattr(settings, "FORGE_BOT_USERNAME", "") or ""),
        )
        logger.info(
            "debug_ci posted root-cause comment on %s#%d (job %d, head %s)",
            repo_full_name,
            pr_number,
            job_id,
            head_sha[:8],
        )
        return {
            "status": "debugged",
            "pr_number": pr_number,
            "job_id": job_id,
            "head_sha": head_sha,
            "is_flaky": result.is_flaky,
        }
    finally:
        if owned_client:
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                await aclose()


async def _upsert_github_debug_comment(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    marker: str,
    body: str,
    *,
    bot_login: str,
) -> None:
    """Sticky-comment upsert: PATCH the marker comment in place, else POST."""
    full_body = f"{marker} {body}"
    try:
        for comment in await client.get_issue_comments(owner, repo, pr_number):
            if marker not in str(comment.get("body") or ""):
                continue
            login = str((comment.get("user") or {}).get("login") or "")
            if bot_login and login != bot_login:
                continue  # someone quoted our marker — only PATCH our own
            await client.update_issue_comment(owner, repo, int(comment["id"]), full_body)
            return
    except Exception:
        logger.warning("Debug comment lookup failed on PR #%d — posting fresh", pr_number)
    await client.create_issue_comment(owner, repo, pr_number, full_body)


# ----------------------------------------------------------------------
# GitLab: the durable ``debug_pipeline`` step (failed pipeline webhook)
# ----------------------------------------------------------------------


def is_factory_branch(branch: str) -> bool:
    """Whether *branch* is a forge factory branch (ADR-0006 ``factory/``).

    Factory-branch CI failures belong to the run's own bounded repair loop:
    ``RunService._build_repair_context`` already fetches the failed jobs'
    logs into the repair brief (ADR-0008 classifies first; the loop only
    spends model calls on *code* failures within ``FORGE_MAX_COMMIT_CYCLES``),
    and the repair-cycle note on the Draft MR is the human-visible comment.
    The durable pipeline debugger must not duplicate either.
    """
    return branch.startswith("factory/")


async def execute_debug_pipeline_command(
    settings: Settings,
    forge_config: ForgeConfig,
    session_factory: "async_sessionmaker[AsyncSession]",
    metadata: dict[str, Any],
    *,
    gitlab: Any | None = None,
    debug_runner: DebugRunner | None = None,
) -> dict[str, Any] | None:
    """Execute a ``debug_pipeline`` step payload — the GitLab failure debugger.

    *gitlab* / *debug_runner* are the test seams (as on the GitHub side).
    Returns the outcome dict recorded on the step.
    """
    project_id = int(metadata.get("project_id") or 0)
    pipeline_id = int(metadata.get("pipeline_id") or 0)
    branch = str(metadata.get("branch") or "")

    if not project_id or not pipeline_id:
        logger.warning("debug_pipeline metadata incomplete — ignoring")
        return {"status": "skipped", "reason": "incomplete_metadata"}

    # -- factory-branch guard: the run's own repair loop owns these (see
    # is_factory_branch / module docstring — no duplicate debugging). ------
    if is_factory_branch(branch):
        logger.info(
            "debug_pipeline skipped for pipeline %d on %s — factory branch, "
            "the run's repair loop owns this failure",
            pipeline_id,
            branch,
        )
        return {"status": "skipped", "reason": "factory_branch_repair_loop"}

    if gitlab is None:
        from forge.gitlab.client import GitLabClient
        from forge.runs.service import forge_token

        gitlab = GitLabClient(base_url=settings.GITLAB_URL, token=forge_token(settings))
        owned_client = True
    else:
        owned_client = False

    try:
        # -- associate: the MR for the failed pipeline ----------------------
        mr_iid = await _find_mr_for_pipeline(gitlab, metadata, project_id, branch)
        if mr_iid is None:
            logger.info(
                "debug_pipeline skipped for pipeline %d — no open MR for branch %s",
                pipeline_id,
                branch,
            )
            return {"status": "skipped", "reason": "no_associated_mr"}

        # -- bounded evidence: failed jobs + tail logs ----------------------
        try:
            jobs = await gitlab.list_pipeline_jobs(project_id, pipeline_id)
        except Exception:
            logger.warning(
                "Job listing failed for pipeline %d — debugging without jobs", pipeline_id
            )
            jobs = []
        failed = [job for job in jobs if job.status == "failed"][:DEBUG_MAX_FAILED_JOBS]
        job_logs: dict[int, str] = {}
        for job in failed:
            try:
                raw_log = await gitlab.get_job_log(project_id, job.id)
            except Exception:
                raw_log = ""
            if raw_log:
                job_logs[job.id] = _bounded_log(raw_log, settings)

        # -- diagnose ---------------------------------------------------------
        if debug_runner is None:

            async def runner(failed_jobs: list[Job], logs: dict[int, str]) -> PipelineDebugResult:
                return await _default_debug_runner(
                    settings,
                    forge_config,
                    failed_jobs,
                    logs,
                    project_path=str(project_id),
                )

            debug_runner = runner
        result = await debug_runner(failed, job_logs)

        # -- surface: root-cause note on the MR ------------------------------
        failed_names = ", ".join(f"`{job.name}`" for job in failed) or "(job listing unavailable)"
        subject = f"pipeline `{pipeline_id}` — failed job(s): {failed_names}"
        body = format_debug_comment(result, subject=subject)
        await gitlab.create_mr_note(project_id, mr_iid, body)
        logger.info(
            "debug_pipeline posted root-cause note on MR !%d (pipeline %d)",
            mr_iid,
            pipeline_id,
        )
        return {
            "status": "debugged",
            "mr_iid": mr_iid,
            "pipeline_id": pipeline_id,
            "failed_jobs": [job.name for job in failed],
            "is_flaky": result.is_flaky,
        }
    finally:
        if owned_client:
            aclose = getattr(gitlab, "aclose", None)
            if aclose is not None:
                await aclose()


async def _find_mr_for_pipeline(
    gitlab: Any,
    metadata: dict[str, Any],
    project_id: int,
    branch: str,
) -> int | None:
    """The MR the failed pipeline belongs to (pipeline payload first, then branch).

    The pipeline webhook carries ``merge_request.iid`` for MR pipelines
    (ci-security-surface §1.1: pipeline detail correlates pipeline→MR);
    branch/push pipelines fall back to the open MR whose source branch is
    the pipeline's ref.
    """
    mr_iid = metadata.get("mr_iid")
    if mr_iid:
        return int(mr_iid)
    if not branch:
        return None
    try:
        mrs = await gitlab.list_merge_requests(project_id, state="opened", per_page=50)
    except Exception:
        logger.warning("MR listing failed for project %d", project_id, exc_info=True)
        return None
    for mr in mrs:
        if mr.source_branch == branch:
            return mr.iid
    return None


# ----------------------------------------------------------------------
# Log parsing re-export (the agent formats parsed logs itself; kept here so
# tests of the bounded-evidence contract can reach the same helpers)
# ----------------------------------------------------------------------

__all__ = [
    "DEBUG_LOG_PER_JOB_CHARS",
    "DEBUG_MARKER_TEMPLATE",
    "DEBUG_MAX_FAILED_JOBS",
    "DebugRunner",
    "execute_debug_ci_command",
    "execute_debug_pipeline_command",
    "format_debug_comment",
    "is_factory_branch",
    "is_forge_harness_run",
]
