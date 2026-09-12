from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class InlineComment(BaseModel):
    """A single inline review comment to post on a diff line."""

    file: str = Field(description="Path to the file in the repository")
    line: int = Field(description="Line number in the diff")
    line_type: Literal["new", "old"] = Field(
        default="new",
        description="Whether the line is in the new (added) or old (removed) side of the diff",
    )
    severity: Literal["suggestion", "warning", "critical"] = Field(
        default="warning",
        description="Severity of the issue found",
    )
    body: str = Field(description="Clear explanation of the issue")
    suggestion: str | None = Field(
        default=None,
        description="Optional corrected code to suggest",
    )


class ReviewResult(BaseModel):
    """Structured output from a code review agent."""

    summary: str = Field(description="Brief overall assessment (1-2 sentences)")
    severity: Literal["info", "warning", "critical"] = Field(
        default="info",
        description="Overall severity of the review findings",
    )
    comments: list[InlineComment] = Field(
        default_factory=list,
        description="List of inline review comments",
    )
    labels_add: list[str] = Field(
        default_factory=list,
        description="Labels to add to the merge request",
    )
    labels_remove: list[str] = Field(
        default_factory=list,
        description="Labels to remove from the merge request",
    )


class FailedJobAnalysis(BaseModel):
    """Analysis of a single failed CI job."""

    job_name: str = Field(description="Name of the failed job")
    job_stage: str = Field(default="", description="Pipeline stage")
    root_cause: str = Field(description="Root cause of the failure")
    error_type: Literal["build", "test", "lint", "dependency", "config", "infra", "unknown"] = (
        Field(default="unknown", description="Classification of the error")
    )
    fix_suggestion: str = Field(description="Actionable fix with specific files/commands")
    relevant_files: list[str] = Field(default_factory=list, description="Files likely involved")
    confidence: Literal["high", "medium", "low"] = Field(default="medium")


class PipelineDebugResult(BaseModel):
    """Structured output from the pipeline debugger agent."""

    summary: str = Field(description="Brief overall assessment of the pipeline failure")
    is_flaky: bool = Field(default=False, description="Whether this appears to be a flaky failure")
    jobs: list[FailedJobAnalysis] = Field(default_factory=list, description="Per-job analysis")
    suggested_actions: list[str] = Field(
        default_factory=list,
        description="Ordered list of suggested actions to resolve the failure",
    )


class SecurityFindingResult(BaseModel):
    """A triaged security finding."""

    id: str = Field(description="Finding ID from the scanner")
    severity: Literal["critical", "high", "medium", "low", "info"] = Field(
        description="Assessed severity"
    )
    category: str = Field(description="Vulnerability category (e.g. SQL Injection, XSS)")
    file: str = Field(default="", description="Affected file path")
    line: int | None = Field(default=None, description="Affected line number")
    description: str = Field(description="Explanation of the vulnerability")
    remediation: str = Field(description="Specific fix suggestion")
    is_false_positive: bool = Field(
        default=False, description="Whether this is likely a false positive"
    )
    justification: str = Field(default="", description="Reasoning for triage decision")


class SecurityTriageResult(BaseModel):
    """Structured output from the security triage agent."""

    summary: str = Field(description="Brief overall security assessment")
    risk_level: Literal["critical", "high", "medium", "low", "none"] = Field(
        default="low", description="Overall risk level"
    )
    findings: list[SecurityFindingResult] = Field(default_factory=list)
    false_positive_count: int = Field(
        default=0, description="Number of findings marked as false positives"
    )
    labels_add: list[str] = Field(default_factory=list)
    labels_remove: list[str] = Field(default_factory=list)


class AgentResult(BaseModel):
    """Wraps agent output with execution metadata."""

    success: bool = True
    review: ReviewResult | None = None
    pipeline_debug: PipelineDebugResult | None = None
    security_triage: SecurityTriageResult | None = None
    text_response: str | None = None
    error: str | None = None
    duration_ms: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    discussions_created: list[str] = Field(default_factory=list)
    status_hint: str | None = None
