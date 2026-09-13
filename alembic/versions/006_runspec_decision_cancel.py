"""RunSpec, decision deadline, cancel-as-revoke (ADR-0018).

Revision ID: 006
Revises: 005
Create Date: 2026-09-13 00:00:00.000000

- ``run_specs``: the immutable, versioned run specification frozen at plan
  acceptance (F14) — the canonical JSON document plus its sha256 digest.
- ``flow_runs.spec_digest``: the digest the pending decision binds; a drift
  invalidates a `/go` (re-approval required).
- ``flow_runs.cancel_requested``: the durable cancel flag that revokes the
  publication grant (F13) before the terminal cancelled transition.
- ``gate_approvals.spec_digest`` / ``.task_digest``: the pending decision is
  created at plan publication and carries the spec digest plus the
  issue-text snapshot digest (F15).
- ``step_runs`` status CHECK gains ``cancelled``: a cancel request
  withdraws scheduled steps that were never claimed.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None

_STEP_STATUSES = (
    "scheduled",
    "running",
    "succeeded",
    "failed",
    "skipped",
    "dead",
    "cancelled",
)

_PRE_006_STEP_STATUSES = tuple(s for s in _STEP_STATUSES if s != "cancelled")


def _status_in(*values: str) -> str:
    return "status IN ({})".format(", ".join(f"'{value}'" for value in values))


def upgrade() -> None:
    # run_specs: the frozen, versioned run specification (F14).
    op.create_table(
        "run_specs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("run_id", sa.String(32), sa.ForeignKey("flow_runs.id"), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_run_specs_run_id", "run_specs", ["run_id"])

    # flow_runs: the bound spec digest + the cancel-as-revoke flag (F13).
    op.add_column("flow_runs", sa.Column("spec_digest", sa.String(64), nullable=True))
    op.add_column(
        "flow_runs",
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.text("'0'")),
    )

    # gate_approvals: the pending decision carries spec + task digests (F15).
    op.add_column("gate_approvals", sa.Column("spec_digest", sa.String(64), nullable=True))
    op.add_column("gate_approvals", sa.Column("task_digest", sa.String(64), nullable=True))

    # step_runs: cancel withdraws unclaimed scheduled steps (F13).
    op.drop_constraint("ck_step_runs_status", "step_runs", type_="check")
    op.create_check_constraint("ck_step_runs_status", "step_runs", _status_in(*_STEP_STATUSES))


def downgrade() -> None:
    op.drop_constraint("ck_step_runs_status", "step_runs", type_="check")
    op.create_check_constraint(
        "ck_step_runs_status", "step_runs", _status_in(*_PRE_006_STEP_STATUSES)
    )
    op.drop_column("gate_approvals", "task_digest")
    op.drop_column("gate_approvals", "spec_digest")
    op.drop_column("flow_runs", "cancel_requested")
    op.drop_column("flow_runs", "spec_digest")
    op.drop_index("ix_run_specs_run_id", table_name="run_specs")
    op.drop_table("run_specs")
