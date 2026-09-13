"""Add ``waiting_harness`` to the flow_runs.status CHECK constraint.

Revision ID: 004
Revises: 003
Create Date: 2026-09-13 00:00:00.000000

ADR-0015: the ci_harness implementer backend delegates implementation to a
coding harness executing as a job in the target project's CI. The wait must
be durable and worker-free (like ``waiting_ci``), so it gets its own status.
Nothing else changes — the constraint is dropped and recreated with the new
closed set.
"""

from alembic import op

# revision identifiers
revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None

_FLOW_STATUSES = (
    "accepted",
    "preflight",
    "planning",
    "waiting_approval",
    "proposing",
    "validating",
    "committing",
    "waiting_harness",
    "ensuring_draft_mr",
    "waiting_ci",
    "evaluating_ci",
    "reviewing",
    "ready_for_human",
    "blocked",
    "failed",
    "cancelled",
)

_PRE_004_FLOW_STATUSES = tuple(s for s in _FLOW_STATUSES if s != "waiting_harness")


def _status_in(*values: str) -> str:
    return "status IN ({})".format(", ".join(f"'{value}'" for value in values))


def upgrade() -> None:
    op.drop_constraint("ck_flow_runs_status", "flow_runs", type_="check")
    op.create_check_constraint("ck_flow_runs_status", "flow_runs", _status_in(*_FLOW_STATUSES))


def downgrade() -> None:
    op.drop_constraint("ck_flow_runs_status", "flow_runs", type_="check")
    op.create_check_constraint(
        "ck_flow_runs_status", "flow_runs", _status_in(*_PRE_004_FLOW_STATUSES)
    )
