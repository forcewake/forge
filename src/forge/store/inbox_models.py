"""The durable native-event and replay inbox, independent of run tables."""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from forge.store.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EventInbox(Base):
    """Shared durable webhook and replay inbox."""

    __tablename__ = "event_inbox"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processed', 'rejected')", name="ck_event_inbox_status"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_event_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    project_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    handler_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
