"""Per-recipient delivery rows for work-wide control broadcasts (NXT-13).

Revision ID: 021
Revises: 020
Create Date: 2026-09-21 00:00:00.000000

A work-wide command (a parent pause scoped to the whole work) had ONE
mailbox status: the first lane to checkpoint it removed it from
``pending()`` for every other child, so a parent pause could never mean
"all child lanes paused" (review §10, NXT-13's observed contract
defect). Migration 020's ``control_commands`` is a single-consumer
ladder; the fix is not another rung on it but a SECOND table:

- **the parent stays ONE row** — ``control_commands`` with
  ``payload.scope = 'work'`` and the frozen ``payload.recipients`` list
  (the work's lane set, snapshotted at submission). The logical intent,
  its work-scoped dedup and its per-work sequence are unchanged;
- **``control_command_deliveries`` holds the per-lane state** — one row
  per ``(command_id, recipient)``, climbing
  ``pending -> acknowledged | uncertain`` through guarded
  ``UPDATE ... WHERE status = <expected>`` compare-and-sets with an
  append-only ``journal``. Each lane consumes independently: parent
  ``completed`` means EVERY recipient acknowledged, and an ``uncertain``
  lane stays individually visible instead of being hidden behind
  another lane's success.

The shape deliberately mirrors ``mr_reservations`` (migration 019): the
logical intent and the per-target effect state are separate concerns,
and the intent is durable and committed BEFORE any lane is interrupted.
A crash between the parent INSERT and the delivery INSERTs heals on
resubmission — the rows are re-derived from the winner's frozen payload
(``INSERT ... ON CONFLICT DO NOTHING`` semantics at the application
layer; existing rows and their ack state are never reset).

``ix_control_command_deliveries_lane (work_id, recipient, status)``
backs the lane drain's ``pending_for(work_id, recipient)`` — the
per-recipient pending view that replaces the single-consumer queue for
work-wide commands.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None

_DELIVERY_STATUSES = ("pending", "acknowledged", "uncertain")


def _jsonb() -> sa.types.TypeEngine:
    """JSONB on Postgres, JSON elsewhere (parity with the ORM model)."""
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "control_command_deliveries",
        sa.Column("command_id", sa.String(length=64), primary_key=True),
        sa.Column("work_id", sa.String(length=64), nullable=False),
        sa.Column("recipient", sa.String(length=64), primary_key=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("journal", _jsonb(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'acknowledged', 'uncertain')",
            name="ck_control_command_deliveries_status",
        ),
    )
    op.create_index(
        "ix_control_command_deliveries_lane",
        "control_command_deliveries",
        ["work_id", "recipient", "status"],
    )
    # Backfill: every 020-shaped work-scoped broadcast parent (payload
    # scope = 'work' with a recipients list) gets its delivery rows at
    # 'pending' — the lanes never acknowledged a row that did not exist,
    # so pending is the only honest starting state. Plain-JSON Postgres
    # functions (the payload column is json, not jsonb — the 019 lesson:
    # dialect-native operators only); the typeof guard keeps non-array
    # payloads out. No parent inserted by the shipped code carries the
    # scope marker yet, so on today's data this is a no-op — it exists
    # so an operator who hand-shaped a broadcast is not silently
    # dropped either.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            INSERT INTO control_command_deliveries (command_id, work_id, recipient, journal)
            SELECT c.id, c.work_id, r.value, '[]'::jsonb
              FROM control_commands c
             CROSS JOIN LATERAL jsonb_array_elements_text(
                  CASE jsonb_typeof(c.payload -> 'recipients')
                    WHEN 'array' THEN c.payload -> 'recipients'
                    ELSE '[]'::jsonb
                  END) AS r(value)
             WHERE c.payload ->> 'scope' = 'work'
            ON CONFLICT DO NOTHING
            """
        )
    )


def downgrade() -> None:
    # Honest downgrade (the 020 precedent): the rows ARE the per-lane
    # acknowledgement history — dropping them re-admits the single-status
    # defect (a second lane can no longer see a pending work-wide pause)
    # and destroys the audit trail of which lane acknowledged what.
    # Refuse while real delivery state exists; roll forward instead.
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT count(*) FROM control_command_deliveries")).scalar()
    if rows and int(rows or 0) > 0:
        raise RuntimeError(
            "downgrade 021->020 refused: control_command_deliveries holds "
            f"{rows} row(s) — the per-recipient acknowledgement history of "
            "work-wide commands. This schema is forward-only while control "
            "state exists; roll forward instead."
        )
    op.drop_index("ix_control_command_deliveries_lane", table_name="control_command_deliveries")
    op.drop_table("control_command_deliveries")
