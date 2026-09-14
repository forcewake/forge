"""Run budgets: reserve-before-dispatch enforcement (ADR-0018 §5, F22 full).

Revision ID: 009
Revises: 008
Create Date: 2026-09-14 00:00:00.000000

- ``run_budgets``: one budget row per run (``run_id`` UNIQUE —
  :func:`forge.durable.budgets.open_budget` is idempotent per run). Limits
  (``wallclock_s``, ``max_calls``, ``max_tokens``) are NULL = unlimited;
  ``reserved_*`` hold in-flight dispatch grants, ``consumed_*`` accumulate
  provider actuals; ``status`` is 'open' | 'exhausted' | 'closed'.
- ``budget_reservations``: audit row per granted hold, flipped to
  ``released`` exactly once at reconcile.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_budgets",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("run_id", sa.String(length=32), sa.ForeignKey("flow_runs.id"), nullable=False),
        sa.Column("spec_digest", sa.String(length=64), nullable=True),
        sa.Column("wallclock_s", sa.Integer(), nullable=True),
        sa.Column("max_calls", sa.Integer(), nullable=True),
        sa.Column("max_tokens", sa.Integer(), nullable=True),
        sa.Column("reserved_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consumed_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consumed_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('open', 'exhausted', 'closed')",
            name="ck_run_budgets_status",
        ),
    )
    # UNIQUE + indexed: idempotent open per run, and the run_id lookup the
    # harness-receipt reconciliation uses.
    op.create_index("ix_run_budgets_run_id", "run_budgets", ["run_id"], unique=True)

    op.create_table(
        "budget_reservations",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "run_budget_id", sa.String(length=32), sa.ForeignKey("run_budgets.id"), nullable=False
        ),
        sa.Column("attempt_id", sa.String(length=100), nullable=True),
        sa.Column("reserved_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("released", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_budget_reservations_run_budget_id", "budget_reservations", ["run_budget_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_budget_reservations_run_budget_id", table_name="budget_reservations")
    op.drop_table("budget_reservations")
    op.drop_index("ix_run_budgets_run_id", table_name="run_budgets")
    op.drop_table("run_budgets")
