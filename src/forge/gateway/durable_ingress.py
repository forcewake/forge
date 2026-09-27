"""The durable-acceptance helpers every gateway command ingress shares.

R41-02 (#357) — the ONE rule, stated once for all three provider gateways
(GitLab ``router.py``, GitHub ``github_webhook.py``, Azure DevOps
``azure_webhook.py``): an actionable command is acknowledged only after the
inbox + scheduled-step transaction COMMITTED, and the Redis dedup marker is
a post-commit positive cache — never the dedup authority.

The helpers here are the cache-side halves of that contract:

- :func:`inbox_record_exists` — the authoritative SQL lookup. Before any
  ``deduplicated`` answer the delivery identity is proven against the
  committed inbox row; a marker without a record (a failed transaction
  behind a pre-commit marker — the P02 schedule) means the delivery is a
  FIRST delivery, not a suppressed one.
- :func:`cache_confirms_duplicate` — the read-only post-commit probe plus
  that authoritative confirmation, with the ``ingress.cache_without_record``
  anomaly surfaced on the orphaned-marker fall-through.
- :func:`mark_delivered_best_effort` — the post-commit positive-cache write.
  It runs strictly AFTER the commit and is best-effort: Redis loss changes
  latency, never correctness.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from forge.durable.models import EventInbox

logger = logging.getLogger(__name__)


async def inbox_record_exists(session_factory: Any, source_event_id: str) -> bool:
    """The authoritative duplicate check: does the inbox row exist in SQL.

    R41-02 (#357): the Redis marker is a post-commit positive cache, never
    the dedup authority. Before any ``deduplicated`` answer the gateway
    proves the delivery identity against PostgreSQL — a marker without a
    record (a failed transaction behind a pre-commit marker) means the
    delivery is treated as a FIRST delivery, not suppressed.
    """
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
    """Whether the cache hint is backed by the committed inbox row.

    A cache hit is only a HINT: the authoritative lookup must confirm the
    committed record before any deduplicated answer. Returns True only on a
    confirmed duplicate; an orphaned marker (the pre-commit SET-NX of a
    failed transaction) falls through — the marker must never suppress an
    unsaved command — and a probe failure degrades to the database deciding
    alone.
    """
    try:
        cached = any([await queue.was_delivered(key) for key in marker_keys])
    except Exception:
        logger.warning("Dedup cache probe failed — database decides alone", exc_info=True)
        return False
    if not cached:
        return False
    if await inbox_record_exists(session_factory, source_event_id):
        return True
    # The anomaly the review asked to surface: a marker whose record never
    # landed (pre-commit marker of a failed transaction, or a stale
    # leftover). Fall through and ingest — the delivery proceeds as a first
    # delivery, never a successful empty duplicate.
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
