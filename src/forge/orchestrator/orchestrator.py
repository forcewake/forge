from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from forge.agents.chat import ChatAgent
from forge.agents.dispatch import get_agent_class
from forge.agents.models import AgentResult
from forge.context.engine import ContextEngine
from forge.gateway.mention import extract_mention
from forge.gitlab.client import GitLabClient
from forge.gitlab.events import (
    GitLabEvent,
    JobEvent,
    MergeRequestEvent,
    NoteEvent,
    PipelineEvent,
)
from forge.llm.provider import get_model
from forge.models.agent_run import AgentRun
from forge.models.review_state import ReviewState
from forge.orchestrator.matcher import match_agents
from forge.orchestrator.project_config import load_project_config
from forge.stores.conversation import ConversationStore

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from forge.agents.registry import AgentDefinition, AgentRegistry
    from forge.config import ForgeConfig, Settings
    from forge.flows.loader import FlowLoader
    from forge.flows.runner import FlowRunner
    from forge.mcp_client.manager import MCPConnectionManager

logger = logging.getLogger(__name__)

_SEVERITY_EMOJI = {
    "info": "\U0001f4a1",  # 💡
    "warning": "\u26a0\ufe0f",  # ⚠️
    "critical": "\U0001f6a8",  # 🚨
}

# Slash command → agent name mapping
_COMMAND_MAP: dict[str, str] = {
    "/review": "code-reviewer",
    "/debug": "pipeline-debugger",
    "/security": "security-triage",
    "/explain": "chat",
    "/summarize": "chat",
}

_HELP_TEXT = """\
## \U0001f916 Forge Help

**Commands:**
- `@forge <question>` \u2014 Ask anything about this MR, issue, or codebase
- `@forge /explain` \u2014 Explain the current diff or referenced code
- `@forge /review` \u2014 Request a code review on this MR
- `@forge /debug` \u2014 Diagnose the latest pipeline failure
- `@forge /security` \u2014 Run security triage on the latest scan results
- `@forge /help` \u2014 Show this help message

**Flows (multi-agent):**
- `@forge /implement` \u2014 Analyze issue, generate code, open MR
- `@forge /full-review` \u2014 Code review + security triage + summary
- `@forge /fix-pipeline` \u2014 Diagnose and report pipeline failure

**Tips:**
- I have access to the MR diff, linked issues, and pipeline logs
- I remember our conversation within this thread
- Mention specific files or line numbers for targeted help
"""


