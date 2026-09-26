"""Budget amendments + the closing partition columns (R40-04 #340).

The recorded defect: ``continue_review_only`` recorded amount/reason/
operator into run EVIDENCE, released the marker and re-ran the review
against the SAME ``RunBudget`` limits and the same ``BudgetGuard`` —
nothing amended the limits/status the guard enforces, so a dollar-only
annotation could never reopen an exhausted call or token budget, and the
coder's reservations could eat the allowance the mandatory closing
review needed (nothing partitioned it BEFORE coding started).

From 030:

- ``budget_amendments`` — ONE persisted amendment per ORIGINATING
  NATIVE COMMAND identity (UNIQUE ``(run_id, command_id)``): two
  identical amount/reason commands are two decisions, a redelivery of
  one command applies once (INSERT ... ON CONFLICT DO NOTHING is the
  arbiter, then the standing row is read). The amendment names exactly
  ONE distinct axis (``usd`` | ``calls`` | ``tokens`` | ``wallclock``
  — the one-axis CHECK), carries a required reason, the operator, the
  typed refusal (with the limiting axis named) and the before/after
  limits — the original approved budget stays as history. Count-axis
  amendments move the ``run_budgets`` limits ATOMICALLY in the same
  transaction that inserts the row; usd amendments raise the closing
  gate's effective cap (the row IS the enforcement record).
- ``run_budgets.closing_reserved_calls`` / ``closing_reserved_tokens``
  / ``closing_partition_policy`` — the closing share of the guard's
  numeric axes, frozen at open time by the stated versioned partition
  policy (``closing-partition/1``: ceil(limit × closing fraction) per
  axis; wall-clock not partitioned under v1). ``reserve`` enforces it:
  the implementation purpose cannot enter the share, the closing
  purpose (the reviewer leg) can. NULL = un-partitioned — every
  pre-030 budget row keeps its exact behavior (the columns default
  NULL; no backfill guesses a share for budgets already opened).

No data migration: pre-030 rows are honest as they stand.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "budget_amendments",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("command_id", sa.String(length=150), nullable=False),
        sa.Column("axis", sa.String(length=20), nullable=False),
        sa.Column("amount_usd", sa.Float(), nullable=True),
        sa.Column("amount_calls", sa.Integer(), nullable=True),
        sa.Column("amount_tokens", sa.Integer(), nullable=True),
        sa.Column("amount_wallclock_s", sa.Integer(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("operator", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="applied"),
        sa.Column("refusal_reason", sa.String(length=300), nullable=True),
        sa.Column("limit_before", sa.JSON(), nullable=True),
        sa.Column("limit_after", sa.JSON(), nullable=True),
        sa.Column(
            "applied_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["run_id"], ["flow_runs.id"]),
        sa.CheckConstraint(
            "axis IN ('usd', 'calls', 'tokens', 'wallclock')",
            name="ck_budget_amendments_axis",
        ),
        sa.CheckConstraint("status IN ('applied', 'refused')", name="ck_budget_amendments_status"),
        sa.CheckConstraint(
            "(axis = 'usd' AND amount_usd IS NOT NULL"
            " AND amount_calls IS NULL AND amount_tokens IS NULL"
            " AND amount_wallclock_s IS NULL)"
            " OR (axis = 'calls' AND amount_calls IS NOT NULL"
            " AND amount_usd IS NULL AND amount_tokens IS NULL"
            " AND amount_wallclock_s IS NULL)"
            " OR (axis = 'tokens' AND amount_tokens IS NOT NULL"
            " AND amount_usd IS NULL AND amount_calls IS NULL"
            " AND amount_wallclock_s IS NULL)"
            " OR (axis = 'wallclock' AND amount_wallclock_s IS NOT NULL"
            " AND amount_usd IS NULL AND amount_calls IS NULL"
            " AND amount_tokens IS NULL)",
            name="ck_budget_amendments_one_axis_amount",
        ),
        sa.UniqueConstraint("run_id", "command_id", name="uq_budget_amendment_command"),
    )
    op.create_index("ix_budget_amendments_run_id", "budget_amendments", ["run_id"])
    op.add_column(
        "run_budgets",
        sa.Column("closing_reserved_calls", sa.Integer(), nullable=True),
    )
    op.add_column(
        "run_budgets",
        sa.Column("closing_reserved_tokens", sa.Integer(), nullable=True),
    )
    op.add_column(
        "run_budgets",
        sa.Column("closing_partition_policy", sa.String(length=40), nullable=True),
    )


def downgrade() -> None:
    # Honest downgrade (the 028/029 precedent): refuse while reverting
    # would DESTROY authorization evidence. Amendment rows are the audit
    # record of every budget the operator moved mid-run — dropping the
    # table while rows exist would leave run_budgets limits whose
    # provenance is gone (a limit that was raised by a command would
    # read as originally approved). The partition columns carry no
    # authority of their own (NULL = un-partitioned) and drop cleanly.
    bind = op.get_bind()
    recorded = bind.execute(sa.text("SELECT count(*) FROM budget_amendments")).scalar_one()
    if recorded:
        raise RuntimeError(
            f"budget_amendments holds {recorded} amendment row(s) — the amendment "
            "records are budget authorization evidence and are never dropped by a "
            "downgrade; archive them explicitly (and accept the lost provenance of "
            "the moved limits) before downgrading"
        )
    op.drop_column("run_budgets", "closing_partition_policy")
    op.drop_column("run_budgets", "closing_reserved_tokens")
    op.drop_column("run_budgets", "closing_reserved_calls")
    op.drop_index("ix_budget_amendments_run_id", table_name="budget_amendments")
    op.drop_table("budget_amendments")
