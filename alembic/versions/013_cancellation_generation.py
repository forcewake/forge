"""Publication-grant generation (R10): fence stale claims out of publishing.

Revision ID: 013
Revises: 012
Create Date: 2026-09-17 00:00:00.000000

A claimed step can run a long way (LLM calls, base-blob reads) after the
queue handed it ownership; today the only publication guard is the
``cancel_requested`` flag, re-read at the boundary. A cancel that lands
AFTER that read but BEFORE the reservation still lets the stale claim
publish — the flag was read too early and there is nothing to compare it
against. This migration adds the comparable:

- ``flow_runs.cancellation_generation`` (``INTEGER NOT NULL``): a monotonic
  counter bumped atomically WITH ``cancel_requested`` by
  :meth:`forge.durable.controller.Controller.request_cancel` (one UPDATE —
  the flag and the counter can never disagree). ``Controller.transition_guarded``
  and the publisher (:func:`forge.runs.publisher.publication_grant_valid`)
  pin and compare it, so a claim minted before a cancel can neither move the
  run nor start a NEW publication reservation.

Conservative shape, mirroring 011/012:

- one added column, ``NOT NULL`` with ``server_default='0'`` — existing rows
  keep working unchanged (generation 0 = "never cancelled"); the server
  default matches the model default so ``create_all`` and the migration
  chain agree, and raw inserts never see NULL.
- no backfill logic: every pre-013 row is by construction "not cancelled at
  generation 0" — the flag column already exists and stays untouched.
- the downgrade drops the column. The counter is pure fence state: nothing
  outside :mod:`forge.durable.controller` assigns meaning to its values, so
  re-upgrading restarts every run at 0 without data decisions.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "flow_runs",
        sa.Column("cancellation_generation", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    # The generation is fence state, not history — dropping it revokes nothing
    # that cancel_requested does not already revoke.
    op.drop_column("flow_runs", "cancellation_generation")
