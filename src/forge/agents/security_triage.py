from __future__ import annotations

import logging

from pydantic import BaseModel

from forge.agents.base import ForgeAgent
from forge.agents.models import SecurityTriageResult
from forge.context.security_report import (
    SecurityReport,
    format_findings_for_prompt,
    prioritize_findings,
)

logger = logging.getLogger(__name__)


class SecurityTriageAgent(ForgeAgent):
    """Triages security scan findings and provides remediation guidance."""

    def _output_schema(self) -> type[BaseModel] | None:
        return SecurityTriageResult

    def _build_user_message(self) -> str:
        """Assemble the security triage prompt from context."""
        parts: list[str] = []
        ctx = self.context

        # Pipeline/job overview
        if ctx.pipeline:
            parts.append(
                f"## Pipeline\n\n"
                f"**Status:** {ctx.pipeline.status}\n"
                f"**SHA:** {ctx.pipeline.sha or 'N/A'}"
            )

        # Security report findings
        has_reports = False
        for report in ctx.security_reports:
            if not isinstance(report, SecurityReport):
                continue
            prioritized = prioritize_findings(report.findings)
            if prioritized:
                has_reports = True
                parts.append(
                    f"## Security Scan: {report.scan_type or 'unknown'} "
                    f"({report.scanner_name or 'unknown scanner'})\n\n"
                    f"{format_findings_for_prompt(prioritized)}"
                )
            if report.errors:
                parts.append(f"**Scanner errors:** {'; '.join(report.errors)}")

        # Fallback: use job logs if no artifact reports available
        if not has_reports and ctx.job_logs:
            for job_id, log in ctx.job_logs.items():
                parts.append(f"## Job Log (job {job_id})\n\n```\n{log}\n```")

        # MR context / diff for correlating findings with changes
        if ctx.mr:
            parts.append(f"## Merge Request\n\n**{ctx.mr.title}** (!{ctx.mr.iid})")
        if ctx.raw_diff:
            parts.append(f"## Changes\n\n```diff\n{ctx.raw_diff}\n```")

        return "\n\n".join(parts)
