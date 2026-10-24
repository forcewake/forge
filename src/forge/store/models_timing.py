"""Append-only observed execution intervals; missing observations remain unknown."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge.store.base import Base
from forge.store.models_common import RunBound


class EngineStepTiming(RunBound, Base):
    __tablename__ = "step_timings"
    __table_args__ = (
        UniqueConstraint("step_id", "lease_epoch", name="uq_step_timing_epoch"),
        CheckConstraint(
            "lease_epoch > 0 AND seconds >= 0 AND seconds <= 86400", name="ck_step_timing_bound"
        ),
        CheckConstraint(
            "outcome IN ('completed','parked','rescheduled')", name="ck_step_timing_outcome"
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    step_id: Mapped[str] = mapped_column(String(32), ForeignKey("steps.id"), index=True)
    lease_epoch: Mapped[int] = mapped_column(Integer)
    handler: Mapped[str] = mapped_column(String(32))
    seconds: Mapped[float] = mapped_column(Float)
    outcome: Mapped[str] = mapped_column(String(20))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
