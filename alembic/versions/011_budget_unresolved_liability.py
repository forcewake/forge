"""Budget unresolved-usage liability counters (F22 hard-limit accuracy).

Revision ID: 011
Revises: 010
Create Date: 2026-09-16 00:00:00.000000

Adds ``run_budgets.unresolved_calls`` / ``unresolved_tokens``: a hold settled
against an UNKNOWN receipt keeps its estimate as a standing liability instead
of being released into zero spend — an unknown receipt must not reopen a hard
budget that the dispatch may already have burned. Additive-only (within a
minor the schema moves additively; existing rows backfill at 0).
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "run_budgets",
        sa.Column("unresolved_calls", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "run_budgets",
        sa.Column("unresolved_tokens", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("run_budgets", "unresolved_tokens")
    op.drop_column("run_budgets", "unresolved_calls")
