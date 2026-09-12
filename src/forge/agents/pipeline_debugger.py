from __future__ import annotations

import logging

from pydantic import BaseModel

from forge.agents.base import ForgeAgent
from forge.agents.models import PipelineDebugResult
from forge.context.log_parser import format_parsed_log, parse_job_log

logger = logging.getLogger(__name__)


class PipelineDebuggerAgent(ForgeAgent):
    """Diagnoses CI pipeline failures and suggests fixes."""

    def _output_schema(self) -> type[BaseModel] | None:
        return PipelineDebugResult

    def _build_user_message(self) -> str:
        """Assemble the pipeline debugging prompt from context."""
        parts: list[str] = []
        ctx = self.context

        # Pipeline overview
        if ctx.pipeline:
            parts.append(
                f"## Pipeline\n\n"
                f"**Status:** {ctx.pipeline.status}\n"
                f"**Ref:** {ctx.pipeline.ref or 'N/A'}\n"
                f"**SHA:** {ctx.pipeline.sha or 'N/A'}"
            )

        # MR context if available
        if ctx.mr:
            parts.append(
                f"## Merge Request\n\n"
                f"**{ctx.mr.title}** (!{ctx.mr.iid})\n"
                f"`{ctx.mr_source_branch}` \u2192 `{ctx.mr_target_branch}`"
            )

        # Failed jobs with parsed logs
        for job in ctx.failed_jobs:
            raw_log = ctx.job_logs.get(job.id, "")
            if raw_log:
                parsed = parse_job_log(raw_log)
                formatted = format_parsed_log(parsed)
            else:
                formatted = "(no log available)"

            parts.append(
                f"## Failed Job: {job.name} (stage: {job.stage or 'N/A'})\n\n"
                f"**Failure reason:** {job.failure_reason or 'unknown'}\n\n"
                f"{formatted}"
            )

        # CI configuration
        if ctx.ci_config:
            parts.append(f"## CI Configuration (.gitlab-ci.yml)\n\n```yaml\n{ctx.ci_config}\n```")

        # Recent changes (diff) if MR context available
        if ctx.raw_diff:
            parts.append(f"## Recent Changes\n\n```diff\n{ctx.raw_diff}\n```")

        return "\n\n".join(parts)
