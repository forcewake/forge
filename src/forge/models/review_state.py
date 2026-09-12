from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy import JSON
from sqlalchemy.orm import Mapped, mapped_column

from forge.models.base import Base


class ReviewState(Base):
    __tablename__ = "review_states"
    __table_args__ = (UniqueConstraint("project_id", "mr_iid", name="uq_review_state_project_mr"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    mr_iid: Mapped[int] = mapped_column(Integer, nullable=False)
    last_reviewed_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    discussion_ids: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
