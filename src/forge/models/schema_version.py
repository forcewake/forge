"""Single-row schema compatibility marker (F26).

The ``schema_version`` table holds exactly one row (``id = 1``) with the
relational schema version this code expects. Alembic migrations create and
update it (the FINAL migration inserts/bumps the row), and
:func:`forge.database.init_db` refuses to bootstrap over an existing forge
database whose marker is missing or newer/older than
:data:`forge.database.EXPECTED_SCHEMA_VERSION` — ``create_all`` is a dev
bootstrap, never an upgrade.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer
from sqlalchemy.orm import Mapped, mapped_column

from forge.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SchemaVersion(Base):
    """The one schema-version row (``id = 1``)."""

    __tablename__ = "schema_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
