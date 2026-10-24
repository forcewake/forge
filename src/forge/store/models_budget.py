"""Exact cumulative accounting, receipts and encrypted gateway material."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge.store.base import Base
from forge.store.models_common import RunBound, Timestamps
from forge.store.types import ExactCount, ExactMoney


class EngineBudgetPool(RunBound, Timestamps, Base):
    __tablename__ = "budget_pools"
    __table_args__ = (
        UniqueConstraint("run_id", "purpose", name="uq_pool_purpose"),
        CheckConstraint(
            "usd >= 0 AND calls >= 0 AND tokens >= 0 AND version > 0", name="ck_pool_capacity"
        ),
        CheckConstraint("purpose IN ('implementation','closing')", name="ck_pool_purpose"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(20))
    usd: Mapped[Decimal] = mapped_column(ExactMoney())
    calls: Mapped[int] = mapped_column(ExactCount())
    tokens: Mapped[int] = mapped_column(ExactCount())
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    state_json: Mapped[str] = mapped_column(Text)


class EngineBudgetReservation(Timestamps, Base):
    __tablename__ = "budget_reservations"
    __table_args__ = (
        UniqueConstraint("pool_id", "reservation_key", name="uq_reservation_key"),
        CheckConstraint(
            "state IN ('reserved','accrued','unknown','settled')", name="ck_reservation_state"
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    pool_id: Mapped[str] = mapped_column(String(32), ForeignKey("budget_pools.id"), index=True)
    reservation_key: Mapped[str] = mapped_column(String(300))
    requested_json: Mapped[str] = mapped_column(Text)
    minimum_json: Mapped[str] = mapped_column(Text)
    allowance_json: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20))
    observed_usd: Mapped[Decimal | None] = mapped_column(ExactMoney())
    observed_calls: Mapped[int | None] = mapped_column(ExactCount())
    observed_tokens: Mapped[int | None] = mapped_column(ExactCount())
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class EngineBudgetAmendment(RunBound, Timestamps, Base):
    __tablename__ = "budget_amendments"
    __table_args__ = (
        UniqueConstraint("run_id", "command_key", name="uq_amendment_command"),
        CheckConstraint(
            "state IN ('authorized','applied','refused','unknown')", name="ck_amendment_state"
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    pool_id: Mapped[str] = mapped_column(String(32), ForeignKey("budget_pools.id"))
    command_key: Mapped[str] = mapped_column(String(300))
    actor: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(String(300))
    amount_json: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20))
    gateway_effect_key: Mapped[str | None] = mapped_column(String(300))
    reservation_key: Mapped[str | None] = mapped_column(String(300))
    target_json: Mapped[str | None] = mapped_column(Text)


class EngineUsage(RunBound, Timestamps, Base):
    __tablename__ = "usage"
    __table_args__ = (
        UniqueConstraint("source_key", name="uq_usage_source"),
        CheckConstraint(
            "completeness IN ('exact','aggregate','partial','unknown','unmetered')",
            name="ck_usage_completeness",
        ),
        CheckConstraint(
            "(usd IS NULL OR usd >= 0) AND (calls IS NULL OR calls >= 0) AND (tokens IS NULL OR tokens >= 0)",
            name="ck_usage_nonnegative",
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_key: Mapped[str] = mapped_column(String(300))
    attempt: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer)
    alias: Mapped[str | None] = mapped_column(String(200))
    completeness: Mapped[str] = mapped_column(String(20))
    final: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    usd: Mapped[Decimal | None] = mapped_column(ExactMoney())
    calls: Mapped[int | None] = mapped_column(ExactCount())
    tokens: Mapped[int | None] = mapped_column(ExactCount())
    receipt_json: Mapped[str] = mapped_column(Text)
    conflict_json: Mapped[str | None] = mapped_column(Text)


class EngineGatewayKey(RunBound, Timestamps, Base):
    __tablename__ = "gateway_keys"
    __table_args__ = (
        UniqueConstraint("run_id", "attempt", "generation", "purpose", name="uq_key_dispatch"),
        CheckConstraint(
            "state IN ('reserved','active','unknown','blocked','purged')", name="ck_key_state"
        ),
        CheckConstraint("state <> 'purged' OR ciphertext IS NULL", name="ck_key_purged"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    alias: Mapped[str] = mapped_column(String(200), unique=True)
    purpose: Mapped[str] = mapped_column(
        String(20), default="implementation", server_default="implementation"
    )
    reference_json: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer)
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    token_digest: Mapped[str | None] = mapped_column(String(64))
    aad_digest: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(20), default="reserved", server_default="reserved")
    block_effect_key: Mapped[str | None] = mapped_column(String(300))
