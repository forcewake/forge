"""Run identity, immutable decisions and canonical admission."""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from forge.store.base import Base
from forge.store.models_common import RunBound, Timestamps

_ACTIVE = "state NOT IN ('ready_for_human', 'blocked', 'failed', 'cancelled')"
_STATES = "'queued','planning','awaiting_approval','running','publishing','verifying','reviewing','ready_for_human','blocked','failed','cancelled'"


class EngineRun(Timestamps, Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(f"state IN ({_STATES})", name="ck_run_state"),
        CheckConstraint("provider IN ('github','gitlab')", name="ck_run_provider"),
        CheckConstraint(
            "attempt > 0 AND generation > 0 AND cancellation_generation >= 0 AND version > 0",
            name="ck_run_counters",
        ),
        CheckConstraint("subject_kind IN ('issue','review')", name="ck_run_subject_kind"),
        Index(
            "uq_active_subject",
            "subject_key",
            unique=True,
            sqlite_where=text(_ACTIVE),
            postgresql_where=text(_ACTIVE),
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider: Mapped[str] = mapped_column(String(20))
    connection_id: Mapped[str] = mapped_column(String(100))
    repository_id: Mapped[str] = mapped_column(String(100))
    subject_kind: Mapped[str] = mapped_column(String(20))
    subject_id: Mapped[str] = mapped_column(String(100))
    subject_key: Mapped[str] = mapped_column(String(600), index=True)
    state: Mapped[str] = mapped_column(String(32), default="queued", server_default="queued")
    failed_handler: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(String(300))
    root_run_id: Mapped[str] = mapped_column(String(32))
    parent_run_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("runs.id"))
    attempt: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    generation: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    cancellation_generation: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class EngineRunSpec(RunBound, Timestamps, Base):
    __tablename__ = "run_specs"
    __table_args__ = (UniqueConstraint("run_id", name="uq_spec_run"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    document_json: Mapped[str] = mapped_column(Text)


class EngineApprovedInput(RunBound, Timestamps, Base):
    __tablename__ = "approved_inputs"
    __table_args__ = (
        UniqueConstraint("run_id", "revision", name="uq_approved_revision"),
        CheckConstraint("revision > 0", name="ck_approved_revision"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer)
    digest: Mapped[str] = mapped_column(String(64))
    predecessor_digest: Mapped[str | None] = mapped_column(String(64))
    spec_digest: Mapped[str] = mapped_column(String(64))
    base_sha: Mapped[str] = mapped_column(String(64))
    document_json: Mapped[str] = mapped_column(Text)


class EngineGate(RunBound, Timestamps, Base):
    __tablename__ = "gates"
    __table_args__ = (
        UniqueConstraint("run_id", "decision_key", name="uq_gate_decision"),
        CheckConstraint(
            "state IN ('prepared','open','consumed','expired','refused')", name="ck_gate_state"
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    decision_key: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(20), default="prepared", server_default="prepared")
    approved_digest: Mapped[str] = mapped_column(String(64))
    actor_scope_json: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_by: Mapped[str | None] = mapped_column(String(200))
    consumed_step_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("steps.id", name="fk_gate_consuming_step")
    )