class Orchestrator:
    """Dispatches webhook events to matching agents."""

    def __init__(
        self,
        settings: Settings,
        forge_config: ForgeConfig,
        session_factory: async_sessionmaker[AsyncSession],
        registry: AgentRegistry,
        flow_loader: FlowLoader | None = None,
        flow_runner: FlowRunner | None = None,
        mcp_manager: MCPConnectionManager | None = None,
    ) -> None:
        self.settings = settings
        self.forge_config = forge_config
        self.session_factory = session_factory
        self.registry = registry
        self.flow_loader = flow_loader
        self.flow_runner = flow_runner
        self.mcp_manager = mcp_manager

    async def handle_event(self, event: GitLabEvent) -> None:
        """Process a webhook event end-to-end.

        1. Load project config
        2. For NoteEvents with @mention → dedicated note handler
        3. Otherwise match event to agents via registry
        4. Check cooldown / rate limits
        5. Build context, execute agents, apply results
        """
        project_id = event.project.id if event.project else 0
        if not project_id:
            logger.warning("Event has no project — skipping")
            return

        async with GitLabClient(
            base_url=self.settings.GITLAB_URL,
            token=self.settings.GITLAB_TOKEN.get_secret_value(),
        ) as gitlab:
            # 1. Load project config
            project_config = await load_project_config(gitlab, project_id)

            # 2. NoteEvent with @mention → dedicated handler
            if isinstance(event, NoteEvent):
                mention_pattern = self.forge_config.defaults.get("mention_trigger", "@forge")
                extra_cmds = self.flow_loader.commands() if self.flow_loader else None
                mention = extract_mention(
                    event.object_attributes.note or "",
                    mention_pattern,
                    extra_commands=extra_cmds,
                )
                if mention.is_mention:
                    await self._handle_note(event, mention, gitlab, project_id, project_config)
                    return
                # Not a mention — fall through to generic matcher

            # 3. Match agents
            matched = match_agents(event, self.registry, project_config, self.forge_config)
            if not matched:
                return

            # 4. Get target IID for cooldown/dedup
            target_iid = self._extract_target_iid(event)
            user_id = event.user.id if event.user else None

            # 5. For each matched agent
            for definition in matched:
                await self._run_agent(
                    definition=definition,
                    event=event,
                    gitlab=gitlab,
                    project_id=project_id,
                    target_iid=target_iid,
                    user_id=user_id,
                    project_config=project_config,
                )

    async def _handle_note(
        self, event: NoteEvent, mention, gitlab, project_id, project_config
    ) -> None:
        """Handle a NoteEvent that contains an @mention."""
        from forge.gateway.mention import MentionInfo

        mention: MentionInfo = mention  # noqa: F841 — type narrowing

        target_iid = self._extract_target_iid(event)
        user_id = event.user.id if event.user else None
        discussion_id = event.object_attributes.discussion_id or ""
        noteable_type = event.object_attributes.noteable_type or "MergeRequest"
        noteable_iid = target_iid or 0

        # Slash command routing
        if mention.slash_command:
            await self._handle_slash_command(
                mention,
                event,
                gitlab,
                project_id,
                target_iid,
                user_id,
                project_config,
                discussion_id,
            )
            return

        # Chat flow
        chat_def = self.registry.get("chat")
        if not chat_def:
            logger.warning("Chat agent not registered — cannot handle @mention")
            return

        # Load conversation history
        store = ConversationStore(self.session_factory)
        history = await store.get_history(project_id, noteable_type, noteable_iid, discussion_id)

        # Build context
        context_engine = ContextEngine(gitlab, self.forge_config)
        try:
            context = await context_engine.build_note_context(event)
        except Exception:
            logger.error("Failed to build context for chat agent", exc_info=True)
            return

        # Resolve model and run chat agent
        model = get_model(chat_def.model_alias, self.forge_config, self.settings)

        # Fetch MCP tools for chat agent (if configured)
        mcp_tools = None
        if self.mcp_manager and chat_def.mcp_servers:
            mcp_tools = await self.mcp_manager.get_tools_for_agent(chat_def, project_config)

        agent = ChatAgent(
            definition=chat_def,
            model=model,
            context=context,
            project_config=project_config,
            gitlab=gitlab,
            thread_history=history,
            mention_text=mention.mention_text,
            mcp_tools=mcp_tools,
        )

        result = await agent.run()

        # Post reply
        if result.success and result.text_response:
            await self._post_chat_reply(
                gitlab,
                project_id,
                target_iid,
                discussion_id,
                event,
                result.text_response,
            )
            # Save conversation
            await store.append(
                project_id,
                noteable_type,
                noteable_iid,
                discussion_id,
                "user",
                mention.mention_text,
            )
            await store.append(
                project_id,
                noteable_type,
                noteable_iid,
                discussion_id,
                "assistant",
                result.text_response,
            )
        elif not result.success and target_iid:
            try:
                await gitlab.create_mr_note(
                    project_id,
                    target_iid,
                    f"**Forge** \u2014 Chat agent encountered an error: "
                    f"`{result.error}`\n\n*This is an automated message.*",
                )
            except Exception:
                logger.error("Failed to post error note", exc_info=True)

        # Record agent run
        await self._record_run(
            project_id=project_id,
            user_id=user_id,
            event_type=event.object_kind,
            target_iid=target_iid,
            agent_name="chat",
            model_used=chat_def.model_alias,
            result=result,
        )

    async def _handle_slash_command(
        self,
        mention,
        event,
        gitlab,
        project_id,
        target_iid,
        user_id,
        project_config,
        discussion_id,
    ) -> None:
        """Route a slash command to the appropriate agent or handler."""
        command = mention.slash_command

        # /help — static response, no LLM call
        if command == "/help":
            await self._post_chat_reply(
                gitlab,
                project_id,
                target_iid,
                discussion_id,
                event,
                _HELP_TEXT,
            )
            return

        # Check if command maps to a flow
        # M1 cutover: /implement is served by the durable RunService path
        # (gateway routes it as a run_command task) — the legacy YAML flow
        # "issue-to-mr" stays disabled here; other flow commands unchanged.
        if command == "/implement":
            logger.info(
                "Command /implement handled by the durable run service — legacy flow disabled"
            )
            return

        if self.flow_loader and self.flow_runner:
            flow_def = self.flow_loader.get_by_command(command)
            if flow_def:
                flow_id = await self.flow_runner.start_flow(flow_def, event, project_id)
                await self._post_chat_reply(
                    gitlab,
                    project_id,
                    target_iid,
                    discussion_id,
                    event,
                    f"\U0001f504 Starting flow `{flow_def.name}` (ID: `{flow_id[:8]}`)",
                )
                return

        # Look up agent for the command
        agent_name = _COMMAND_MAP.get(command)
        if agent_name is None:
            # Unknown command
            await self._post_chat_reply(
                gitlab,
                project_id,
                target_iid,
                discussion_id,
                event,
                f"Unknown command `{command}`. Try `@forge /help` for available commands.",
            )
            return

        definition = self.registry.get(agent_name)
        if definition is None:
            await self._post_chat_reply(
                gitlab,
                project_id,
                target_iid,
                discussion_id,
                event,
                f"Agent `{agent_name}` is not available. Try `@forge /help` for available commands.",
            )
            return

        await self._run_agent(
            definition=definition,
            event=event,
            gitlab=gitlab,
            project_id=project_id,
            target_iid=target_iid,
            user_id=user_id,
            project_config=project_config,
        )

    async def _post_chat_reply(
        self,
        gitlab: GitLabClient,
        project_id: int,
        target_iid: int | None,
        discussion_id: str,
        event: NoteEvent,
        body: str,
    ) -> None:
        """Post a reply to a discussion thread or as a standalone note."""
        try:
            if discussion_id and target_iid and event.merge_request:
                await gitlab.reply_to_discussion(project_id, target_iid, discussion_id, body)
            elif target_iid and event.merge_request:
                await gitlab.create_mr_note(project_id, target_iid, body)
            elif target_iid and hasattr(event, "issue") and event.issue:
                await gitlab.create_issue_note(project_id, target_iid, body)
            else:
                logger.warning("Cannot post chat reply — no target IID or MR/issue context")
        except Exception:
            logger.error("Failed to post chat reply", exc_info=True)

    async def _run_agent(
        self,
        definition: AgentDefinition,
        event: GitLabEvent,
        gitlab: GitLabClient,
        project_id: int,
        target_iid: int | None,
        user_id: int | None,
        project_config,
    ) -> None:
        """Execute a single agent with all checks and recording."""
        # Cooldown check
        cooldown = definition.settings.get("cooldown", 120)
        if target_iid and await self._check_cooldown(
            definition.name, project_id, target_iid, cooldown
        ):
            logger.info(
                "Agent '%s' on cooldown for project %d MR !%d",
                definition.name,
                project_id,
                target_iid,
            )
            return

        # Rate limit check
        if await self._check_rate_limits(project_id):
            logger.warning(
                "Rate limit exceeded for project %d — skipping '%s'",
                project_id,
                definition.name,
            )
            return

        # Build context
        context_engine = ContextEngine(gitlab, self.forge_config)
        try:
            if isinstance(event, MergeRequestEvent):
                context = await context_engine.build_mr_context(event)
            elif isinstance(event, NoteEvent):
                context = await context_engine.build_note_context(event)
            elif isinstance(event, PipelineEvent):
                context = await context_engine.build_pipeline_context(event)
            elif isinstance(event, JobEvent):
                if definition.type == "security-triage":
                    context = await context_engine.build_security_context(event)
                else:
                    context = await context_engine.build_job_context(event)
            else:
                logger.debug("No context builder for event type %s", event.object_kind)
                return
        except Exception:
            logger.error(
                "Failed to build context for agent '%s'",
                definition.name,
                exc_info=True,
            )
            return

        # --- Incremental review logic (code-reviewer on MR events only) ---
        is_incremental = False
        inter_diff_paths: set[str] = set()
        review_state: ReviewState | None = None
        resolved_count = 0

        if (
            definition.name == "code-reviewer"
            and isinstance(event, MergeRequestEvent)
            and context.mr
            and context.mr.sha
        ):
            review_state = await self._get_review_state(project_id, context.mr.iid)

            if review_state and review_state.last_reviewed_sha:
                if review_state.last_reviewed_sha == context.mr.sha:
                    # Same SHA already reviewed — skip
                    logger.info(
                        "MR !%d already reviewed at %s — skipping",
                        context.mr.iid,
                        context.mr.sha[:8],
                    )
                    return

                # Different SHA — build incremental context
                try:
                    context, inter_diff_paths = await context_engine.build_incremental_mr_context(
                        context,
                        review_state.last_reviewed_sha,
                        context.mr.sha,
                    )
                    if not inter_diff_paths:
                        logger.info(
                            "No file changes since last review of MR !%d — skipping",
                            context.mr.iid,
                        )
                        return
                    is_incremental = True
                    logger.info(
                        "Incremental review for MR !%d: %s..%s (%d files changed)",
                        context.mr.iid,
                        review_state.last_reviewed_sha[:8],
                        context.mr.sha[:8],
                        len(inter_diff_paths),
                    )
                except Exception:
                    logger.warning(
                        "Failed to build incremental context — falling back to full review",
                        exc_info=True,
                    )

        # Resolve model
        model = get_model(definition.model_alias, self.forge_config, self.settings)

        # Fetch MCP tools for this agent (if configured)
        mcp_tools = None
        if self.mcp_manager and definition.mcp_servers:
            mcp_tools = await self.mcp_manager.get_tools_for_agent(definition, project_config)

        # Instantiate and run agent via dispatch
        agent_cls = get_agent_class(definition.type)
        agent = agent_cls(
            definition=definition,
            model=model,
            context=context,
            project_config=project_config,
            gitlab=gitlab,
            mcp_tools=mcp_tools,
        )

        result = await agent.run()

        # --- Resolve addressed threads (incremental reviews only) ---
        if is_incremental and review_state and inter_diff_paths and result.success:
            try:
                resolved = await self._resolve_addressed_threads(
                    gitlab,
                    project_id,
                    context.mr.iid,
                    review_state,
                    inter_diff_paths,
                )
                resolved_count = len(resolved)
                if resolved_count:
                    logger.info(
                        "Auto-resolved %d discussion(s) on MR !%d",
                        resolved_count,
                        context.mr.iid,
                    )
            except Exception:
                logger.warning("Thread resolution failed", exc_info=True)

        # Post summary note if enabled and review available
        if result.success and result.review and definition.actions.get("summary_note"):
            try:
                summary_body = _format_summary_note(
                    result,
                    definition.name,
                    definition.version,
                    is_incremental=is_incremental,
                    resolved_count=resolved_count,
                )
                mr_iid = context.mr.iid if context.mr else target_iid
                if mr_iid:
                    await gitlab.create_mr_note(project_id, mr_iid, summary_body)
            except Exception:
                logger.error("Failed to post summary note", exc_info=True)

        # Post pipeline debug note
        if result.success and result.pipeline_debug and definition.actions.get("summary_note"):
            try:
                body = _format_pipeline_debug_note(result, definition.name, definition.version)
                if target_iid:
                    await gitlab.create_mr_note(project_id, target_iid, body)
                elif isinstance(event, PipelineEvent) and event.object_attributes.sha:
                    await gitlab.create_commit_comment(
                        project_id, event.object_attributes.sha, body
                    )
            except Exception:
                logger.error("Failed to post pipeline debug note", exc_info=True)

        # Post security triage note
        if result.success and result.security_triage and definition.actions.get("summary_note"):
            try:
                body = _format_security_triage_note(result, definition.name, definition.version)
                if target_iid:
                    await gitlab.create_mr_note(project_id, target_iid, body)
                elif isinstance(event, PipelineEvent) and event.object_attributes.sha:
                    await gitlab.create_commit_comment(
                        project_id, event.object_attributes.sha, body
                    )
            except Exception:
                logger.error("Failed to post security triage note", exc_info=True)

            # Apply labels if enabled
            if (
                definition.actions.get("labels")
                and result.security_triage.labels_add
                and target_iid
            ):
                try:
                    await gitlab.add_mr_labels(
                        project_id, target_iid, result.security_triage.labels_add
                    )
                except Exception:
                    logger.error("Failed to add security labels", exc_info=True)

        # Post error note on failure
        if not result.success and target_iid:
            try:
                error_body = _format_error_note(definition.name, result)
                await gitlab.create_mr_note(project_id, target_iid, error_body)
            except Exception:
                logger.error("Failed to post error note", exc_info=True)

        # Record agent run
        await self._record_run(
            project_id=project_id,
            user_id=user_id,
            event_type=event.object_kind,
            target_iid=target_iid,
            agent_name=definition.name,
            model_used=definition.model_alias,
            result=result,
        )

        # Update review state
        if result.success and target_iid and context.mr:
            await self._update_review_state(
                project_id=project_id,
                mr_iid=target_iid,
                sha=context.mr.sha,
                discussion_ids=result.discussions_created,
            )

    async def _get_review_state(self, project_id: int, mr_iid: int) -> ReviewState | None:
        """Fetch existing review state for this MR, if any."""
        async with self.session_factory() as session:
            stmt = select(ReviewState).where(
                ReviewState.project_id == project_id,
                ReviewState.mr_iid == mr_iid,
            )
            return (await session.execute(stmt)).scalar_one_or_none()

    async def _resolve_addressed_threads(
        self,
        gitlab: GitLabClient,
        project_id: int,
        mr_iid: int,
        review_state: ReviewState,
        inter_diff_paths: set[str],
    ) -> list[str]:
        """Resolve bot discussions on files changed since the last review.

        Uses file-level matching: if the file referenced by a bot discussion
        was modified in the inter-diff, the issue is assumed addressed.

        Returns list of discussion IDs that were resolved.
        """
        if not review_state.discussion_ids:
            return []

        discussions = await gitlab.list_discussions(project_id, mr_iid)
        discussion_map = {d.id: d for d in discussions}
        resolved_ids: list[str] = []

        for disc_id in review_state.discussion_ids:
            disc = discussion_map.get(disc_id)
            if not disc or not disc.notes:
                continue

            first_note = disc.notes[0]
            # Skip already-resolved discussions
            if first_note.resolved:
                continue

            # Check if the file referenced by this discussion was modified
            position = first_note.position
            if not position:
                continue

            file_path = position.new_path or position.old_path
            if file_path and file_path in inter_diff_paths:
                try:
                    await gitlab.reply_to_discussion(
                        project_id,
                        mr_iid,
                        disc_id,
                        "\u2705 This appears to have been addressed in the latest push. "
                        "Resolving automatically.\n\n*\u2014 Forge*",
                    )
                    await gitlab.resolve_discussion(project_id, mr_iid, disc_id, resolved=True)
                    resolved_ids.append(disc_id)
                except Exception:
                    logger.warning(
                        "Failed to resolve discussion %s",
                        disc_id,
                        exc_info=True,
                    )

        return resolved_ids

    async def _check_cooldown(
        self,
        agent_name: str,
        project_id: int,
        target_iid: int,
        cooldown_seconds: int,
    ) -> bool:
        """Return True if the agent ran too recently on this target."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=cooldown_seconds)
        async with self.session_factory() as session:
            stmt = (
                select(AgentRun.id)
                .where(
                    AgentRun.agent_name == agent_name,
                    AgentRun.project_id == project_id,
                    AgentRun.target_iid == target_iid,
                    AgentRun.status == "success",
                    AgentRun.created_at > cutoff,
                )
                .limit(1)
            )
            row = (await session.execute(stmt)).first()
            return row is not None

    async def _check_rate_limits(self, project_id: int) -> bool:
        """Return True if rate limits are exceeded."""
        limits = self.forge_config.rate_limits
        async with self.session_factory() as session:
            # Per-project per hour
            hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
            stmt = (
                select(func.count())
                .select_from(AgentRun)
                .where(
                    AgentRun.project_id == project_id,
                    AgentRun.created_at > hour_ago,
                )
            )
            count = (await session.execute(stmt)).scalar() or 0
            if count >= limits.get("per_project_per_hour", 30):
                return True

            # Global per minute
            minute_ago = datetime.now(timezone.utc) - timedelta(minutes=1)
            stmt = (
                select(func.count()).select_from(AgentRun).where(AgentRun.created_at > minute_ago)
            )
            count = (await session.execute(stmt)).scalar() or 0
            if count >= limits.get("global_per_minute", 10):
                return True

        return False

    async def _record_run(
        self,
        project_id: int,
        user_id: int | None,
        event_type: str,
        target_iid: int | None,
        agent_name: str,
        model_used: str,
        result: AgentResult,
    ) -> None:
        """Record agent execution in the database."""
        async with self.session_factory() as session:
            run = AgentRun(
                project_id=project_id,
                user_id=user_id,
                event_type=event_type,
                target_iid=target_iid,
                agent_name=agent_name,
                model_used=model_used,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                duration_ms=result.duration_ms,
                status="success" if result.success else (result.status_hint or "error"),
                error_message=result.error,
            )
            session.add(run)
            await session.commit()
            logger.info(
                "Recorded agent run: %s on project %d (status=%s, %dms)",
                agent_name,
                project_id,
                run.status,
                result.duration_ms,
                extra={
                    "agent": agent_name,
                    "project_id": project_id,
                    "status": run.status,
                    "duration_ms": result.duration_ms,
                    "event_type": event_type,
                },
            )

    async def _update_review_state(
        self,
        project_id: int,
        mr_iid: int,
        sha: str | None,
        discussion_ids: list[str],
    ) -> None:
        """Update or create review state for this MR."""
        async with self.session_factory() as session:
            stmt = select(ReviewState).where(
                ReviewState.project_id == project_id,
                ReviewState.mr_iid == mr_iid,
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()

            if existing:
                existing.last_reviewed_sha = sha
                # Append new discussion IDs
                current_ids = existing.discussion_ids or []
                existing.discussion_ids = current_ids + discussion_ids
            else:
                state = ReviewState(
                    project_id=project_id,
                    mr_iid=mr_iid,
                    last_reviewed_sha=sha,
                    discussion_ids=discussion_ids,
                )
                session.add(state)

            await session.commit()

    @staticmethod
    def _extract_target_iid(event: GitLabEvent) -> int | None:
        """Extract the MR or issue IID from the event."""
        if isinstance(event, MergeRequestEvent):
            return event.object_attributes.iid
        if isinstance(event, NoteEvent):
            if event.merge_request and event.merge_request.iid:
                return event.merge_request.iid
        if isinstance(event, PipelineEvent):
            if event.merge_request and event.merge_request.iid:
                return event.merge_request.iid
        return None


def _format_summary_note(
    result: AgentResult,
    agent_name: str,
    agent_version: str,
    *,
    is_incremental: bool = False,
    resolved_count: int = 0,
) -> str:
    """Format the review summary as a GitLab markdown note."""
    review = result.review
    if not review:
        return ""

    emoji = _SEVERITY_EMOJI.get(review.severity, "\U0001f4a1")

    # Count comments by severity
    counts: dict[str, int] = {"suggestion": 0, "warning": 0, "critical": 0}
    for comment in review.comments:
        counts[comment.severity] = counts.get(comment.severity, 0) + 1

    total_inline = len(result.discussions_created)
    if total_inline > 0:
        inline_note = f"\n\n*{total_inline} inline comment(s) posted on the diff.*"
    else:
        inline_note = ""

    header = (
        "\U0001f504 Forge Incremental Review" if is_incremental else "\U0001f916 Forge Code Review"
    )

    resolved_line = ""
    if is_incremental and resolved_count > 0:
        resolved_line = (
            f"\n\n\u2705 *Auto-resolved {resolved_count} previous discussion(s) "
            f"where the issue appears addressed.*"
        )

    return (
        f"## {header}\n\n"
        f"**Overall:** {emoji} {review.summary}\n\n"
        f"| Severity | Count |\n"
        f"|----------|-------|\n"
        f"| \U0001f4a1 Suggestions | {counts['suggestion']} |\n"
        f"| \u26a0\ufe0f Warnings | {counts['warning']} |\n"
        f"| \U0001f6a8 Critical | {counts['critical']} |\n"
        f"{inline_note}{resolved_line}\n\n"
        f"*Reviewed by Forge \u00b7 {agent_name} v{agent_version}*"
    )


def _format_error_note(agent_name: str, result: AgentResult) -> str:
    """Format a user-friendly error note based on the failure type."""
    if result.status_hint == "timeout":
        return (
            f"**Forge** \u2014 Agent `{agent_name}` timed out. "
            "This usually means the diff was too large for the model to process in time. "
            "Try pushing smaller commits or excluding large generated files via "
            "`skip_paths` in `.forge.yml`.\n\n"
            "*This is an automated message.*"
        )
    if result.status_hint == "rate_limit":
        return (
            f"**Forge** \u2014 Agent `{agent_name}` hit a rate limit on the AI model. "
            "It will retry automatically on the next push.\n\n"
            "*This is an automated message.*"
        )
    return (
        f"**Forge** \u2014 Agent `{agent_name}` encountered an error: "
        f"`{result.error}`\n\n*This is an automated message.*"
    )


_CONFIDENCE_EMOJI = {
    "high": "\u2705",  # ✅
    "medium": "\U0001f7e1",  # 🟡
    "low": "\u2753",  # ❓
}


def _format_pipeline_debug_note(
    result: AgentResult,
    agent_name: str,
    agent_version: str,
) -> str:
    """Format pipeline debug result as a GitLab markdown note."""
    debug = result.pipeline_debug
    if not debug:
        return ""

    flaky_badge = " \u26a0\ufe0f *(likely flaky)*" if debug.is_flaky else ""

    parts: list[str] = [
        f"## \U0001f527 Pipeline Failure Analysis{flaky_badge}\n",
        debug.summary,
        "",
    ]

    for job in debug.jobs:
        confidence_emoji = _CONFIDENCE_EMOJI.get(job.confidence, "")
        parts.append(f"### `{job.job_name}` \u2014 {job.job_stage or 'N/A'}\n")
        parts.append(f"**Root Cause:** {job.root_cause}\n")
        parts.append(f"**Suggested Fix:** {job.fix_suggestion}\n")
        if job.relevant_files:
            files = ", ".join(f"`{f}`" for f in job.relevant_files)
            parts.append(f"**Files:** {files}\n")
        parts.append(f"**Confidence:** {confidence_emoji} {job.confidence}\n")
        parts.append("---\n")

    if debug.suggested_actions:
        parts.append("### Suggested Actions\n")
        for i, action in enumerate(debug.suggested_actions, 1):
            parts.append(f"{i}. {action}")
        parts.append("")

    parts.append(f"*Diagnosed by Forge \u00b7 {agent_name} v{agent_version}*")
    return "\n".join(parts)


_RISK_EMOJI = {
    "critical": "\U0001f6a8",  # 🚨
    "high": "\U0001f534",  # 🔴
    "medium": "\U0001f7e0",  # 🟠
    "low": "\U0001f7e1",  # 🟡
    "none": "\u2705",  # ✅
}


def _format_security_triage_note(
    result: AgentResult,
    agent_name: str,
    agent_version: str,
) -> str:
    """Format security triage result as a GitLab markdown note."""
    triage = result.security_triage
    if not triage:
        return ""

    risk_emoji = _RISK_EMOJI.get(triage.risk_level, "\u2753")

    confirmed = [f for f in triage.findings if not f.is_false_positive]
    false_pos = [f for f in triage.findings if f.is_false_positive]

    parts: list[str] = [
        "## \U0001f6e1\ufe0f Security Triage Report\n",
        f"{risk_emoji} **Risk Level:** {triage.risk_level.upper()}\n",
        triage.summary,
        "",
        "| Verdict | Count |",
        "|---------|-------|",
        f"| \U0001f534 Confirmed | {len(confirmed)} |",
        f"| \u2705 False positive | {len(false_pos)} |",
        "",
    ]

    for finding in confirmed:
        loc = finding.file
        if finding.line:
            loc += f":{finding.line}"
        parts.append(
            f"<details>\n"
            f"<summary>\U0001f534 Confirmed: {finding.category} \u2014 "
            f"{finding.description[:80]}</summary>\n\n"
            f"**File:** `{loc}`\n"
            f"**Severity:** {finding.severity}\n"
            f"**Reasoning:** {finding.justification}\n\n"
            f"**Remediation:**\n{finding.remediation}\n\n"
            f"</details>\n"
        )

    for finding in false_pos:
        loc = finding.file
        if finding.line:
            loc += f":{finding.line}"
        parts.append(
            f"<details>\n"
            f"<summary>\u2705 False positive: {finding.category} \u2014 "
            f"{finding.description[:80]}</summary>\n\n"
            f"**File:** `{loc}`\n"
            f"**Reasoning:** {finding.justification}\n\n"
            f"</details>\n"
        )

    parts.append(f"*Triaged by Forge \u00b7 {agent_name} v{agent_version}*")
    return "\n".join(parts)
