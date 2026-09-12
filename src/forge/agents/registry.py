from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class TriggerSpec:
    """Defines when an agent should be triggered."""

    event: str  # "merge_request", "note", "pipeline", "build", etc.
    actions: list[str] = field(default_factory=list)  # ["open", "update"]
    mention: bool = False
    job_names: list[str] = field(default_factory=list)  # For job events: ["semgrep-sast", ...]


@dataclass
class AgentDefinition:
    """Parsed agent definition from a YAML file."""

    name: str
    type: str = "generic"
    description: str = ""
    version: str = "1.0"
    triggers: list[TriggerSpec] = field(default_factory=list)
    model_alias: str = "default"
    system_prompt: str = ""
    settings: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    actions: dict[str, bool] = field(default_factory=dict)
    mcp_servers: list[str] = field(default_factory=list)


def _parse_triggers(raw: dict) -> list[TriggerSpec]:
    """Parse the trigger section of a YAML agent definition."""
    events = raw.get("events", [])
    job_names = raw.get("job_names", [])
    triggers: list[TriggerSpec] = []
    for ev_str in events:
        # Format: "merge_request.open" or "note" or "pipeline.failed"
        parts = ev_str.split(".", 1)
        event = parts[0]
        action = parts[1] if len(parts) > 1 else ""
        triggers.append(
            TriggerSpec(
                event=event,
                actions=[action] if action else [],
                mention=raw.get("mention", False),
                job_names=list(job_names),
            )
        )
    # Merge triggers with the same event type
    merged: dict[str, TriggerSpec] = {}
    for t in triggers:
        if t.event in merged:
            merged[t.event].actions.extend(t.actions)
            # Union job_names
            existing = set(merged[t.event].job_names)
            for name in t.job_names:
                if name not in existing:
                    merged[t.event].job_names.append(name)
        else:
            merged[t.event] = TriggerSpec(
                event=t.event,
                actions=list(t.actions),
                mention=t.mention,
                job_names=list(t.job_names),
            )
    return list(merged.values())


def _parse_definition(data: dict) -> AgentDefinition:
    """Parse a raw YAML dict into an AgentDefinition."""
    trigger_raw = data.get("trigger", {})
    triggers = _parse_triggers(trigger_raw)

    # Merge trigger-level settings into settings
    settings = {
        "skip_draft": trigger_raw.get("skip_draft", True),
        "skip_wip": trigger_raw.get("skip_wip", True),
        "cooldown": trigger_raw.get("cooldown", 120),
    }
    settings.update(data.get("settings", {}))

    model_raw = data.get("model", {})
    model_alias = model_raw.get("default", "default") if isinstance(model_raw, dict) else model_raw

    return AgentDefinition(
        name=data["name"],
        type=data.get("type", "generic"),
        description=data.get("description", ""),
        version=str(data.get("version", "1.0")),
        triggers=triggers,
        model_alias=model_alias,
        system_prompt=data.get("system_prompt", ""),
        settings=settings,
        context=data.get("context", {}),
        output=data.get("output", {}),
        actions=data.get("actions", {}),
        mcp_servers=data.get("mcp_servers", []),
    )


class AgentRegistry:
    """Discovers, loads, and provides access to agent definitions from YAML."""

    def __init__(self, agents_dir: str | Path = "agents") -> None:
        self._agents: dict[str, AgentDefinition] = {}
        self._agents_dir = Path(agents_dir)

    def load(self) -> None:
        """Scan agents directory for .yml files and load definitions."""
        if not self._agents_dir.is_dir():
            logger.warning("Agents directory not found: %s", self._agents_dir)
            return

        for yml_path in sorted(self._agents_dir.glob("*.yml")):
            try:
                with open(yml_path, encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                if not isinstance(data, dict) or "name" not in data:
                    logger.warning("Skipping invalid agent YAML: %s", yml_path)
                    continue
                definition = _parse_definition(data)
                self._agents[definition.name] = definition
                logger.info(
                    "Loaded agent '%s' v%s from %s",
                    definition.name,
                    definition.version,
                    yml_path.name,
                )
            except Exception:
                logger.warning("Failed to load agent YAML: %s", yml_path, exc_info=True)

    def get(self, name: str) -> AgentDefinition | None:
        """Get a specific agent definition by name."""
        return self._agents.get(name)

    def all(self) -> list[AgentDefinition]:
        """Return all loaded agent definitions."""
        return list(self._agents.values())
