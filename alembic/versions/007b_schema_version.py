"""Schema compatibility marker (F26).

Revision ID: 007b
Revises: 007
Create Date: 2026-09-13 00:00:00.000000

Creates the single-row ``schema_version`` table and inserts the current
expected version. CONVENTION for future migrations: the FINAL migration in
every release writes/bumps the ``schema_version`` row (id = 1) to match
``forge.database.EXPECTED_SCHEMA_VERSION``. ``init_db`` refuses to start an
app against a database whose marker is missing or different — run
``alembic upgrade head`` first (docs/operations/upgrade.md).
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "007b"
down_revision = "007"
branch_labels = None
depends_on = None

#: Keep in sync with forge.database.EXPECTED_SCHEMA_VERSION.
SCHEMA_VERSION = 1


def upgrade() -> None:
    op.create_table(
        "schema_version",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # CURRENT_TIMESTAMP (not now()) so the row inserts on SQLite and Postgres.
    op.execute(
        sa.text(
            "INSERT INTO schema_version (id, version, updated_at) "
            "VALUES (1, :version, CURRENT_TIMESTAMP)"
        ).bindparams(version=SCHEMA_VERSION)
    )


def downgrade() -> None:
    op.drop_table("schema_version")
