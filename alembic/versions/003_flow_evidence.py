"""Flow-run evidence and commit-cycle budget (M2-1, ADR-0004/0008/0013).

Revision ID: 003
Revises: 002
Create Date: 2026-09-13 00:00:00.000000

- ``flow_runs.evidence``: incremental ADR-0008 evidence blob (plan digest and
  summary, review verdict + reviewed SHA, pipeline id/url/status).
- ``flow_runs.commit_cycle``: the ADR-0004 commit-cycle counter (1 = initial
  candidate; bounded code repairs increment it up to FORGE_MAX_COMMIT_CYCLES).

``sa.JSON`` is used for parity with the other JSON columns in this schema
(it maps to JSON on Postgres, JSON on SQLite, so tests and prod agree).
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("flow_runs", sa.Column("evidence", sa.JSON(), nullable=True))
    op.add_column(
        "flow_runs",
        sa.Column("commit_cycle", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("flow_runs", "commit_cycle")
    op.drop_column("flow_runs", "evidence")
