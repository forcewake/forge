from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel

from agno.tools.mcp import MCPTools

from forge.agents.base import ForgeAgent

if TYPE_CHECKING:
    from agno.models.litellm import LiteLLM

    from forge.agents.registry import AgentDefinition
    from forge.context.engine import AgentContext
    from forge.gitlab.client import GitLabClient
    from forge.orchestrator.project_config import ProjectConfig

logger = logging.getLogger(__name__)


class ChatAgent(ForgeAgent):
    """Conversational agent that responds to @mention interactions.

    Unlike ``CodeReviewAgent``, this agent produces free-form text output
    (no structured ``ReviewResult``). It accepts optional thread history
    and a mention_text parameter for building the user message.
    """

    def __init__(
        self,
        definition: AgentDefinition,
        model: LiteLLM,
        context: AgentContext,
        project_config: ProjectConfig,
        gitlab: GitLabClient,
        *,
        thread_history: list[dict[str, str]] | None = None,
        mention_text: str | None = None,
        mcp_tools: list[MCPTools] | None = None,
    ) -> None:
        super().__init__(
            definition=definition,
            model=model,
            context=context,
            project_config=project_config,
            gitlab=gitlab,
            mcp_tools=mcp_tools,
        )
        self.thread_history = thread_history or []
        self.mention_text = mention_text

    def _output_schema(self) -> type[BaseModel] | None:
        return None  # Free-form text output

    def _render_system_prompt(self) -> str:
        """Build a contextual system prompt for the chat agent."""
        parts: list[str] = [
            f"You are Forge, an AI assistant embedded in GitLab project {self.context.project_path}.",
            "You help developers understand code, issues, merge requests, and pipelines.",
        ]

        # Add MR context if available
        if self.context.mr:
            mr = self.context.mr
            parts.append(
                f"\n## Merge Request\n"
                f"Title: {mr.title} (!{mr.iid})\n"
                f"Source: `{self.context.mr_source_branch}` → `{self.context.mr_target_branch}`"
            )
            if self.context.mr_description:
                parts.append(f"Description: {self.context.mr_description}")

        # Add issue context if available
        if self.context.issue_title:
            parts.append(f"\n## Issue\nTitle: {self.context.issue_title}")
            if self.context.issue_description:
                parts.append(f"Description: {self.context.issue_description}")

        parts.append(
            "\n## Guidelines\n"
            "- Be concise and helpful\n"
            "- Use code blocks with language tags for code\n"
            "- Reference specific files and line numbers when relevant\n"
            "- If you don't have enough context, say so and suggest what to look at\n"
            "- Format responses in Markdown (GitLab renders it)"
        )

        return "\n".join(parts)

    def _build_user_message(self) -> str:
        """Build the user message from mention text and context."""
        parts: list[str] = []

        # Include changed files summary if on an MR
        if self.context.parsed_diff:
            file_list = ", ".join(f.new_path or f.old_path for f in self.context.parsed_diff[:20])
            parts.append(f"**Changed files:** {file_list}")

        # Include diff if relevant and short enough
        if self.context.raw_diff:
            parts.append(f"```diff\n{self.context.raw_diff}\n```")

        # Include thread history context
        if self.thread_history:
            history_parts: list[str] = []
            for msg in self.thread_history:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                prefix = "User" if role == "user" else "Forge"
                history_parts.append(f"**{prefix}:** {content}")
            parts.append("## Previous conversation\n" + "\n\n".join(history_parts))

        # The actual user question/request
        question = self.mention_text or self.context.trigger_note or ""
        if question:
            parts.append(f"## Current request\n{question}")

        return "\n\n".join(parts)
