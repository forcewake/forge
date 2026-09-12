"""Flow execution engine.

Executes multi-agent flows step by step, coordinating via Redis.
Each step is a separate task in the worker queue — enabling distributed execution.
"""

from __future__ import annotations


import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

from forge.agents.dispatch import get_agent_class
from forge.context.engine import ContextEngine
from forge.flows.conditions import evaluate_condition
from forge.flows.models import FlowAction, FlowDefinition, FlowInstance, FlowStep
from forge.flows.state import FlowStateManager
from forge.flows.templates import render_template
from forge.gitlab.client import GitLabClient
from forge.gitlab.events import (
    GitLabEvent,
    MergeRequestEvent,
    NoteEvent,
    PipelineEvent,
    JobEvent,
)
from forge.llm.provider import get_model
from forge.orchestrator.project_config import load_project_config
from forge.worker.tasks import create_flow_step_task

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from forge.agents.registry import AgentRegistry
    from forge.config import ForgeConfig, Settings
    from forge.flows.loader import FlowLoader
    from forge.worker.queue import TaskQueue

logger = logging.getLogger(__name__)


class FlowRunner:
    """Orchestrates multi-agent flow execution."""

    def __init__(
        self,
        state_mgr: FlowStateManager,
        loader: FlowLoader,
        registry: AgentRegistry,
        queue: TaskQueue,
        settings: Settings,
        forge_config: ForgeConfig,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.state_mgr = state_mgr
        self.loader = loader
        self.registry = registry
        self.queue = queue
        self.settings = settings
        self.forge_config = forge_config
        self.session_factory = session_factory

    async def start_flow(
        self,
        flow_def: FlowDefinition,
        event: GitLabEvent,
        project_id: int,
    ) -> str:
        """Start a new flow instance. Returns the flow ID."""
        flow_id = uuid4().hex
        now = datetime.now(timezone.utc).isoformat()

        flow = FlowInstance(
            id=flow_id,
            flow_name=flow_def.name,
            project_id=project_id,
            trigger_event=event.model_dump(mode="json"),
            current_step=0,
            state={},
            status="running",
            started_at=now,
            updated_at=now,
        )

        await self.state_mgr.create(flow)

        # Enqueue the first step
        task = create_flow_step_task(flow_id, 0, flow_def.name)
        await self.queue.submit(task)

        logger.info(
            "Flow '%s' started (ID: %s) with %d steps",
            flow_def.name,
            flow_id[:8],
            len(flow_def.steps),
        )
        return flow_id

    async def execute_step(self, flow_id: str, step_index: int) -> None:
        """Execute a single flow step (called by the worker)."""
        flow = await self.state_mgr.get(flow_id)
        if flow is None:
            logger.error("Flow %s not found — cannot execute step %d", flow_id[:8], step_index)
            return

        if flow.status != "running":
            logger.info("Flow %s is %s — skipping step %d", flow_id[:8], flow.status, step_index)
            return

        flow_def = self.loader.get(flow.flow_name)
        if flow_def is None:
            await self.state_mgr.set_status(
                flow_id, "failed", f"Flow definition '{flow.flow_name}' not found"
            )
            return

        # Check flow-level timeout
        started = datetime.fromisoformat(flow.started_at)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        if elapsed > flow_def.timeout:
            await self.state_mgr.set_status(flow_id, "failed", "Flow timeout exceeded")
            logger.warning("Flow %s timed out after %.0fs", flow_id[:8], elapsed)
            return

        if step_index >= len(flow_def.steps):
            await self.state_mgr.set_status(flow_id, "completed")
            logger.info("Flow %s completed", flow_id[:8])
            return

        step = flow_def.steps[step_index]

        # Check condition
        condition = step.condition if isinstance(step, (FlowStep, FlowAction)) else None
        if condition and not evaluate_condition(condition, flow.state):
            logger.info(
                "Flow %s step %d (%s) skipped — condition false: %s",
                flow_id[:8],
                step_index,
                step.name,
                condition,
            )
            await self._advance(flow_id, step_index, step.name, {})
            return

        # Execute the step
        try:
            if isinstance(step, FlowStep):
                output = await self._execute_agent_step(step, flow, flow_def)
            elif isinstance(step, FlowAction):
                output = await self._execute_action_step(step, flow)
            else:
                output = {}
        except Exception as exc:
            logger.error(
                "Flow %s step %d (%s) failed: %s",
                flow_id[:8],
                step_index,
                step.name,
                exc,
                exc_info=True,
            )
            on_failure = step.on_failure if isinstance(step, FlowStep) else "abort"
            if on_failure == "skip":
                logger.info("Skipping failed step %s (on_failure=skip)", step.name)
                await self._advance(flow_id, step_index, step.name, {"error": str(exc)})
                return
            # abort (default)
            await self.state_mgr.set_status(flow_id, "failed", str(exc))
            return

        await self._advance(flow_id, step_index, step.name, output)

    async def _execute_agent_step(
        self,
        step: FlowStep,
        flow: FlowInstance,
        flow_def: FlowDefinition,
    ) -> dict:
        """Run an agent as a flow step."""
        definition = self.registry.get(step.agent)
        if definition is None:
            raise ValueError(f"Agent '{step.agent}' not registered")

        # Reconstruct the trigger event for context building
        from forge.gateway.parser import EVENT_KIND_MAP

        event_data = flow.trigger_event
        event_kind = event_data.get("object_kind", "")
        model_cls = EVENT_KIND_MAP.get(event_kind, GitLabEvent)
        event = model_cls.model_validate(event_data)

        async with GitLabClient(
            base_url=self.settings.GITLAB_URL,
            token=self.settings.GITLAB_TOKEN.get_secret_value(),
        ) as gitlab:
            project_config = await load_project_config(gitlab, flow.project_id)
            context_engine = ContextEngine(gitlab, self.forge_config)

            # Build context based on event type
            if isinstance(event, MergeRequestEvent):
                context = await context_engine.build_mr_context(event)
            elif isinstance(event, NoteEvent):
                context = await context_engine.build_note_context(event)
            elif isinstance(event, PipelineEvent):
                context = await context_engine.build_pipeline_context(event)
            elif isinstance(event, JobEvent):
                context = await context_engine.build_job_context(event)
            else:
                # Minimal context for other event types
                context = await context_engine.build_note_context(event)

            model = get_model(definition.model_alias, self.forge_config, self.settings)
            agent_cls = get_agent_class(definition.type)
            agent = agent_cls(
                definition=definition,
                model=model,
                context=context,
                project_config=project_config,
                gitlab=gitlab,
            )

            result = await agent.run()

        if not result.success:
            raise RuntimeError(f"Agent '{step.agent}' failed: {result.error}")

        # Build output dict from the agent result
        output: dict = {}
        if result.review:
            output = result.review.model_dump()
        elif result.pipeline_debug:
            output = result.pipeline_debug.model_dump()
        elif result.security_triage:
            output = result.security_triage.model_dump()
        elif result.text_response:
            output = {"text": result.text_response}

        output["success"] = True
        output["duration_ms"] = result.duration_ms
        return output

    async def _execute_action_step(
        self,
        action: FlowAction,
        flow: FlowInstance,
    ) -> dict:
        """Execute a GitLab API action."""
        # Render template params against flow state
        rendered_params = {}
        for key, value in action.params.items():
            if isinstance(value, str):
                rendered_params[key] = render_template(value, flow.state)
            elif isinstance(value, list):
                rendered_params[key] = [
                    render_template(v, flow.state) if isinstance(v, str) else v for v in value
                ]
            else:
                rendered_params[key] = value

        async with GitLabClient(
            base_url=self.settings.GITLAB_URL,
            token=self.settings.GITLAB_TOKEN.get_secret_value(),
        ) as gitlab:
            if action.action == "create_branch":
                result = await gitlab.create_branch(
                    flow.project_id,
                    rendered_params["branch_name"],
                    rendered_params.get("ref", "main"),
                )
                return {
                    "name": result.get("name", ""),
                    "commit": result.get("commit", {}).get("id", ""),
                }

            if action.action == "open_merge_request":
                result = await gitlab.create_merge_request(
                    flow.project_id,
                    source_branch=rendered_params["source_branch"],
                    target_branch=rendered_params["target_branch"],
                    title=rendered_params.get("title", ""),
                    description=rendered_params.get("description", ""),
                    labels=rendered_params.get("labels"),
                )
                return {
                    "iid": result.get("iid"),
                    "url": result.get("web_url", ""),
                    "id": result.get("id"),
                }

            if action.action == "post_comment":
                body = rendered_params.get("body", "")
                target = rendered_params.get("target", "merge_request")

                # Determine target IID from trigger event
                event_data = flow.trigger_event
                if target == "issue":
                    iid = _extract_issue_iid(event_data)
                    if iid:
                        await gitlab.create_issue_note(flow.project_id, iid, body)
                else:
                    iid = _extract_mr_iid(event_data)
                    if iid:
                        await gitlab.create_mr_note(flow.project_id, iid, body)

                return {"posted": True}

            if action.action == "add_label":
                labels = rendered_params.get("labels", [])
                iid = _extract_mr_iid(flow.trigger_event)
                if iid and labels:
                    await gitlab.add_mr_labels(flow.project_id, iid, labels)
                return {"labels_added": labels}

        return {}

    async def _advance(
        self,
        flow_id: str,
        step_index: int,
        step_name: str,
        output: dict,
    ) -> None:
        """Store step output and enqueue the next step (or complete the flow)."""
        flow = await self.state_mgr.get(flow_id)
        if flow is None:
            return

        flow_def = self.loader.get(flow.flow_name)
        if flow_def is None:
            return

        next_index = step_index + 1

        # Store output under the step's output_key or step name
        step = flow_def.steps[step_index]
        output_key = None
        if isinstance(step, FlowStep):
            output_key = step.output_key
        elif isinstance(step, FlowAction):
            output_key = step.output_key
        store_key = output_key or step_name

        if next_index >= len(flow_def.steps):
            # Flow complete
            await self.state_mgr.update_step(
                flow_id, step_index, store_key, output, status="completed"
            )
            logger.info("Flow %s completed after step %d (%s)", flow_id[:8], step_index, step_name)
        else:
            await self.state_mgr.update_step(flow_id, step_index, store_key, output)
            task = create_flow_step_task(flow_id, next_index, flow.flow_name)
            await self.queue.submit(task)
            logger.debug(
                "Flow %s advancing to step %d",
                flow_id[:8],
                next_index,
            )

    async def abort_flow(self, flow_id: str, reason: str) -> None:
        """Abort a running flow."""
        await self.state_mgr.set_status(flow_id, "aborted", reason)
        logger.info("Flow %s aborted: %s", flow_id[:8], reason)


def _extract_mr_iid(event_data: dict) -> int | None:
    """Extract MR IID from serialized event data."""
    attrs = event_data.get("object_attributes", {})
    if attrs.get("iid") and event_data.get("object_kind") == "merge_request":
        return attrs["iid"]
    mr = event_data.get("merge_request", {})
    if mr and mr.get("iid"):
        return mr["iid"]
    return None


def _extract_issue_iid(event_data: dict) -> int | None:
    """Extract issue IID from serialized event data."""
    attrs = event_data.get("object_attributes", {})
    if attrs.get("iid") and event_data.get("object_kind") == "issue":
        return attrs["iid"]
    issue = event_data.get("issue", {})
    if issue and issue.get("iid"):
        return issue["iid"]
    return None
