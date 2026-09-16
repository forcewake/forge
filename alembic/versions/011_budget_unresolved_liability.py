"""Budget unresolved-usage liability counters (ADR-0013 unknown ≠ zero).

Revision ID: 011
Revises: 010
Create Date: 2026-09-16 00:00:00.000000

- ``run_budgets.unresolved_calls`` / ``unresolved_tokens``: usage a receipt
  failed to report, parked beside the known actuals so an unknown receipt
  fences hard-budget capacity instead of releasing it as spendable. Reserve
  and exhaust predicates count exposure as ``consumed + reserved +
  unresolved`` (:mod:`forge.durable.budgets`).
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
