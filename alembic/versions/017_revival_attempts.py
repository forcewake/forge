"""Durable revival attempts (A11): retry/auto-revive as ONE idempotent transition.

Revision ID: 017
Revises: 016
Create Date: 2026-09-18 00:00:00.000000

The revival FLOW used to separate the persisted transition from the backend
dispatch (a crash between them lost the redispatch), two reconcilers could
drive the same recovery window, and a repeated ``/retry`` event bumped the
commit cycle twice. This migration turns the existing revival
``action_log`` rows (``retry_requested`` / ``auto_revive``) into the durable
REVIVAL ATTEMPT record by adding three nullable columns and one arbiter
index — the attempt row is written in the SAME transaction as the CAS
revival transition:

- ``idempotency_key`` (``VARCHAR(150)``): the delivery/event id of the
  triggering command (``delivery:<note id>``) or the auto-revive window
  identity (``revive:<run id>:<stamp count>``). A redelivered /retry with
  the SAME key is a no-op; a DIFFERENT key is refused while an attempt is
  still open.
- ``retryability`` (``VARCHAR(40)``): the typed retryability class —
  ``transient_infrastructure`` | ``operator_override`` |
  ``verification_timeout`` — deliberately orthogonal to the A12
  effect-certainty states (an unknown publication blocks revival
  regardless of the cause class).
- ``dispatch_state`` (``VARCHAR(20)``): ``pending`` → ``dispatched``. The
  recovery scan re-drives ``pending`` attempts older than a bound (a crash
  between the revive commit and the dispatch) exactly once; a
  ``dispatched`` attempt is never blindly re-driven — its journaled
  dispatch-leg action is the double-dispatch guard.

- ``uq_revival_attempt_inflight``: a PARTIAL unique index on
  ``flow_run_id`` for open (``status='requested'``) revival rows — "one
  in-flight revival attempt per run" becomes a DB invariant, so two
  concurrent drivers of one recovery window collapse to one attempt at the
  index.

Conservative shape, mirroring 011/013:

- three added nullable columns and one partial index — no existing reader
  changes meaning (every non-revival action keeps all three columns NULL);
- no backfill: pre-017 stranded attempts (a ``requested`` revival row with
  NULL ``dispatch_state``) are treated as ``pending`` by the recovery scan
  and re-driven exactly once like fresh ones;
- the downgrade drops the index and the columns — the attempt bookkeeping
  is operational recoverable state, not history (the audit trail in
  ``action_kind``/``status``/``remote_result`` is untouched).
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None

_INFLIGHT_PREDICATE = sa.text(
    "action_kind IN ('retry_requested', 'auto_revive') AND status = 'requested'"
)


def upgrade() -> None:
    op.add_column("action_log", sa.Column("idempotency_key", sa.String(length=150), nullable=True))
    op.add_column("action_log", sa.Column("retryability", sa.String(length=40), nullable=True))
    op.add_column("action_log", sa.Column("dispatch_state", sa.String(length=20), nullable=True))
    op.create_index("ix_action_log_idempotency_key", "action_log", ["idempotency_key"])
    op.create_index(
        "uq_revival_attempt_inflight",
        "action_log",
        ["flow_run_id"],
        unique=True,
        postgresql_where=_INFLIGHT_PREDICATE,
        sqlite_where=_INFLIGHT_PREDICATE,
    )
    op.create_check_constraint(
        "ck_action_log_retryability",
        "action_log",
        "retryability IS NULL OR retryability IN "
        "('transient_infrastructure', 'operator_override', 'verification_timeout')",
    )
    op.create_check_constraint(
        "ck_action_log_dispatch_state",
        "action_log",
        "dispatch_state IS NULL OR dispatch_state IN ('pending', 'dispatched')",
    )


def downgrade() -> None:
    # Attempt bookkeeping only: the audit columns (kind/status/result) stay.
    op.drop_constraint("ck_action_log_dispatch_state", "action_log", type_="check")
    op.drop_constraint("ck_action_log_retryability", "action_log", type_="check")
    op.drop_index("uq_revival_attempt_inflight", table_name="action_log")
    op.drop_index("ix_action_log_idempotency_key", table_name="action_log")
    op.drop_column("action_log", "dispatch_state")
    op.drop_column("action_log", "retryability")
    op.drop_column("action_log", "idempotency_key")
