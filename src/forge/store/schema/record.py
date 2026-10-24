"""Installation evidence links the fresh engine to its verified offline archive."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from forge.store.base import Base


class SchemaInstallation(Base):
    __tablename__ = "schema_installation"
    __table_args__ = (CheckConstraint("id = 1", name="ck_schema_installation_singleton"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    installation_id: Mapped[str] = mapped_column(String(32), unique=True)
    baseline_sha256: Mapped[str] = mapped_column(String(64))
    archive_sha256: Mapped[str | None] = mapped_column(String(64))
    backup_sha256: Mapped[str | None] = mapped_column(String(64))
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
