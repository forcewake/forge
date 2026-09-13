"""Harness usage receipts on the llm_calls ledger (ADR-0016 §4, F22 lite).

Revision ID: 007
Revises: 006
Create Date: 2026-09-13 00:00:00.000000

- ``llm_calls.driver``: the harness driver id ("claude-code", "grok-build",
  "opencode", ...) for receipt rows recorded from candidate artifacts —
  NULL for forge-side LLM calls.
- ``llm_calls.completeness``: "exact" | "aggregate" | "unknown" — sums of
  per-turn receipts are "aggregate"; an absent receipt stays "unknown",
  never zero.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("llm_calls", sa.Column("driver", sa.String(length=50), nullable=True))
    op.add_column("llm_calls", sa.Column("completeness", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_calls", "completeness")
    op.drop_column("llm_calls", "driver")
