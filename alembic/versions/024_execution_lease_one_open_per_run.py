"""One OPEN execution lease per run (R32-06, review 0fca1b7).

Revision ID: 024
Revises: 023
Create Date: 2026-09-23 00:00:00.000000

The 023 slot index protects ``(project_id, provider, slot)`` — it decides
who owns a SLOT, but says nothing about how many slots one run may hold.
The review's P02 schedule is legal SQL under 023 alone: two acquirers
both observe no existing lease for the run, one wins slot 1, the loser of
slot 1 legally wins slot 2 — one task silently consuming two capacity
slots. This revision makes the idempotency unit — the RUN — a database
invariant: the partial unique index ``uq_execution_lease_open_run`` over
``run_id`` WHERE ``released_at IS NULL``. ``run_id`` stays nullable
(anonymous leases); NULLs are distinct in both SQLite and PostgreSQL
unique indexes, so anonymous rows never collide.

Pre-existing duplicates are reconciled, never deleted (attempt history
is the NEXT-12 audit trail): for each run holding more than one OPEN
lease, the EARLIEST-acquired one survives — the same verdict
``try_acquire_lease``'s read-winner path gives a racing second acquire —
and the later ones are RELEASED with an explicit reason. Rows stay, slots
return, the accounting stays honest.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "024"
down_revision = "023"
branch_labels = None
depends_on = None

_LEASE_OPEN = sa.text("released_at IS NULL")

#: Keep the earliest OPEN lease per run; release the rest (R32-06). The
#: window function is portable SQLite 3.25+/PostgreSQL; ``id`` breaks
#: acquired_at ties deterministically.
_RECONCILE = sa.text(
    """
    UPDATE execution_leases
       SET released_at = :now,
           release_reason = :reason
     WHERE released_at IS NULL
       AND run_id IS NOT NULL
       AND id NOT IN (
            SELECT id FROM (
                SELECT id,
                       row_number() OVER (
                           PARTITION BY run_id ORDER BY acquired_at, id
                       ) AS rn
                  FROM execution_leases
                 WHERE released_at IS NULL AND run_id IS NOT NULL
            ) ranked WHERE rn = 1
       )
    """
)


def upgrade() -> None:
    from datetime import UTC, datetime

    bind = op.get_bind()
    duplicates = bind.execute(
        sa.text(
            "SELECT run_id, count(*) FROM execution_leases "
            "WHERE released_at IS NULL AND run_id IS NOT NULL "
            "GROUP BY run_id HAVING count(*) > 1"
        )
    ).all()
    if duplicates:
        # Reported (the log is the duplicate-repair evidence R32-06 asks
        # for), then reconciled by release — no row is deleted.
        released = bind.execute(
            _RECONCILE,
            {
                "now": datetime.now(UTC).isoformat(),
                "reason": "reconciled: duplicate open lease per run (024, R32-06)",
            },
        ).rowcount
        print(  # noqa: T201 — alembic's operator surface is the console
            f"024: released {released} duplicate open lease(s) across "
            f"{len(duplicates)} run(s) before adding uq_execution_lease_open_run"
        )
    op.create_index(
        "uq_execution_lease_open_run",
        "execution_leases",
        ["run_id"],
        unique=True,
        sqlite_where=_LEASE_OPEN,
        postgresql_where=_LEASE_OPEN,
    )


def downgrade() -> None:
    # Symmetric with 023: drop the invariant, keep the data. Released
    # reconciliation rows stay released — they are history, not capacity.
    op.drop_index("uq_execution_lease_open_run", table_name="execution_leases")
