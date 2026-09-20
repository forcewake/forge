"""ONE create_merge_request action row per run+branch (FI-suite race).

Revision ID: 018
Revises: 017
Create Date: 2026-09-20 00:00:00.000000

The S2/S4 failure-injection windows caught (2026-09-20) a check-then-create
race in the Draft-MR leg: a lease-expiry re-drive and the
publication-intent scanner can both decide "no Draft MR exists yet" before
either creates — two ``create_merge_request`` calls and two audit rows for
one intent. The provider-side effect is the customer-visible half (a
duplicate MR); the audit half breaks the "exactly one row per external
write" contract the recovery paths read back.

``uq_create_mr_per_branch`` makes ONE ``create_merge_request`` row per
``(flow_run_id, correlation_id=branch)`` a DB invariant: the second creator's
INSERT collapses onto the first row (``ON CONFLICT DO NOTHING``), which the
loser then locks (``FOR UPDATE``) and adopts — journal check, provider
check, create — exactly the A04/A10 pattern: the arbiter lives in the
database, not in application timing.

- the migration first COLLAPSES existing duplicates (keeps the lowest row
  per group — the extra rows are artifacts of the very defect being
  closed, not history: they never represent a distinct external write);
- the downgrade drops the index only.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None

_MR_KIND = "create_merge_request"


def _collapse_duplicates() -> None:
    """Keep the LOWEST create_merge_request row per (run, branch).

    The extras are artifacts of the race this migration closes: they were
    never distinct external writes (their creation was the defect), so
    removing them restores the audit contract instead of rewriting it.
    """
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            DELETE FROM action_log
             WHERE action_kind = :kind
               AND id NOT IN (
                   SELECT MIN(id)
                     FROM action_log
                    WHERE action_kind = :kind
                    GROUP BY flow_run_id, correlation_id
               )
            """
        ),
        {"kind": _MR_KIND},
    )


def upgrade() -> None:
    _collapse_duplicates()
    op.create_index(
        "uq_create_mr_per_branch",
        "action_log",
        ["flow_run_id", "correlation_id"],
        unique=True,
        postgresql_where=sa.text(f"action_kind = '{_MR_KIND}'"),
        sqlite_where=sa.text(f"action_kind = '{_MR_KIND}'"),
    )


def downgrade() -> None:
    op.drop_index("uq_create_mr_per_branch", table_name="action_log")
