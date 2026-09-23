"""Durable execution-slot leases at dispatch (NEXT-11/NEXT-12, review ccab247).

Revision ID: 023
Revises: 022
Create Date: 2026-09-23 00:00:00.000000

Queue admission counted ACTIVE runs at ``/implement`` time, but the
execution slot was only reserved when work was DISPATCHED (``/go``) —
four tasks could all pass admission while the gate was quiet and then
all activate together. ``execution_leases`` is the reservation: one row
per held slot, taken by compare-and-set INSERT against the partial
unique index ``uq_execution_lease_slot`` over
``(project_id, provider, slot)``
WHERE ``released_at IS NULL`` — two workers racing for the final slot
produce exactly one winner because the database rejects the loser's
INSERT. The lease is held from dispatch until the run's terminal
status; a released row stays as the completed-attempt audit trail
(NEXT-12's ``execution_attempts.completed`` counter) and its slot is
immediately reusable.

No backfill: a lease is a live capacity authority for work that is
dispatching NOW. Fabricating historical leases for already-running work
would either under-count (capacity appears free while runs execute) or
over-count (phantom slots refuse honest dispatch). Existing runs keep
their status-quo accounting — the count-at-admission behavior — until
their next dispatch takes a real lease; operators draining a project to
the new discipline can inspect :func:`forge.adaptive.admission.
admission_report` for the split.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None

_LEASE_OPEN = sa.text("released_at IS NULL")


def upgrade() -> None:
    op.create_table(
        "execution_leases",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("run_id", sa.String(length=32), nullable=True),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.String(length=100), nullable=True),
        sa.CheckConstraint("slot >= 1", name="ck_execution_leases_slot"),
    )
    # The CAS key: one OPEN lease per (project, slot). Point lookups by
    # project and by run (the terminal-release spelling).
    op.create_index("ix_execution_leases_project_id", "execution_leases", ["project_id"])
    op.create_index("ix_execution_leases_run_id", "execution_leases", ["run_id"])
    op.create_index(
        "uq_execution_lease_slot",
        "execution_leases",
        ["project_id", "provider", "slot"],
        unique=True,
        sqlite_where=_LEASE_OPEN,
        postgresql_where=_LEASE_OPEN,
    )


def downgrade() -> None:
    # Honest downgrade (the 020-022 precedent): an OPEN lease row is a
    # live capacity reservation — dropping it would silently un-reserve
    # slots whose dispatch already relied on them. Refuse while any
    # lease is held; roll forward.
    bind = op.get_bind()
    held = bind.execute(
        sa.text("SELECT count(*) FROM execution_leases WHERE released_at IS NULL")
    ).scalar_one()
    if held:
        raise RuntimeError(
            f"execution_leases still holds {held} open lease(s) — release them "
            "(drive the runs to terminal status) before downgrading; an open "
            "lease is a live capacity reservation, not historical data"
        )
    op.drop_index("uq_execution_lease_slot", table_name="execution_leases")
    op.drop_index("ix_execution_leases_run_id", table_name="execution_leases")
    op.drop_index("ix_execution_leases_project_id", table_name="execution_leases")
    op.drop_table("execution_leases")
