"""Durable webhook inbox (ADR-0005).

GitLab delivers webhooks at-least-once. Ingestion is therefore idempotent:
the caller derives a ``source_event_id`` (see :func:`build_source_event_id`)
and :func:`ingest_event` inserts the row only if that identity has never been
seen. Duplicate deliveries return the existing row with ``created=False`` and
must not trigger side effects again.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from forge.durable.models import EventInbox


def build_source_event_id(
    project_id: int,
    object_kind: str,
    object_iid: int | None,
    action: str,
    delivery_id: str,
) -> str:
    """Derive the inbox identity for a GitLab webhook delivery.

    sha256 over project id, object kind, object iid, the action (e.g. ``open``
    / ``merge`` / a note identifier) and the GitLab delivery uniqueness (the
    ``X-Gitlab-Event-UUID`` / hook delivery id). Two deliveries of the same
    logical event produce the same id.
    """
    material = "|".join(
        (
            str(project_id),
            str(object_kind),
            "" if object_iid is None else str(object_iid),
            str(action),
            str(delivery_id),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def ingest_event(
    session: AsyncSession,
    *,
    source_event_id: str,
    project_id: int,
    event_type: str,
    payload: dict,
) -> tuple[EventInbox, bool]:
    """Insert an inbox row unless *source_event_id* was already ingested.

    Returns ``(row, created)``. The insert is the dedup arbiter (ADR-0017 §1):
    ``INSERT ... ON CONFLICT DO NOTHING`` against the unique delivery key, so
    a concurrent ingester racing in another transaction is resolved by the
    index itself — no select-then-insert window, no savepoint. Duplicate
    deliveries return the existing row with ``created=False``; the original
    payload is preserved.
    """
    # ON CONFLICT DO NOTHING works identically on Postgres and SQLite (tests);
    # the pg insert construct compiles to the same statement on both.
    result = await session.execute(
        pg_insert(EventInbox)
        .values(
            source_event_id=source_event_id,
            project_id=project_id,
            event_type=event_type,
            payload=payload,
        )
        .on_conflict_do_nothing(index_elements=[EventInbox.source_event_id])
    )
    # rowcount is the INSERT's inserted-row count; SQLAlchemy 2.0 stubs only
    # type it on CursorResult, so access it via the runtime attr.
    if result.rowcount == 1:  # type: ignore[attr-defined]
        row = (
            await session.execute(
                select(EventInbox).where(EventInbox.source_event_id == source_event_id)
            )
        ).scalar_one()
        return row, True

    winner = (
        await session.execute(
            select(EventInbox)
            .where(EventInbox.source_event_id == source_event_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return winner, False


async def mark_processed(
    session: AsyncSession,
    event: EventInbox,
    *,
    handler_result: dict | None = None,
) -> EventInbox:
    """Mark an ingested event as handled."""
    event.status = "processed"
    event.processed_at = datetime.now(timezone.utc)
    event.handler_result = handler_result
    await session.flush()
    return event


async def mark_rejected(
    session: AsyncSession,
    event: EventInbox,
    *,
    handler_result: dict | None = None,
) -> EventInbox:
    """Mark an ingested event as rejected (seen but not acted upon)."""
    event.status = "rejected"
    event.processed_at = datetime.now(timezone.utc)
    event.handler_result = handler_result
    await session.flush()
    return event
