"""Review rounds — the linked post-readiness work unit (R40-02 #338).

The recorded defect: the correction window closed when the run reached
``ready_for_human`` (``correction_window_closed``), so the reviewer's
natural AFTER-readiness correction had no honest route — while reopening
the terminal record would rewrite a verified verdict. From 0338 a
follow-up round is a LINKED attempt: a new child ``flow_runs`` row with
its own admission/scope/budget/base head, related to the immutable ready
delivery through the new ``review_rounds`` table:

- ``uq_review_round_request`` (``parent_run_id``, ``note_id``) — one
  reviewer comment admits at most ONE round however many times the
  webhook replays (the note id is the idempotency key, matching the
  review-feedback request record);
- ``uq_review_round_open_per_root`` (partial UNIQUE on ``root_run_id``
  WHERE status IN ('admitted','dispatched')) — ONE outstanding
  correction per delivery lineage as a DB invariant: two authorized
  /fix notes racing the same head collapse to one round at the index,
  and the slot frees only when the round's child run reaches a terminal
  status (the reconciler pass closes the row).

The row never carries authority of its own: the base head, the decision
identity and the requesting approver are the audit join to the
review-feedback request and the child run's seeded active plan.

No data migration: rounds only exist from the new admission path.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_rounds",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("parent_run_id", sa.String(length=32), nullable=False),
        sa.Column("child_run_id", sa.String(length=32), nullable=False),
        sa.Column("root_run_id", sa.String(length=32), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("note_id", sa.String(length=150), nullable=False),
        sa.Column("mr_iid", sa.Integer(), nullable=False),
        sa.Column("base_head_sha", sa.String(length=40), nullable=False),
        sa.Column("decision_id", sa.String(length=64), nullable=False),
        sa.Column("requested_by", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="admitted"),
        sa.Column("status_reason", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["parent_run_id"], ["flow_runs.id"]),
        sa.ForeignKeyConstraint(["child_run_id"], ["flow_runs.id"]),
        sa.ForeignKeyConstraint(["root_run_id"], ["flow_runs.id"]),
        sa.CheckConstraint(
            "status IN ('admitted', 'dispatched', 'stale', 'completed', 'ended')",
            name="ck_review_rounds_status",
        ),
        sa.UniqueConstraint("parent_run_id", "note_id", name="uq_review_round_request"),
    )
    op.create_index("ix_review_rounds_parent_run_id", "review_rounds", ["parent_run_id"])
    op.create_index("ix_review_rounds_child_run_id", "review_rounds", ["child_run_id"])
    op.create_index("ix_review_rounds_root_run_id", "review_rounds", ["root_run_id"])
    op.create_index(
        "uq_review_round_open_per_root",
        "review_rounds",
        ["root_run_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('admitted', 'dispatched')"),
        sqlite_where=sa.text("status IN ('admitted', 'dispatched')"),
    )


def downgrade() -> None:
    # Honest downgrade (the 030 precedent): refuse while reverting would
    # DESTROY linkage evidence. Round rows are the only record that a
    # ready delivery was superseded by a later correction round — dropping
    # them while any exist would leave child runs reading as independent
    # deliveries with no lineage. Archive explicitly before downgrading.
    bind = op.get_bind()
    recorded = bind.execute(sa.text("SELECT count(*) FROM review_rounds")).scalar_one()
    if recorded:
        raise RuntimeError(
            f"review_rounds holds {recorded} round row(s) — the round linkage is "
            "supersession evidence and is never dropped by a downgrade; archive "
            "the rows explicitly (and accept the lost lineage) before downgrading"
        )
    op.drop_index("uq_review_round_open_per_root", table_name="review_rounds")
    op.drop_index("ix_review_rounds_root_run_id", table_name="review_rounds")
    op.drop_index("ix_review_rounds_child_run_id", table_name="review_rounds")
    op.drop_index("ix_review_rounds_parent_run_id", table_name="review_rounds")
    op.drop_table("review_rounds")
