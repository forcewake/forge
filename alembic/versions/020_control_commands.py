"""Durable control-command mailbox (NXT-09, the NXT-12 status ladder).

Revision ID: 020
Revises: 019
Create Date: 2026-09-21 00:00:00.000000

The adaptive control plane kept its mailbox in process dictionaries
(``Mailbox.commands`` / ``by_key`` / ``_last_sequence`` in
``forge.adaptive.control``): a restart forgot every accepted command,
idempotency held only as long as the object lived, and a bare
idempotency key could collide across works. ``control_commands`` moves
that state into the database, with the identity in the schema rather
than in caller discipline:

- **work-scoped dedup** — ``UNIQUE (work_id, dedup_key)``: a redelivered
  command loses the insert race and READS the winner (first-result-wins,
  the ``forge.adaptive.dedup.insert_or_read`` semantics), refused
  atomically BEFORE the caller bumps an epoch or spends anything. The
  same provider event number in two works is two commands, never a
  collision.
- **monotonic per-work sequence** — ``UNIQUE (work_id, sequence)`` backs
  the strictly-increasing delivery order (CTL-07): the mailbox checks
  ``max(sequence)`` first and the constraint closes the concurrent-race
  window the check cannot.
- **the NXT-12 ladder** — status distinguishes "intended to send" from
  "the agent applied it"::

      received -> authorized -> dispatching
          -> vendor_accepted | outcome_unknown
          -> applied -> checkpointed

  with ``expired`` (a CAS miss at dispatch) and ``rejected`` as exits.
  The bridge's old premature ``applied`` — marked before the vendor
  effect — becomes ``dispatching`` (the intent, with the vendor
  correlation and execution epoch persisted BEFORE the call);
  ``outcome_unknown`` is the lost-response window; ``applied`` is the
  APPLICATION observed. Transitions are guarded
  ``UPDATE ... WHERE status = <expected>`` compare-and-sets, each
  appending an auditable ``journal`` entry.
- **query shape** — ``ix_control_commands_work_status`` backs the lane
  drain's ``pending(work_id)`` (received/authorized only).

Payload/journal use ``sa.JSON().with_variant(JSONB, "postgresql")``:
JSONB on Postgres (where the chain runs), JSON on the SQLite dev/test
bootstrap — same bytes, dialect-native type.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None

_KINDS = ("pause", "resume", "steer", "answer", "amend", "approve-revision")
_ORIGINS = ("server_authenticated_human", "operator_token", "automation_reconciler")
#: The NXT-12 ladder rungs plus the two exits; the CHECK keeps typos out
#: of the status column and the ORM model declares the same set.
_STATUSES = (
    "received",
    "authorized",
    "dispatching",
    "vendor_accepted",
    "outcome_unknown",
    "applied",
    "checkpointed",
    "rejected",
    "expired",
)


def _in_set(column: str, values: tuple[str, ...]) -> str:
    listing = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({listing})"


def _jsonb() -> sa.types.TypeEngine:
    """JSONB on Postgres, JSON elsewhere (parity with the ORM model)."""
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "control_commands",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("work_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("payload", _jsonb(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="received"),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("dedup_key", sa.String(length=255), nullable=False),
        sa.Column("actor_ref", sa.String(length=255), nullable=False),
        sa.Column("actor_origin", sa.String(length=40), nullable=False),
        sa.Column("expected_plan_revision", sa.Integer(), nullable=True),
        sa.Column("expected_execution_epoch", sa.Integer(), nullable=True),
        sa.Column("epoch", sa.Integer(), nullable=True),
        sa.Column("journal", _jsonb(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_in_set("kind", _KINDS), name="ck_control_commands_kind"),
        sa.CheckConstraint(
            _in_set("actor_origin", _ORIGINS), name="ck_control_commands_actor_origin"
        ),
        sa.CheckConstraint(_in_set("status", _STATUSES), name="ck_control_commands_status"),
        sa.UniqueConstraint("work_id", "dedup_key", name="uq_control_command_dedup_scope"),
        sa.UniqueConstraint("work_id", "sequence", name="uq_control_command_work_sequence"),
    )
    op.create_index(
        "ix_control_commands_work_status",
        "control_commands",
        ["work_id", "status"],
    )


def downgrade() -> None:
    # Honest downgrade: the table IS the durable control history — the
    # dedup memory (dropping it re-admits redeliveries as new commands)
    # and the auditable transition journal. Refuse to destroy recorded
    # control state; an empty mailbox drops cleanly (index first, then
    # the table — nothing else references it).
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT count(*) FROM control_commands")).scalar()
    if rows and int(rows or 0) > 0:
        raise RuntimeError(
            "downgrade 020->019 refused: control_commands holds "
            f"{rows} command row(s) — the durable mailbox history and its "
            "dedup memory. This schema is forward-only while control "
            "state exists; roll forward instead."
        )
    op.drop_index("ix_control_commands_work_status", table_name="control_commands")
    op.drop_table("control_commands")
