from __future__ import annotations

import logging
from pathlib import Path

import yaml

from forge.flows.models import FlowAction, FlowDefinition, FlowStep, FlowTrigger

logger = logging.getLogger(__name__)


class FlowLoader:
    """Loads flow definitions from YAML files."""

    def __init__(self, flows_dir: str | Path = "agents/flows") -> None:
        self._flows_dir = Path(flows_dir)
        self._flows: dict[str, FlowDefinition] = {}
        self._command_map: dict[str, FlowDefinition] = {}

    def load(self) -> None:
        """Scan the flows directory and load all .yml files."""
        self._flows.clear()
        self._command_map.clear()

        if not self._flows_dir.exists():
            logger.info("Flows directory %s does not exist — no flows loaded", self._flows_dir)
            return

        for path in sorted(self._flows_dir.glob("*.yml")):
            try:
                flow_def = self._parse_file(path)
                self._flows[flow_def.name] = flow_def
                # Extract the slash command from the trigger (e.g., "@forge /implement" → "/implement")
                command = self._extract_command(flow_def.trigger.command)
                if command:
                    self._command_map[command] = flow_def
                logger.info("Loaded flow: %s (command: %s)", flow_def.name, command)
            except Exception:
                logger.error("Failed to load flow from %s", path, exc_info=True)

    def get(self, name: str) -> FlowDefinition | None:
        """Get a flow definition by name."""
        return self._flows.get(name)

    def get_by_command(self, command: str) -> FlowDefinition | None:
        """Get a flow definition by its trigger command (e.g., '/implement')."""
        return self._command_map.get(command)

    def all(self) -> list[FlowDefinition]:
        """Return all loaded flow definitions."""
        return list(self._flows.values())

    def commands(self) -> frozenset[str]:
        """Return all registered flow commands as a frozenset."""
        return frozenset(self._command_map.keys())

    def _parse_file(self, path: Path) -> FlowDefinition:
        """Parse a YAML file into a FlowDefinition."""
        with open(path) as f:
            data = yaml.safe_load(f)

        trigger_data = data.get("trigger", {})
        trigger = FlowTrigger(
            command=trigger_data.get("command", ""),
            target=trigger_data.get("target", "merge_request"),
            auto=trigger_data.get("auto", False),
            events=trigger_data.get("events", []),
        )

        steps: list[FlowStep | FlowAction] = []
        for step_data in data.get("steps", []):
            steps.append(self._parse_step(step_data))

        return FlowDefinition(
            name=data["name"],
            version=data.get("version", "1.0"),
            description=data.get("description", ""),
            trigger=trigger,
            steps=steps,
            timeout=data.get("timeout", 1800),
        )

    @staticmethod
    def _parse_step(data: dict) -> FlowStep | FlowAction:
        """Parse a step dict into either a FlowStep or FlowAction."""
        if data.get("type") == "action":
            return FlowAction(
                name=data.get("name", data.get("action", "")),
                action=data["action"],
                params=data.get("params", {}),
                condition=data.get("condition"),
                output_key=data.get("output_key"),
            )
        return FlowStep(
            name=data["name"],
            agent=data["agent"],
            action=data.get("action"),
            input_from=data.get("input_from"),
            output_key=data.get("output_key"),
            condition=data.get("condition"),
            on_failure=data.get("on_failure", "abort"),
            timeout=data.get("timeout", 300),
        )

    @staticmethod
    def _extract_command(trigger_command: str) -> str | None:
        """Extract the slash command from a trigger string.

        e.g., '@forge /implement' → '/implement'
        """
        parts = trigger_command.strip().split()
        for part in parts:
            if part.startswith("/"):
                return part.lower()
        return None
