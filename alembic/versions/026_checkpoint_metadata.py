"""Checkpoint metadata table — the postgres durability contract (R32-16, review 0fca1b7).

Revision ID: 026
Revises: 025
Create Date: 2026-09-23 00:00:00.000000

Retention decisions and active checkpoint selection lived in per-work
filesystem JSON — fine for one API process on a shared volume, but a
second replica (or blobs moving to object storage) makes that index a
single-writer fiction the deployment cannot honor. ``FORGE_CHECKPOINT_
DURABILITY=postgres`` selects the contract this table backs: the
checkpoint INDEX becomes rows in ``checkpoint_metadata`` — transactional
puts under ``SELECT ... FOR UPDATE`` over the work's rows, idempotent
re-puts by the composite primary key ``(work_id, checkpoint_id)`` (the
content address IS the identity), and active selection DERIVED from
``(sequence, checkpoint_id)`` exactly as the filesystem index derives
it. The BLOBS do not move: they stay content-addressed filesystem bytes
whose writes need no lock.

No backfill: the filesystem index remains authoritative for work
uploaded under the best-effort contract — copying it here would let two
authorities disagree the moment a best-effort writer lands after the
cutover. A deployment switching contracts starts the table empty and
re-uploads (checkpoints are content-addressed and idempotent); the
health report names the ACTIVE mode so the split is visible.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "checkpoint_metadata",
        sa.Column("work_id", sa.String(length=128), primary_key=True),
        sa.Column("checkpoint_id", sa.String(length=64), primary_key=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("files", sa.Integer(), nullable=False),
        sa.Column("uploaded_at", sa.String(length=32), nullable=False, server_default=""),
    )
    # The composite PK's work_id prefix already serves the per-work scans;
    # this index serves the reconciler/operator views that order by
    # sequence across one work without sorting held rows.
    op.create_index(
        "ix_checkpoint_metadata_work_sequence",
        "checkpoint_metadata",
        ["work_id", "sequence"],
    )


def downgrade() -> None:
    # Honest downgrade (the 023 precedent): the rows ARE the authoritative
    # checkpoint index under this contract — dropping them strands every
    # referenced blob behind an index nobody can rebuild (the filesystem
    # index was never written in this mode). Refuse while metadata exists.
    bind = op.get_bind()
    held = bind.execute(sa.text("SELECT count(*) FROM checkpoint_metadata")).scalar_one()
    if held:
        raise RuntimeError(
            f"checkpoint_metadata still indexes {held} checkpoint(s) — restore the "
            "filesystem index (re-upload under the best-effort contract) before "
            "downgrading; these rows are the only authority over which blob "
            "a paused work resumes from"
        )
    op.drop_index("ix_checkpoint_metadata_work_sequence", table_name="checkpoint_metadata")
    op.drop_table("checkpoint_metadata")
