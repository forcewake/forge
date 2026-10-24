"""Lane mailbox, named checkpoint authorization and review lineage."""

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


class EngineControlCommand(RunBound, Timestamps, Base):
    __tablename__ = "control_commands"
    __table_args__ = (
        UniqueConstraint("run_id", "command_key", name="uq_control_key"),
        UniqueConstraint("run_id", "attempt", "sequence", name="uq_control_sequence"),
        CheckConstraint(
            "state IN ('pending','acknowledged','refused','expired')", name="ck_control_state"
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    command_key: Mapped[str] = mapped_column(String(300))
    attempt: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer)
    sequence: Mapped[int] = mapped_column(Integer)
    world_version: Mapped[int] = mapped_column(Integer)
    payload_json: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    acknowledgement_json: Mapped[str | None] = mapped_column(Text)


class EngineCheckpoint(RunBound, Timestamps, Base):
    __tablename__ = "checkpoints"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "attempt", "generation", "digest", name="uq_checkpoint_identity"
        ),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempt: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer)
    digest: Mapped[str] = mapped_column(String(64))
    base_sha: Mapped[str] = mapped_column(String(64))
    approved_digest: Mapped[str] = mapped_column(String(64))
    driver: Mapped[str] = mapped_column(String(100))
    manifest_json: Mapped[str] = mapped_column(Text)
    retained: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))


class EngineBlob(RunBound, Timestamps, Base):
    __tablename__ = "blobs"
    __table_args__ = (
        UniqueConstraint("run_id", "attempt", "generation", "digest", name="uq_blob_authorization"),
        CheckConstraint("size >= 0", name="ck_blob_size"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    attempt: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer)
    digest: Mapped[str] = mapped_column(String(64), index=True)
    size: Mapped[int] = mapped_column(Integer)
    storage_ref: Mapped[str] = mapped_column(String(500))
    checkpoint_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("checkpoints.id"))


class EngineReviewRound(RunBound, Timestamps, Base):
    __tablename__ = "review_rounds"
    __table_args__ = (
        UniqueConstraint("root_run_id", "feedback_key", name="uq_round_feedback"),
        CheckConstraint("state IN ('open','closed','refused')", name="ck_round_state"),
        Index(
            "uq_round_slot",
            "root_review_ref",
            unique=True,
            sqlite_where=text("state = 'open'"),
            postgresql_where=text("state = 'open'"),
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    root_run_id: Mapped[str] = mapped_column(String(32), ForeignKey("runs.id"))
    parent_run_id: Mapped[str] = mapped_column(String(32), ForeignKey("runs.id"))
    child_run_id: Mapped[str] = mapped_column(String(32), ForeignKey("runs.id"), unique=True)
    root_review_ref: Mapped[str] = mapped_column(String(600))
    feedback_key: Mapped[str] = mapped_column(String(300))
    base_sha: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20), default="open", server_default="open")


class EngineLaneSession(RunBound, Timestamps, Base):
    __tablename__ = "lane_sessions"
    __table_args__ = (
        UniqueConstraint("run_id", "attempt", "generation", name="uq_session_dispatch"),
        CheckConstraint(
            "attempt > 0 AND generation > 0 AND cancellation_generation >= 0",
            name="ck_session_counters",
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    attempt: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer)
    cancellation_generation: Mapped[int] = mapped_column(Integer)
    nonce_hash: Mapped[str] = mapped_column(String(64), unique=True)
    nonce_consumed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dispatch_json: Mapped[str] = mapped_column(Text)
    brief_json: Mapped[str] = mapped_column(Text)
    provider_binding_json: Mapped[str] = mapped_column(Text)
    first_job_json: Mapped[str | None] = mapped_column(Text)
    api_state_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
