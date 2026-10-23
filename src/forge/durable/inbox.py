"""Deduplicate accepted provider events before scheduling their work."""

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
    """Derive the inbox identity for a GitLab webhook delivery."""
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
    """Insert an inbox row unless *source_event_id* was already ingested."""
    # ON CONFLICT DO NOTHING works identically on Postgres and SQLite (tests); the pg insert
    # construct compiles to the same statement on both.
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
    # rowcount is the INSERT's inserted-row count; SQLAlchemy 2.0 stubs only type it on
    # CursorResult, so access it via the runtime attr.
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
