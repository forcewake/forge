"""Budgets: unresolved usage liability counters (R12).

Revision ID: 011
Revises: 010
Create Date: 2026-09-16 00:00:00.000000

- ``run_budgets.unresolved_calls`` / ``unresolved_tokens``: exposure whose
  true figure never became known. A settled hold with unknown usage moves
  here from ``reserved_*`` instead of releasing spendable headroom as if the
  receipt had cost nothing (unknown is never zero — ADR-0013). Both counters
  enter the reservable exposure the reservation predicate enforces.
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
