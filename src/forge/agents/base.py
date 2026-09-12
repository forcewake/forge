from __future__ import annotations

import asyncio
import logging
import time
from string import Formatter
from typing import TYPE_CHECKING, Any

from agno.agent import Agent
from agno.tools import Toolkit
from agno.tools.mcp import MCPTools

from pydantic import BaseModel

from forge.agents.models import (
    AgentResult,
    PipelineDebugResult,
    ReviewResult,
    SecurityTriageResult,
)
from forge.agents.tools.gitlab_tools import GitLabToolkit

if TYPE_CHECKING:
    from agno.models.litellm import LiteLLM

    from forge.agents.registry import AgentDefinition
    from forge.context.engine import AgentContext
    from forge.gitlab.client import GitLabClient
    from forge.orchestrator.project_config import ProjectConfig

logger = logging.getLogger(__name__)


class SafeFormatter(Formatter):
    """String formatter that leaves unknown placeholders intact."""

    def get_value(self, key: int | str, args: tuple, kwargs: dict[str, Any]) -> Any:
        if isinstance(key, str) and key not in kwargs:
            return f"{{{key}}}"
        return super().get_value(key, args, kwargs)

    def format_field(self, value: Any, format_spec: str) -> str:
        if isinstance(value, str) and value.startswith("{") and value.endswith("}"):
            return value
        return super().format_field(value, format_spec)


class ForgeAgent:
    """Base class for all Forge agents.

    Wraps an Agno Agent with model, tools, system prompt, and output schema.
    Subclasses override ``_build_user_message`` and optionally
    ``_render_system_prompt`` to customize behavior per agent type.
    """

    def __init__(
        self,
        definition: AgentDefinition,
        model: LiteLLM,
        context: AgentContext,
        project_config: ProjectConfig,
        gitlab: GitLabClient,
        mcp_tools: list[MCPTools] | None = None,
    ) -> None:
        self.definition = definition
        self.model = model
        self.context = context
        self.project_config = project_config
        self.gitlab = gitlab
        self.mcp_tools = mcp_tools or []

    def _render_system_prompt(self) -> str:
        """Render the YAML system_prompt template with context variables."""
        fmt = SafeFormatter()
        rules_text = ""
        if self.project_config.review_rules:
            rules_text = "\n".join(f"- {r}" for r in self.project_config.review_rules)

        return fmt.format(
            self.definition.system_prompt,
            project_path=self.context.project_path,
            source_branch=self.context.mr_source_branch,
            target_branch=self.context.mr_target_branch,
            project_rules=rules_text or "No project-specific rules configured.",
        )

    def _output_schema(self) -> type[BaseModel] | None:
        """Return the Pydantic model for structured output, or None for free-form text."""
        return ReviewResult

    def _build_user_message(self) -> str:
        """Assemble the user prompt from context. Subclasses must override."""
        raise NotImplementedError

    def _build_tools(self) -> list[Toolkit]:
        """Create toolkits for the agent based on context and actions config."""
        tools: list[Toolkit] = []
        if self.context.mr and self.definition.actions.get("inline_comments"):
            tools.append(
                GitLabToolkit(
                    gitlab=self.gitlab,
                    project_id=self.context.project_id,
                    mr_iid=self.context.mr.iid,
                    diff_refs=self.context.mr.diff_refs,
                )
            )
        # Append MCP tools from external servers
        if self.mcp_tools:
            tools.extend(self.mcp_tools)
        return tools

    def _get_toolkit(self, tools: list[Toolkit]) -> GitLabToolkit | None:
        """Return the GitLabToolkit instance from the tools list, if any."""
        for t in tools:
            if isinstance(t, GitLabToolkit):
                return t
        return None

    async def run(self) -> AgentResult:
        """Execute the agent: build Agno Agent, call LLM, return result."""
        start = time.monotonic()
        tools = self._build_tools()
        timeout_seconds = self.definition.settings.get("timeout", 120)

        try:
            schema = self._output_schema()
            agent = Agent(
                model=self.model,
                tools=tools or None,
                system_message=self._render_system_prompt(),
                output_schema=schema,
                markdown=True,
                retries=1,
                telemetry=False,
            )

            response = await asyncio.wait_for(
                agent.arun(self._build_user_message()),
                timeout=timeout_seconds,
            )
            duration_ms = int((time.monotonic() - start) * 1000)

            # Extract output from response
            review: ReviewResult | None = None
            pipeline_debug: PipelineDebugResult | None = None
            security_triage: SecurityTriageResult | None = None
            text_response: str | None = None

            if response and response.content:
                if schema is not None:
                    # Structured output mode — parse and assign to correct field
                    parsed = _parse_structured_output(
                        response.content, schema, self.definition.name
                    )
                    if isinstance(parsed, ReviewResult):
                        review = parsed
                    elif isinstance(parsed, PipelineDebugResult):
                        pipeline_debug = parsed
                    elif isinstance(parsed, SecurityTriageResult):
                        security_triage = parsed
                else:
                    # Free-form text mode
                    text_response = (
                        response.content
                        if isinstance(response.content, str)
                        else str(response.content)
                    )

            # Collect discussion IDs from toolkit
            toolkit = self._get_toolkit(tools)
            discussion_ids = toolkit.discussion_ids if toolkit else []

            # Extract token usage from response metrics
            input_tokens = None
            output_tokens = None
            if response and hasattr(response, "metrics") and response.metrics:
                metrics = response.metrics
                input_tokens = getattr(metrics, "input_tokens", None)
                output_tokens = getattr(metrics, "output_tokens", None)

            return AgentResult(
                success=True,
                review=review,
                pipeline_debug=pipeline_debug,
                security_triage=security_triage,
                text_response=text_response,
                duration_ms=duration_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                discussions_created=discussion_ids,
            )

        except asyncio.TimeoutError:
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.error(
                "Agent '%s' timed out after %ds",
                self.definition.name,
                timeout_seconds,
            )
            return AgentResult(
                success=False,
                error=f"Timed out after {timeout_seconds}s",
                duration_ms=duration_ms,
                status_hint="timeout",
            )

        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            status_hint = _classify_llm_error(exc)
            logger.error(
                "Agent '%s' failed: %s",
                self.definition.name,
                exc,
                exc_info=True,
            )
            return AgentResult(
                success=False,
                error=str(exc),
                duration_ms=duration_ms,
                status_hint=status_hint,
            )


def _parse_structured_output(
    content: Any, schema: type[BaseModel], agent_name: str
) -> BaseModel | None:
    """Parse LLM response content into the expected schema type."""
    if isinstance(content, schema):
        return content
    if isinstance(content, str):
        try:
            return schema.model_validate_json(content)
        except Exception:
            logger.warning(
                "Agent '%s' returned non-structured response",
                agent_name,
            )
    return None


def _classify_llm_error(exc: Exception) -> str:
    """Classify an LLM error for status recording."""
    exc_str = str(exc).lower()
    exc_type = type(exc).__name__.lower()
    if "timeout" in exc_str or "timeout" in exc_type:
        return "timeout"
    if "rate" in exc_str or "429" in exc_str or "ratelimit" in exc_type:
        return "rate_limit"
    return "error"
