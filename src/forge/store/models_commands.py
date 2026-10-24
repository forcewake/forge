"""Native command receipts and narrowly owned operator-effect authority."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, Index
from sqlalchemy.orm import Mapped, mapped_column

from forge.models.base import Base
from forge.store.models_common import Timestamps


class EngineCommand(Timestamps, Base):
    __tablename__ = "eng_commands"
    __table_args__ = (
        CheckConstraint(
            "state IN ('accepted','running','resolved','completed')", name="ck_eng_command_state"
        ),
        CheckConstraint(
            "lease_epoch >= 0 AND attempts >= 0 AND max_attempts > 0", name="ck_eng_command_counts"
        ),
        Index("ix_eng_commands_due", "state", "due_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_key: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(32))
    document_json: Mapped[str] = mapped_column(Text)
    run_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("eng_runs.id"), index=True)
    state: Mapped[str] = mapped_column(String(20), default="accepted", server_default="accepted")
    result_json: Mapped[str | None] = mapped_column(Text)
    reply_json: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    lease_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
