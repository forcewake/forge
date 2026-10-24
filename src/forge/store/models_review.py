"""Native review cursors and immutable human-push requests without lane authority."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge.store.base import Base
from forge.store.models_common import Timestamps


class EngineReviewCursor(Timestamps, Base):
    __tablename__ = "review_cursors"
    __table_args__ = (CheckConstraint("version > 0", name="ck_review_cursor_version"),)
    root_review_ref: Mapped[str] = mapped_column(String(600), primary_key=True)
    root_run_id: Mapped[str] = mapped_column(String(32), ForeignKey("runs.id"), index=True)
    review_json: Mapped[str] = mapped_column(Text)
    last_reviewed_sha: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class EngineReactiveReview(Timestamps, Base):
    __tablename__ = "reactive_reviews"
    __table_args__ = (
        UniqueConstraint("root_review_ref", "head_sha", name="uq_reactive_review_head"),
        CheckConstraint(
            "state IN ('accepted','model_requested','model_completed','commented','stale')",
            name="ck_reactive_review_state",
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("runs.id"), unique=True)
    root_run_id: Mapped[str] = mapped_column(String(32), ForeignKey("runs.id"), index=True)
    root_review_ref: Mapped[str] = mapped_column(
        String(600), ForeignKey("review_cursors.root_review_ref"), index=True
    )
    base_sha: Mapped[str] = mapped_column(String(64))
    head_sha: Mapped[str] = mapped_column(String(64))
    actor_json: Mapped[str] = mapped_column(Text)
    policy_json: Mapped[str] = mapped_column(Text)
    input_json: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), default="accepted", server_default="accepted")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
