"""Persistence layer for @mention conversation history."""

from __future__ import annotations


import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import delete, select

from forge.models.conversation import Conversation

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


class ConversationStore:
    """Manage @mention conversation history.

    Conversations are scoped to
    ``(project_id, noteable_type, noteable_iid, discussion_id)``,
    giving each discussion thread its own independent history.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def get_history(
        self,
        project_id: int,
        noteable_type: str,
        noteable_iid: int,
        discussion_id: str,
    ) -> list[dict[str, str]]:
        """Load conversation history for a specific thread.

        Returns a list of ``{"role": "user"|"assistant", "content": "..."}``
        dicts, or an empty list if no conversation exists.
        """
        async with self.session_factory() as session:
            stmt = select(Conversation).where(
                Conversation.project_id == project_id,
                Conversation.noteable_type == noteable_type,
                Conversation.noteable_iid == noteable_iid,
                Conversation.discussion_id == discussion_id,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None or not row.messages:
                return []
            return list(row.messages)

    async def append(
        self,
        project_id: int,
        noteable_type: str,
        noteable_iid: int,
        discussion_id: str,
        role: str,
        content: str,
    ) -> None:
        """Append a message to conversation history (upsert)."""
        async with self.session_factory() as session:
            stmt = select(Conversation).where(
                Conversation.project_id == project_id,
                Conversation.noteable_type == noteable_type,
                Conversation.noteable_iid == noteable_iid,
                Conversation.discussion_id == discussion_id,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()

            message = {"role": role, "content": content}

            if row is not None:
                current = list(row.messages or [])
                current.append(message)
                row.messages = current
                row.updated_at = datetime.now(timezone.utc)
            else:
                row = Conversation(
                    project_id=project_id,
                    noteable_type=noteable_type,
                    noteable_iid=noteable_iid,
                    discussion_id=discussion_id,
                    messages=[message],
                )
                session.add(row)

            await session.commit()

    async def cleanup_expired(self, max_age_days: int = 30) -> int:
        """Remove conversations older than *max_age_days*.

        Returns the number of rows deleted.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        async with self.session_factory() as session:
            stmt = delete(Conversation).where(Conversation.updated_at < cutoff)
            result = await session.execute(stmt)
            await session.commit()
            # DML executes as a CursorResult, whose ``rowcount`` the typed
            # Result facade does not carry (mypy: attr-defined).
            count = cast("CursorResult[Any]", result).rowcount
            logger.info("Cleaned up %d expired conversation(s)", count)
            return count
