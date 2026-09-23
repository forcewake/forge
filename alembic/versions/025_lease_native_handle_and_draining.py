"""Native execution correlation and the draining state (R32-07, review 0fca1b7).

Revision ID: 025
Revises: 024
Create Date: 2026-09-23 00:00:00.000000

Local terminal status and actual native job completion are not the same
thing: a FlowRun reaching ``ready_for_human`` says the control plane is
done with the work, not that the dispatched CI pipeline stopped burning
a runner. Releasing the lease at the local transition could hand the
slot to the next dispatch while the native job still runs — silent
overbooking. This revision gives ``execution_leases`` the two columns
that split the reservation's lifetime from the run's status:

- ``native_handle`` — the dispatched native job's correlation (the
  pipeline/run id), recorded at dispatch once the provider answers;
  NULL before that (nothing to probe yet);
- ``draining_at`` — set by ``release_lease(..., native_completed=False)``
  when the run is locally terminal but the native job is still running.
  A draining lease still HOLDS its slot (``released_at`` stays NULL, the
  partial open indexes keep firing) until the reconciler's native probe
  observes the job terminal and releases it.

No backfill: pre-existing leases have no native correlation to invent —
they keep the release-at-local-terminal behavior (the columns stay
NULL, which IS that behavior). A partial index over the draining rows
gives the reconciler's periodic scan its worklist without sweeping the
whole audit trail.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None

#: Only the RECONCILER's worklist: open leases currently parked draining.
_DRAINING = sa.text("released_at IS NULL AND draining_at IS NOT NULL")


def upgrade() -> None:
    op.add_column(
        "execution_leases",
        sa.Column("native_handle", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "execution_leases",
        sa.Column("draining_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_execution_leases_draining",
        "execution_leases",
        ["draining_at"],
        sqlite_where=_DRAINING,
        postgresql_where=_DRAINING,
    )


def downgrade() -> None:
    # Honest downgrade (the 023 precedent): a DRAINING lease is a live
    # capacity reservation whose release is waiting on a NATIVE job —
    # dropping the correlation columns would make that observation
    # impossible (the slot could never be reconciled free). Refuse while
    # any lease is draining; released/audit history is unaffected.
    bind = op.get_bind()
    draining = bind.execute(
        sa.text(
            "SELECT count(*) FROM execution_leases "
            "WHERE released_at IS NULL AND draining_at IS NOT NULL"
        )
    ).scalar_one()
    if draining:
        raise RuntimeError(
            f"execution_leases still holds {draining} draining lease(s) — wait for "
            "their native jobs to be observed terminal (run the reconciler) before "
            "downgrading; a draining lease is a live reservation whose probe key "
            "lives in these columns"
        )
    op.drop_index("ix_execution_leases_draining", table_name="execution_leases")
    op.drop_column("execution_leases", "draining_at")
    op.drop_column("execution_leases", "native_handle")
