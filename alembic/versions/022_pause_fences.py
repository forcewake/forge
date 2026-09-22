"""The durable publication pause fence (R28-08, review 1ae5290 §6).

Revision ID: 022
Revises: 021
Create Date: 2026-09-22 00:00:00.000000

The pause authority lived in process memory (``OperatorControlService.
pause_states``) and in the lane process's own command drain; the classic
publisher re-checked only run cancellation. Restarting the API or a
worker erased the pause, and a stale lane presenting a previously valid
candidate could still authorize a new provider effect.

``pause_fences`` makes the fence ONE persisted authority state shared by
control processing and every publisher: a row per work recording
``(work_id, publication_epoch_bumped, fenced_at)`` when the pause lands;
``cleared_at`` / ``resumed_publication_epoch`` / ``cleared_by_command``
when resume opens a NEW epoch. The publisher (the GitHub classic path's
``publish_validated`` native-effect boundary) reads the row immediately
before the commit-API call and refuses while it is active — the refusal
is durable, not in-memory.

No backfill: a pause that happened before this migration was never
durable, and fabricating fence rows for past pauses would claim an
authority that was not there. The table starts empty; the next pause
writes the first row.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pause_fences",
        sa.Column("work_id", sa.String(length=64), primary_key=True),
        sa.Column("publication_epoch_bumped", sa.Integer(), nullable=False),
        sa.Column("fenced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raised_by_command", sa.String(length=64), nullable=True),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resumed_publication_epoch", sa.Integer(), nullable=True),
        sa.Column("cleared_by_command", sa.String(length=64), nullable=True),
    )
    # The publisher's boundary read is point-lookup by work_id; the
    # operator's audit view scans recency.
    op.create_index("ix_pause_fences_fenced_at", "pause_fences", ["fenced_at"])


def downgrade() -> None:
    # Honest downgrade (the 020/021 precedent): an ACTIVE fence row is a
    # live publication authority — dropping it would silently un-fence
    # every paused work. Refuse while any fence is active; roll forward.
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT count(*) FROM pause_fences WHERE cleared_at IS NULL")
    ).scalar()
    if rows and int(rows or 0) > 0:
        raise RuntimeError(
            "downgrade 022->021 refused: pause_fences holds "
            f"{rows} active fence(s) — dropping them would un-fence paused "
            "works. This schema is forward-only while control state exists; "
            "roll forward instead."
        )
    op.drop_index("ix_pause_fences_fenced_at", table_name="pause_fences")
    op.drop_table("pause_fences")
