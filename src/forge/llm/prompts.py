from __future__ import annotations

from forge.context.engine import AgentContext


def build_review_user_message(
    ctx: AgentContext,
    rules: list[str] | None = None,
) -> str:
    """Assemble the user-message portion of a code review prompt.

    This is extracted so it can be reused by both ``format_review_prompt``
    and agent subclasses that supply their own system prompt (from YAML).
    """
    user_parts: list[str] = []
    user_parts.append(_section("Merge Request", _mr_header(ctx)))

    if ctx.mr_description:
        user_parts.append(_section("Description", ctx.mr_description))

    if ctx.raw_diff:
        user_parts.append(_section("Diff", f"```diff\n{ctx.raw_diff}\n```"))

    if ctx.discussions:
        review_lines: list[str] = []
        for disc in ctx.discussions:
            for note in disc.notes:
                if not note.system:
                    author = note.author.username if note.author else "unknown"
                    review_lines.append(f"**{author}**: {note.body}")
        if review_lines:
            user_parts.append(_section("Previous Review Comments", "\n\n".join(review_lines)))

    if rules:
        user_parts.append(_section("Project-Specific Rules", "\n".join(f"- {r}" for r in rules)))

    return "\n\n".join(user_parts)


def format_review_prompt(
    ctx: AgentContext,
    rules: list[str] | None = None,
) -> list[dict[str, str]]:
    """Format context for a code review agent."""
    system = (
        "You are a senior code reviewer for a GitLab merge request. "
        "Review the changes carefully and provide concise, actionable feedback.\n\n"
        "Guidelines:\n"
        "- Focus on bugs, security issues, performance problems, and readability.\n"
        "- Reference specific file paths and line numbers.\n"
        "- Be concise — avoid restating the obvious.\n"
        "- Use markdown formatting.\n"
        "- If the changes look good, say so briefly.\n"
    )
    if rules:
        system += "\nProject-specific rules:\n"
        for rule in rules:
            system += f"- {rule}\n"

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": build_review_user_message(ctx)},
    ]


def format_chat_prompt(
    ctx: AgentContext,
    user_message: str | None = None,
    thread_history: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Format context for a chat / Q&A agent."""
    system = (
        "You are a helpful AI assistant for a GitLab project. "
        "Answer questions about the code, merge requests, and issues. "
        "Be concise and use markdown formatting.\n"
    )

    # Build reference context
    ref_parts: list[str] = []
    if ctx.mr:
        ref_parts.append(_section("Merge Request Context", _mr_header(ctx)))
    if ctx.mr_description:
        ref_parts.append(_section("Description", ctx.mr_description))
    if ctx.raw_diff:
        ref_parts.append(_section("Diff", f"```diff\n{ctx.raw_diff}\n```"))
    if ctx.issue_title:
        ref_parts.append(_section("Issue", ctx.issue_title))
        if ctx.issue_description:
            ref_parts.append(ctx.issue_description)

    messages: list[dict[str, str]] = [{"role": "system", "content": system}]

    # Include thread history
    if thread_history:
        for msg in thread_history:
            messages.append(msg)

    # Build the user message
    trigger = user_message or ctx.trigger_note or ""
    if ref_parts:
        trigger = "\n\n".join(ref_parts) + "\n\n---\n\n" + trigger

    messages.append({"role": "user", "content": trigger})
    return messages


def format_pipeline_prompt(ctx: AgentContext) -> list[dict[str, str]]:
    """Format context for a pipeline debugger agent."""
    system = (
        "You are a CI/CD pipeline debugger for GitLab. "
        "Analyse the failed job logs and identify the root cause. "
        "Suggest a fix with specific file paths and commands.\n"
        "Be concise and use markdown formatting.\n"
    )

    user_parts: list[str] = []

    if ctx.pipeline:
        user_parts.append(
            _section(
                "Pipeline",
                f"**Status:** {ctx.pipeline.status}\n"
                f"**Ref:** {ctx.pipeline.ref or 'N/A'}\n"
                f"**SHA:** {ctx.pipeline.sha or 'N/A'}",
            )
        )

    if ctx.mr:
        user_parts.append(_section("Merge Request", _mr_header(ctx)))

    for job in ctx.failed_jobs:
        header = f"{job.name} (stage: {job.stage or 'N/A'})"
        log = ctx.job_logs.get(job.id, "")
        body = f"**Failure reason:** {job.failure_reason or 'unknown'}\n\n"
        if log:
            body += f"```\n{log}\n```"
        user_parts.append(_section(f"Failed Job: {header}", body))

    if ctx.raw_diff:
        user_parts.append(_section("Recent Changes", f"```diff\n{ctx.raw_diff}\n```"))

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def format_security_prompt(ctx: AgentContext) -> list[dict[str, str]]:
    """Format context for a security triage agent."""
    system = (
        "You are a security reviewer analysing code changes for vulnerabilities. "
        "Focus on OWASP Top 10 categories: injection, broken auth, sensitive data "
        "exposure, XXE, broken access control, security misconfiguration, XSS, "
        "insecure deserialization, vulnerable components, and insufficient logging.\n\n"
        "For each finding, provide:\n"
        "- Severity (critical / high / medium / low)\n"
        "- File path and line number\n"
        "- Description of the vulnerability\n"
        "- Suggested fix\n"
        "Use markdown formatting.\n"
    )

    user_parts: list[str] = []
    user_parts.append(_section("Merge Request", _mr_header(ctx)))

    if ctx.mr_description:
        user_parts.append(_section("Description", ctx.mr_description))

    if ctx.raw_diff:
        user_parts.append(_section("Changes to Review", f"```diff\n{ctx.raw_diff}\n```"))

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def _mr_header(ctx: AgentContext) -> str:
    """Build a compact MR header string."""
    parts: list[str] = []
    if ctx.mr:
        parts.append(f"**{ctx.mr.title}** (!{ctx.mr.iid})")
    if ctx.mr_source_branch:
        parts.append(f"`{ctx.mr_source_branch}` → `{ctx.mr_target_branch}`")
    return "\n".join(parts)


def _section(title: str, content: str) -> str:
    """Format a labelled markdown section."""
    return f"## {title}\n\n{content}"
