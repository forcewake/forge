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
    # Backfill confirmed reservations from succeeded journal rows. NB:
    # ``->>`` (not the ``?`` operator — that is jsonb-only and the
    # action_log column is plain json; the canary caught this on a real
    # Postgres), and a regex guard so a non-numeric mr_iid cannot fail
    # the whole chain.
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
               AND a.remote_result ->> 'mr_iid' ~ '^[0-9]+$'
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
    # C09: the 019-upgrade schema legitimately holds MULTIPLE observation
    # rows per (run, branch) (create attempts, adoptions, reconciliations —
    # each its own immutable journal row). Re-creating the 018 unique index
    # over such data would fail mid-downgrade; the downgrade must not
    # silently DELETE audit history either. So: keep exactly ONE row per
    # group — the succeeded observation when present, else the latest —
    # and mark the rest as reconciled observations rather than removing
    # them outright is NOT possible under the 018 contract; instead we
    # refuse when genuine multi-row history exists, telling the operator
    # the rollback needs the forward-only path.
    bind = op.get_bind()
    multi = bind.execute(
        sa.text(
            f"""
            SELECT count(*) FROM (
                SELECT flow_run_id, correlation_id
                  FROM action_log
                 WHERE action_kind = '{_MR_KIND}'
                 GROUP BY flow_run_id, correlation_id
                HAVING count(*) > 1
            ) g
            """
        )
    ).scalar()
    if multi and int(multi or 0) > 0:
        raise RuntimeError(
            "downgrade 019->018 refused: the action journal holds "
            f"{multi} (run, branch) group(s) with multiple create_merge_request "
            "observation rows — genuine 019-shaped history that the 018 unique "
            "index cannot represent without deleting audit rows. This schema "
            "is forward-only from here; roll forward instead."
        )
    op.create_index(
        "uq_create_mr_per_branch",
        "action_log",
        ["flow_run_id", "correlation_id"],
        unique=True,
        postgresql_where=sa.text(f"action_kind = '{_MR_KIND}'"),
        sqlite_where=sa.text(f"action_kind = '{_MR_KIND}'"),
    )
    op.drop_table("mr_reservations")
