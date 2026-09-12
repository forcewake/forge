from __future__ import annotations

import logging
from datetime import datetime, timezone

from forge.flows.models import FlowInstance
from forge.utils.redis_client import RedisManager

logger = logging.getLogger(__name__)

_PREFIX = "forge:flow"
_DEFAULT_TTL = 86400  # 24 hours


class FlowStateManager:
    """Redis-backed flow instance state management."""

    def __init__(self, redis: RedisManager) -> None:
        self.redis = redis

    def _key(self, flow_id: str) -> str:
        return f"{_PREFIX}:{flow_id}"

    async def create(self, flow: FlowInstance) -> str:
        """Create a new flow instance in Redis. Returns the flow ID."""
        await self.redis.set_ex(
            self._key(flow.id),
            flow.to_json(),
            ex=_DEFAULT_TTL,
        )
        logger.info("Flow instance created: %s (%s)", flow.id[:8], flow.flow_name)
        return flow.id

    async def get(self, flow_id: str) -> FlowInstance | None:
        """Get a flow instance by ID."""
        raw = await self.redis.get(self._key(flow_id))
        if raw is None:
            return None
        return FlowInstance.from_json(raw)

    async def update_step(
        self,
        flow_id: str,
        step_index: int,
        step_name: str,
        output: dict,
        status: str = "running",
    ) -> None:
        """Record step output and advance the flow."""
        flow = await self.get(flow_id)
        if flow is None:
            logger.error("Flow %s not found for step update", flow_id[:8])
            return

        flow.current_step = step_index + 1
        flow.state[step_name] = output
        flow.status = status
        flow.updated_at = datetime.now(timezone.utc).isoformat()

        await self.redis.set_ex(
            self._key(flow_id),
            flow.to_json(),
            ex=_DEFAULT_TTL,
        )

    async def set_status(
        self,
        flow_id: str,
        status: str,
        error: str | None = None,
    ) -> None:
        """Update flow status (completed, failed, aborted)."""
        flow = await self.get(flow_id)
        if flow is None:
            logger.error("Flow %s not found for status update", flow_id[:8])
            return

        flow.status = status
        flow.error = error
        flow.updated_at = datetime.now(timezone.utc).isoformat()

        await self.redis.set_ex(
            self._key(flow_id),
            flow.to_json(),
            ex=_DEFAULT_TTL,
        )

    async def get_step_output(self, flow_id: str, step_name: str) -> dict | None:
        """Get output from a specific step by name."""
        flow = await self.get(flow_id)
        if flow is None:
            return None
        return flow.state.get(step_name)
