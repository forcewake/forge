"""Usage receipt identity (R23): the durable ledger of ingested receipts.

Harness spend arrives as candidate-artifact usage receipts that the control
plane ingests at-least-once: a repeated reconciler tick, a crash between the
ingest and the run transition, or a re-downloaded artifact all replay the
SAME receipt, and every replay used to be able to consume the run budget (or
duplicate the spend ledger) again. ``usage_receipts`` closes it:

- one row per ``(run_id, attempt_id, receipt_id)`` — the receipt identity
  sha256 over the run, the lane attempt and the normalized usage JSON,
  deterministic across re-downloads of the same artifact (a repair
  re-dispatch changes the attempt id, so it is a legitimately distinct
  receipt);
- the unique index is the ingest arbiter: ``INSERT ... ON CONFLICT DO
  NOTHING`` (:func:`forge.durable.budgets.ingest_usage_receipt`) turns every
  replay into a no-op at the DB — no select-then-insert window, no
  read-modify-write race;
- the canonical counters carry the ADR-0013 honesty rules: unknown stays
  NULL (never zero), cache counters stay out of ``input_tokens``, and
  ``raw`` keeps the verbatim usage block so spend stays reconstructable;
- a receipt whose usage was missing/invalid still lands here with
  ``completeness='unknown'`` — the attempt's cost is recorded as unknown,
  never silently as zero.

Conservative shape, mirroring 012-014:

- one new table, no changes to existing tables, no backfill: pre-015
  receipts keep reconciling through the paths they use today (the GitLab
  lane's episode dedupe claim);
- ``CHECK`` on the closed completeness set like every other durable table;
- the downgrade drops the table — spend remains explainable from the
  ``llm_calls`` ledger rows the ingest wrote alongside.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_receipts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("attempt_id", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("receipt_id", sa.String(length=64), nullable=False),
        sa.Column("driver", sa.String(length=50), nullable=True),
        sa.Column("model", sa.String(length=200), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("completeness", sa.String(length=20), nullable=False, server_default="unknown"),
        sa.Column("source", sa.String(length=100), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["flow_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "completeness IN ('exact', 'aggregate', 'unknown')",
            name="ck_usage_receipts_completeness",
        ),
    )
    op.create_index(
        op.f("ix_usage_receipts_run_id"),
        "usage_receipts",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        "uq_usage_receipt_identity",
        "usage_receipts",
        ["run_id", "attempt_id", "receipt_id"],
        unique=True,
    )


def downgrade() -> None:
    # The receipt identity is idempotency state; the spend it guarded stays
    # visible on the run budgets and the llm_calls ledger.
    op.drop_index("uq_usage_receipt_identity", table_name="usage_receipts")
    op.drop_index(op.f("ix_usage_receipts_run_id"), table_name="usage_receipts")
    op.drop_table("usage_receipts")
