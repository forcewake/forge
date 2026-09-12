from __future__ import annotations

import asyncio
import logging
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from forge.config import ForgeConfig
from forge.context.diff_parser import FileDiff, parse_diff
from forge.context.redactor import Redactor
from forge.context.token_counter import TokenBudget
from forge.gitlab.client import GitLabClient
from forge.gitlab.events import (
    JobEvent,
    MergeRequestEvent,
    NoteEvent,
    PipelineEvent,
    UserInfo,
)
from forge.gitlab.schemas import Discussion, Job, MergeRequest, Pipeline

logger = logging.getLogger(__name__)


class AgentContext(BaseModel):
    """Assembled context ready for prompt formatting."""

    model_config = ConfigDict(frozen=True)

    event_type: str
    project_id: int
    project_path: str = ""

    # MR context
    mr: MergeRequest | None = None
    mr_description: str = ""
    mr_source_branch: str = ""
    mr_target_branch: str = ""

    # Diff context
    raw_diff: str = ""
    parsed_diff: list[FileDiff] = Field(default_factory=list)

    # Discussion context
    discussions: list[Discussion] = Field(default_factory=list)

    # Pipeline context
    pipeline: Pipeline | None = None
    failed_jobs: list[Job] = Field(default_factory=list)
    job_logs: dict[int, str] = Field(default_factory=dict)
    ci_config: str = ""  # Raw .gitlab-ci.yml content

    # Security context
    security_reports: list[Any] = Field(default_factory=list)  # SecurityReport objects

    # Note trigger context
    trigger_note: str | None = None
    discussion_id: str | None = None

    # Issue context
    issue_title: str | None = None
    issue_description: str | None = None

    # Metadata
    user: UserInfo | None = None
    total_tokens_used: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextEngine:
    """Gathers and assembles context from GitLab API for LLM consumption."""

    def __init__(self, client: GitLabClient, config: ForgeConfig) -> None:
        self.client = client
        self.config = config
        redaction_cfg = config.redaction
        self._redactor = Redactor(
            extra_patterns=redaction_cfg.get("extra_patterns"),
            entropy_threshold=redaction_cfg.get("entropy_threshold", 4.5),
        )

    def _make_budget(self) -> TokenBudget:
        return TokenBudget(self.config.token_budgets)

    async def build_mr_context(self, event: MergeRequestEvent) -> AgentContext:
        """Gather context for MR-triggered agents."""
        project_id = event.project.id if event.project else 0
        project_path = event.project.path_with_namespace if event.project else ""
        mr_iid = event.object_attributes.iid

        # Parallel fetch
        mr_task = self.client.get_merge_request(project_id, mr_iid)
        diff_task = self.client.get_merge_request_raw_diff(project_id, mr_iid)
        disc_task = self.client.list_discussions(project_id, mr_iid)
        mr, raw_diff, discussions = await asyncio.gather(mr_task, diff_task, disc_task)

        budget = self._make_budget()

        # Budget and parse diff
        fitted_diff = budget.fit(raw_diff, "diff", strategy="tail")
        parsed_diff = parse_diff(fitted_diff)

        # Budget MR description
        mr_desc = mr.description or ""
        fitted_desc = budget.fit(mr_desc, "description", strategy="tail")

        # Format previous reviews for budget tracking
        review_text = _format_discussions(discussions)
        if review_text:
            budget.fit(review_text, "previous_reviews", strategy="tail")

        # Redact all text
        fitted_diff = self._redactor.redact(fitted_diff)
        fitted_desc = self._redactor.redact(fitted_desc)
        parsed_diff = parse_diff(fitted_diff)

        return AgentContext(
            event_type="merge_request",
            project_id=project_id,
            project_path=project_path,
            mr=mr,
            mr_description=fitted_desc,
            mr_source_branch=mr.source_branch,
            mr_target_branch=mr.target_branch,
            raw_diff=fitted_diff,
            parsed_diff=parsed_diff,
            discussions=discussions,
            user=event.user,
            total_tokens_used=budget.total_used,
        )

    async def build_note_context(self, event: NoteEvent) -> AgentContext:
        """Gather context for @mention-triggered agents."""
        project_id = event.project.id if event.project else 0
        project_path = event.project.path_with_namespace if event.project else ""

        trigger_note = event.object_attributes.note
        discussion_id = event.object_attributes.discussion_id

        # If this note is on a merge request, fetch MR context
        if event.merge_request and event.merge_request.iid:
            mr_iid = event.merge_request.iid

            mr_task = self.client.get_merge_request(project_id, mr_iid)
            diff_task = self.client.get_merge_request_raw_diff(project_id, mr_iid)
            disc_task = self.client.list_discussions(project_id, mr_iid)
            mr, raw_diff, discussions = await asyncio.gather(mr_task, diff_task, disc_task)

            budget = self._make_budget()
            fitted_diff = budget.fit(raw_diff, "diff", strategy="tail")
            fitted_desc = budget.fit(mr.description or "", "description", strategy="tail")

            fitted_diff = self._redactor.redact(fitted_diff)
            fitted_desc = self._redactor.redact(fitted_desc)
            parsed_diff = parse_diff(fitted_diff)

            return AgentContext(
                event_type="note",
                project_id=project_id,
                project_path=project_path,
                mr=mr,
                mr_description=fitted_desc,
                mr_source_branch=mr.source_branch,
                mr_target_branch=mr.target_branch,
                raw_diff=fitted_diff,
                parsed_diff=parsed_diff,
                discussions=discussions,
                trigger_note=self._redactor.redact(trigger_note),
                discussion_id=discussion_id,
                user=event.user,
                total_tokens_used=budget.total_used,
            )

        # Note on an issue (no diff)
        return AgentContext(
            event_type="note",
            project_id=project_id,
            project_path=project_path,
            issue_title=event.issue.title if event.issue else None,
            trigger_note=self._redactor.redact(trigger_note),
            discussion_id=discussion_id,
            user=event.user,
        )

    async def build_pipeline_context(self, event: PipelineEvent) -> AgentContext:
        """Gather context for pipeline failure agents."""
        project_id = event.project.id if event.project else 0
        project_path = event.project.path_with_namespace if event.project else ""
        pipeline_id = event.object_attributes.id

        pipeline, jobs = await asyncio.gather(
            self.client.get_pipeline(project_id, pipeline_id),
            self.client.list_pipeline_jobs(project_id, pipeline_id),
        )

        failed_jobs = [j for j in jobs if j.status == "failed"]
        budget = self._make_budget()

        # Fetch logs for up to 3 failed jobs
        log_tasks = [self.client.get_job_log(project_id, j.id) for j in failed_jobs[:3]]
        logs_raw = await asyncio.gather(*log_tasks) if log_tasks else []

        # Budget and redact each log
        per_job_budget = max(1, budget.remaining("pipeline_logs") // max(1, len(logs_raw)))
        job_logs: dict[int, str] = {}
        counter = budget._counter  # noqa: SLF001 — internal access for truncation
        for job, log in zip(failed_jobs[:3], logs_raw):
            truncated = counter.truncate(log, per_job_budget, strategy="head")
            budget.consume("pipeline_logs", counter.count(truncated))
            job_logs[job.id] = self._redactor.redact(truncated)

        # Fetch CI config (.gitlab-ci.yml)
        ci_config = ""
        try:
            ref = event.object_attributes.ref or "HEAD"
            ci_file = await self.client.get_file(project_id, ".gitlab-ci.yml", ref=ref)
            import base64

            ci_raw = base64.b64decode(ci_file.content).decode("utf-8", errors="replace")
            ci_config = budget.fit(ci_raw, "description", strategy="tail")
            ci_config = self._redactor.redact(ci_config)
        except Exception:
            logger.debug("Could not fetch .gitlab-ci.yml for pipeline %d", pipeline_id)

        # Optionally fetch MR info if pipeline is MR-triggered
        mr: MergeRequest | None = None
        raw_diff = ""
        if event.merge_request and event.merge_request.iid:
            mr_iid = event.merge_request.iid
            mr, raw_diff = await asyncio.gather(
                self.client.get_merge_request(project_id, mr_iid),
                self.client.get_merge_request_raw_diff(project_id, mr_iid),
            )
            raw_diff = budget.fit(raw_diff, "diff", strategy="tail")
            raw_diff = self._redactor.redact(raw_diff)

        return AgentContext(
            event_type="pipeline",
            project_id=project_id,
            project_path=project_path,
            mr=mr,
            mr_description=self._redactor.redact(mr.description or "") if mr else "",
            mr_source_branch=mr.source_branch if mr else "",
            mr_target_branch=mr.target_branch if mr else "",
            raw_diff=raw_diff,
            parsed_diff=parse_diff(raw_diff) if raw_diff else [],
            pipeline=pipeline,
            failed_jobs=failed_jobs,
            job_logs=job_logs,
            ci_config=ci_config,
            user=event.user,
            total_tokens_used=budget.total_used,
        )

    async def build_incremental_mr_context(
        self,
        full_context: AgentContext,
        from_sha: str,
        to_sha: str,
    ) -> tuple[AgentContext, set[str]]:
        """Build context with only the inter-diff between two SHAs.

        Returns (incremental_context, set_of_changed_file_paths).
        If the inter-diff is empty, returns (full_context, empty set).
        """
        project_id = full_context.project_id
        raw_inter_diff = await self.client.compare_commits_raw_diff(project_id, from_sha, to_sha)

        if not raw_inter_diff.strip():
            return full_context, set()

        # Parse to extract changed file paths
        parsed_inter_diff = parse_diff(raw_inter_diff)
        changed_paths: set[str] = set()
        for fd in parsed_inter_diff:
            if fd.new_path:
                changed_paths.add(fd.new_path)
            if fd.old_path:
                changed_paths.add(fd.old_path)

        # Apply token budget and redaction
        budget = self._make_budget()
        fitted_diff = budget.fit(raw_inter_diff, "diff", strategy="tail")
        fitted_diff = self._redactor.redact(fitted_diff)
        parsed_inter_diff = parse_diff(fitted_diff)

        incremental_ctx = full_context.model_copy(
            update={
                "raw_diff": fitted_diff,
                "parsed_diff": parsed_inter_diff,
                "metadata": {
                    **full_context.metadata,
                    "incremental": True,
                    "from_sha": from_sha,
                    "to_sha": to_sha,
                },
            }
        )

        return incremental_ctx, changed_paths

    async def build_job_context(self, event: JobEvent) -> AgentContext:
        """Gather context for individual job events (e.g., SAST results)."""
        project_id = event.project_id or (event.project.id if event.project else 0)
        project_path = event.project.path_with_namespace if event.project else ""

        budget = self._make_budget()

        # Fetch job log
        job_log = ""
        if event.build_id:
            log_raw = await self.client.get_job_log(project_id, event.build_id)
            job_log = budget.fit(log_raw, "pipeline_logs", strategy="head")
            job_log = self._redactor.redact(job_log)

        # Fetch pipeline if available
        pipeline: Pipeline | None = None
        if event.pipeline_id:
            pipeline = await self.client.get_pipeline(project_id, event.pipeline_id)

        job_logs: dict[int, str] = {}
        if event.build_id and job_log:
            job_logs[event.build_id] = job_log

        return AgentContext(
            event_type="build",
            project_id=project_id,
            project_path=project_path,
            pipeline=pipeline,
            job_logs=job_logs,
            user=event.user,
            total_tokens_used=budget.total_used,
        )

    async def build_security_context(self, event: JobEvent) -> AgentContext:
        """Gather context for security triage: job log + artifact reports."""
        import json

        from forge.context.security_report import parse_gitlab_security_report

        base_ctx = await self.build_job_context(event)

        project_id = event.project_id or (event.project.id if event.project else 0)
        reports: list[Any] = []

        if event.build_id:
            artifact_paths = _security_artifact_paths(event.build_name or "")
            for path in artifact_paths:
                try:
                    raw_bytes = await self.client.get_job_artifacts_file(
                        project_id, event.build_id, path
                    )
                    raw_json = json.loads(raw_bytes)
                    report = parse_gitlab_security_report(
                        raw_json, scan_type=_infer_scan_type(path)
                    )
                    reports.append(report)
                except Exception:
                    logger.debug(
                        "Could not fetch artifact %s for job %d",
                        path,
                        event.build_id,
                    )

        return base_ctx.model_copy(update={"security_reports": reports})


def _security_artifact_paths(job_name: str) -> list[str]:
    """Return likely artifact paths for a security scanner job."""
    mapping: dict[str, list[str]] = {
        "semgrep-sast": ["gl-sast-report.json"],
        "sast": ["gl-sast-report.json"],
        "bandit-sast": ["gl-sast-report.json"],
        "gemnasium-dependency_scanning": ["gl-dependency-scanning-report.json"],
        "dependency_scanning": ["gl-dependency-scanning-report.json"],
        "dast": ["gl-dast-report.json"],
        "secret_detection": ["gl-secret-detection-report.json"],
        "container_scanning": ["gl-container-scanning-report.json"],
    }
    if job_name in mapping:
        return mapping[job_name]
    # Prefix match
    for prefix, paths in mapping.items():
        if job_name.startswith(prefix):
            return paths
    # Fallback: try common paths
    return ["gl-sast-report.json", "gl-dependency-scanning-report.json"]


def _infer_scan_type(artifact_path: str) -> str:
    """Infer scan type from artifact file name."""
    if "sast" in artifact_path:
        return "sast"
    if "dependency" in artifact_path:
        return "dependency_scanning"
    if "dast" in artifact_path:
        return "dast"
    if "secret" in artifact_path:
        return "secret_detection"
    if "container" in artifact_path:
        return "container_scanning"
    return ""


def _format_discussions(discussions: list[Discussion]) -> str:
    """Format discussions into readable text for context inclusion."""
    parts: list[str] = []
    for disc in discussions:
        if disc.individual_note:
            continue
        for note in disc.notes:
            if note.system:
                continue
            author = note.author.username if note.author else "unknown"
            parts.append(f"[{author}]: {note.body}")
    return "\n\n".join(parts)
