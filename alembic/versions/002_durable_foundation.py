"""Durable execution foundation tables.

Revision ID: 002
Revises: 001
Create Date: 2026-09-12 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "002"
down_revision = "001"
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
    "ensuring_draft_mr",
    "waiting_ci",
    "evaluating_ci",
    "reviewing",
    "ready_for_human",
    "blocked",
    "failed",
    "cancelled",
)


def _status_in(*values: str) -> str:
    return "status IN ({})".format(", ".join(f"'{value}'" for value in values))


def upgrade() -> None:
    # event_inbox
    op.create_table(
        "event_inbox",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_event_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("handler_result", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            _status_in("pending", "processed", "rejected"), name="ck_event_inbox_status"
        ),
    )
    op.create_index(
        "ix_event_inbox_source_event_id", "event_inbox", ["source_event_id"], unique=True
    )
    op.create_index("ix_event_inbox_project_id", "event_inbox", ["project_id"])

    # flow_runs
    op.create_table(
        "flow_runs",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("issue_iid", sa.Integer(), nullable=True),
        sa.Column("mr_iid", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("status_reason", sa.String(200), nullable=True),
        sa.Column("base_sha", sa.String(40), nullable=True),
        sa.Column("candidate_shas", sa.JSON(), nullable=True),
        sa.Column("plan_digest", sa.String(64), nullable=True),
        sa.Column("config_digest", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(_status_in(*_FLOW_STATUSES), name="ck_flow_runs_status"),
    )
    op.create_index("ix_flow_runs_project_id", "flow_runs", ["project_id"])

    # step_runs
    op.create_table(
        "step_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("flow_run_id", sa.String(32), nullable=False),
        sa.Column("step_name", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_owner", sa.String(100), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("output", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["flow_run_id"], ["flow_runs.id"]),
        sa.CheckConstraint(
            _status_in("running", "succeeded", "failed", "skipped"), name="ck_step_runs_status"
        ),
    )
    op.create_index("ix_step_runs_flow_run_id", "step_runs", ["flow_run_id"])

    # gate_approvals
    op.create_table(
        "gate_approvals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("flow_run_id", sa.String(32), nullable=False),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("base_sha", sa.String(40), nullable=False),
        sa.Column("policy_digest", sa.String(64), nullable=False),
        sa.Column("approver_user_id", sa.Integer(), nullable=False),
        sa.Column("source_event_id", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["flow_run_id"], ["flow_runs.id"]),
    )
    op.create_index("ix_gate_approvals_flow_run_id", "gate_approvals", ["flow_run_id"])

    # outbox
    op.create_table(
        "outbox",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("flow_run_id", sa.String(32), nullable=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outbox_processed_at", "outbox", ["processed_at"])

    # action_log
    op.create_table(
        "action_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("flow_run_id", sa.String(32), nullable=True),
        sa.Column("action_kind", sa.String(50), nullable=False),
        sa.Column("params_digest", sa.String(64), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="requested"),
        sa.Column("remote_result", sa.JSON(), nullable=True),
        sa.Column("correlation_id", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            _status_in("requested", "succeeded", "failed", "unknown_outcome"),
            name="ck_action_log_status",
        ),
    )
    op.create_index("ix_action_log_flow_run_id", "action_log", ["flow_run_id"])

    # llm_calls
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("flow_run_id", sa.String(32), nullable=True),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_tokens", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(_status_in("ok", "failed", "cancelled"), name="ck_llm_calls_status"),
    )
    op.create_index("ix_llm_calls_flow_run_id", "llm_calls", ["flow_run_id"])


def downgrade() -> None:
    op.drop_table("llm_calls")
    op.drop_table("action_log")
    op.drop_table("outbox")
    op.drop_table("gate_approvals")
    op.drop_table("step_runs")
    op.drop_table("flow_runs")
    op.drop_table("event_inbox")
