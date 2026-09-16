"""Budget admission counts unresolved usage liability (ADR-0013, ADR-0018 §5).

Revision ID: 011
Revises: 010
Create Date: 2026-09-16 00:00:00.000000

- ``run_budgets.unresolved_calls`` / ``unresolved_tokens``: holds settled
  against an UNKNOWN actual (unknown is never counted as zero). The hold used
  to be released outright, which let an unreadable receipt open hard-budget
  capacity as if nothing had been spent; the liability stays on the books and
  :func:`forge.durable.budgets.reserve` counts it against the limit.
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
