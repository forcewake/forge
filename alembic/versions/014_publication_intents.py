"""Publication intents (R11): persist the intent BEFORE the remote effect.

Revision ID: 014
Revises: 013
Create Date: 2026-09-17 00:00:00.000000

A publication whose HTTP outcome is lost (worker stall, crash, timeout)
leaves the remote mutated and the journal unfinished. On re-publish the
fresh attempt mints a NEW operation key, so its probe cannot find the
landed commit — the live failure this week: the push LANDED, the journal
never completed, the post-restart re-publish hit ``branch_drift`` and the
run blocked (the R11 window). ``publication_intents`` closes it:

- one row per (run, provider subject, branch, retry scope) minted in the
  SAME transaction as the ``action_log`` intent row and strictly BEFORE
  the HTTP effect;
- ``operation_key`` is minted once per intent and reused across retries —
  it rides in the commit message as ``(forge-op:<key>)`` so a probe can
  attribute a found commit to THIS intent (plus the ``expected_parent_oid``
  captured pre-dispatch, which a repeating human message cannot fake);
- the recovery scanner resolves stranded ``dispatched`` rows by identity:
  exact marker + parent match → ``adopted``; marker absent + parent intact
  → safe re-dispatch; marker absent + head moved → ``duplicated``;
  inconclusive → ``unknown`` (block the run, never blind-POST).

Conservative shape, mirroring 012/013:

- one new table, no changes to existing tables, no backfill: pre-014 runs
  keep recovering through the ``action_log``-based paths they use today;
- ``CHECK`` on the closed state set like every other durable table;
- the unique index is the find-or-create arbiter (concurrent retries of
  the same logical publication collapse to one intent row at the DB);
- the downgrade drops the table — the intent is operational recoverable
  state, not history (the remote effects remain addressable in the forge).
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "publication_intents",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("repo", sa.String(length=255), nullable=False),
        sa.Column("operation", sa.String(length=20), nullable=False),
        sa.Column("target_ref", sa.String(length=255), nullable=False),
        sa.Column("idempotency_scope", sa.String(length=100), nullable=False),
        sa.Column("operation_key", sa.String(length=64), nullable=False),
        sa.Column("commit_cycle", sa.Integer(), nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=True),
        sa.Column("expected_parent_oid", sa.String(length=40), nullable=True),
        sa.Column("expected_head", sa.String(length=40), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="requested",
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("next_probe_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_object_id", sa.String(length=80), nullable=True),
        sa.Column("remote_result", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["flow_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('requested', 'dispatched', 'probing', 'committed', 'adopted', "
            "'duplicated', 'unknown', 'failed')",
            name="ck_publication_intents_status",
        ),
    )
    op.create_index(
        op.f("ix_publication_intents_run_id"),
        "publication_intents",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        "uq_publication_intent_key",
        "publication_intents",
        ["provider", "repo", "target_ref", "idempotency_scope", "operation_key"],
        unique=True,
    )
    op.create_index(
        "ix_publication_intents_state_probe",
        "publication_intents",
        # The lifecycle column is ``status`` (the docstring's "state" is the
        # state-machine concept, not the column name — the ORM index and the
        # recovery scanner both read ``status``).
        ["status", "next_probe_at"],
        unique=False,
    )


def downgrade() -> None:
    # Operational recoverable state only: dropping it loses no run history
    # (the remote effects stay in the forge; action_log keeps its audit).
    op.drop_index("ix_publication_intents_state_probe", table_name="publication_intents")
    op.drop_index("uq_publication_intent_key", table_name="publication_intents")
    op.drop_index(op.f("ix_publication_intents_run_id"), table_name="publication_intents")
    op.drop_table("publication_intents")
