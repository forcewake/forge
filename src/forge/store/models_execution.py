"""Durable queue, external effect journal and candidate receipts."""

from datetime import datetime

from sqlalchemy import (
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


class EngineStep(RunBound, Timestamps, Base):
    __tablename__ = "steps"
    __table_args__ = (
        CheckConstraint(
            "state IN ('scheduled','running','succeeded','failed','cancelled','dead')",
            name="ck_step_state",
        ),
        CheckConstraint(
            "attempts >= 0 AND max_attempts > 0 AND lease_epoch >= 0", name="ck_step_counts"
        ),
        CheckConstraint(
            "handler IN ('plan','approve','dispatch','await_lane','publish','verify','review','ready','feedback_review')",
            name="ck_step_handler",
        ),
        CheckConstraint(
            "(state = 'running' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) OR state <> 'running'",
            name="ck_step_lease",
        ),
        Index("ix_steps_due", "state", "due_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    key: Mapped[str] = mapped_column(String(300), unique=True)
    handler: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20), default="scheduled", server_default="scheduled")
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    lease_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer)
    cancellation_generation: Mapped[int] = mapped_column(Integer)
    result_json: Mapped[str | None] = mapped_column(Text)


class EngineEffect(Timestamps, Base):
    __tablename__ = "effects"
    __table_args__ = (
        CheckConstraint(
            "(step_id IS NOT NULL AND run_id IS NOT NULL AND command_id IS NULL) OR "
            "(step_id IS NULL AND command_id IS NOT NULL)",
            name="ck_effect_owner",
        ),
        CheckConstraint(
            "state IN ('requested','dispatched','committed','adopted','conflict','failed','unknown')",
            name="ck_effect_state",
        ),
        CheckConstraint("version > 0 AND lease_epoch > 0", name="ck_effect_counters"),
        CheckConstraint(
            "kind <> 'ensure_draft' OR (repo_key IS NOT NULL AND head_branch IS NOT NULL)",
            name="ck_effect_draft_identity",
        ),
        Index(
            "uq_effect_draft",
            "repo_key",
            "head_branch",
            unique=True,
            sqlite_where=text("kind = 'ensure_draft'"),
            postgresql_where=text("kind = 'ensure_draft'"),
        ),
        CheckConstraint(
            "(kind = 'dispatch_lane' AND dispatch_nonce_digest IS NOT NULL) OR "
            "(kind <> 'dispatch_lane' AND dispatch_nonce_digest IS NULL)",
            name="ck_effect_dispatch_nonce",
        ),
        Index("uq_effect_dispatch_nonce", "dispatch_nonce_digest", unique=True),
        Index("ix_effect_unresolved", "state", "updated_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    key: Mapped[str] = mapped_column(String(300), unique=True)
    kind: Mapped[str] = mapped_column(String(100))
    run_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("runs.id"), index=True)
    step_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("steps.id"))
    command_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("commands.id"), index=True
    )
    lease_epoch: Mapped[int] = mapped_column(Integer)
    payload_digest: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20), default="requested", server_default="requested")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    outcome_json: Mapped[str | None] = mapped_column(Text)
    observations_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    repo_key: Mapped[str | None] = mapped_column(String(400))
    head_branch: Mapped[str | None] = mapped_column(String(255))
    dispatch_nonce_digest: Mapped[str | None] = mapped_column(String(64))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatch_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EngineCandidate(RunBound, Timestamps, Base):
    __tablename__ = "candidates"
    __table_args__ = (
        UniqueConstraint("run_id", "attempt", "generation", name="uq_candidate_dispatch"),
        CheckConstraint(
            "state IN ('received','validated','refused','published')", name="ck_candidate_state"
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    attempt: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer)
    digest: Mapped[str] = mapped_column(String(64))
    base_sha: Mapped[str] = mapped_column(String(64))
    approved_digest: Mapped[str] = mapped_column(String(64))
    manifest_json: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20), default="received", server_default="received")
    validation_json: Mapped[str | None] = mapped_column(Text)
    published_sha: Mapped[str | None] = mapped_column(String(64))
