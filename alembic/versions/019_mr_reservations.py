"""ONE logical MR intent per run+branch, separate from immutable attempts (B03).

Revision ID: 019
Revises: 018
Create Date: 2026-09-21 00:00:00.000000

Migration 018 serialized Draft-MR creation behind ONE immutable
``create_merge_request`` action row — closing the two-creator race but
welding the LOGICAL intent to a row whose journal contract is terminal
and immutable. The lost-response window (provider created the MR, the
response died, the row recorded ``unknown_outcome``) then made adoption
impossible: reconciling the found MR would need ``complete_action(
succeeded)`` on a terminal row — ``InvalidActionTransition`` — so the run
stayed mid-publish forever (review e53ffd2 B03, probe P03). The intent
row also carried the reservation inside the same open transaction as the
provider I/O: the intent was not yet durable when the remote effect could
already happen.

``mr_reservations`` separates the two concerns:

- **reservation** (``UNIQUE(flow_run_id, branch)``) — the logical "one MR
  for this run+branch" intent: ``open → confirmed``; durable and committed
  BEFORE any provider I/O; ``FOR UPDATE`` on it serializes concurrent
  creators (a loser blocks behind the winner, then sees ``confirmed``);
- **action_log rows** — the immutable attempt/observation history. Each
  create attempt, adoption and reconciliation journals its OWN row; the
  018 partial unique index is dropped (history may hold several rows
  again — they are distinct observations, not duplicate writes).

Backfill: for every (run, branch) whose journal already holds a
``succeeded`` create row, the reservation is created ``confirmed`` with
that mr_iid; runs with only open/failed/unknown rows get an ``open``
reservation. The 018 duplicate-collapse DELETE is NOT re-run — 018
deployments already collapsed, and on 018-skipped deployments (fresh
chains) there is nothing to collapse.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None

_MR_KIND = "create_merge_request"


def upgrade() -> None:
    op.create_table(
        "mr_reservations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("flow_run_id", sa.String(length=32), nullable=False),
        sa.Column("branch", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("mr_iid", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("flow_run_id", "branch", name="uq_mr_reservation_run_branch"),
        sa.CheckConstraint("status IN ('open', 'confirmed')", name="ck_mr_reservation_status"),
    )
    # Backfill confirmed reservations from succeeded journal rows.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            f"""
            INSERT INTO mr_reservations (flow_run_id, branch, status, mr_iid)
            SELECT a.flow_run_id, a.correlation_id, 'confirmed',
                   (a.remote_result ->> 'mr_iid')::int
              FROM action_log a
             WHERE a.action_kind = '{_MR_KIND}'
               AND a.status = 'succeeded'
               AND a.remote_result ? 'mr_iid'
               AND a.correlation_id IS NOT NULL
             ORDER BY a.id
            ON CONFLICT DO NOTHING
            """
        )
    )
    # Open reservations for runs whose create rows exist but never succeeded.
    bind.execute(
        sa.text(
            f"""
            INSERT INTO mr_reservations (flow_run_id, branch, status)
            SELECT a.flow_run_id, a.correlation_id, 'open'
              FROM action_log a
             WHERE a.action_kind = '{_MR_KIND}'
               AND a.correlation_id IS NOT NULL
             ORDER BY a.id
            ON CONFLICT DO NOTHING
            """
        )
    )
    # The 018 index welded intent to the journal — history may hold several
    # observation rows again (each adoption/reconciliation is its own row).
    op.drop_index("uq_create_mr_per_branch", table_name="action_log")


def downgrade() -> None:
    op.create_index(
        "uq_create_mr_per_branch",
        "action_log",
        ["flow_run_id", "correlation_id"],
        unique=True,
        postgresql_where=sa.text(f"action_kind = '{_MR_KIND}'"),
        sqlite_where=sa.text(f"action_kind = '{_MR_KIND}'"),
    )
    op.drop_table("mr_reservations")
