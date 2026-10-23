"""Persist command intake before scheduling durable execution."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from forge.durable.models import EventInbox

logger = logging.getLogger(__name__)


async def inbox_record_exists(session_factory: Any, source_event_id: str) -> bool:
    """The authoritative duplicate check: does the inbox row exist in SQL."""
    async with session_factory() as session:
        row = (
            await session.execute(
                select(EventInbox.id).where(EventInbox.source_event_id == source_event_id).limit(1)
            )
        ).scalar_one_or_none()
    return row is not None


async def cache_confirms_duplicate(
    queue: Any,
    marker_keys: list[str],
    session_factory: Any,
    source_event_id: str,
) -> bool:
    """Whether the cache hint is backed by the committed inbox row."""
    try:
        cached = any([await queue.was_delivered(key) for key in marker_keys])
    except Exception:
        logger.warning("Dedup cache probe failed — database decides alone", exc_info=True)
        return False
    if not cached:
        return False
    if await inbox_record_exists(session_factory, source_event_id):
        return True
    # A receipt marker without its durable record must be reported as anomalous
    # (pre-commit marker of a failed transaction, or a stale leftover).
    logger.warning(
        "ingress.cache_without_record identity=%s — treating as first delivery",
        source_event_id[:12],
    )
    return False


async def mark_delivered_best_effort(queue: Any, marker_keys: list[str]) -> None:
    """The post-commit positive-cache write; loss costs latency only."""
    try:
        for key in marker_keys:
            await queue.mark_delivered(key)
    except Exception:
        logger.warning("Run command dedup cache write failed", exc_info=True)
