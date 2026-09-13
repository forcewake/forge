"""Durable step runtime: scheduling columns, fences, DB invariants (ADR-0017).

Revision ID: 005
Revises: 004
Create Date: 2026-09-13 00:00:00.000000

- ``step_runs`` becomes the claimed-work table: ``due_at`` (the durable timer),
  ``deadline_at``, ``max_attempts``, the per-row ``fence_token`` and the
  command's inbox identity (``source_event_id``). ``flow_run_id`` becomes
  nullable — command steps (start_run/go/cancel) bind their run at execution
  time. The status CHECK gains ``scheduled`` and ``dead``.
- ``uq_active_run_per_issue``: partial unique index over the non-terminal
  flow_run statuses — one active run per (project, issue), enforced by the DB
  (F12). ``ready_for_human`` is terminal per ADR-0004 and does NOT block.
- ``gate_approvals.generation`` + unique (flow_run_id, generation): one gate
  per approval round (ADR-0017 §4).
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None

_STEP_STATUSES = (
    "scheduled",
    "running",
    "succeeded",
    "failed",
    "skipped",
    "dead",
)

_PRE_005_STEP_STATUSES = tuple(s for s in _STEP_STATUSES if s not in ("scheduled", "dead"))

#: Terminal statuses as a literal list in the predicate (F12): ready_for_human
#: is terminal per ADR-0004, so a finished-but-unmerged run does NOT block.
_TERMINAL_STATUS_PREDICATE = sa.text(
    "status NOT IN ('ready_for_human', 'blocked', 'failed', 'cancelled')"
)


def _status_in(*values: str) -> str:
    return "status IN ({})".format(", ".join(f"'{value}'" for value in values))


def upgrade() -> None:
    # step_runs: scheduling + lease/fence columns
    op.add_column("step_runs", sa.Column("payload", sa.JSON(), nullable=True))
    op.add_column("step_runs", sa.Column("due_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("step_runs", sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "step_runs",
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
    )
    op.add_column(
        "step_runs",
        sa.Column("fence_token", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column("step_runs", sa.Column("source_event_id", sa.String(64), nullable=True))
    op.create_index("ix_step_runs_due_at", "step_runs", ["due_at"])
    op.create_index("ix_step_runs_lease_expires_at", "step_runs", ["lease_expires_at"])
    op.create_index("ix_step_runs_source_event_id", "step_runs", ["source_event_id"])
    op.alter_column("step_runs", "flow_run_id", existing_type=sa.String(32), nullable=True)
    op.drop_constraint("ck_step_runs_status", "step_runs", type_="check")
    op.create_check_constraint("ck_step_runs_status", "step_runs", _status_in(*_STEP_STATUSES))

    # flow_runs: one active run per (project, issue) — the F12 invariant.
    op.create_index(
        "uq_active_run_per_issue",
        "flow_runs",
        ["project_id", "issue_iid"],
        unique=True,
        postgresql_where=_TERMINAL_STATUS_PREDICATE,
    )

    # gate_approvals: one gate per (run, approval generation).
    op.add_column(
        "gate_approvals",
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "uq_gate_per_run_generation",
        "gate_approvals",
        ["flow_run_id", "generation"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_gate_per_run_generation", table_name="gate_approvals")
    op.drop_column("gate_approvals", "generation")
    op.drop_index("uq_active_run_per_issue", table_name="flow_runs")
    op.create_check_constraint(
        "ck_step_runs_status", "step_runs", _status_in(*_PRE_005_STEP_STATUSES)
    )
    op.alter_column("step_runs", "flow_run_id", existing_type=sa.String(32), nullable=False)
    op.drop_index("ix_step_runs_source_event_id", table_name="step_runs")
    op.drop_index("ix_step_runs_lease_expires_at", table_name="step_runs")
    op.drop_index("ix_step_runs_due_at", table_name="step_runs")
    op.drop_column("step_runs", "source_event_id")
    op.drop_column("step_runs", "fence_token")
    op.drop_column("step_runs", "max_attempts")
    op.drop_column("step_runs", "deadline_at")
    op.drop_column("step_runs", "due_at")
    op.drop_column("step_runs", "payload")
