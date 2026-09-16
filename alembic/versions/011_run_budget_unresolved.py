"""Run budgets: track unresolved usage liability separately (ADR-0018 §5, F22).

Revision ID: 011
Revises: 010
Create Date: 2026-09-16 00:00:00.000000

- ``run_budgets.unresolved_calls`` / ``unresolved_tokens``: holds whose
  settlement produced no known actual (ADR-0013 — unknown is never zero).
  They leave ``reserved_*`` (the hold is released, the audit row flipped) but
  land here, so a receipt the provider never costed keeps occupying budget
  capacity instead of opening it as zero spend. The reserve predicate counts
  ``consumed + unresolved + reserved`` against the limit.
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
