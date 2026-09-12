from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from forge.agents.registry import AgentDefinition
from forge.gitlab.events import (
    GitLabEvent,
    JobEvent,
    MergeRequestEvent,
    NoteEvent,
    PipelineEvent,
)

if TYPE_CHECKING:
    from forge.agents.registry import AgentRegistry
    from forge.config import ForgeConfig
    from forge.orchestrator.project_config import ProjectConfig

logger = logging.getLogger(__name__)


def match_agents(
    event: GitLabEvent,
    registry: AgentRegistry,
    project_config: ProjectConfig,
    forge_config: ForgeConfig,
) -> list[AgentDefinition]:
    """Return agent definitions whose triggers match the given event.

    Applies project-config filtering and agent-level settings (draft skip, etc.).
    """
    matched: list[AgentDefinition] = []

    for definition in registry.all():
        if not _agent_enabled(definition, project_config):
            continue

        if not _triggers_match(definition, event, forge_config):
            continue

        if not _passes_settings(definition, event, forge_config):
            continue

        matched.append(definition)

    if matched:
        logger.info(
            "Matched %d agent(s) for %s event: %s",
            len(matched),
            event.object_kind,
            ", ".join(a.name for a in matched),
        )
    else:
        logger.debug("No agents matched for %s event", event.object_kind)

    return matched


def _agent_enabled(
    definition: AgentDefinition,
    project_config: ProjectConfig,
) -> bool:
    """Check if the agent is enabled per project config."""
    if definition.name in project_config.disabled_agents:
        return False
    if (
        project_config.enabled_agents is not None
        and definition.name not in project_config.enabled_agents
    ):
        return False
    return True


def _triggers_match(
    definition: AgentDefinition,
    event: GitLabEvent,
    forge_config: ForgeConfig,
) -> bool:
    """Check if any of the agent's triggers match the event."""
    for trigger in definition.triggers:
        if trigger.event != event.object_kind:
            continue

        # MR events: match on action (open, update, reopen, etc.)
        if isinstance(event, MergeRequestEvent):
            action = event.object_attributes.action or ""
            if trigger.actions and action not in trigger.actions:
                continue
            return True

        # Note events: optionally require a @mention
        if isinstance(event, NoteEvent):
            if trigger.mention:
                mention_pattern = forge_config.defaults.get("mention_trigger", "@forge")
                note_text = event.object_attributes.note or ""
                if not re.search(re.escape(mention_pattern), note_text, re.IGNORECASE):
                    continue
            return True

        # Pipeline events: match on status (e.g. "failed")
        if isinstance(event, PipelineEvent):
            status = event.object_attributes.status or ""
            if trigger.actions and status not in trigger.actions:
                continue
            return True

        # Job events: match on job name and/or status
        if isinstance(event, JobEvent):
            if trigger.job_names and event.build_name not in trigger.job_names:
                continue
            if trigger.actions and event.build_status not in trigger.actions:
                continue
            return True

        # Other event types: match on event kind alone
        return True

    return False


def _passes_settings(
    definition: AgentDefinition,
    event: GitLabEvent,
    forge_config: ForgeConfig,
) -> bool:
    """Apply agent-level and forge-level settings filters."""
    if not isinstance(event, MergeRequestEvent):
        return True

    attrs = event.object_attributes

    # Skip draft MRs
    skip_draft = definition.settings.get(
        "skip_draft",
        forge_config.defaults.get("skip_draft_mrs", True),
    )
    if skip_draft and attrs.draft:
        logger.debug(
            "Skipping agent '%s' — MR !%d is a draft",
            definition.name,
            attrs.iid,
        )
        return False

    # Skip WIP MRs (legacy GitLab title prefix)
    skip_wip = definition.settings.get("skip_wip", True)
    if skip_wip and attrs.title and attrs.title.startswith("WIP:"):
        logger.debug(
            "Skipping agent '%s' — MR !%d is WIP",
            definition.name,
            attrs.iid,
        )
        return False

    return True
