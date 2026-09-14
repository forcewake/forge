"""GitHub run rows (E3a): FlowRun-backed runs for the GitHub vertical.

Revision ID: 008
Revises: 007b
Create Date: 2026-09-14 00:00:00.000000

- ``flow_runs.provider``: which integration owns the subject — ``gitlab``
  (default, every pre-existing row) or ``github``.
- ``flow_runs.github_repo_full_name`` / ``.github_issue_number``: the GitHub
  subject identity (``owner/repo`` + issue number) a ``provider='github'``
  run is bound to. ``project_id``/``issue_iid`` keep carrying the webhook's
  numeric repository id and issue number, so the existing partial unique
  index ``uq_active_run_per_issue`` enforces one active run per (repo,
  issue) for GitHub too.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "008"
down_revision = "007b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "flow_runs",
        sa.Column("provider", sa.String(20), nullable=False, server_default="gitlab"),
    )
    op.add_column("flow_runs", sa.Column("github_repo_full_name", sa.String(255), nullable=True))
    op.add_column("flow_runs", sa.Column("github_issue_number", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("flow_runs", "github_issue_number")
    op.drop_column("flow_runs", "github_repo_full_name")
    op.drop_column("flow_runs", "provider")
