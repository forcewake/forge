"""Native-start intent on execution leases (Q35-04, review c7ae8db).

Revision ID: 027
Revises: 026
Create Date: 2026-09-23 00:00:00.000000

The capacity limit was a RESERVATION guarantee, not an OCCUPANCY
guarantee: nothing recorded that a provider start call was attempted,
so a lost start response (or a worker death between the provider
accepting the start and the handle landing) freed the slot on the
run's LOCAL status while the native job kept burning a runner. This
revision gives ``execution_leases`` the two evidence columns of the
derived occupancy state machine:

- ``native_intent_at`` — set BEFORE the provider call
  (``record_native_start_intent``); NULL is the PROOF of
  never-dispatched occupancy (the builtin lane, a pre-call abort);
- ``native_intent_ref`` — the short provider-shaped correlation marker
  (``github:workflow:...@branch`` / ``gitlab:pipeline:...@branch`` /
  ``azure:pipeline:...@branch``): its prefix routes the reconciler's
  native-status probe when the handle was never recorded.

Occupancy itself is NOT stored — it is derived (never_dispatched /
dispatched_unknown / native_running / draining / observed_terminal)
from these columns, ``native_handle``, ``draining_at`` and
``released_at``. A partial index over the OPEN intent-carrying rows
gives the reconciler and the operator's occupancy report their
watchlist without sweeping the audit trail.

No backfill: pre-existing leases dispatched no intent we can invent —
they keep the release-at-local-terminal behavior (NULL intent columns
IS that behavior, the same 025 posture). The 024 unique-open-reservation
invariants are untouched; attempt replacement reuses a held reservation
for the same run id, so no new constraint is needed.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None

#: The occupancy watchlist: OPEN leases whose dispatch intent is live.
_INTENT_OPEN = sa.text("released_at IS NULL AND native_intent_at IS NOT NULL")


def upgrade() -> None:
    op.add_column(
        "execution_leases",
        sa.Column("native_intent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "execution_leases",
        sa.Column("native_intent_ref", sa.String(length=200), nullable=True),
    )
    op.create_index(
        "ix_execution_leases_native_intent",
        "execution_leases",
        ["native_intent_at"],
        sqlite_where=_INTENT_OPEN,
        postgresql_where=_INTENT_OPEN,
    )


def downgrade() -> None:
    # Honest downgrade (the 023/025 precedent): an OPEN lease carrying a
    # start intent is a live reservation whose occupancy evidence lives
    # in these columns — dropping them would recast dispatched-unknown
    # capacity as never-dispatched (premature release, silent
    # overbooking). Refuse while any open lease still carries an intent;
    # released/audit history is unaffected.
    bind = op.get_bind()
    undecided = bind.execute(
        sa.text(
            "SELECT count(*) FROM execution_leases "
            "WHERE released_at IS NULL AND native_intent_at IS NOT NULL"
        )
    ).scalar_one()
    if undecided:
        raise RuntimeError(
            f"execution_leases still holds {undecided} open lease(s) with a live "
            "native-start intent — wait for their native jobs to be observed "
            "terminal (run the reconciler) before downgrading; dropping the "
            "intent columns would make undecided occupancy look never-dispatched"
        )
    op.drop_index("ix_execution_leases_native_intent", table_name="execution_leases")
    op.drop_column("execution_leases", "native_intent_ref")
    op.drop_column("execution_leases", "native_intent_at")
