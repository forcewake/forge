"""The keyed operation-grant authority (R40-05 #341, review b521e1a).

The recorded defect: ``persist_operation_grant`` read the run row,
merged the grant into the WHOLE ``FlowRun.evidence`` document and wrote
the entire JSON back — an unversioned full-document replacement with no
row-lock discipline. A concurrent session committing a native handle or
checkpoint between the read and the write was silently ERASED (the
P02 SQLite schedule), and two concurrent initial grants could return
different effective identities. This is the adjacent write path the
#322 append-only audit fix did not cover.

From 029 the authority is a DEDICATED KEYED ROW: ``operation_grants``,
UNIQUE per canonical (work, attempt, route), carrying the grant id, the
full value-free grant document, the ABSOLUTE redemption deadline and a
status (``active`` | ``revoked``). Creation is serialized by the unique
index (INSERT ... ON CONFLICT DO NOTHING then read — every contender
gets the COMMITTED effective grant); a replay keeps the first deadline;
a corrupt document is a typed failure, never silently replaced with a
fresh window. The ``run.evidence["credential_operation_grants"]`` map
becomes a DERIVED projection of this table, rewritten through a
targeted compare-and-swap that touches ONLY that key — concurrent
checkpoint / native-handle / review fields in the same evidence
document SURVIVE.

No backfill runs in the migration itself: pre-029 evidence grants stay
readable (the redemption endpoint keeps loading them), and the FIRST
post-029 persist of an attempt+route ADOPTS the standing same-ref
evidence grant into the keyed row (its window stands — the upgrade is a
no-op for in-flight attempts), so there is no legacy row set to migrate
up front.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operation_grants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("work_id", sa.String(length=32), nullable=False),
        sa.Column("attempt_generation", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("grant_id", sa.String(length=64), nullable=False),
        sa.Column("credential_ref", sa.String(length=255), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("redemption_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["work_id"], ["flow_runs.id"]),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_operation_grants_status"),
        sa.UniqueConstraint(
            "work_id", "attempt_generation", "provider", name="uq_operation_grant_key"
        ),
    )
    op.create_index("ix_operation_grants_work_id", "operation_grants", ["work_id"])


def downgrade() -> None:
    # Honest downgrade (the 028 precedent): refuse while reverting would
    # DESTROY authorization evidence. Dropping the keyed authority while
    # rows exist would leave the still-readable evidence projection as
    # the ONLY copy of grants whose keyed row already serialized a
    # rotation or adoption — and re-persisting the same attempt+route
    # would mint FRESH windows over the standing ones (the exact
    # re-anchoring #320 closed). The guard runs BEFORE any change so a
    # refusal leaves the schema untouched.
    bind = op.get_bind()
    recorded = bind.execute(sa.text("SELECT count(*) FROM operation_grants")).scalar_one()
    if recorded:
        raise RuntimeError(
            f"operation_grants holds {recorded} authority row(s) — the keyed grant "
            "records are authorization evidence and are never dropped by a downgrade; "
            "archive them explicitly (and accept the re-anchoring risk) before "
            "downgrading"
        )
    op.drop_index("ix_operation_grants_work_id", table_name="operation_grants")
    op.drop_table("operation_grants")
